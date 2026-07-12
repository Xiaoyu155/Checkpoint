param(
    [ValidateSet("all", "python", "venv", "install", "extras", "browsers", "workspace", "mcp-config", "smoke")]
    [string]$Step = "all",
    [string]$Python = "python",
    [string]$Extras = "web,mcp",
    [string]$WorkspaceRoot = ".agent-workspace"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$WorkspacePath = Join-Path $RepoRoot $WorkspaceRoot
$BrowserPath = Join-Path $RepoRoot ".pw-browsers"

function Invoke-Step($Name, [scriptblock]$Body) {
    Write-Host ""
    Write-Host "[$Name] Starting..."
    try {
        & $Body
        Write-Host "[$Name] OK"
    } catch {
        Write-Host "[$Name] FAILED: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Fix the error above, then rerun: powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Step $Name"
        exit 1
    }
}

function Test-PythonVersion {
    & (Join-Path $RepoRoot "scripts\check_python.ps1") -Python $Python
    if ($LASTEXITCODE -ne 0) {
        throw "Python version check failed."
    }
}

function Ensure-Venv {
    $venvPath = Join-Path $RepoRoot ".venv"
    if (Test-Path $VenvPython) {
        Write-Host ".venv already exists: $venvPath"
        return
    }
    & $Python -m venv $venvPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        throw "Failed to create virtual environment at $venvPath"
    }
    Write-Host "Created virtual environment: $venvPath"
    Write-Host "Use this Python: .\.venv\Scripts\python.exe"
}

function Install-Core {
    if (-not (Test-Path $VenvPython)) {
        Ensure-Venv
    }
    Push-Location $RepoRoot
    try {
        & $VenvPython -m pip install -e .
        if ($LASTEXITCODE -ne 0) {
            throw "pip install -e . failed."
        }
        & $VenvPython -c "import visual_agent; print('visual_agent import OK')"
        if ($LASTEXITCODE -ne 0) {
            throw "visual_agent import check failed."
        }
    } finally {
        Pop-Location
    }
}

function Install-Extras {
    if (-not (Test-Path $VenvPython)) {
        Ensure-Venv
    }
    $extraSpec = $Extras.Trim()
    if (-not $extraSpec) {
        $extraSpec = "web,mcp"
    }
    Push-Location $RepoRoot
    try {
        & $VenvPython -m pip install -e ".[$extraSpec]"
        if ($LASTEXITCODE -ne 0) {
            throw "pip install -e .[$extraSpec] failed."
        }
        if ($extraSpec -match "(^|,)web(,|$)") {
            & $VenvPython -c "import playwright; print('playwright import OK')"
            if ($LASTEXITCODE -ne 0) {
                throw "playwright import check failed."
            }
        }
        if ($extraSpec -match "(^|,)mcp(,|$)") {
            & $VenvPython -c "import mcp; print('mcp import OK')"
            if ($LASTEXITCODE -ne 0) {
                throw "mcp import check failed."
            }
        }
    } finally {
        Pop-Location
    }
}

function Clear-PlaywrightLocks {
    # A stale __dirlock (left by an interrupted install) makes every later
    # `playwright install` exit 0 with no output and download nothing. Clearing
    # it is the single most important fix for "install silently does nothing".
    $lockRoots = @($BrowserPath, (Join-Path $env:LOCALAPPDATA "ms-playwright"))
    foreach ($root in $lockRoots) {
        $lock = Join-Path $root "__dirlock"
        if (Test-Path $lock) {
            Write-Host "Clearing stale Playwright lock: $lock"
            Remove-Item -Recurse -Force $lock -ErrorAction SilentlyContinue
        }
    }
}

function Test-PlaywrightLaunch {
    & $VenvPython -c "from playwright.sync_api import sync_playwright as s; p=s().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Invoke-PlaywrightInstall {
    param([string]$DownloadHost)
    if ($DownloadHost) {
        $env:PLAYWRIGHT_DOWNLOAD_HOST = $DownloadHost
        Write-Host "Installing Playwright browsers via download host: $DownloadHost"
    } else {
        Remove-Item Env:PLAYWRIGHT_DOWNLOAD_HOST -ErrorAction SilentlyContinue
        Write-Host "Installing Playwright browsers via the default host."
    }
    Clear-PlaywrightLocks
    # Headless verification needs chromium-headless-shell, which an older install
    # can miss; request it explicitly alongside chromium.
    & $VenvPython -m playwright install chromium chromium-headless-shell
}

