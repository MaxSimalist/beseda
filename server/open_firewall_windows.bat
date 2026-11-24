@echo off
chcp 65001 >nul
rem Открывает порты тестового сервера в брандмауэре Windows (только для частных сетей).
rem Нужно, только если другие компьютеры в сети не могут подключиться. Запускать от администратора.
net session >nul 2>&1
if errorlevel 1 (
    echo Запустите этот файл правой кнопкой - "Запуск от имени администратора".
    pause
    exit /b 1
)
netsh advfirewall firewall delete rule name="Beseda TCP 8765" >nul 2>&1
netsh advfirewall firewall delete rule name="Beseda UDP 50000" >nul 2>&1
netsh advfirewall firewall add rule name="Beseda TCP 8765" dir=in action=allow protocol=TCP localport=8765 profile=private
netsh advfirewall firewall add rule name="Beseda UDP 50000" dir=in action=allow protocol=UDP localport=50000 profile=private
echo.
echo Готово. Чтобы убрать правила потом:
echo   netsh advfirewall firewall delete rule name="Beseda TCP 8765"
echo   netsh advfirewall firewall delete rule name="Beseda UDP 50000"
pause
