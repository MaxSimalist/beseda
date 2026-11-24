@echo off
chcp 65001 >nul
title Беседа — тестовый сервер
cd /d "%~dp0"

if not exist .venv (
    echo Первый запуск: создаю окружение Python и ставлю библиотеки...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if not exist .venv\Scripts\python.exe (
        echo Не найден Python. Установите Python 3.10+ с python.org с галочкой "Add Python to PATH".
        pause
        exit /b 1
    )
    .venv\Scripts\python -m pip install -q -U pip
    .venv\Scripts\python -m pip install -q -r requirements.txt
)

rem --- настройки тестового сервера ---
set BESEDA_HOST=0.0.0.0
set BESEDA_PORT=8765
set BESEDA_UDP_HOST=0.0.0.0
set BESEDA_UDP_PORT=50000
set BESEDA_DATA=%~dp0data
set BESEDA_REG_CODE=
set BESEDA_MAX_UPLOAD_MB=50

echo.
echo Если Windows спросит про доступ к сети для Python - разрешите
echo (галочка "Частные сети"), иначе другие компьютеры не подключатся.
echo.
.venv\Scripts\python -m beseda_server
pause
