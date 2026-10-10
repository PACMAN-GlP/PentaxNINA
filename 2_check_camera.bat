@echo off
setlocal
cd /d "%~dp0"
if /i "%~1"=="elevated" goto :admin_ok
net session >nul 2>&1
if not errorlevel 1 goto :admin_ok
echo Asking for administrator rights - pkTriggerCord needs them to open the camera drive ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList 'elevated' -Verb RunAs"
exit /b
:admin_ok
cd /d "%~dp0"
title Pentax K-5 II - NINA bridge: camera check
if not exist venv\Scripts\python.exe (
    echo Please run 1_install.bat first.
    pause
    exit /b 1
)
call venv\Scripts\activate.bat
python pentax_alpaca_camera.py --probe
echo.
pause
