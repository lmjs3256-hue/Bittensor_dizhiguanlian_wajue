param(
    [int]$Port = 8766,
    # 甲方案：让其他设备（家里 / 公司 / 手机）通过 Tailscale 或局域网访问。
    # 会绑定 0.0.0.0 并要求访问口令，口令由 run.py 打印到 logs\wallet-link.stderr.log。
    [switch]$Remote
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

function Show-RemotePanel {
    # run.py 把「访问口令 + 各设备该用的地址」写进了 stderr 日志（本脚本重定向了输出），
    # 这里回显到当前窗口——不然用户为了一个口令还得去翻日志文件。
    $lines = @()
    if (Test-Path -LiteralPath $ErrLog) {
        # run.py 在 PYTHONUTF8=1 下写的是 UTF-8；PowerShell 5.1 的 Get-Content 默认按
        # 系统 ANSI（简中 GBK）读，不指定编码会把中文读成乱码。
        $lines = @(Get-Content -LiteralPath $ErrLog -Encoding UTF8 -ErrorAction SilentlyContinue |
            Select-String -Pattern '访问口令|Tailscale|局域网|其他网卡|未检测到|提醒|存在 data' |
            ForEach-Object { $_.Line })
    }
    Write-Host ""
    Write-Host "============== 远程访问已开启 ==============" -ForegroundColor Cyan
    if ($lines.Count -gt 0) {
        $lines | ForEach-Object { Write-Host $_ }
    } else {
        Write-Host "口令与访问地址见日志：$ErrLog" -ForegroundColor Yellow
    }
    Write-Host "===========================================" -ForegroundColor Cyan
    Write-Host "记下上面的访问口令。窗口关闭后仍可在这个文件里找到：" -ForegroundColor Yellow
    Write-Host "  $ErrLog" -ForegroundColor Yellow
    Write-Host ""
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
$restartNeeded = $false
try {
    $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
    if ($health.service -eq 'bittensor-wallet-link-intel') {
        $serviceHere = $true
        $runningRoot = ''
        if ($null -ne $health.root) { $runningRoot = ([string]$health.root).TrimEnd('\') }
        if ($runningRoot -ieq $Root.TrimEnd('\')) {
            # 同一个目录已经在跑：不能简单当成"已启动"直接退出。git pull 更新代码后
            # 用户双击的正是这个脚本，旧进程还在跑，新代码永远不生效——页面上的版本号
            # 读的是磁盘 VERSION 文件，于是显示新版本、功能却还是旧的，极难排查。
            # 这里比对进程版本和磁盘版本，不一致就先停掉再启动。
            $diskVersion = ''
            $versionFile = Join-Path $Root 'VERSION'
            if (Test-Path -LiteralPath $versionFile) {
                $diskVersion = (Get-Content -LiteralPath $versionFile -Raw).Trim()
            }
            $runningVersion = ''
            if ($null -ne $health.version) { $runningVersion = ([string]$health.version).Trim() }
            $runningRemote = $false
            try {
                $accessStatus = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/access/status" -TimeoutSec 2
                if ($null -ne $accessStatus.remote_mode) { $runningRemote = [bool]$accessStatus.remote_mode }
            } catch {
                # 旧版本没有这个接口，当作本地模式。
            }
            if ($diskVersion -and $runningVersion -and ($diskVersion -ne $runningVersion)) {
                Write-Host "检测到代码已更新：运行中 $runningVersion → 磁盘 $diskVersion"
                Write-Host "正在重启服务以加载新版本…"
                $restartNeeded = $true
            } elseif ($Remote -and -not $runningRemote) {
                # 之前是本地模式，这次要求远程：必须重启才能改监听地址。
                Write-Host "检测到当前以本机模式运行，正在切换为远程访问模式…"
                $restartNeeded = $true
            } elseif ((-not $Remote) -and $runningRemote) {
                Write-Host "提示：服务当前以「远程访问」模式运行（已开启访问口令）。"
                Write-Host "      如需切回只允许本机访问，请先双击 停止.cmd 再双击 启动.cmd。"
                exit 0
            } else {
                "Service is already running at http://127.0.0.1:$Port"
                exit 0
            }
        } elseif ($runningRoot) {
            $portOwner = $runningRoot
        } else {
            $portOwner = '另一个副本（旧版本，没有目录信息）'
        }
    }
} catch {
    # Not running yet.
}

if ($restartNeeded) {
    # 注意：不能 `& stop.ps1`——那个脚本里用了 exit，被调用时会把当前会话一起结束。
    # 这里就地停：先按 pid 文件（最准），再按端口兜底，只处理 python 进程。
    $stopped = $false
    if (Test-Path -LiteralPath $PidFile) {
        $rawPid = (Get-Content -LiteralPath $PidFile -Raw -ErrorAction SilentlyContinue)
        $oldPid = 0
        if ($null -ne $rawPid -and [int]::TryParse($rawPid.Trim(), [ref]$oldPid)) {
            $old = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
            if ($old -and $old.ProcessName -like 'python*') {
                Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
                $stopped = $true
            }
        }
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
    if (-not $stopped) {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($conn) {
            $owner = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($owner -and $owner.ProcessName -like 'python*') {
                Stop-Process -Id $owner.Id -Force -ErrorAction SilentlyContinue
            }
        }
    }
    # 等端口真正释放：Windows 上进程退出到端口可绑之间有短暂延迟。
    $releaseDeadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $releaseDeadline) {
        if (Test-PortFree $Port) { break }
        Start-Sleep -Milliseconds 300
    }
    $serviceHere = $false
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
# --remote 会自己把 host 改成 0.0.0.0（并打印访问口令面板），这里不再重复传 --host。
$runArgs = if ($Remote) {
    @((Join-Path $Root 'run.py'), '--remote', '--port', $Port)
} else {
    @((Join-Path $Root 'run.py'), '--host', '127.0.0.1', '--port', $Port)
}
$process = Start-Process -FilePath $Python `
    -ArgumentList $runArgs `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru
Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ascii
# 端口一启动就记下来：即使等待超时，启动器也知道页面在哪个端口，不会打开错的地址。
Set-Content -LiteralPath $PortFile -Value $Port -Encoding ascii

# 首次启动时 Windows Defender 会扫描 python.exe 和一堆依赖，30 秒经常不够，
# 所以这里放宽到 90 秒，并每 10 秒提示一次，别让人以为卡死了。
$timeout = 90
$deadline = (Get-Date).AddSeconds($timeout)
$nextNotice = 10
while ((Get-Date) -lt $deadline) {
    try {
        $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 2
        if ($health.service -eq 'bittensor-wallet-link-intel') {
            $readyRoot = ''
            if ($null -ne $health.root) { $readyRoot = ([string]$health.root).TrimEnd('\') }
            if ($readyRoot -eq '' -or $readyRoot -ieq $Root.TrimEnd('\')) {
                Write-Host "Ready: http://127.0.0.1:$Port"
                if ($Remote) { Show-RemotePanel }
                exit 0
            }
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }

    $elapsed = [int]($timeout - ($deadline - (Get-Date)).TotalSeconds)
    if ($elapsed -ge $nextNotice) {
        Write-Host "还在启动…已等 $elapsed 秒（首次启动要初始化，最长等 $timeout 秒）"
        $nextNotice += 10
    }
}
throw "服务没有在 $timeout 秒内就绪。请检查是否已有其他程序占用端口 $Port，或查看日志：$ErrLog"
