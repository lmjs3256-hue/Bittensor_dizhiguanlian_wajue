param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('remote', 'tunnel')]
    [string]$Mode,

    [int]$Port = 8766
)

# 由 app/remote_launch.py 以独立进程拉起。开头先睡几秒，等触发它的那次
# HTTP 响应发完，再停服务；否则页面会看到请求被硬生生掐断。
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $Root 'logs'
$StateFile = Join-Path $LogDir 'remote-launch.json'
$UrlFile = Join-Path $LogDir 'cloudflared.url'
$CfLog = Join-Path $LogDir 'cloudflared.log'
$CfOut = Join-Path $LogDir 'cloudflared.stdout.log'
$Cloudflared = Join-Path $Root 'tools\cloudflared.exe'
$StopScript = Join-Path $Root 'stop.ps1'
$StartScript = Join-Path $Root 'start.ps1'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

function Set-LaunchState {
    param([string]$State, [string]$Message, [string]$Url = '')

    $started = ''
    if (Test-Path -LiteralPath $StateFile) {
        try {
            $old = Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $started = [string]$old.started_at
        } catch { $started = '' }
    }
    $finished = ''
    if ($State -ne 'launching') {
        $finished = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    $payload = [ordered]@{
        state       = $State
        mode        = $Mode
        message     = $Message
        url         = $Url
        started_at  = $started
        finished_at = $finished
        updated_at  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    # 用无 BOM UTF-8：PS 5.1 的 Set-Content -Encoding UTF8 会带 BOM，
    # Python 那侧 json.loads 会把 \ufeff 当成内容而解析失败。
    [System.IO.File]::WriteAllText(
        $StateFile,
        ($payload | ConvertTo-Json -Compress),
        $Utf8NoBom
    )
}

function Invoke-Step {
    param([string]$Script, [string[]]$Arguments)

    $p = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList (@('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Script) + $Arguments) `
        -WorkingDirectory $Root -NoNewWindow -Wait -PassThru
    return $p.ExitCode
}

function Wait-TunnelUrl {
    param($Process, [int]$TimeoutSeconds = 60)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $CfLog) {
            $text = Get-Content -LiteralPath $CfLog -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
            if ($text) {
                $m = [regex]::Match($text, 'https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com')
                if ($m.Success) { return $m.Value }
            }
        }
        if ($Process.HasExited) { return '' }
        Start-Sleep -Milliseconds 400
    }
    return ''
}

Start-Sleep -Seconds 3

try {
    Set-LaunchState -State 'launching' -Message '正在停止当前服务…'

    # 1. 停掉当前实例，腾出端口。stop.ps1 只处理本端口上的 python 进程。
    $null = Invoke-Step -Script $StopScript -Arguments @('-Port', "$Port")
    Start-Sleep -Seconds 1

    # 2. 公网模式：先建隧道拿网址、写文件，再启服务。
    #    顺序很重要——run.py 启动时会读 logs/cloudflared.url 打印公网入口，
    #    老版本先启服务、后建隧道，服务永远只能看到上一次的旧网址。
    $url = ''
    if ($Mode -eq 'tunnel') {
        if (-not (Test-Path -LiteralPath $Cloudflared)) {
            throw '找不到 tools\cloudflared.exe，请先双击一次「启动-公网隧道.cmd」下载。'
        }
        Remove-Item -LiteralPath $UrlFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $CfLog -Force -ErrorAction SilentlyContinue
        Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1

        Set-LaunchState -State 'launching' -Message '正在建立公网隧道、等待分配网址…'
        $cf = Start-Process -FilePath $Cloudflared `
            -ArgumentList @('tunnel', '--url', "http://127.0.0.1:$Port", '--no-autoupdate') `
            -WorkingDirectory $Root -WindowStyle Hidden `
            -RedirectStandardOutput $CfOut -RedirectStandardError $CfLog -PassThru
        $url = Wait-TunnelUrl -Process $cf
        if (-not $url) {
            throw "隧道没有在 60 秒内分配到网址，详见 $CfLog（常见于出站 UDP 7844 被拦）。"
        }
        [System.IO.File]::WriteAllText($UrlFile, $url, $Utf8NoBom)
    }

    # 3. 按目标模式把服务拉起来。start.ps1 内部会等 health 就绪。
    Set-LaunchState -State 'launching' -Message '正在按新模式启动服务…'
    $modeArgs = if ($Mode -eq 'tunnel') { @('-Tunnel') } else { @('-Remote') }
    $code = Invoke-Step -Script $StartScript -Arguments (@('-Port', "$Port") + $modeArgs)
    if ($code -ne 0) {
        throw "服务启动失败（退出码 $code），详见 logs\wallet-link.stderr.log。"
    }

    # 4. 再确认一次健康状态，避免把「脚本没报错但端口没起来」当成成功。
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 8
    if ($health.service -ne 'bittensor-wallet-link-intel') { throw '服务标识不匹配' }

    $done = if ($Mode -eq 'tunnel') { "公网隧道已就绪：$url" } else { '远程访问已开启' }
    Set-LaunchState -State 'ready' -Message $done -Url $url
} catch {
    $tip = "$($_.Exception.Message) 如未能自动恢复，请双击对应的「启动-远程访问.cmd」或「启动-公网隧道.cmd」。"
    Set-LaunchState -State 'error' -Message $tip
}
