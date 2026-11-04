@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Wallet Link - Machine Code
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo [!] Runtime not installed. Run the installer first.
  echo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m app.license
echo.
pause
