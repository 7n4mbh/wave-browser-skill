#!/usr/bin/env bash
# bootstrap.sh — create the local Python venv used by wave-browser
# Idempotent: safe to re-run. The wave-browser launcher calls this on first
# use, so an agent does not normally need to invoke it directly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="${WAVE_BROWSER_VENV:-$SKILL_DIR/.venv}"
REQ_FILE="$SKILL_DIR/requirements.txt"
STAMP="$VENV_DIR/.installed"

PY="${WAVE_BROWSER_PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[wave-browser] python3 not found on PATH. Install Python 3.10+." >&2
  exit 1
fi

PY_MAJOR=$("$PY" -c 'import sys;print(sys.version_info[0])')
PY_MINOR=$("$PY" -c 'import sys;print(sys.version_info[1])')
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  echo "[wave-browser] need Python >= 3.10, found $PY_MAJOR.$PY_MINOR" >&2
  exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "[wave-browser] creating venv at $VENV_DIR" >&2
  "$PY" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
. "$VENV_DIR/bin/activate"

if [ ! -f "$STAMP" ] || [ "$REQ_FILE" -nt "$STAMP" ]; then
  echo "[wave-browser] installing Python dependencies" >&2
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r "$REQ_FILE"
  echo "[wave-browser] installing Chromium for Playwright" >&2
  python -m playwright install --with-deps chromium 2>/dev/null \
    || python -m playwright install chromium
  date > "$STAMP"
fi

echo "[wave-browser] ready (venv: $VENV_DIR)"
