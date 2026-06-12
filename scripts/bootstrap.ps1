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

function Install-Browsers {
    if (-not (Test-Path $VenvPython)) {
        Ensure-Venv
    }
    $env:PLAYWRIGHT_BROWSERS_PATH = $BrowserPath
    Push-Location $RepoRoot
    try {
        & $VenvPython -m playwright install chromium
        if ($LASTEXITCODE -ne 0) {
            throw "playwright install chromium failed."
        }
        Write-Host "PLAYWRIGHT_BROWSERS_PATH=$BrowserPath"
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
    $configDir = Join-Path $RepoRoot "examples\mcp_config"
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
