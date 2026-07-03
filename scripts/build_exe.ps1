#Requires -Version 5.1
<#
.SYNOPSIS
    Builds DevPacer.exe — a standalone Windows desktop workbench.

.DESCRIPTION
    Installs PyInstaller into the project venv (if missing), then
    packages the Tkinter workbench into dist\DevPacer\DevPacer.exe.
    Double-click DevPacer.exe to launch — no Python or venv needed.

.EXAMPLE
    .\build_exe.ps1
    # Output: dist\DevPacer\DevPacer.exe
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$SpecPath = Join-Path $PSScriptRoot "checkpoint.spec"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Virtual environment not found. Run: python -m venv .venv && .venv\Scripts\pip install -e ."
}

Write-Host "==> Checking PyInstaller..." -ForegroundColor Cyan
$piCheck = & $VenvPython -c "import PyInstaller" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "    Installing PyInstaller..." -ForegroundColor Yellow
    & $VenvPython -m pip install pyinstaller --quiet
    if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller install failed." }
}

Write-Host "==> Building DevPacer.exe..." -ForegroundColor Cyan
Push-Location $RepoRoot
try {
    & $VenvPython -m PyInstaller $SpecPath --noconfirm
} finally {
    Pop-Location
}

if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller build failed." }

$ExePath = Join-Path $RepoRoot "dist\DevPacer\DevPacer.exe"
if (Test-Path $ExePath) {
    $SizeMB = [math]::Round((Get-Item $ExePath).Length / 1MB, 1)
    Write-Host ""
    Write-Host "✓ Build succeeded!" -ForegroundColor Green
    Write-Host "  EXE  : $ExePath  ($SizeMB MB)" -ForegroundColor Green
    Write-Host "  Folder: $(Join-Path $RepoRoot 'dist\DevPacer')" -ForegroundColor Green
    Write-Host ""
    Write-Host "  把整个 dist\DevPacer 文件夹复制给用户，双击 DevPacer.exe 即可运行。" -ForegroundColor Cyan
} else {
    Write-Error "Build finished but EXE not found at $ExePath"
}