function Install-Browsers {
    if (-not (Test-Path $VenvPython)) {
        Ensure-Venv
    }
    $env:PLAYWRIGHT_BROWSERS_PATH = $BrowserPath
    $mirrorHost = "https://cdn.npmmirror.com/binaries/playwright"
    Push-Location $RepoRoot
    try {
        # 1) Default host first (complete, but can be slow in mainland China).
        Invoke-PlaywrightInstall -DownloadHost $null
        if (-not (Test-PlaywrightLaunch)) {
            Write-Warning "Default Playwright download failed or is incomplete; retrying via the China mirror."
            # 2) China mirror fallback (faster in CN; may lack the newest build).
            Invoke-PlaywrightInstall -DownloadHost $mirrorHost
        }
        Remove-Item Env:PLAYWRIGHT_DOWNLOAD_HOST -ErrorAction SilentlyContinue
        if (-not (Test-PlaywrightLaunch)) {
            throw @"
Playwright Chromium could not be installed or launched.

Common causes: a stale lock, or a slow/blocked download (frequent in mainland China).
Fix manually, then re-run:  bootstrap.ps1 -Step browsers

  1) Remove any stale lock:
       Remove-Item -Recurse -Force "$BrowserPath\__dirlock" -ErrorAction SilentlyContinue
       Remove-Item -Recurse -Force "$env:LOCALAPPDATA\ms-playwright\__dirlock" -ErrorAction SilentlyContinue
  2) Try the China mirror (fast in CN):
       `$env:PLAYWRIGHT_DOWNLOAD_HOST = "$mirrorHost"
       `$env:PLAYWRIGHT_BROWSERS_PATH = "$BrowserPath"
       & "$VenvPython" -m playwright install chromium chromium-headless-shell
  3) If the mirror lacks a build, use the default host (slower, ~100MB, be patient):
       Remove-Item Env:PLAYWRIGHT_DOWNLOAD_HOST -ErrorAction SilentlyContinue
       & "$VenvPython" -m playwright install chromium chromium-headless-shell
  4) Verify:
       & "$VenvPython" -c "from playwright.sync_api import sync_playwright as s; p=s().start(); b=p.chromium.launch(); b.close(); p.stop(); print('OK')"
"@
        }
        Write-Host "Playwright Chromium ready. PLAYWRIGHT_BROWSERS_PATH=$BrowserPath"
    } finally {
        Pop-Location
    }
}

function Initialize-Workspace {
    if (-not (Test-Path $VenvPython)) {
        Ensure-Venv
    }
    if (Test-Path (Join-Path $WorkspacePath "workspace.json")) {
        Write-Host "Workspace already exists: $WorkspacePath"
        return
    }
    Push-Location $RepoRoot
    try {
        & $VenvPython -m visual_agent.cli init-workspace --root $WorkspaceRoot
        if ($LASTEXITCODE -ne 0) {
            throw "init-workspace failed."
        }
    } finally {
        Pop-Location
    }
}

function Write-McpConfig {
    if (-not (Test-Path $VenvPython)) {
        Ensure-Venv
    }
    # Generated configs contain machine-specific absolute paths. Keep them in
    # the ignored runtime workspace instead of overwriting tracked examples.
    $configDir = Join-Path $WorkspacePath "mcp_config"
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null
    $srcPath = Join-Path $RepoRoot "src"
    $server = [ordered]@{
        command = $VenvPython
        args = @("-m", "visual_agent.mcp_server", "--workspace-root", $WorkspacePath)
        cwd = $RepoRoot
        env = [ordered]@{
            PYTHONPATH = $srcPath
        }
    }
    $payload = [ordered]@{
        mcpServers = [ordered]@{
            "visual-agent" = $server
        }
    }
    $json = $payload | ConvertTo-Json -Depth 8
    $claudePath = Join-Path $configDir "claude_desktop_config.json"
    $cursorPath = Join-Path $configDir "cursor_mcp.json"
    Set-Content -Path $claudePath -Value $json -Encoding UTF8
    Set-Content -Path $cursorPath -Value $json -Encoding UTF8
    Write-Host "Wrote MCP configs:"
    Write-Host "  $claudePath"
    Write-Host "  $cursorPath"
    Write-Host "These files are machine-specific and remain under the local workspace."
}

function Run-OnboardingSmoke {
    if (-not (Test-Path $VenvPython)) {
        Ensure-Venv
    }
    if (-not (Test-Path (Join-Path $WorkspacePath "workspace.json"))) {
        Initialize-Workspace
    }
    Push-Location $RepoRoot
    try {
        & $VenvPython -m visual_agent.cli doctor
        if ($LASTEXITCODE -ne 0) {
            throw "doctor failed."
        }
        & $VenvPython -m visual_agent.cli demo-workspace-check --root $WorkspaceRoot --format markdown
        if ($LASTEXITCODE -ne 0) {
            throw "demo-workspace-check failed."
        }
    } finally {
        Pop-Location
    }
}

if ($Step -eq "python" -or $Step -eq "all") {
    Invoke-Step "python" { Test-PythonVersion }
}

if ($Step -eq "venv" -or $Step -eq "all") {
    Invoke-Step "venv" { Ensure-Venv }
}

if ($Step -eq "install" -or $Step -eq "all") {
    Invoke-Step "install" { Install-Core }
}

if ($Step -eq "extras" -or $Step -eq "all") {
    Invoke-Step "extras" { Install-Extras }
}

if ($Step -eq "browsers" -or $Step -eq "all") {
    Invoke-Step "browsers" { Install-Browsers }
}

if ($Step -eq "workspace" -or $Step -eq "all") {
    Invoke-Step "workspace" { Initialize-Workspace }
}

if ($Step -eq "mcp-config" -or $Step -eq "all") {
    Invoke-Step "mcp-config" { Write-McpConfig }
}

if ($Step -eq "smoke" -or $Step -eq "all") {
    Invoke-Step "smoke" { Run-OnboardingSmoke }
}

Write-Host ""
Write-Host "Bootstrap step '$Step' completed."
if ($Step -eq "all") {
    Write-Host "Next steps:"
    Write-Host "  .\.venv\Scripts\python.exe -m visual_agent.cli mcp-smoke --workspace-root $WorkspaceRoot --format markdown"
    Write-Host "  .\.venv\Scripts\python.exe -m visual_agent.cli workspace-gui --root $WorkspaceRoot"
} elseif ($Step -ne "smoke") {
    Write-Host "Smoke check:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Step smoke"
}
