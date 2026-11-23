@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Set up .venv first. See README.md.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -B "start_xun.py"
