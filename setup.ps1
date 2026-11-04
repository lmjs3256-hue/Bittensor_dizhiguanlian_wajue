param(
    [switch]$SkipShortcut,
    [switch]$UseMirror
)

# 一键安装脚本：检测 Python、创建虚拟环境、安装依赖、建目录、建桌面快捷方式。
# 设计目标是“复制整个文件夹到另一台 Windows 电脑，双击 安装.cmd 就能用”。
# 因此不依赖任何本机绝对路径，并且会检测失效的 .venv 后自动重建。

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# 让中文提示在 Windows PowerShell 5.1 控制台里也能正常显示
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$VenvDir = Join-Path $Root '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$Requirements = Join-Path $Root 'requirements.txt'
$DataDir = Join-Path $Root 'data'
$LogDir = Join-Path $Root 'logs'
$PythonDownload = 'https://www.python.org/downloads/windows/'
$MirrorIndex = 'https://pypi.tuna.tsinghua.edu.cn/simple'

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "== $Text" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host "   [OK] $Text" -ForegroundColor Green
}

function Write-Warn([string]$Text) {
    Write-Host "   [!] $Text" -ForegroundColor Yellow
}

function Test-Python([string]$Exe) {
    # 可用的 Python 返回可执行文件路径，否则返回 $null。
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $null }
    if ($Exe -like '*WindowsApps*') { return $null }  # 应用商店占位符，执行会弹商店
    try {
        $check = & $Exe -c "import sys;print('OK' if (3,10)<=sys.version_info[:2]<(3,14) else 'BAD')" 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0) { return $null }
    if ("$check".Trim() -ne 'OK') { return $null }
    return $Exe
}

function Get-PythonCandidates {
    # 普通用户的 Python 常常装好了但不在 PATH 里，所以必须扫常见安装目录。
    $found = New-Object System.Collections.Generic.List[string]

    if (Test-Path -LiteralPath $VenvPython) { $found.Add($VenvPython) }

    $versionDirs = @('Python312', 'Python311', 'Python310', 'Python313')
    $bases = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)},
        'C:\'
    )
    foreach ($versionDir in $versionDirs) {
        foreach ($base in $bases) {
            if (-not $base) { continue }
            $candidate = Join-Path $base "$versionDir\python.exe"
            if (Test-Path -LiteralPath $candidate) { $found.Add($candidate) }
        }
    }

    # 兜底：扫任何 Python3x 目录，兼容未列出的版本号
    foreach ($base in @((Join-Path $env:LOCALAPPDATA 'Programs\Python'), $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $base -or -not (Test-Path -LiteralPath $base)) { continue }
        Get-ChildItem -LiteralPath $base -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
            ForEach-Object {
                $exe = Join-Path $_.FullName 'python.exe'
                if (Test-Path -LiteralPath $exe) { $found.Add($exe) }
            }
    }

    # py 启动器（部分安装方式不把它放进 PATH）
    foreach ($launcher in @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Launcher\py.exe'),
        (Join-Path $env:WINDIR 'py.exe')
    )) {
        if (-not (Test-Path -LiteralPath $launcher)) { continue }
        foreach ($version in @('-3.12', '-3.11', '-3.10', '-3.13', '-3')) {
            try { $resolved = & $launcher $version -c "import sys;print(sys.executable)" 2>$null } catch { continue }
            if ($LASTEXITCODE -eq 0 -and $resolved) { $found.Add("$resolved".Trim()) }
        }
    }

    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { $found.Add($cmd.Source) }
    }

    return $found
}

function Find-Python {
    foreach ($candidate in (Get-PythonCandidates)) {
        $usable = Test-Python $candidate
        if ($usable) {
            $source = if ($usable -eq $VenvPython) { 'venv' } else { Split-Path -Parent $usable }
            return @{ Path = $usable; Source = $source }
        }
    }
    return $null
}

function Install-PythonFromBundle {
    # 优先用文件夹里自带的安装包，整个过程不需要联网。
    $bundleDir = Join-Path $Root 'installers'
    if (-not (Test-Path -LiteralPath $bundleDir)) { return $false }
    $installer = Get-ChildItem -LiteralPath $bundleDir -Filter 'python-3.*-amd64.exe' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $installer) { return $false }
    Write-Warn "使用文件夹内的安装包安装 Python（$($installer.Name)），约 1 分钟，期间请不要关闭窗口…"
    $arguments = @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1',
        'Include_test=0', 'Include_pip=1', 'Include_launcher=1'
    )
    try {
        $process = Start-Process -FilePath $installer.FullName -ArgumentList $arguments -Wait -PassThru
    } catch {
        return $false
    }
    if ($process.ExitCode -ne 0) { return $false }
    foreach ($candidate in (Get-PythonCandidates)) {
        if (Test-Python $candidate) { return $true }
    }
    return $false
}

