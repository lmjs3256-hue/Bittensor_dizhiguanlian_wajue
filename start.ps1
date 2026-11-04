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
$PortFile = Join-Path $LogDir 'wallet-link.port'
$HealthUrl = "http://127.0.0.1:$Port/api/health"

function Test-PortFree {
    param([int]$Candidate)
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Candidate)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $listener) { $listener.Stop() }
    }
}

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

$portOwner = $null
$serviceHere = $false
try {
    $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
    if ($health.service -eq 'bittensor-wallet-link-intel') {
        $serviceHere = $true
        $runningRoot = ''
        if ($null -ne $health.root) { $runningRoot = ([string]$health.root).TrimEnd('\') }
        if ($runningRoot -ieq $Root.TrimEnd('\')) {
            "Service is already running at http://127.0.0.1:$Port"
            exit 0
        }
        if ($runningRoot) { $portOwner = $runningRoot } else { $portOwner = '另一个副本（旧版本，没有目录信息）' }
    }
} catch {
    # Not running yet.
}

if (-not $serviceHere -and -not (Test-PortFree $Port)) {
    $portOwner = '其他程序'
}

if ($portOwner) {
    # 同一台电脑复制了第二份（比如拷到 D 盘做测试）时，端口会被原副本占着。
    # 与其报错让人去查，不如自动换一个空闲端口启动，把实际端口写进 logs\wallet-link.port，
    # 由 launch_desktop.ps1 据此打开正确的页面。
    $fallback = 0
    foreach ($candidate in (($Port + 1)..($Port + 10))) {
        if (Test-PortFree $candidate) { $fallback = $candidate; break }
    }
    if (-not $fallback) {
        throw ("端口 $Port 已被 $portOwner 占用，$($Port + 1)-$($Port + 10) 也都被占用。" +
            "请先双击各自文件夹里的 停止.cmd 关掉不用的副本，再启动本目录。")
    }
    Write-Host "提示：端口 $Port 已被 $portOwner 占用。"
    Write-Host "      本副本改用端口 $fallback 启动（数据独立，不影响原副本）。"
    $Port = $fallback
    $HealthUrl = "http://127.0.0.1:$Port/api/health"
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
            $readyRoot = ''
            if ($null -ne $health.root) { $readyRoot = ([string]$health.root).TrimEnd('\') }
            if ($readyRoot -eq '' -or $readyRoot -ieq $Root.TrimEnd('\')) {
                Set-Content -LiteralPath $PortFile -Value $Port -Encoding ascii
                "Ready: http://127.0.0.1:$Port"
                exit 0
            }
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
throw "服务没有在 30 秒内就绪。请检查是否已有其他程序占用端口 $Port，或查看日志：$ErrLog"
