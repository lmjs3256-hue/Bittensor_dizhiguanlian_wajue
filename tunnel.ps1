param(
    [int]$Port = 8766,
    # 可选：只允许这些邮箱进入（Cloudflare 一次性验证码门禁）。
    # 留空则任何拿到网址的人都能看到口令输入框。
    [string]$AllowedMail = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $Root 'logs'
$ToolsDir = Join-Path $Root 'tools'
$Cloudflared = Join-Path $ToolsDir 'cloudflared.exe'
$CfLog = Join-Path $LogDir 'cloudflared.log'
$CfOut = Join-Path $LogDir 'cloudflared.stdout.log'
$ErrLog = Join-Path $LogDir 'wallet-link.stderr.log'
$UrlFile = Join-Path $LogDir 'cloudflared.url'
$HealthUrl = "http://127.0.0.1:$Port/api/health"
$DownloadUrl = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

# PowerShell 5.1 在存在仅大小写不同的环境变量（HTTP_PROXY / http_proxy）时
# 连 Get-ChildItem Env: 都会抛异常，Start-Process 也会失败。只在这种情况下去掉小写副本。
try {
    $null = Get-ChildItem Env: -ErrorAction Stop
} catch {
    foreach ($name in @('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')) {
        [System.Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $Root '.venv\Scripts\python.exe'))) {
    Write-Host ""
    Write-Host "[!] 你还没有安装运行环境。请先双击 安装.cmd，完成后再运行本脚本。" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "按回车键关闭"
    exit 1
}

# -- 1. 准备 cloudflared -----------------------------------------------------
if (-not (Test-Path -LiteralPath $Cloudflared)) {
    Write-Host "首次使用：正在下载隧道程序 cloudflared（约 30MB，只需一次）…" -ForegroundColor Cyan
    Write-Host "  来源：$DownloadUrl"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $partial = "$Cloudflared.part"
    try {
        Invoke-WebRequest -Uri $DownloadUrl -OutFile $partial -UseBasicParsing
    } catch {
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        Write-Host ""
        Write-Host "[!] 下载失败：$($_.Exception.Message)" -ForegroundColor Red
        Write-Host "    可以手动下载后放到：$Cloudflared" -ForegroundColor Yellow
        Write-Host "    $DownloadUrl" -ForegroundColor Yellow
        Write-Host ""
        Read-Host "按回车键关闭"
        exit 1
    }
    Move-Item -LiteralPath $partial -Destination $Cloudflared -Force
}

$cfVersion = (& $Cloudflared --version 2>&1 | Select-Object -First 1)
Write-Host "隧道程序：$cfVersion"

# -- 2. 建立隧道 -------------------------------------------------------------
# 顺序不能反：run.py 启动时会读 logs/cloudflared.url 打印公网入口，
# 先启服务、后建隧道的话，服务只能看到上一次留下的旧网址（或压根没有）。
# 所以这里先清掉旧网址和旧隧道进程，拿到新网址并落盘，最后才启动服务。
Remove-Item -LiteralPath $UrlFile -Force -ErrorAction SilentlyContinue
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
function Start-Tunnel {
    param([string[]]$Extra)
    $arguments = @('tunnel', '--url', "http://127.0.0.1:$Port", '--no-autoupdate') + $Extra
    Remove-Item -LiteralPath $CfLog -Force -ErrorAction SilentlyContinue
    $process = Start-Process -FilePath $Cloudflared `
        -ArgumentList $arguments `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $CfOut `
        -RedirectStandardError $CfLog `
        -PassThru
    return $process
}

function Wait-TunnelUrl {
    param($Process, [int]$TimeoutSeconds = 45)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $CfLog) {
            $text = Get-Content -LiteralPath $CfLog -Raw -ErrorAction SilentlyContinue
            if ($text) {
                $match = [regex]::Match($text, 'https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com')
                if ($match.Success) { return $match.Value }
            }
        }
        if ($Process.HasExited) { return $null }
        Start-Sleep -Milliseconds 400
    }
    return $null
}

$cfArgs = @()
$mailGate = $false
if ($AllowedMail) {
    $cfArgs = @('--allowed-mail', $AllowedMail)
}
Write-Host "正在建立隧道…"
$cf = Start-Tunnel -Extra $cfArgs
$url = Wait-TunnelUrl -Process $cf

# 老版本 cloudflared 不认识 --allowed-mail，会直接退出。识别到就退回无邮箱门禁，
# 而不是让用户对着一个空窗口猜哪里错了。
if (-not $url -and $cf.HasExited -and $AllowedMail) {
    $message = ''
    if (Test-Path -LiteralPath $CfLog) {
        $message = (Get-Content -LiteralPath $CfLog -Raw -ErrorAction SilentlyContinue)
    }
    if ($message -match 'allowed-mail|flag provided but not defined|unknown flag') {
        Write-Host "[!] 当前 cloudflared 版本不支持邮箱门禁，已退回仅口令保护。" -ForegroundColor Yellow
        Write-Host "    想开启邮箱门禁：删除 tools\cloudflared.exe 后重新运行本脚本（会下载新版）。" -ForegroundColor Yellow
        $cfArgs = @()
        $cf = Start-Tunnel -Extra $cfArgs
        $url = Wait-TunnelUrl -Process $cf
    }
} elseif ($url -and $AllowedMail) {
    $mailGate = $true
}

