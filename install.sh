#!/usr/bin/env bash
# User-level install (no root): ./install.sh [--uninstall] [--service]   PREFIX defaults to ~/.local
#   --service   also install and enable a systemd *user* service that runs `chatbridge watch` (auto-sync in the background)
set -euo pipefail

PREFIX="${PREFIX:-$HOME/.local}"
APP_DIR="$PREFIX/share/chatbridge"
BIN="$PREFIX/bin/chatbridge-gui"
CLI="$PREFIX/bin/chatbridge"
DESKTOP="$PREFIX/share/applications/io.github.pwnapplehat.ChatBridge.desktop"
UNIT="$HOME/.config/systemd/user/chatbridge-sync.service"
ICON="$PREFIX/share/icons/hicolor/scalable/apps/io.github.pwnapplehat.ChatBridge.svg"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "--uninstall" ]]; then
  # Removes only the files this script created; your chats, Claude data and settings are untouched.
  systemctl --user disable --now chatbridge-sync.service >/dev/null 2>&1 || true
  rm -f "$BIN" "$CLI" "$DESKTOP" "$ICON" "$UNIT"
  rm -rf "$APP_DIR/venv"
  rmdir "$APP_DIR" 2>/dev/null || true
  echo "Uninstalled from $PREFIX"
  exit 0
fi

python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null || {
  echo "GTK4 + libadwaita Python bindings are missing. On Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1" >&2
  exit 1
}

mkdir -p "$APP_DIR" "$PREFIX/bin" "$PREFIX/share/applications" "$(dirname "$ICON")"
python3 -m venv --system-site-packages "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet "$HERE"

printf '#!/usr/bin/env bash\nexec "%s/venv/bin/python" -m chatbridge.gui "$@"\n' "$APP_DIR" > "$BIN"
printf '#!/usr/bin/env bash\nexec "%s/venv/bin/python" -m chatbridge "$@"\n' "$APP_DIR" > "$CLI"
chmod +x "$BIN" "$CLI"
install -m 644 "$HERE/packaging/io.github.pwnapplehat.ChatBridge.svg" "$ICON"
sed "s|@LAUNCHER@|$BIN|" "$HERE/packaging/io.github.pwnapplehat.ChatBridge.desktop.in" > "$DESKTOP"
command -v update-desktop-database >/dev/null && update-desktop-database "$PREFIX/share/applications" || true
if [[ "${1:-}" == "--service" ]]; then
  mkdir -p "$(dirname "$UNIT")"
  sed "s|@CLI@|$CLI|" "$HERE/packaging/chatbridge-sync.service.in" > "$UNIT"
  systemctl --user daemon-reload
  systemctl --user enable --now chatbridge-sync.service
  echo "Auto-sync service enabled (systemctl --user status chatbridge-sync)."
fi
echo "Installed. Launch 'ChatBridge' from the app menu, or run: $BIN"
