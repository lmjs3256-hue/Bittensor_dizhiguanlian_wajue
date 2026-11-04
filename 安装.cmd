@echo off
chcp 65001 >nul
title Bittensor 地址关联挖掘 - 安装
cd /d "%~dp0"
echo.
echo ==========================================
echo   Bittensor 地址关联挖掘 安装向导
echo ==========================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
echo.
pause
