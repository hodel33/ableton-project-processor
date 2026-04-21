@echo off
setlocal
cd /d "%~dp0"

rem ── If this is the first run (webview not installed yet), stay visible so the
rem    user can see the "Installing pywebview..." progress. Otherwise relaunch
rem    minimized just like before.
python -c "import webview" 2>nul
if errorlevel 1 goto run

if not "%~1"=="min" (
    start "" /min cmd /c ""%~f0" min"
    exit /b
)

:run
python gui.py
if errorlevel 1 pause
