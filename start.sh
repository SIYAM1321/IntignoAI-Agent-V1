#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
PYTHON="/usr/bin/python3"
VENV_DIR="$APP_DIR/myenv"
if [[ ! -x "$PYTHON" ]] || ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,9) else 1)'; then
  echo "[ERROR] This macOS launcher needs /usr/bin/python3 version 3.9.x" >&2; exit 1
fi
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  if [[ -e "$VENV_DIR" ]]; then mv "$VENV_DIR" "${VENV_DIR}-backup-$(date +%Y%m%d-%H%M%S)"; fi
  "$PYTHON" -m venv "$VENV_DIR"
fi
VENV_PYTHON="$VENV_DIR/bin/python"
"$VENV_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,9) else 1)' || { echo '[ERROR] Existing myenv must be Python 3.9'; exit 1; }
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"
"$VENV_PYTHON" "$APP_DIR/tools/ensure_playwright.py"
echo '[INFO] Web UI: http://127.0.0.1:2000'
# Only open the IntignoAI UI, never a physical agent browser.
(sleep 2; open 'http://127.0.0.1:2000' >/dev/null 2>&1 || true) &
exec "$VENV_PYTHON" "$APP_DIR/app.py"
