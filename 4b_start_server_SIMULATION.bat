@echo off
setlocal
cd /d "%~dp0"
title Pentax K-5 II - NINA bridge: SIMULATION (no camera)
if not exist venv\Scripts\python.exe (
    echo Please run 1_install.bat first.
    pause
    exit /b 1
)
call venv\Scripts\activate.bat
python pentax_alpaca_camera.py --simulate
echo.
pause
