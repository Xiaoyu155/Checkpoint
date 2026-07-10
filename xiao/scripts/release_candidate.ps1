param(
    [switch]$SkipPackageInstall
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Checkpoint = Join-Path $Root ".venv\Scripts\checkpoint.exe"
$ProjectVersionMatch = Select-String -Path (Join-Path $Root "pyproject.toml") -Pattern '^version = "([^"]+)"' | Select-Object -First 1
if ($null -eq $ProjectVersionMatch) {
    throw "Could not read project version from pyproject.toml."
}
$ProjectVersion = $ProjectVersionMatch.Matches[0].Groups[1].Value

if (-not (Test-Path $Python)) {
    throw "Missing virtual environment. Run scripts\bootstrap.ps1 -Step all first."
}

if (-not (Test-Path $Checkpoint)) {
    throw "Missing checkpoint.exe. Run scripts\bootstrap.ps1 -Step all first."
}

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Command
    )
    Write-Host ""
    Write-Host "[$Name]"
    try {
        & $Command
    }
    catch {
        throw "[$Name] failed: $($_.Exception.Message)"
    }
}

function Invoke-NativeCommand {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )
    & $FilePath @ArgumentList
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "Command failed with exit code ${ExitCode}: $FilePath $($ArgumentList -join ' ')"
    }
}

Invoke-Step "Python core tests" {
    Invoke-NativeCommand $Python @("-m", "pytest", "tests\test_cli.py", "tests\test_workflow.py", "tests\test_validation.py", "tests\test_reports.py", "tests\test_verify.py", "tests\test_codex_check.py", "tests\test_playwright_env.py", "-q", "--tb=short")
}

Invoke-Step "Release smoke" {
    Invoke-NativeCommand $Checkpoint @("release-smoke", "--run", "--workspace-root", ".agent-workspace", "--format", "markdown")
}

Invoke-Step "Public demo case" {
    Invoke-NativeCommand "powershell" @("-ExecutionPolicy", "Bypass", "-File", "scripts\public_demo_case.ps1", "-SkipBootstrap")
}

if (-not $SkipPackageInstall) {
    Invoke-Step "Python build tools" {
        Invoke-NativeCommand $Python @("-m", "pip", "install", "--upgrade", "build", "twine")
    }
}

Invoke-Step "Python package build" {
    Invoke-NativeCommand $Python @("-m", "build")
}

Invoke-Step "Python package metadata check" {
    Invoke-NativeCommand $Python @("-m", "twine", "check", "dist\*")
}

Invoke-Step "Clean temporary Python build directory" {
    $BuildDir = Join-Path $Root "build"
    $ResolvedRoot = (Resolve-Path $Root).Path
    if (Test-Path $BuildDir) {
        $ResolvedBuild = (Resolve-Path $BuildDir).Path
        if (-not $ResolvedBuild.StartsWith($ResolvedRoot)) {
            throw "Refusing to remove build directory outside repository root: $ResolvedBuild"
        }
        Remove-Item -LiteralPath $ResolvedBuild -Recurse -Force
    }
}

Invoke-Step "VS Code extension tests" {
    Push-Location vscode-extension
    try {
        Invoke-NativeCommand "npm" @("test")
    }
    finally {
        Pop-Location
    }
}

Invoke-Step "VS Code extension package" {
    Push-Location vscode-extension
    try {
        Invoke-NativeCommand "npm" @("run", "package")
    }
    finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "Release candidate is ready."
Write-Host "Python artifacts: dist\visual_agent-$ProjectVersion*"
Write-Host "VS Code artifact: vscode-extension\visual-agent-$ProjectVersion.vsix"
