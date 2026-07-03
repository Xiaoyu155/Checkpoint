#Requires -Version 5.1
<#
.SYNOPSIS
    在桌面创建 DevPacer 快捷方式。
.DESCRIPTION
    运行一次即可。快捷方式指向 dist\DevPacer\DevPacer.exe，
    双击打开工作台窗口，不需要 Python 或终端。
#>

$ExePath = Join-Path $PSScriptRoot "dist\DevPacer\DevPacer.exe"
$IcoPath = Join-Path $PSScriptRoot "assets\DevPacer.ico"

if (-not (Test-Path $ExePath)) {
    Write-Error "找不到 $ExePath`n请先运行 .\build_exe.ps1 构建 EXE。"
}

$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "DevPacer 工作台.lnk"

$WshShell = New-Object -ComObject WScript.Shell
$sc = $WshShell.CreateShortcut($ShortcutPath)
$sc.TargetPath       = $ExePath
$sc.WorkingDirectory = Split-Path $ExePath
$sc.Description      = "DevPacer AI 编码工作台"
if (Test-Path $IcoPath) { $sc.IconLocation = $IcoPath }
$sc.Save()

Write-Host ""
Write-Host "✓ 桌面快捷方式已创建：$ShortcutPath" -ForegroundColor Green
Write-Host "  双击"DevPacer 工作台"即可启动，无需任何环境。" -ForegroundColor Cyan
