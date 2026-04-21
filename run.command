#!/bin/bash
cd "${0%/*}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is not installed. Please install it from https://www.python.org/downloads/ first."
    read -p "Press Enter to continue..."
    exit 1
fi

python3 ableton_project_processor.py
read -p "Press Enter to continue..."
