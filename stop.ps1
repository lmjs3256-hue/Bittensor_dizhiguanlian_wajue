$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidFile = Join-Path $Root 'logs\wallet-link.pid'
if (-not (Test-Path -LiteralPath $PidFile)) { throw 'No wallet-link PID file found.' }
$ProcessId = [int](Get-Content -LiteralPath $PidFile -Raw).Trim()
$process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
if ($process) {
    Stop-Process -Id $ProcessId -Force
    "Stopped process $ProcessId."
} else {
    'Process is already stopped.'
}
Remove-Item -LiteralPath $PidFile -Force
