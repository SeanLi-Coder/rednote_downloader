@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD="
if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe -c "import sys; raise SystemExit(sys.version_info ^< (3, 10))"
  if not errorlevel 1 set "PYTHON_CMD=.venv\Scripts\python.exe"
)
if not defined PYTHON_CMD (
  where py >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
  ) else (
    where python >nul 2>nul
    if errorlevel 1 (
      echo Python 3.10 or newer is required.
      pause
      exit /b 1
    )
    set "PYTHON_CMD=python"
  )
)

%PYTHON_CMD% -c "import sys; raise SystemExit(sys.version_info ^< (3, 10))"
if errorlevel 1 (
  echo Python 3.10 or newer is required.
  pause
  exit /b 1
)

%PYTHON_CMD% stop.py
if errorlevel 1 (
  echo Stop did not complete. Review the message above, then try again.
  pause
  exit /b 1
)
exit /b 0
