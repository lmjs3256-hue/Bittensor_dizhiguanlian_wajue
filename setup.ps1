param(
    [switch]$SkipShortcut,
    [switch]$UseMirror,
    [switch]$Force
)

# 一键安装脚本：检测 Python、创建虚拟环境、安装依赖、建目录、建桌面快捷方式。
# 设计目标是“复制整个文件夹到另一台 Windows 电脑，双击 安装.cmd 就能用”。
#
# 每一步都是幂等的：已经装好的部分会跳过，不会重复安装。
#   · Python 已装且版本合规  -> 跳过安装
#   · .venv 可用             -> 跳过创建
#   · 依赖已完整且清单没变   -> 跳过 pip install
#   · 桌面快捷方式已指向本项目 -> 跳过创建
#
# 参数：
#   -SkipShortcut  只装环境，不创建桌面快捷方式
#   -UseMirror     强制走国内镜像（忽略文件夹内的离线依赖包）
#   -Force         忽略已有环境，强制重建 .venv 并重装依赖（环境疑似损坏时用）

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# 让中文提示在 Windows PowerShell 5.1 控制台里也能正常显示
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$VenvDir = Join-Path $Root '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$StateFile = Join-Path $VenvDir '.setup-state.json'
$Requirements = Join-Path $Root 'requirements.txt'
$WheelDir = Join-Path $Root 'installers\wheels'
$DataDir = Join-Path $Root 'data'
$LogDir = Join-Path $Root 'logs'
$VersionFile = Join-Path $Root 'VERSION'
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

function Write-Skip([string]$Text) {
    Write-Host "   [--] $Text" -ForegroundColor Gray
}

# ---------------------------------------------------------------- Python 探测

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

function Get-PythonVersionText([string]$Exe) {
    # 返回 "3.12.10" 这样的版本号，取不到就返回空字符串。
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return '' }
    try {
        $text = & $Exe -c "import sys;print('.'.join(map(str,sys.version_info[:3])))" 2>$null
    } catch {
        return ''
    }
    if ($LASTEXITCODE -ne 0) { return '' }
    return "$text".Trim()
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

function Get-FirstPythonVersion {
    # 仅用于提示：机器上装了 Python 但版本不合规时，告诉用户将要额外装一个。
    foreach ($candidate in (Get-PythonCandidates)) {
        if ($candidate -like '*WindowsApps*') { continue }
        $text = Get-PythonVersionText $candidate
        if ($text) { return "$text（$candidate）" }
    }
    return ''
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
    return [bool](Find-Python)
}

