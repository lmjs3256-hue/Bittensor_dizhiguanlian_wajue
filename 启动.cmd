@echo off
chcp 65001 >nul
title Bittensor 地址关联挖掘
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo.
  echo [!] 还没有安装运行环境。
  echo     请先双击本文件夹里的 安装.cmd ，装完再启动。
  echo.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_desktop.ps1"
if errorlevel 1 (
  echo.
  echo [!] 启动失败。可以双击 停止.cmd 清理后重试，
  echo     或查看 logs\wallet-link.stderr.log 里的错误信息。
  echo.
  pause
)
