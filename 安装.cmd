@echo off
chcp 65001 >nul
title Bittensor 地址关联挖掘 - 安装
cd /d "%~dp0"
echo.
echo ==========================================
echo   Bittensor 地址关联挖掘 安装向导
echo ==========================================
rem git 合并冲突没解决时，脚本里会残留冲突标记（一排小于号加 HEAD）。
rem PowerShell 只会报「小于号是保留运算符」这种看不懂的错误，这里先拦下来。
findstr /m /c:"<<<<" "%~dp0*.ps1" >nul 2>&1
if errorlevel 1 goto scripts_ok
echo.
echo [!] 脚本里残留了 Git 冲突标记（^<^<^<^<^<^<^< HEAD），安装无法继续。
echo     在本文件夹打开命令行，执行：
echo         git status
echo         git checkout origin/main -- *.ps1
echo.
pause
exit /b 1
:scripts_ok

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
echo.
pause
