#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10 or newer is required."
  read -r -p "Press Enter to close..."
  exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "Python 3.10 or newer is required."
  read -r -p "Press Enter to close..."
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Warning: FFmpeg was not found. Highest-quality split video/audio downloads may fail."
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating the local Python environment..."
  python3 -m venv .venv
fi

if ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "The existing .venv uses an unsupported Python version. Delete .venv and run again."
  read -r -p "Press Enter to close..."
  exit 1
fi

if ! /usr/bin/open -Ra "Google Chrome" >/dev/null 2>&1; then
  echo "Warning: Google Chrome was not found. Cookie access and verification cannot work until it is installed."
fi

echo "Checking dependencies..."
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt

echo "Starting Original Media Downloader at http://127.0.0.1:8765"
exec .venv/bin/python run.py
