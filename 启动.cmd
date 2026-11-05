@echo off
chcp 65001 >nul
title Bittensor 地址关联挖掘
cd /d "%~dp0"
rem 可选参数：第一个参数是端口，例如：启动.cmd 8767；不填默认 8766。
set "WL_PORT=%~1"
if "%WL_PORT%"=="" set "WL_PORT=8766"
rem git 合并如果没解决冲突，脚本里会残留冲突标记（一排小于号加 HEAD）。
rem PowerShell 会把它当成保留运算符，报一堆看不懂的语法错误，这里先拦下来。
findstr /m /c:"<<<<" "%~dp0*.ps1" >nul 2>&1
if errorlevel 1 goto scripts_ok
echo.
echo [!] 脚本里残留了 Git 冲突标记（^<^<^<^<^<^<^< HEAD），无法启动。
echo     通常是在这台电脑上 git pull 时 start.ps1 有本地改动、冲突没解决。
echo.
echo     在本文件夹打开命令行，执行下面两条就能修好：
echo         git status
echo         git checkout origin/main -- start.ps1 launch_desktop.ps1
echo.
echo     如果 git status 显示还有别的冲突文件，把它们一起加进上面第二条命令。
echo.
pause
exit /b 1
:scripts_ok

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo.
  echo [!] 还没有安装运行环境。
  echo     请先双击本文件夹里的 安装.cmd ，装完再启动。
  echo.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_desktop.ps1" -Port %WL_PORT%
if errorlevel 1 (
  echo.
  echo [!] 启动失败。可以双击 停止.cmd 清理后重试，
  echo     或查看 logs\wallet-link.stderr.log 里的错误信息。
  echo.
  pause
)
