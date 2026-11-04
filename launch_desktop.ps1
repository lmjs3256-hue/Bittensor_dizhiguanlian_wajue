param(
    [int]$Port = 8766
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'

function Open-InBrowser {
    param([string]$Url)

    # 1. 交给系统默认程序打开（正常情况下走这里）
    try {
        Start-Process -FilePath $Url -ErrorAction Stop
        return $true
    } catch {
        # 默认浏览器关联坏了（精简系统、卸载过浏览器、被安全软件拦）时会失败，继续兜底
    }

    # 2. 从注册表读出默认浏览器的 exe，直接带参数启动
    try {
        $progId = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice' -ErrorAction Stop).ProgId
        $command = (Get-ItemProperty "Registry::HKEY_CLASSES_ROOT\$progId\shell\open\command" -ErrorAction Stop).'(default)'
        if ($command -match '"([^"]+\.exe)"') {
            $browser = $matches[1]
            if (Test-Path -LiteralPath $browser) {
                Start-Process -FilePath $browser -ArgumentList $Url -ErrorAction Stop
                return $true
            }
        }
    } catch {
        # 用户从未设置过默认浏览器时也没有 UserChoice，继续兜底
    }

    # 3. 最后按常见安装路径找一遍
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
    )
    foreach ($item in $candidates) {
        if ($item -and (Test-Path -LiteralPath $item)) {
            try {
                Start-Process -FilePath $item -ArgumentList $Url -ErrorAction Stop
                return $true
            } catch {
                continue
            }
        }
    }
    return $false
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host ""
    Write-Host "还没有安装运行环境。" -ForegroundColor Yellow
    Write-Host "请先双击本文件夹里的 安装.cmd ，装完再启动。" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

$startError = $null
try {
    & (Join-Path $Root "start.ps1") -Port $Port
} catch {
    # start.ps1 可能只是等超时了（首次启动被杀软拖慢），进程其实已经在跑，
    # 所以先记下错误，接着再探一次页面，能探到就照常打开浏览器。
    $startError = $_.Exception.Message
}

# 同一台电脑上有多份副本时，start.ps1 会自动换用空闲端口，实际端口写在这个文件里。
$PortFile = Join-Path $Root 'logs\wallet-link.port'
if (Test-Path -LiteralPath $PortFile) {
    $raw = (Get-Content -LiteralPath $PortFile -Raw -ErrorAction SilentlyContinue)
    $actual = 0
    if ($null -ne $raw -and [int]::TryParse($raw.Trim(), [ref]$actual) -and $actual -gt 0) {
        $Port = $actual
    }
}

$url = "http://127.0.0.1:$Port"

# 打开前先确认页面真的能访问，避免打开一个“无法连接”的标签页。
# 最多等 20 秒：start.ps1 已经在里面等过一轮，这里只是给“还在初始化”留点余量。
$healthy = $false
$waitUntil = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $waitUntil) {
    try {
        $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 2
        if ($health.service -eq 'bittensor-wallet-link-intel') { $healthy = $true; break }
    } catch {
        Start-Sleep -Milliseconds 800
    }
}

if (-not $healthy) {
    Write-Host ""
    if ($startError) {
        Write-Host ("启动失败：" + $startError) -ForegroundColor Red
    } else {
        Write-Host "服务已启动，但页面暂时没有响应。" -ForegroundColor Yellow
    }
    $errLog = Join-Path $Root 'logs\wallet-link.stderr.log'
    if (Test-Path -LiteralPath $errLog) {
        Write-Host "---- logs\wallet-link.stderr.log 最后几行 ----" -ForegroundColor Yellow
        Get-Content -LiteralPath $errLog -Tail 12
    }
    Write-Host ""
    Write-Host "可以稍等几秒后手动打开：$url" -ForegroundColor Cyan
    Write-Host ""
    exit 1
}

if (Open-InBrowser -Url $url) {
    Write-Host ""
    Write-Host "已在浏览器打开：$url" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "服务已在运行，但没能自动打开浏览器（可能是没有设置默认浏览器）。" -ForegroundColor Yellow
    Write-Host "请复制下面的地址到浏览器打开：" -ForegroundColor Yellow
    Write-Host "  $url" -ForegroundColor Cyan
}

Write-Host "（这个窗口 5 秒后自动关闭，服务继续在后台运行）"
Start-Sleep -Seconds 5
