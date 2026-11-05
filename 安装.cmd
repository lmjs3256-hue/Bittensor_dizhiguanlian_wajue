@echo off
title Bittensor 地址关联挖掘 - 安装
cd /d "%~dp0"
echo.
echo ==========================================
echo   Bittensor 地址关联挖掘 安装向导
echo ==========================================
rem Unresolved git merge leaves conflict markers (a run of '<' plus HEAD)
rem in .ps1, which breaks parsing. Catch it here first.
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
