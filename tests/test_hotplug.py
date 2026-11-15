from r7s.hw.uevent import is_relevant, parse_uevent
from r7s.reconciler import HOTPLUG_FOLLOWUPS

from .test_reconciler import BASIC, DEVICE, Rig

CARD = b"/devices/pci0000:00/0000:00:14.0/usb1/1-2/1-2:1.0/sound/card1"


def msg(action, devpath, subsystem="sound"):
    return (f"{action}@".encode() + devpath + b"\0" + f"ACTION={action}\0".encode()
            + b"DEVPATH=" + devpath + b"\0" + f"SUBSYSTEM={subsystem}\0SEQNUM=4242\0".encode())


def test_parse_kernel_uevents():
    ev = parse_uevent(msg("add", CARD))
    assert (ev.action, ev.subsystem, ev.card, ev.props["SEQNUM"]) == ("add", "sound", "card1", "4242")
    assert is_relevant(ev)
    assert not is_relevant(parse_uevent(msg("add", CARD + b"/pcmC1D0p")))  # a PCM, not the card
    assert not is_relevant(parse_uevent(msg("change", CARD)))
    assert not is_relevant(parse_uevent(msg("add", b"/devices/x/input/input9", "input")))
    assert parse_uevent(b"libudev\0\xfe\xed...") is None  # udev's own broadcast format
    assert parse_uevent(b"garbage") is None


def test_hotplug_schedules_followups(tmp_path):
    h = Rig(tmp_path)
    h.put(DEVICE + "---" + BASIC)
    h.sync()
    t0 = h.clock.t
    h.r.hardware_changed("add card1")
    assert h.r._hw_dirty and h.r._hw_followups == [t0 + d for d in HOTPLUG_FOLLOWUPS]
    assert h.r._next_wait() == HOTPLUG_FOLLOWUPS[0]
    assert any(e.reason == "Hotplug" and e.priority == 7 for e in h.r.events)

    # The device only shows up in PipeWire a moment later: the first pass
    # misses it, a follow-up catches it without waiting for HW_REFRESH.
    h.plug_scarlett(False)
    h.r.reconcile_once()
    assert h.r.status["devices"]["scarlett"]["present"] is False
    h.plug_scarlett(True)
    h.r.reconcile_once()  # not due yet, not dirty: still the stale snapshot
    assert h.r.status["devices"]["scarlett"]["present"] is False
    h.clock.t = t0 + 0.6
    h.r.reconcile_once()
    assert h.r.status["devices"]["scarlett"]["present"] is True
    assert h.r._hw_followups == [t0 + d for d in HOTPLUG_FOLLOWUPS[1:]]
    h.clock.t = t0 + 10
    h.r.reconcile_once()
    assert h.r._hw_followups == [] and h.r._next_wait() == 2.0
