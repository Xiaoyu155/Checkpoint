#Requires -Version 5.1
<#
.SYNOPSIS
    在桌面创建 Pacer 快捷方式。
.DESCRIPTION
    运行一次即可。快捷方式指向 dist\Pacer\Pacer.exe，
    双击打开工作台窗口，不需要 Python 或终端。
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ExePath = Join-Path $RepoRoot "dist\Pacer\Pacer.exe"
$IcoPath = Join-Path $RepoRoot "assets\Pacer.ico"

if (-not (Test-Path $ExePath)) {
    throw "找不到 $ExePath`n请先运行 scripts\build_exe.ps1 构建 EXE。"
}

$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "Pacer 工作台.lnk"

$WshShell = New-Object -ComObject WScript.Shell
$sc = $WshShell.CreateShortcut($ShortcutPath)
$sc.TargetPath       = $ExePath
$sc.WorkingDirectory = Split-Path $ExePath
$sc.Description      = "Pacer AI 编码工作台"
if (Test-Path $IcoPath) { $sc.IconLocation = $IcoPath }
$sc.Save()

Write-Host ""
Write-Host "Pacer 桌面快捷方式已创建：$ShortcutPath" -ForegroundColor Green
Write-Host '  双击“Pacer 工作台”即可启动，无需任何环境。' -ForegroundColor Cyan
