#!/bin/bash
# Cloud sessions only: a venv with anidash (editable) + dev tools, so pytest and ruff work.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -e ".[dev]" ruff pyinstaller

# optional: real qBittorrent for tests/test_integration_qbit.py (skipped when missing)
if ! command -v qbittorrent-nox >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
  (apt-get install -y -q qbittorrent-nox >/dev/null 2>&1 \
    || (apt-get update -q >/dev/null 2>&1 && apt-get install -y -q qbittorrent-nox >/dev/null 2>&1)) \
    || echo "qbittorrent-nox not installed; the integration test will be skipped" >&2
fi

echo "export VIRTUAL_ENV=\"$CLAUDE_PROJECT_DIR/.venv\"" >> "$CLAUDE_ENV_FILE"
echo "export PATH=\"$CLAUDE_PROJECT_DIR/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
