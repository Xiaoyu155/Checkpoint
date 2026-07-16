param(
    [ValidateSet("smoke", "local", "ci")]
    [string]$Profile = "local",
    [string]$WorkspaceRoot = ".agent-workspace",
    [switch]$Run,
    [double]$TimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Python = "python"
if (Test-Path $VenvPython) {
    $Python = $VenvPython
}

$ArgsList = @(
    "-m",
    "visual_agent.cli",
    "quality-gate",
    "--profile",
    $Profile,
    "--workspace-root",
    $WorkspaceRoot,
    "--timeout-seconds",
    [string]$TimeoutSeconds
)

if ($Run) {
    $ArgsList += "--run"
}

& $Python @ArgsList
exit $LASTEXITCODE
