#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Nimbus -- a weather application for GNOME.
# Copyright (C) 2026  Walter Rice
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# Install Nimbus into the user's home (no root required).
#
# Copies the package to ~/.local/share/nimbus-weather, installs the icon set
# into the hicolor theme, and writes a desktop entry pointing at the launcher.
set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
APPDIR="$PREFIX/nimbus-weather"
BINDIR="$HOME/.local/bin"
LAUNCHER="$BINDIR/nimbus-weather"

echo "Installing Nimbus to $APPDIR"
rm -rf "$APPDIR"
mkdir -p "$APPDIR" "$BINDIR" "$PREFIX/applications"
cp -r "$SOURCE/nimbus" "$APPDIR/"
find "$APPDIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

cat > "$LAUNCHER" <<LAUNCH
#!/usr/bin/env bash
exec python3 -m nimbus "\$@"
LAUNCH
sed -i "2i export PYTHONPATH=\"$APPDIR:\${PYTHONPATH:-}\"" "$LAUNCHER"
chmod +x "$LAUNCHER"

for size in 16 24 32 48 64 128 256 512; do
  icon="$SOURCE/data/icons/nimbus-$size.png"
  [ -f "$icon" ] || continue
  target="$PREFIX/icons/hicolor/${size}x${size}/apps"
  mkdir -p "$target"
  cp "$icon" "$target/org.nimbus.Weather.png"
done

sed "s|@EXEC@|$LAUNCHER|" "$SOURCE/data/org.nimbus.Weather.desktop.in" \
  > "$PREFIX/applications/org.nimbus.Weather.desktop"

command -v gtk-update-icon-cache >/dev/null && \
  gtk-update-icon-cache -qtf "$PREFIX/icons/hicolor" 2>/dev/null || true
command -v update-desktop-database >/dev/null && \
  update-desktop-database -q "$PREFIX/applications" 2>/dev/null || true

echo "Done. Launch from your applications list, or run: $LAUNCHER"
case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *) echo "Note: $BINDIR is not on your PATH." ;;
esac