function Install-Python {
    if (Install-PythonFromBundle) { return $true }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { return $false }
    Write-Warn "没有检测到 Python，尝试用 winget 自动安装 Python 3.12（约 1-2 分钟）…"
    try {
        & $winget.Source install --id Python.Python.3.12 --exact --source winget --silent `
            --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
    } catch {
        return $false
    }
    # 安装完成后直接重新扫目录，不依赖当前会话的 PATH
    foreach ($candidate in (Get-PythonCandidates)) {
        if (Test-Python $candidate) { return $true }
    }
    return $false
}

Write-Host ""
Write-Host "Bittensor 地址关联挖掘 · 安装向导" -ForegroundColor White
Write-Host "安装目录：$Root"

# ---------------------------------------------------------------- 1. Python
Write-Step "第 1 步 / 4：检查 Python"
$python = Find-Python
if (-not $python) {
    if ((Install-Python) -and ($python = Find-Python)) {
        Write-Ok "已通过 winget 安装 Python"
    } else {
        $bundleDir = Join-Path $Root 'installers'
        $bundleInstaller = if (Test-Path -LiteralPath $bundleDir) {
            Get-ChildItem -LiteralPath $bundleDir -Filter 'python-3.*-amd64.exe' -ErrorAction SilentlyContinue |
                Select-Object -First 1
        }
        Write-Warn "没有找到可用的 Python（需要 3.10 - 3.13），自动安装也没有成功。"
        Write-Host ""
        Write-Host "请手动安装后重新双击 安装.cmd：" -ForegroundColor Yellow
        if ($bundleInstaller) {
            Write-Host "  1) 双击文件夹里的 installers\$($bundleInstaller.Name)"
            Write-Host "  2) 安装时勾选 “Add python.exe to PATH”，点 Install Now"
        } else {
            Write-Host "  1) 打开 $PythonDownload"
            Write-Host "  2) 下载 Python 3.12 的 Windows installer (64-bit)"
            Write-Host "  3) 安装时勾选 “Add python.exe to PATH”"
        }
        Write-Host "  3) 装完重新双击 安装.cmd"
        exit 1
    }
}
$pyVersion = & $python.Path -c "import sys;print('.'.join(map(str,sys.version_info[:3])))"
Write-Ok "Python $pyVersion（$($python.Source)）"

# ---------------------------------------------------------------- 2. 虚拟环境
Write-Step "第 2 步 / 4：准备独立运行环境（.venv）"
if (Test-Path -LiteralPath $VenvDir) {
    if ((Test-Path -LiteralPath $VenvPython) -and (Test-Python $VenvPython)) {
        Write-Ok "已有可用的 .venv，直接复用"
    } else {
        Write-Warn "检测到 .venv 失效（常见于从别的电脑复制过来），正在重建…"
        Remove-Item -LiteralPath $VenvDir -Recurse -Force
    }
}
if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $python.Path -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        Write-Warn "创建 .venv 失败。请确认 Python 安装完整（包含 venv 组件）。"
        exit 1
    }
    Write-Ok "已创建 .venv"
}

# ---------------------------------------------------------------- 3. 依赖
Write-Step "第 3 步 / 4：安装依赖（首次约 2-5 分钟，请勿关闭窗口）"
& $VenvPython -m pip install --upgrade pip --disable-pip-version-check --quiet
if ($LASTEXITCODE -ne 0) { Write-Warn "升级 pip 失败，继续尝试安装依赖" }

$installArgs = @('-m', 'pip', 'install', '-r', $Requirements, '--disable-pip-version-check')
if ($UseMirror) { $installArgs += @('-i', $MirrorIndex) }

# 优先用文件夹内的离线依赖包；版本不匹配或缺失时才联网。
$WheelDir = Join-Path $Root 'installers\wheels'
$installed = $false
if ((Test-Path -LiteralPath $WheelDir) -and -not $UseMirror) {
    Write-Host "   发现离线依赖包，优先从 installers\wheels 安装（无需联网）…"
    & $VenvPython -m pip install --no-index --find-links $WheelDir -r $Requirements --disable-pip-version-check
    if ($LASTEXITCODE -eq 0) {
        $installed = $true
    } else {
        Write-Warn "本地依赖包与当前 Python 版本不匹配，改为联网安装…"
    }
}

if (-not $installed) {
    Write-Host "   正在下载依赖，下面会出现较多 pip 日志，属正常现象；结束时以 [OK] 为准。"
    Write-Host ""
    & $VenvPython @installArgs
    if ($LASTEXITCODE -ne 0 -and -not $UseMirror) {
        Write-Warn "官方源安装失败，改用国内镜像重试…"
        & $VenvPython -m pip install -r $Requirements --disable-pip-version-check -i $MirrorIndex
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "依赖安装失败。请检查网络（或公司代理）后重新双击 安装.cmd。"
        exit 1
    }
}
$verify = & $VenvPython -c "import fastapi, uvicorn, sqlalchemy, httpx, async_substrate_interface; print('OK')" 2>&1
if ("$verify".Trim() -ne 'OK') {
    Write-Warn "依赖自检失败：$verify"
    exit 1
}
Write-Ok "依赖安装完成并通过自检"

# ---------------------------------------------------------------- 4. 目录与快捷方式
Write-Step "第 4 步 / 4：创建数据目录与桌面快捷方式"
foreach ($dir in @($DataDir, $LogDir)) {
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}
Write-Ok "数据目录：data\（数据库）、logs\（运行日志）"

if (-not $SkipShortcut) {
    try {
        & (Join-Path $Root 'create_desktop_shortcut.ps1') | Out-Host
    } catch {
        Write-Warn "桌面快捷方式创建失败（不影响使用，可改用 启动.cmd）：$($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
Write-Host ""
Write-Host "下次启动：" -ForegroundColor White
Write-Host "  · 双击桌面的 “Bittensor 地址关联挖掘” 快捷方式，或"
Write-Host "  · 双击本文件夹里的 启动.cmd"
Write-Host ""
Write-Host "浏览器会自动打开 http://127.0.0.1:8766"
Write-Host "详细操作见 使用说明.md"
Write-Host ""
