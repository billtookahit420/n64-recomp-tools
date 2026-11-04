@echo off
rem N64Recomp Tools launcher (Windows): the GUI can run on Windows,
rem but conversions produce Linux packages only (the N64 runtime's
rem Windows build is MSVC-only). Bootstraps a local venv with
rem PySide6 + Pillow; checks the imports so a half-made venv heals.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv
)
.venv\Scripts\python -c "import PySide6, PIL" 2>nul
if errorlevel 1 .venv\Scripts\pip install --quiet PySide6 Pillow
.venv\Scripts\python app\main.py %*
