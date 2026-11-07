@echo off
title Bittensor 地址关联挖掘 - 公网隧道（客户端免安装）
cd /d "%~dp0"
rem 客户端不用装任何软件：手机 / 外网电脑直接用浏览器打开打印出来的 https 网址。
rem 可选：把邮箱作为第一个参数传进来，开启「只有这个邮箱能进」的验证码门禁，
rem       例如   启动-公网隧道.cmd you@example.com
rem 可选：第二个参数是端口，默认 8766。
set "WL_MAIL=%~1"
set "WL_PORT=%~2"
if "%WL_PORT%"=="" set "WL_PORT=8766"

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo.
  echo [!] 你还没有安装运行环境。
  echo     请先双击本文件夹里的 安装.cmd 安装，完成后再启动。
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tunnel.ps1" -Port %WL_PORT% -AllowedMail "%WL_MAIL%"
if errorlevel 1 (
  echo.
  echo [!] 隧道启动失败。可以先双击 停止.cmd 再重试，
  echo     或查看 logs\cloudflared.log 里的错误信息。
  echo.
  pause
)
