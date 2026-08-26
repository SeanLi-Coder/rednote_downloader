@echo off
setlocal
cd /d "%~dp0"

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

%PYTHON_CMD% -c "import sys; raise SystemExit(sys.version_info ^< (3, 10))"
if errorlevel 1 (
  echo Python 3.10 or newer is required.
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 echo Warning: FFmpeg was not found. Highest-quality split video/audio downloads may fail.

if not exist ".venv\Scripts\python.exe" (
  echo Creating the local Python environment...
  %PYTHON_CMD% -m venv .venv
  if errorlevel 1 goto :error
)

.venv\Scripts\python.exe -c "import sys; raise SystemExit(sys.version_info ^< (3, 10))"
if errorlevel 1 (
  echo The existing .venv uses an unsupported Python version. Delete .venv and run again.
  pause
  exit /b 1
)

echo Checking dependencies...
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :error

echo Starting Original Media Downloader at http://127.0.0.1:8765
.venv\Scripts\python.exe run.py
exit /b %errorlevel%

:error
echo Setup failed. Review the error above and try again.
pause
exit /b 1
