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
    $sharedPython = if ($env:BITTENSOR_PYTHON) {
        $env:BITTENSOR_PYTHON
    } else {
        'G:\jiaoben\.venv\Scripts\python.exe'
    }
    if (Test-Path -LiteralPath $sharedPython) {
        $Python = $sharedPython
    } else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $pythonCommand) {
            throw "Python environment not found. Create .venv with: python -m venv .venv"
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
throw "Service did not become healthy. Check $ErrLog"
