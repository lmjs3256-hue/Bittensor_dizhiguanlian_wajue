param(
    [int]$Port = 8766
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host ""
    Write-Host "还没有安装运行环境。" -ForegroundColor Yellow
    Write-Host "请先双击本文件夹里的 安装.cmd ，装完再启动。" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

& (Join-Path $Root "start.ps1") -Port $Port
Start-Process "http://127.0.0.1:$Port"
