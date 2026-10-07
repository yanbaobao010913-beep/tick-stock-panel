@echo off
rem TickFlow desktop app (pywebview window + embedded backend).
rem Diagnostic fallback only - the desktop shortcut runs pythonw directly (no console).
rem Shares the repo data\ directory with dev mode - do not run both at once.
cd /d "%~dp0backend"
uv run --no-sync python -m app.desktop
