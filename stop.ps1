param(
    [int]$Port = 8766
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root 'logs\wallet-link.pid'

function Stop-ByPidFile {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $false }
    $raw = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    $processId = 0
    if (-not [int]::TryParse($raw, [ref]$processId)) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $false
    }
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($process -and $process.ProcessName -like 'python*') {
        Stop-Process -Id $processId -Force
        Write-Host "已停止服务（进程 $processId）。"
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return $true
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    return $false
}

function Stop-ByPort {
    # 兼容手动启动或 PID 文件丢失：只处理占用本端口的 python 进程，不碰其他程序。
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $false }
    $process = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    if ($process -and $process.ProcessName -like 'python*') {
        Stop-Process -Id $process.Id -Force
        Write-Host "已停止服务（端口 $Port 上的进程 $($process.Id)）。"
        return $true
    }
    $name = if ($process) { $process.ProcessName } else { '未知程序' }
    Write-Host "端口 $Port 被其他程序占用（$name），未做处理。请关闭该程序，或用 .\start.ps1 -Port 8777 换端口启动。"
    return $true
}

if (Stop-ByPidFile) { exit 0 }
if (Stop-ByPort) { exit 0 }
Write-Host '服务当前没有在运行。'
