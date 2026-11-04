@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Wallet Link - Issue License
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo [!] Runtime not installed. Run the installer first.
  echo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "tools\issue_license.py" --interactive
if errorlevel 1 pause
