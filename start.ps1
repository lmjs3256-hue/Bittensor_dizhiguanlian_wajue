param(
    [int]$Port = 8766
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'
$OutLog = Join-Path $LogDir 'wallet-link.stdout.log'
$ErrLog = Join-Path $LogDir 'wallet-link.stderr.log'
$PidFile = Join-Path $LogDir 'wallet-link.pid'
$HealthUrl = "http://127.0.0.1:$Port/api/health"

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
if (-not (Test-Path -LiteralPath $Python)) {
    $sharedPython = $env:BITTENSOR_PYTHON
    if ($sharedPython -and (Test-Path -LiteralPath $sharedPython)) {
        $Python = $sharedPython
    } else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $pythonCommand) {
            throw "未找到运行环境。请先双击本文件夹里的 安装.cmd，装完再启动。"
        }
        $Python = $pythonCommand.Source
    }
}

try {
    $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
    if ($health.service -eq 'bittensor-wallet-link-intel') {
        "Service is already running at http://127.0.0.1:$Port"
        exit 0
    }
} catch {
    # Not running yet.
}

# PowerShell 5.1 cannot enumerate the environment (and Start-Process fails)
# when variable names differ only by case, e.g. HTTP_PROXY vs http_proxy.
# Only in that case drop the lowercase spellings; normal environments are untouched.
try {
    $null = Get-ChildItem Env: -ErrorAction Stop
} catch {
    foreach ($name in @('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')) {
        [System.Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
}

$env:PYTHONUTF8 = '1'
$process = Start-Process -FilePath $Python `
    -ArgumentList @((Join-Path $Root 'run.py'), '--host', '127.0.0.1', '--port', $Port) `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru
Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ascii

$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    try {
        $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
        if ($health.service -eq 'bittensor-wallet-link-intel') {
            "Ready: http://127.0.0.1:$Port"
            exit 0
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
throw "服务没有在 30 秒内就绪。请检查是否已有其他程序占用端口 $Port，或查看日志：$ErrLog"
