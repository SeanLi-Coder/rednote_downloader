#!/bin/bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_CMD=""
if [ -x ".venv/bin/python" ] \
  && .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  PYTHON_CMD=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1 \
  && python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  PYTHON_CMD="$(command -v python3)"
else
  echo "Python 3.10 or newer is required."
  read -r -p "Press Enter to close..."
  exit 1
fi

set +e
"$PYTHON_CMD" stop.py
task_exit_code=$?
set -e
if [ "$task_exit_code" -ne 0 ]; then
  echo ""
  read -r -p "Stop did not complete. Review the message above, then press Enter to close..."
fi
exit "$task_exit_code"
