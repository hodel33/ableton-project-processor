#!/bin/bash
cd "${0%/*}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is not installed. Please install it from https://www.python.org/downloads/ first."
    exit 1
fi

# Minimize Terminal once pywebview is already installed; on first run keep it
# visible so the user sees the "Installing pywebview..." progress from gui.py.
if python3 -c "import webview" 2>/dev/null; then
    osascript -e 'tell application "Terminal" to set miniaturized of front window to true' &
fi

python3 gui.py
