@echo off
chcp 65001 >nul
title Беседа — клиент (с консолью для отладки)
cd /d "%~dp0"
if not exist .venv (
    echo Сначала запустите setup_and_run.bat
    pause
    exit /b 1
)
.venv\Scripts\python.exe run_beseda.py %*
echo.
echo Журнал: %LOCALAPPDATA%\Beseda\beseda.log
pause
