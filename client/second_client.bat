@echo off
chcp 65001 >nul
title Беседа — второй клиент (test2)
rem Второй клиент на этом же компьютере со своим аккаунтом (отдельный профиль настроек).
cd /d "%~dp0"
if not exist .venv (
    echo Сначала запустите setup_and_run.bat
    pause
    exit /b 1
)
.venv\Scripts\python.exe run_beseda.py --profile test2
pause
