@echo off
title EDITH AI Assistant

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo EDITH virtual environment not found!
    pause
    exit /b 1
)

echo Starting EDITH AI Assistant...

start "" "venv\Scripts\pythonw.exe" "assistant_interface.py"

exit