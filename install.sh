#!/bin/sh
# Install the Warsaw smog widget for the current user and start it at login.
#
#   ./install.sh              install or update
#   ./install.sh --uninstall  remove it (settings, history and cache are kept)
#
# Nothing here needs root; all files go under your home directory.
set -eu

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
APP_DIR="$DATA/warsaw-smog/app"
APP="$APP_DIR/warsaw_smog.py"
AUTOSTART="$CONF/autostart/warsaw-smog.desktop"
LAUNCHER="$DATA/applications/warsaw-smog.desktop"
SRC="$(cd "$(dirname "$0")" && pwd)/warsaw_smog.py"

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$AUTOSTART" "$LAUNCHER" "$APP"
    rmdir "$APP_DIR" 2>/dev/null || true
    echo "Removed the widget, its autostart entry and its menu entry."
    echo "Kept: settings ($CONF/warsaw-smog.json), history ($DATA/warsaw-smog/),"
    echo "      cache (${XDG_CACHE_HOME:-$HOME/.cache}/warsaw-smog/). Delete them by hand if you wish."
    echo "If the widget is running now, close it with right-click > Quit."
    exit 0
fi

if [ -n "${1:-}" ]; then
    echo "Unknown option: $1 (use no option to install, or --uninstall)" >&2
    exit 2
fi

if [ ! -f "$SRC" ]; then
    echo "warsaw_smog.py not found next to install.sh" >&2
    exit 1
fi

# The widget needs GTK 3 for Python. Check, but do not install anything ourselves.
if ! python3 -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk; import cairo" 2>/dev/null; then
    echo "Missing dependencies. Install them first, then run this script again:" >&2
    echo "    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0" >&2
    exit 1
fi

mkdir -p "$APP_DIR" "$(dirname "$AUTOSTART")" "$(dirname "$LAUNCHER")"
install -m 755 "$SRC" "$APP"

# One entry for the applications menu, one for autostart. The short delay gives
# the network a moment to come up after login, so the first refresh succeeds.
write_entry() {
    cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Name=Warsaw smog
Comment=Air quality in Warsaw on a map
Exec=python3 "$APP"
Icon=weather-fog
Terminal=false
Categories=Utility;
EOF
}
write_entry "$LAUNCHER"
write_entry "$AUTOSTART"
cat >> "$AUTOSTART" <<EOF
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=10
EOF

echo "Installed to $APP"
echo "It will start automatically at your next login."
echo "To start it now: open \"Warsaw smog\" from the applications menu, or run"
echo "    python3 \"$APP\" &"
if pgrep -f "^python3 .*warsaw_smog\.py" >/dev/null 2>&1; then
    echo "A copy is running already: quit it (right-click > Quit) and start it again to use this version."
fi
echo "To remove it later: $0 --uninstall"
