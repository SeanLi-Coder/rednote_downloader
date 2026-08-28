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

if ! /usr/bin/open -Ra "Google Chrome" >/dev/null 2>&1; then
  echo "Warning: Google Chrome was not found. Cookie access and verification cannot work until it is installed."
fi

echo "Starting Original Media Downloader at http://127.0.0.1:8766"
launcher_pid=""
shutdown_requested=0

forward_stop() {
  shutdown_requested=1
  if [ -n "$launcher_pid" ] && kill -0 "$launcher_pid" 2>/dev/null; then
    kill -TERM "$launcher_pid" 2>/dev/null || true
  fi
}

trap forward_stop HUP INT TERM
set +e
python3 launcher.py --parent-pid "$$" &
launcher_pid=$!
wait "$launcher_pid"
task_exit_code=$?
while kill -0 "$launcher_pid" 2>/dev/null; do
  wait "$launcher_pid"
  task_exit_code=$?
done
set -e
trap - HUP INT TERM
if [ "$shutdown_requested" -eq 1 ]; then
  task_exit_code=0
fi
if [ "$task_exit_code" -ne 0 ]; then
  echo ""
  read -r -p "Startup stopped. Review the message above, then press Enter to close..."
fi
exit "$task_exit_code"
