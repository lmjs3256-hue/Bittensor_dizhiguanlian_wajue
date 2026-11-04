$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "Bittensor 地址关联挖掘.lnk"
$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PowerShell
$Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $Root 'launch_desktop.ps1')`""
$Shortcut.WorkingDirectory = $Root
$Shortcut.Description = "启动 Bittensor 地址关联挖掘工具"
$Shortcut.IconLocation = "$PowerShell,0"
$Shortcut.Save()

Write-Output "已创建桌面快捷方式：$ShortcutPath"
