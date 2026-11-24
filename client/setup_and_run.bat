@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
    echo Создаю окружение Python...
    py -3 -m venv .venv || python -m venv .venv
    call .venv\Scripts\activate.bat
    python -m pip install -U pip
    pip install -r requirements.txt
    python tools\get_opus.py
) else (
    call .venv\Scripts\activate.bat
)
start "" .venv\Scripts\pythonw.exe run_beseda.py
