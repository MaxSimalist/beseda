@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
    echo Сначала запустите setup_and_run.bat
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
pip install -q pyinstaller
if not exist libopus-0.x64.dll python tools\get_opus.py

set OPUS=
if exist libopus-0.x64.dll set OPUS=--add-binary "libopus-0.x64.dll;."

pyinstaller --noconfirm --clean --windowed --name Beseda %OPUS% ^
    --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.Qt3DCore ^
    --exclude-module PySide6.QtQuick --exclude-module PySide6.QtQml ^
    run_beseda.py

echo.
echo Готово: dist\Beseda\Beseda.exe
echo Папку dist\Beseda можно заархивировать и раздать.
pause
