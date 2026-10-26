#!/bin/sh
# Start the fleet GUI at http://127.0.0.1:8765 (Ctrl+C to stop it).
cd "$(dirname "$0")"
exec python3 tools/gui/server.py
