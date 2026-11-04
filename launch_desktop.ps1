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

# 同一台电脑上有多份副本时，start.ps1 会自动换用空闲端口，实际端口写在这里。
$PortFile = Join-Path $Root 'logs\wallet-link.port'
if (Test-Path -LiteralPath $PortFile) {
    $raw = (Get-Content -LiteralPath $PortFile -Raw -ErrorAction SilentlyContinue)
    $actual = 0
    if ($null -ne $raw -and [int]::TryParse($raw.Trim(), [ref]$actual) -and $actual -gt 0) {
        $Port = $actual
    }
}
Start-Process "http://127.0.0.1:$Port"
