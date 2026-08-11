@echo off
setlocal
cd /d "%~dp0"

title Thomasson Farms Dashboard

if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo Starting Thomasson Farms Dashboard...
echo Leave this window open while using the dashboard.
echo.

"%PY%" "%~dp0launch_dashboard.py"
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo.
  echo Dashboard exited with an error.
  pause
)

endlocal & exit /b %EXITCODE%
