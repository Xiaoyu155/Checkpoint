param(
    [string]$OutputPath = "smoke_results\notepad_uia.txt"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Output = Join-Path $RepoRoot $OutputPath
$OutputDir = Split-Path $Output -Parent
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$lines = @()
$lines += "Notepad UIA smoke started: $(Get-Date -Format o)"
$notepad = $null

Push-Location $RepoRoot
try {
    & $Python -c "import uiautomation; print('uiautomation import OK')" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $lines += "status=skipped"
        $lines += "reason=uiautomation is not installed"
        $lines | Set-Content -Path $Output -Encoding UTF8
        Get-Content $Output
        exit 2
    }

    $notepad = Start-Process notepad.exe -PassThru
    Start-Sleep -Seconds 3
    $notepad.Refresh()
    $processTitle = $notepad.MainWindowTitle
    $inspect = & $Python -m visual_agent.cli inspect-uia --max-depth 6 --limit 200 2>&1
    $inspectText = $inspect -join "`n"
    $containsNotepad = $inspectText -match "Notepad|记事本"
    $processWindowDetected = -not [string]::IsNullOrWhiteSpace($processTitle)

    $fixtureRunDir = Join-Path $RepoRoot "smoke_results\notepad_fixture_run"
    & $Python -m visual_agent.cli run-workflow --file examples/windows_notepad_demo_workflow.yaml --output-dir $fixtureRunDir --run-profile dry-run | Out-Null
    $workflowExit = $LASTEXITCODE

    $lines += "status=$(if (($containsNotepad -or $processWindowDetected) -and $workflowExit -eq 0) { 'pass' } else { 'fail' })"
    $lines += "notepad_window_detected=$containsNotepad"
    $lines += "notepad_process_window_detected=$processWindowDetected"
    $lines += "notepad_process_title=$processTitle"
    $lines += "workflow_exit_code=$workflowExit"
    $lines += "inspect_excerpt_start"
    $lines += ($inspectText.Substring(0, [Math]::Min(2000, $inspectText.Length)))
    $lines += "inspect_excerpt_end"
} finally {
    if ($notepad -ne $null -and -not $notepad.HasExited) {
        Stop-Process -Id $notepad.Id -Force
    }
    Pop-Location
}

$lines | Set-Content -Path $Output -Encoding UTF8
Get-Content $Output
if (($lines -join "`n") -match "status=pass") {
    exit 0
}
exit 1
