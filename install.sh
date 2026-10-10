#!/usr/bin/env bash
# anidash installer for EndeavourOS / Arch. Run as your normal user from the project folder.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/anime-dl"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/anime-dl"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
APP="$DATA/app"
VENV="$DATA/venv"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
[[ $EUID -eq 0 ]] && { echo "Run as your user, not root (sudo is used only for pacman)."; exit 1; }
for f in anidash/api.py Dashboard.dc.html support.js requirements.txt; do
  [[ -e "$SRC/$f" ]] || { echo "Missing $f — run from the project folder."; exit 1; }
done

say "System packages"
PKGS=(python python-pip python-gobject webkit2gtk-4.1 glib2 libsecret vlc qbittorrent-nox)
missing=()
for p in "${PKGS[@]}"; do pacman -Qi "$p" &>/dev/null || missing+=("$p"); done
if ((${#missing[@]})); then sudo pacman -S --needed "${missing[@]}"; else echo "all present"; fi

say "App files → $APP"
systemctl --user stop anidash.service 2>/dev/null || true
mkdir -p "$APP" "$CONF" "$UNITS" "$APPS"
rm -rf "$APP/anidash" "$APP/web" "$APP/tests"
cp -r "$SRC/anidash" "$SRC/tests" "$APP/"
mkdir -p "$APP/web"
cp "$SRC/Dashboard.dc.html" "$SRC/support.js" "$APP/web/"
cp -r "$SRC/_ds" "$APP/web/"
cp "$SRC/requirements.txt" "$SRC/README.md" "$APP/" 2>/dev/null || true
find "$APP" -name '__pycache__' -prune -exec rm -rf {} +

say "Python venv → $VENV"
# system site packages so the GTK webview (python-gobject) is visible to pywebview
[[ -x "$VENV/bin/python" ]] || python -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$APP/requirements.txt"
"$VENV/bin/pip" install -q anitomy-ng || echo "   (anitomy-ng unavailable: using the built-in regex parser)"
( cd "$APP" && "$VENV/bin/python" -m pytest -q tests ) || { echo "Parser tests failed"; exit 1; }

say "qBittorrent"
if ! systemctl is-enabled --quiet "qbittorrent-nox@$USER" 2>/dev/null; then
  read -rp "Enable qbittorrent-nox as a service for $USER now? [Y/n] " yn
  if [[ ${yn:-Y} =~ ^[Yy] ]]; then
    sudo systemctl enable --now "qbittorrent-nox@$USER"
    sleep 3
    echo "   qBittorrent 5 prints a temporary Web UI password on first start:"
    sudo journalctl -u "qbittorrent-nox@$USER" --no-pager | grep -i 'password' | tail -n 1 || true
    echo "   Log in at http://127.0.0.1:8080, set a real password, and in Options → Web UI:"
    echo "   keep 'Bypass authentication for localhost' OFF. Bind qBit to your VPN interface (Options → Advanced)."
  fi
fi

say "First-run setup"
( cd "$APP" && PYTHONPATH="$APP" "$VENV/bin/python" -m anidash setup )

say "systemd user units"
cp "$SRC/systemd/anidash.service" "$SRC/systemd/anidash-kick.service" "$SRC/systemd/anidash-kick.timer" "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now anidash.service anidash-kick.timer

say "Desktop launcher"
cat > "$APPS/anidash.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=anidash
Comment=Anime episode automation dashboard
Exec=env PYTHONPATH=$APP $VENV/bin/python -m anidash window
Icon=video-x-generic
Categories=AudioVideo;Video;
Terminal=false
EOF
update-desktop-database "$APPS" 2>/dev/null || true

for i in {1..20}; do curl -fs http://127.0.0.1:8787/api/health >/dev/null && break; sleep 0.5; done
if curl -fs http://127.0.0.1:8787/api/health >/dev/null; then
  say "Done. Open “anidash” from your app launcher. Logs: journalctl --user -u anidash -f"
else
  say "Service did not answer. Check: journalctl --user -u anidash -e"; exit 1
fi
