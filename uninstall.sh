#!/usr/bin/env bash
# Removes the app, units and launcher. Keeps your library, database and config unless --purge.
set -euo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/anime-dl"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/anime-dl"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
systemctl --user disable --now anidash.service anidash-kick.timer 2>/dev/null || true
rm -f "$UNITS"/anidash.service "$UNITS"/anidash-kick.service "$UNITS"/anidash-kick.timer
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/anidash.desktop"
systemctl --user daemon-reload
rm -rf "$DATA/app" "$DATA/venv"
if [[ ${1:-} == --purge ]]; then
  rm -rf "$DATA" "$CONF"
  python - <<'EOF' 2>/dev/null || true
import keyring
for k in ("anilist_token", "qbit_password"):
    try: keyring.delete_password("anidash", k)
    except Exception: pass
EOF
fi
echo "Removed. Your ~/Anime library was not touched."
