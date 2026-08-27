#!/bin/bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"

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

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "Warning: FFprobe was not found. Douyin and Xiaohongshu video quality verification will fail."
  echo "On macOS with Homebrew, run: brew install ffmpeg"
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Warning: FFmpeg was not found. Split video/audio downloads may fail."
  echo "On macOS with Homebrew, run: brew install ffmpeg"
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
set +e
.venv/bin/python run.py
task_exit_code=$?
set -e
if [ "$task_exit_code" -ne 0 ]; then
  echo ""
  read -r -p "Startup stopped. Review the message above, then press Enter to close..."
fi
exit "$task_exit_code"