function Install-Python {
    if (Install-PythonFromBundle) { return $true }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { return $false }
    Write-Warn "尝试用 winget 安装 Python 3.12（约 1-2 分钟）…"
    try {
        & $winget.Source install --id Python.Python.3.12 --exact --source winget --silent `
            --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
    } catch {
        return $false
    }
    # 安装完成后直接重新扫目录，不依赖当前会话的 PATH
    return [bool](Find-Python)
}

function Remove-VenvDirectory {
    # 刚停止服务时文件句柄可能还没释放，重试几次比直接失败可靠
    $script:LastRemoveError = ''
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction Stop
            return $true
        } catch {
            $script:LastRemoveError = $_.Exception.Message
            Start-Sleep -Milliseconds 800
        }
    }
    return $false
}

# ---------------------------------------------------------------- 依赖状态检测

function Get-RequirementsHash {    if (-not (Test-Path -LiteralPath $Requirements)) { return '' }
    try {
        return (Get-FileHash -LiteralPath $Requirements -Algorithm SHA256).Hash
    } catch {
        return ''
    }
}

function Get-VenvState {
    # 读取 .venv 里记录的安装状态，用于判断依赖清单是否变过。
    if (-not (Test-Path -LiteralPath $StateFile)) { return $null }
    try {
        return (Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Save-VenvState {
    $appVersion = ''
    if (Test-Path -LiteralPath $VersionFile) {
        try { $appVersion = (Get-Content -LiteralPath $VersionFile -Raw).Trim() } catch { }
    }
    $payload = [ordered]@{
        requirements_sha256 = (Get-RequirementsHash)
        python_version      = (Get-PythonVersionText $VenvPython)
        app_version         = $appVersion
        installed_at        = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    }
    try {
        $json = $payload | ConvertTo-Json
        [System.IO.File]::WriteAllText($StateFile, $json, (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        # 状态文件写不进去不影响使用，只是下次会多做一次检测
    }
}

function Test-Dependencies {
    # 依赖是否完整：关键模块能导入 + pip check 无冲突。
    $result = @{ Ok = $false; Detail = '未检测' }
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        $result.Detail = '还没有 .venv'
        return $result
    }

    # PowerShell 5.1 会把 native 命令的 stderr 当成错误终止，检测阶段临时放宽
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'

    # 单行脚本：避免多行字符串经 -c 传参时被解析破坏
    $code = "import importlib.util as u;" +
        "names=('fastapi','uvicorn','sqlalchemy','pydantic','httpx','async_substrate_interface','multipart','pytest');" +
        "missing=[n for n in names if u.find_spec(n) is None];" +
        "print('OK' if not missing else 'MISSING '+','.join(missing))"

    try {
        $probe = (& $VenvPython -c $code 2>&1 | Out-String).Trim()
        $probeExit = $LASTEXITCODE
        if ($probeExit -ne 0 -or $probe -notmatch '^OK') {
            $result.Detail = if ($probe) { "依赖不完整：$probe" } else { '依赖检测失败' }
            return $result
        }
        $null = & $VenvPython -m pip check 2>&1
        if ($LASTEXITCODE -ne 0) {
            $result.Detail = '依赖版本有冲突（pip check 未通过）'
            return $result
        }
    } finally {
        $ErrorActionPreference = $previous
    }

    $result.Ok = $true
    $result.Detail = '依赖完整'
    return $result
}

function Test-RequirementsSatisfied {
    # 用 pip --dry-run 判断 requirements.txt 是否已全部装好。
    # 返回 $true / $false，判断不了（老版本 pip 或不支持离线）返回 $null。
    if (-not (Test-Path -LiteralPath $WheelDir)) { return $null }
    $probeArgs = @(
        '-m', 'pip', 'install', '--no-index', '--find-links', $WheelDir,
        '-r', $Requirements, '--dry-run', '--disable-pip-version-check'
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = (& $VenvPython @probeArgs 2>&1 | Out-String)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0) {
        if ($output -match 'no such option|unknown option|unrecognized arguments') { return $null }
        return $false
    }
    if ($output -match 'Would install') { return $false }
    return $true
}

# ---------------------------------------------------------------- 开始安装

Write-Host ""
Write-Host "Bittensor 地址关联挖掘 · 安装向导" -ForegroundColor White
Write-Host "安装目录：$Root"
Write-Host "本脚本可重复运行：已经装好的部分会自动跳过，不会重复安装。" -ForegroundColor DarkGray

$installedPython = $false
$installedDeps = $false

# ---------------------------------------------------------------- 1. Python
Write-Step "第 1 步 / 4：检查 Python"
$python = Find-Python
if ($python) {
    $pyVersion = Get-PythonVersionText $python.Path
    if ($python.Source -eq 'venv') {
        Write-Skip "Python $pyVersion 已就绪（复用 .venv 内的解释器）"
    } else {
        Write-Skip "Python $pyVersion 已就绪（系统已安装：$($python.Source)）"
    }
} else {
    $other = Get-FirstPythonVersion
    if ($other) {
        Write-Warn "检测到 Python $other，但本工具需要 3.10 - 3.13，将并列安装 3.12（不影响已有版本）"
    }
    if (Install-Python) {
        $python = Find-Python
        $installedPython = $true
        if ($python) { Write-Ok "已安装 Python $(Get-PythonVersionText $python.Path)" }
    }
}

if (-not $python) {
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

# ---------------------------------------------------------------- 2. 虚拟环境
Write-Step "第 2 步 / 4：准备独立运行环境（.venv）"
if ($Force -and (Test-Path -LiteralPath $VenvDir)) {
    Write-Warn "已指定 -Force，删除现有 .venv 后重建…"
    if (-not (Remove-VenvDirectory)) {
        Write-Warn "删除 .venv 失败：$script:LastRemoveError"
        Write-Warn "通常是本工具的服务还在运行并占用了文件。请先双击 停止.cmd，再重新运行安装。"
        exit 1
    }
}

if ((Test-Path -LiteralPath $VenvPython) -and (Test-Python $VenvPython)) {
    Write-Skip "已有可用的 .venv，跳过创建"
} else {
    if (Test-Path -LiteralPath $VenvDir) {
        Write-Warn "检测到 .venv 失效（常见于从别的电脑复制过来），正在重建…"
        if (-not (Remove-VenvDirectory)) {
            Write-Warn "删除失效的 .venv 失败：$script:LastRemoveError"
            Write-Warn "通常是本工具的服务还在运行并占用了文件。请先双击 停止.cmd，再重新运行安装。"
            exit 1
        }
    }
    & $python.Path -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        Write-Warn "创建 .venv 失败。请确认 Python 安装完整（包含 venv 组件）。"
        exit 1
    }
    Write-Ok "已创建 .venv"
}

# ---------------------------------------------------------------- 3. 依赖
Write-Step "第 3 步 / 4：检查依赖"

$reqHash = Get-RequirementsHash
$state = Get-VenvState
$depCheck = Test-Dependencies
$needInstall = $false
$reasons = New-Object System.Collections.Generic.List[string]

if ($Force) {
    $needInstall = $true
    $reasons.Add('指定了 -Force')
} elseif (-not $depCheck.Ok) {
    $needInstall = $true
    $reasons.Add($depCheck.Detail)
} elseif ($state -and $state.requirements_sha256 -and $state.requirements_sha256 -ne $reqHash) {
    $needInstall = $true
    $reasons.Add('requirements.txt 有更新')
} elseif (-not $state) {
    # 没有历史状态文件（例如 0.3.1 之前装的环境），再确认一次清单里没有漏装的包
    $satisfied = Test-RequirementsSatisfied
    if ($satisfied -eq $false) {
        $needInstall = $true
        $reasons.Add('依赖清单中有尚未安装的包')
    }
}

if (-not $needInstall) {
    Write-Skip "依赖已完整（pip check 通过），跳过安装"
    Save-VenvState
} else {
    Write-Warn ("开始安装依赖：" + ($reasons -join '；'))
    Write-Warn "首次安装约 2-5 分钟，请勿关闭窗口。"

    $null = & $VenvPython -m pip install --upgrade pip --disable-pip-version-check --quiet 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Warn "升级 pip 失败，继续尝试安装依赖" }

    $installArgs = @('-m', 'pip', 'install', '-r', $Requirements, '--disable-pip-version-check')
    if ($UseMirror) { $installArgs += @('-i', $MirrorIndex) }

    # 优先用文件夹内的离线依赖包；版本不匹配或缺失时才联网。
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

    $afterCheck = Test-Dependencies
    if (-not $afterCheck.Ok) {
        Write-Warn "依赖自检失败：$($afterCheck.Detail)"
        exit 1
    }
    $installedDeps = $true
    Save-VenvState
    Write-Ok "依赖安装完成并通过自检"
}

# ---------------------------------------------------------------- 4. 目录与快捷方式
Write-Step "第 4 步 / 4：数据目录与桌面快捷方式"
$createdDir = $false
foreach ($dir in @($DataDir, $LogDir)) {
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        $createdDir = $true
    }
}
if ($createdDir) {
    Write-Ok "已创建 data\（数据库）与 logs\（运行日志）"
} else {
    Write-Skip "data\ 与 logs\ 目录已存在，跳过创建"
}

if ($SkipShortcut) {
    Write-Skip "已指定 -SkipShortcut，跳过桌面快捷方式"
} else {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shortcutPath = Join-Path $desktop 'Bittensor 地址关联挖掘.lnk'
    $expectedTarget = Join-Path $Root 'launch_desktop.ps1'
    $needShortcut = $true

    if ((Test-Path -LiteralPath $shortcutPath) -and -not $Force) {
        try {
            $shell = New-Object -ComObject WScript.Shell
            $existing = $shell.CreateShortcut($shortcutPath)
            if ($existing.Arguments -like "*$expectedTarget*") {
                Write-Skip "桌面快捷方式已指向本项目，跳过创建"
                $needShortcut = $false
            } else {
                Write-Warn "桌面上已有同名快捷方式但指向旧目录，正在更新…"
            }
        } catch {
            Write-Warn "已有快捷方式读取失败，将重新创建…"
        }
    }

    if ($needShortcut) {
        try {
            & (Join-Path $Root 'create_desktop_shortcut.ps1') | Out-Host
        } catch {
            Write-Warn "桌面快捷方式创建失败（不影响使用，可改用 启动.cmd）：$($_.Exception.Message)"
        }
    }
}

# ---------------------------------------------------------------- 汇总
Write-Host ""
if (-not $installedPython -and -not $installedDeps) {
    Write-Host "环境已就绪：本次没有重复安装任何组件。" -ForegroundColor Green
} else {
    Write-Host "安装完成。" -ForegroundColor Green
}
Write-Host ""
Write-Host "下次启动：" -ForegroundColor White
Write-Host "  · 双击桌面的 “Bittensor 地址关联挖掘” 快捷方式，或"
Write-Host "  · 双击本文件夹里的 启动.cmd"
Write-Host ""
Write-Host "浏览器会自动打开 http://127.0.0.1:8766"
Write-Host "详细操作见 使用说明.md"
Write-Host ""
