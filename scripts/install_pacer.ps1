# One-shot Windows install for Pacer (product) / visual-agent (package).
# Usage (from repo root or xiao/):
#   powershell -ExecutionPolicy Bypass -File scripts\install_pacer.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_pacer.ps1 -User

param(
    [switch]$User,
    [switch]$SkipDoctor
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$XiaoRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path

Write-Host ""
Write-Host "Pacer install" -ForegroundColor Cyan
Write-Host "  source: $XiaoRoot"
Write-Host ""

function Resolve-Python {
    $candidates = @(
        (Join-Path $XiaoRoot ".venv\Scripts\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) {
            return $path
        }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        return $cmd.Source
    }
    throw "Python 3.10+ not found. Install Python and re-run."
}

$Python = Resolve-Python
Write-Host "[1/4] Python: $Python"

& $Python -c "import sys; assert sys.version_info >= (3, 10), sys.version"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.10+ required."
}

Write-Host "[2/4] Installing package (editable) as visual-agent / pacer / checkpoint ..."
$pipArgs = @("-m", "pip", "install", "-e", $XiaoRoot)
if ($User) {
    $pipArgs += "--user"
}
& $Python @pipArgs
if ($LASTEXITCODE -ne 0) {
    throw "pip install failed."
}

Write-Host "[3/4] Ensuring pytest is available for acceptance commands ..."
& $Python -m pip install "pytest>=8.0.0" | Out-Null

Write-Host "[4/4] Verifying entry points ..."
$checks = @()
foreach ($name in @("pacer", "checkpoint")) {
    $exe = Join-Path (Split-Path $Python -Parent) "Scripts\$name.exe"
    if (-not (Test-Path $exe)) {
        $found = Get-Command $name -ErrorAction SilentlyContinue
        if ($found) { $exe = $found.Source }
    }
    if (Test-Path $exe) {
        $ver = & $exe --version 2>&1 | Out-String
        $checks += "  $name OK -> $exe"
        $checks += "    " + ($ver.Trim() -split "`n" | Select-Object -First 1)
    } else {
        $checks += "  $name missing on PATH (install succeeded; open a new shell or add Scripts to PATH)"
    }
}
$checks | ForEach-Object { Write-Host $_ }

if (-not $SkipDoctor) {
    Write-Host ""
    Write-Host "Running agents doctor ..."
    try {
        & checkpoint agents doctor
    } catch {
        Write-Host "agents doctor skipped: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "Next:"
Write-Host "  cd <your-git-project>"
Write-Host '  checkpoint mission start --goal "..." --test-command "python -m pytest -q" --agent codex --execute'
Write-Host "  # or open managed Codex session:"
Write-Host "  pacer"
Write-Host ""
Write-Host "Help: pacer --help   |   checkpoint --help"
Write-Host ""
