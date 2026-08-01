#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build a binary .deb from the current checkout. Run from the repository
# root on any system with dpkg-deb (Debian, Ubuntu, or a container):
#
#   packaging/build-deb.sh
#
# The finished package lands in dist/. It is a plain staged binary package
# rather than a debhelper source build: the app is pure Python with no
# compiled parts, so staging the tree is the whole job.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

VERSION="$(python3 -c 'import nimbus; print(nimbus.__version__)')"
STAGE="$(pwd)/build/deb/nimbus-weather_${VERSION}_all"

rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" dist

# -- payload ----------------------------------------------------------------

PKGDIR="$STAGE/usr/lib/python3/dist-packages"
mkdir -p "$PKGDIR"
cp -r nimbus "$PKGDIR/"
find "$PKGDIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

install -Dm0755 /dev/stdin "$STAGE/usr/bin/nimbus-weather" <<'LAUNCHER'
#!/usr/bin/python3
import sys

from nimbus.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
LAUNCHER

sed 's|@EXEC@|nimbus-weather|' data/org.nimbus.Weather.desktop.in \
    | install -Dm0644 /dev/stdin "$STAGE/usr/share/applications/org.nimbus.Weather.desktop"
install -Dm0644 data/org.nimbus.Weather.metainfo.xml \
    "$STAGE/usr/share/metainfo/org.nimbus.Weather.metainfo.xml"
for size in 16 24 32 48 64 128 256 512; do
    install -Dm0644 "data/icons/nimbus-${size}.png" \
        "$STAGE/usr/share/icons/hicolor/${size}x${size}/apps/org.nimbus.Weather.png"
done
install -Dm0644 LICENSE "$STAGE/usr/share/doc/nimbus-weather/copyright"

# -- control ------------------------------------------------------------------

INSTALLED_SIZE="$(du -sk "$STAGE" --exclude=DEBIAN | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: nimbus-weather
Version: ${VERSION}
Architecture: all
Section: utils
Priority: optional
Maintainer: Walter Rice <waltricejr@gmail.com>
Installed-Size: ${INSTALLED_SIZE}
Depends: python3 (>= 3.11), python3-gi, python3-gi-cairo, python3-cairo, gir1.2-gtk-4.0, gir1.2-adw-1
Homepage: https://github.com/WaltRiceJr/nimbus
Description: Weather for the United States, using National Weather Service data
 A weather application for GNOME. Every weather symbol and sky scene is
 drawn as vector art at runtime: the sky gradient follows the sun's real
 altitude, the moon is shown at its true phase, and rain, snow, fog and
 lightning animate over drifting cloud layers. Includes a 48-hour forecast
 strip, a 7-day outlook, animated radar with satellite cloud cover, and
 NWS watches and warnings.
 .
 An independent project, not affiliated with or endorsed by the National
 Weather Service or NOAA.
CONTROL

dpkg-deb --build --root-owner-group "$STAGE" dist/
echo "Package in dist/:"
ls -1 dist/*.deb
