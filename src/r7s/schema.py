"""r7s/v1 resource schema."""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    StrictBool,
    Tag,
    field_validator,
    model_validator,
)

API_VERSION = "r7s/v1"

NAME_RE = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
PLUGIN_ID_RE = r"^[a-z0-9]([-_a-z0-9]*[a-z0-9])?$"
HWID_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")
ALSA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,31}$")  # ALSA card id (/proc/asound/cards)

Name = Annotated[str, Field(pattern=NAME_RE, max_length=63)]
PluginId = Annotated[str, Field(pattern=PLUGIN_ID_RE, max_length=48)]  # JACK names: "r7s-" + id <= 63


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- parameter values ----------------------------------------------------

# Tags used by the parameter-value discriminator. They show up in pydantic
# error locations; the loader strips them so paths stay readable.
VALUE_TAGS = frozenset({"mapping", "bool", "number", "label"})


def _scalar_tag(v: Any) -> str | None:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "label"
    return None


def _value_tag(v: Any) -> str | None:
    if isinstance(v, dict) or isinstance(v, ParamMapping):
        return "mapping"
    return _scalar_tag(v)


Scalar = Annotated[
    Union[
        Annotated[StrictBool, Tag("bool")],
        Annotated[float, Tag("number")],
        Annotated[str, Tag("label")],
    ],
    Discriminator(
        _scalar_tag,
        custom_error_type="invalid_value",
        custom_error_message="expected a number, a boolean, or a scale-point label",
    ),
]


class ParamMapping(Model):
    """Long form of a parameter: initial value and/or a MIDI CC mapping."""

    value: Scalar | None = None
    cc: Annotated[int, Field(ge=0, le=127)] | None = None
    channel: Annotated[int, Field(ge=1, le=16)] | None = None
    device: Name | None = None
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _check(self) -> ParamMapping:
        if self.cc is None:
            stray = [f for f in ("channel", "device", "min", "max") if getattr(self, f) is not None]
            if stray:
                verb = "applies" if len(stray) == 1 else "apply"
                raise ValueError(f"{', '.join(stray)} only {verb} to a CC mapping; add 'cc'")
            if self.value is None:
                raise ValueError("set 'value', 'cc', or both")
        elif self.device is None:
            raise ValueError("a CC mapping needs 'device' (a Device alias)")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must be <= max")
        return self


ParamValue = Annotated[
    Union[
        Annotated[ParamMapping, Tag("mapping")],
        Annotated[StrictBool, Tag("bool")],
        Annotated[float, Tag("number")],
        Annotated[str, Tag("label")],
    ],
    Discriminator(
        _value_tag,
        custom_error_type="invalid_value",
        custom_error_message="expected a number, a boolean, a scale-point label, or a mapping",
    ),
]


def param_initial(v: ParamValue) -> bool | float | str | None:
    return v.value if isinstance(v, ParamMapping) else v


# --- Device --------------------------------------------------------------


class DeviceSpec(Model):
    usb: str | None = None
    pci: str | None = None
    alsa: str | None = None  # ALSA card id, for HATs/onboard/virtual cards
    serial: str | None = None
    # Pick one endpoint when the device has several (e.g. "Speaker", "HDMI1").
    capture: str | None = None
    playback: str | None = None

    @field_validator("usb", "pci")
    @classmethod
    def _hwid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower()
        if not HWID_RE.match(v):
            raise ValueError("expected vendor:product as two 4-digit hex IDs, e.g. '1235:8210'")
        return v

    @field_validator("alsa")
    @classmethod
    def _alsa_id(cls, v: str | None) -> str | None:
        if v is not None and not ALSA_ID_RE.match(v):
            raise ValueError("expected an ALSA card id from /proc/asound/cards, e.g. 'pisound'")
        return v

    @model_validator(mode="after")
    def _one_bus(self) -> DeviceSpec:
        if sum(x is not None for x in (self.usb, self.pci, self.alsa)) != 1:
            raise ValueError("set exactly one of 'usb', 'pci', or 'alsa'")
        if self.serial is not None and self.usb is None:
            raise ValueError("'serial' only applies to usb devices")
        return self

    @property
    def bus(self) -> str:
        return "usb" if self.usb else "pci" if self.pci else "alsa"

    @property
    def hwid(self) -> str:
        return self.usb or self.pci or self.alsa  # type: ignore[return-value]


# --- Rig -----------------------------------------------------------------

PLUGIN_FORMATS = ("lv2", "vst3", "clap")
SUPPORTED_FORMATS = ("lv2",)


class PluginSpec(Model):
    id: PluginId
    lv2: str | None = None
    vst3: str | None = None
    clap: str | None = None
    bundle: str | None = None
    ui: bool | None = None
    parameters: dict[str, ParamValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_format(self) -> PluginSpec:
        set_ = [f for f in PLUGIN_FORMATS if getattr(self, f)]
        if len(set_) != 1:
            raise ValueError("set exactly one plugin format: 'lv2', 'vst3', or 'clap'")
        if self.bundle is not None and self.lv2 is None:
            raise ValueError("'bundle' only applies to lv2 plugins")
        return self

    @property
    def format(self) -> str:
        return next(f for f in PLUGIN_FORMATS if getattr(self, f))

    @property
    def ref(self) -> str:
        return getattr(self, self.format)


class Connection(Model):
    from_: str = Field(alias="from")
    to: str


class Connections(Model):
    audio: list[Connection] = Field(default_factory=list)
    midi: list[Connection] = Field(default_factory=list)


class RigSpec(Model):
    plugins: list[PluginSpec] = Field(default_factory=list)
    connections: Connections = Field(default_factory=Connections)


# --- ActiveRig / Config --------------------------------------------------


class ActiveRigSpec(Model):
    rig: Name


class Backend(str, Enum):
    auto = "auto"
    jalv = "jalv"
    mod_host = "mod-host"


class Display(str, Enum):
    auto = "auto"
    always = "always"
    never = "never"


class ConfigSpec(Model):
    backend: Backend = Backend.auto
    display: Display = Display.auto
    plugin_paths: list[str] | None = Field(default=None, alias="pluginPaths")
    jalv_path: str | None = Field(default=None, alias="jalvPath")


# --- envelope ------------------------------------------------------------


class Metadata(Model):
    name: Name
    labels: dict[str, str] = Field(default_factory=dict)


class ConfigMetadata(Metadata):
    name: Name = "default"


class _Resource(Model):
    api_version: Literal["r7s/v1"] = Field(alias="apiVersion")


class Device(_Resource):
    kind: Literal["Device"]
    metadata: Metadata
    spec: DeviceSpec


class Rig(_Resource):
    kind: Literal["Rig"]
    metadata: Metadata
    spec: RigSpec


class ActiveRig(_Resource):
    kind: Literal["ActiveRig"]
    metadata: Metadata
    spec: ActiveRigSpec


class Config(_Resource):
    kind: Literal["Config"]
    metadata: ConfigMetadata = Field(default_factory=ConfigMetadata)
    spec: ConfigSpec = Field(default_factory=ConfigSpec)


KINDS: dict[str, type[_Resource]] = {
    "Device": Device,
    "Rig": Rig,
    "ActiveRig": ActiveRig,
    "Config": Config,
}

Resource = Union[Device, Rig, ActiveRig, Config]