if (-not $url) {
    Write-Host ""
    Write-Host "[!] 隧道没有建立成功。" -ForegroundColor Red
    if (Test-Path -LiteralPath $CfLog) {
        Write-Host "    最后几行日志：" -ForegroundColor Yellow
        Get-Content -LiteralPath $CfLog -Encoding UTF8 -Tail 12 | ForEach-Object { Write-Host "      $_" }
    }
    Write-Host "    常见原因：网络被代理/防火墙拦住出站 UDP 7844，或公司网络限制。"
    if (-not $cf.HasExited) { Stop-Process -Id $cf.Id -Force -ErrorAction SilentlyContinue }
    Read-Host "按回车键关闭"
    exit 1
}

Set-Content -LiteralPath $UrlFile -Value $url -Encoding ascii

# -- 3. 启动服务（隧道模式） -------------------------------------------------
# 网址已经落盘，run.py 启动时就能把公网入口写进 stderr 日志和启动面板。
Write-Host "正在启动服务（公网隧道模式）…"
# 必须开子进程调用：start.ps1 里有多处 exit，直接 & 调用会把本窗口一起结束。
$startScript = Join-Path $Root 'start.ps1'
$launcher = Start-Process -FilePath 'powershell.exe' `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $startScript, '-Port', $Port, '-Tunnel') `
    -WorkingDirectory $Root -NoNewWindow -Wait -PassThru
if ($launcher.ExitCode -ne 0) {
    Write-Host "[!] 服务启动失败（退出码 $($launcher.ExitCode)），详见 $ErrLog" -ForegroundColor Red
    Read-Host "按回车键关闭"
    exit 1
}

try {
    $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5
    if ($health.service -ne 'bittensor-wallet-link-intel') { throw '服务标识不匹配' }
} catch {
    Write-Host "[!] 服务没有就绪，详见 $ErrLog" -ForegroundColor Red
    Read-Host "按回车键关闭"
    exit 1
}

# 口令从服务日志里回显，免得为了一个口令去翻文件。
$token = ''
$hintLine = @()
if (Test-Path -LiteralPath $ErrLog) {
    $hintLine = @(Get-Content -LiteralPath $ErrLog -Encoding UTF8 -ErrorAction SilentlyContinue |
        Select-String -Pattern '访问口令|本机免验证|公网入口' | ForEach-Object { $_.Line })
    $matchToken = [regex]::Match(($hintLine -join "`n"), '访问口令\s+([A-Z0-9-]{8,})')
    if ($matchToken.Success) { $token = $matchToken.Groups[1].Value }
}

Write-Host ""
Write-Host "============ 公网隧道已就绪 ============" -ForegroundColor Green
Write-Host " 手机 / 外网电脑打开：" -ForegroundColor Cyan
Write-Host "   $url" -ForegroundColor White
Write-Host ""
if ($token) {
    Write-Host " 访问口令：$token" -ForegroundColor Cyan
    Write-Host "   （首次打开网页会要求输入；之后这个浏览器记住 90 天）"
}
if ($mailGate) {
    Write-Host " 邮箱门禁：已开启，只有 $AllowedMail 能收到验证码进入" -ForegroundColor Green
} else {
    Write-Host " 邮箱门禁：未开启（拿到网址的人能看到口令输入框）" -ForegroundColor Yellow
    Write-Host "   想加一道邮箱验证码：启动-公网隧道.cmd 你的邮箱@example.com"
}
Write-Host ""
Write-Host " 本机访问：http://127.0.0.1:$Port" -ForegroundColor DarkGray
Write-Host "--------------------------------------"
Write-Host " 注意 1  这是临时地址，关闭本窗口即失效，下次启动会换新网址。"
Write-Host " 注意 2  网址 + 口令 = 你的数据。别随手转发这个网址。"
Write-Host " 注意 3  别在别的电脑上再启动一份服务：同一把 API key 会互相抢额度（429）。"
Write-Host " 注意 4  保持本窗口开着。要停止：关闭本窗口，或双击 停止.cmd。"
Write-Host "======================================"
Write-Host ""

# -- 4. 守护：隧道断了就提示，窗口异常退出时清理 ------------------------------
try {
    while (-not $cf.HasExited) {
        Start-Sleep -Seconds 5
    }
    Write-Host "[!] 隧道进程已退出，公网地址已失效。" -ForegroundColor Yellow
    Write-Host "    本机 http://127.0.0.1:$Port 仍可访问；重新双击 启动-公网隧道.cmd 可换新地址。" -ForegroundColor Yellow
} finally {
    if (-not $cf.HasExited) {
        Stop-Process -Id $cf.Id -Force -ErrorAction SilentlyContinue
    }
}
