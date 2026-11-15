#!/usr/bin/env bash
# Build the patched jalv that r7s drives.
#
# Distro jalv 1.8.0 can't change a control on a running plugin (console
# "set" is silently ignored), and it spins at 100% CPU once stdin closes.
# This builds 1.8.0 plus the patches in ./patches against the system's
# LV2 libraries, console frontend only, into a private prefix so the
# distro jalv is left alone.
#
# Build deps (Debian/Ubuntu):
#   sudo apt install meson ninja-build pkg-config gcc libjack-jackd2-dev \
#     liblilv-dev libsuil-dev libserd-dev libsord-dev libsratom-dev \
#     libzix-dev lv2-dev
#
# Usage: third_party/jalv/build.sh [PREFIX]   (default ~/.local/opt/r7s-jalv)
set -euo pipefail

VERSION=v1.8.0
REPO=https://github.com/drobilla/jalv.git
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${1:-$HOME/.local/opt/r7s-jalv}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/r7s-jalv.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

git clone --quiet --depth 1 --branch "$VERSION" "$REPO" "$WORK/jalv"
cd "$WORK/jalv"
for p in "$HERE"/patches/*.patch; do
    git apply --verbose "$p"
done

meson setup build --prefix "$PREFIX" --buildtype release \
    -Djack=enabled -Dportaudio=disabled -Dsuil=enabled \
    -Dgtk3=disabled -Dqt5=disabled -Dqt6=disabled -Dcxx=disabled \
    -Dman=disabled -Dtests=disabled
meson compile -C build
meson install -C build

# Marker so r7sd can tell this build from a distro 1.8.0 with the same version.
mkdir -p "$PREFIX/share/r7s-jalv"
(cd "$HERE/patches" && ls *.patch) > "$PREFIX/share/r7s-jalv/PATCHED"

echo
echo "Installed: $PREFIX/bin/jalv"
"$PREFIX/bin/jalv" -V | head -1
