param(
    [int]$Runs = 5,
    [string]$OutputPath = "smoke_results\browser_form.txt"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Output = Join-Path $RepoRoot $OutputPath
$OutputDir = Split-Path $Output -Parent
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $RepoRoot ".pw-browsers"
$passed = 0
$failed = 0
$elapsedValues = @()
$lines = @()
$lines += "Browser form smoke started: $(Get-Date -Format o)"
$lines += "Runs: $Runs"
$lines += "PLAYWRIGHT_BROWSERS_PATH=$env:PLAYWRIGHT_BROWSERS_PATH"

Push-Location $RepoRoot
try {
    for ($i = 1; $i -le $Runs; $i++) {
        $runDir = Join-Path $RepoRoot ("smoke_results\browser_form_run_{0}" -f $i)
        $started = Get-Date
        & $Python -m visual_agent.cli run-workflow --file examples/browser_form_workflow.yaml --inputs-file examples/inputs/demo_login.json --output-dir $runDir --run-profile supervised | Out-Null
        $exit = $LASTEXITCODE
        $elapsed = ((Get-Date) - $started).TotalSeconds
        $elapsedValues += $elapsed
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

$average = if ($elapsedValues.Count -gt 0) { ($elapsedValues | Measure-Object -Average).Average } else { 0 }
$lines += "passed=$passed"
$lines += "failed=$failed"
$lines += "success_rate=$passed/$Runs"
$lines += "average_elapsed_seconds=$([Math]::Round($average, 3))"
$lines | Set-Content -Path $Output -Encoding UTF8

Get-Content $Output
if ($failed -gt 0) {
    exit 1
}
exit 0
