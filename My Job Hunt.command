#!/bin/sh
# Double-click in Finder to open the local Job Hunt board in your browser.
cd "$(dirname "$0")" || exit 1
echo "Starting Job Hunt board on http://127.0.0.1:8765/"
echo "Press Ctrl+C to stop it."
exec python3 -m jobhunt board
