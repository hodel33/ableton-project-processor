#!/bin/bash
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is not installed. Please install it from https://www.python.org/downloads/ first."
    read -p "Press Enter to close..."
    exit 1
fi

# Run out of a local .venv rather than whichever python3 happens to win the PATH
# race. Homebrew/MacPorts Python refuse `pip install` into the global environment
# (PEP 668, "externally-managed-environment") — creating a venv is not blocked,
# and installs inside one always are allowed.
PY=".venv/bin/python3"

if [ ! -x "$PY" ]; then
    echo "First-run setup: creating local Python environment (.venv)..."
    if ! python3 -m venv .venv; then
        echo "Could not create .venv — falling back to the system Python."
        PY="python3"
    fi
fi

if "$PY" -c "import webview" 2>/dev/null; then
    # Already set up: minimize Terminal, same as before.
    osascript -e 'tell application "Terminal" to set miniaturized of front window to true' &
else
    # First run — keep Terminal visible so the install progress is readable
    # (~1 min; pyobjc is large).
    echo "First-run setup: installing dependencies (this may take a minute)..."
    if ! "$PY" -m pip install -r requirements.txt; then
        echo
        echo "Dependency install failed — see the error above."
        read -p "Press Enter to close..."
        exit 1
    fi
fi

"$PY" gui.py
