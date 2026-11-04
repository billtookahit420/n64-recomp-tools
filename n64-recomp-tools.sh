#!/bin/sh
# N64Recomp Tools launcher: bootstraps a local venv with PySide6 +
# Pillow on first run (checks the imports, not just the venv, so a
# half-made venv heals itself), then starts the GUI.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
if ! .venv/bin/python -c "import PySide6, PIL" 2>/dev/null; then
    .venv/bin/pip install --quiet PySide6 Pillow
fi
exec .venv/bin/python app/main.py "$@"
