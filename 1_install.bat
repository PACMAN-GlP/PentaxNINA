@echo off
setlocal
cd /d "%~dp0"
title Pentax K-5 II - NINA bridge: install
echo ============================================================
echo   Pentax K-5 II to NINA bridge : install
echo ============================================================
echo.

set "PYCMD="
for %%V in (3.12 3.13 3.11 3.10) do if not defined PYCMD (py -%%V --version >nul 2>nul && set "PYCMD=py -%%V")
if not defined PYCMD (py -3 --version >nul 2>nul && set "PYCMD=py -3")
if not defined PYCMD (python --version >nul 2>nul && set "PYCMD=python")
if not defined PYCMD goto :nopython

echo Using: %PYCMD%
%PYCMD% --version
echo.
echo Creating a private Python environment in the folder "venv" ...
%PYCMD% -m venv venv
if errorlevel 1 goto :fail

call venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo   Install finished.
echo   Next: open pentax_alpaca_config.json in Notepad, set cli_path,
echo   then double-click 2_check_camera.bat
echo ============================================================
pause
exit /b 0

:nopython
echo Python was not found.
echo Install Python 3.12 (64-bit) from https://www.python.org/downloads/windows/
echo and tick "Add python.exe to PATH" on the first page of the installer.
echo Then run this file again.
pause
exit /b 1

:fail
echo.
echo Something went wrong - please read the messages above.
echo If the error mentions rawpy or "no matching distribution", install Python 3.12 (64-bit).
pause
exit /b 1
