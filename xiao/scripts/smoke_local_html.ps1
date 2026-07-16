param(
    [int]$Runs = 10,
    [string]$OutputPath = "smoke_results\local_html.txt"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Output = Join-Path $RepoRoot $OutputPath
$OutputDir = Split-Path $Output -Parent
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$passed = 0
$failed = 0
$lines = @()
$lines += "Local HTML smoke started: $(Get-Date -Format o)"
$lines += "Runs: $Runs"

Push-Location $RepoRoot
try {
    for ($i = 1; $i -le $Runs; $i++) {
        $runDir = Join-Path $RepoRoot ("smoke_results\local_html_run_{0}" -f $i)
        $started = Get-Date
        & $Python -m visual_agent.cli run-workflow --file examples/minimal_testable_workflow.yaml --output-dir $runDir --run-profile dry-run | Out-Null
        $exit = $LASTEXITCODE
        $elapsed = ((Get-Date) - $started).TotalSeconds
        if ($exit -eq 0) {
            $passed += 1
            $lines += "run=$i status=pass elapsed_seconds=$([Math]::Round($elapsed, 3))"
        } else {
            $failed += 1
            $lines += "run=$i status=fail exit_code=$exit elapsed_seconds=$([Math]::Round($elapsed, 3))"
        }
    }
} finally {
    Pop-Location
}

$lines += "passed=$passed"
$lines += "failed=$failed"
$lines += "success_rate=$passed/$Runs"
$lines | Set-Content -Path $Output -Encoding UTF8

Get-Content $Output
if ($failed -gt 0) {
    exit 1
}
exit 0
