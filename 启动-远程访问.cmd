@echo off
title Bittensor 地址关联挖掘 - 远程访问
cd /d "%~dp0"
rem 甲方案：这台电脑跑服务，家里 / 公司 / 手机用浏览器连过来（需要访问口令）。
rem 启动窗口会打印访问口令和各设备该用的地址。
rem 可选参数：端口，例如 8767。默认 8766。
set "WL_PORT=%~1"
if "%WL_PORT%"=="" set "WL_PORT=8766"

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo.
  echo [!] 你还没有安装运行环境。
  echo     请先双击本文件夹里的 安装.cmd 安装，完成后再启动。
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_desktop.ps1" -Port %WL_PORT% -Remote
if errorlevel 1 (
  echo.
  echo [!] 启动失败。可以先双击 停止.cmd 再重试，
  echo     或查看 logs\wallet-link.stderr.log 里的错误信息。
  echo.
  pause
)
