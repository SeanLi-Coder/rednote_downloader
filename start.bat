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
if errorlevel 1 echo Warning: FFmpeg was not found. Split video/audio downloads may fail.
where ffprobe >nul 2>nul
if errorlevel 1 echo Warning: FFprobe was not found. Douyin and Xiaohongshu video quality verification will fail.

echo Starting Original Media Downloader at http://127.0.0.1:8766
%PYTHON_CMD% launcher.py
if errorlevel 1 goto :run_error
exit /b 0

:run_error
echo Startup stopped. Review the message above, then try again.
pause
exit /b 1
