#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
  exit 1
fi

exec "$PYTHON_CMD" stop.py
