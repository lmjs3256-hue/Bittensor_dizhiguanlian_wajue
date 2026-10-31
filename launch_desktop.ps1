param(
    [int]$Port = 8766
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

& (Join-Path $Root "start.ps1") -Port $Port
Start-Process "http://127.0.0.1:$Port"
