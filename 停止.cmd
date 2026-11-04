@echo off
chcp 65001 >nul
title Bittensor 地址关联挖掘 - 停止
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
echo.
pause
