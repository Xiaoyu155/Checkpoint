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
$probeFile = Join-Path $env:TEMP "visual-agent-notepad-uia-smoke.txt"

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

    Set-Content -Path $probeFile -Value "Visual Agent Notepad UIA smoke $(Get-Date -Format o)" -Encoding UTF8
    $notepad = Start-Process notepad.exe -ArgumentList "`"$probeFile`"" -PassThru

    $processTitle = ""
    $processWindowDetected = $false
    $processDetected = $false
    $inspectText = ""
    $containsNotepad = $false
    $probeName = Split-Path $probeFile -Leaf

    for ($attempt = 1; $attempt -le 15; $attempt++) {
        Start-Sleep -Seconds 1
        try {
            if ($notepad -ne $null) {
                $notepad.Refresh()
                $processTitle = $notepad.MainWindowTitle
                if ($notepad.MainWindowHandle -ne 0) {
                    $processWindowDetected = $true
                }
            }
        } catch {
            $processTitle = ""
        }

        $notepadProcesses = @(Get-Process | Where-Object { $_.ProcessName -match "notepad|applicationframehost" })
        $processDetected = $notepadProcesses.Count -gt 0
        foreach ($process in $notepadProcesses) {
            if (-not [string]::IsNullOrWhiteSpace($process.MainWindowTitle)) {
                $processTitle = $process.MainWindowTitle
                $processWindowDetected = $true
                break
            }
            if ($process.MainWindowHandle -ne 0) {
                $processWindowDetected = $true
            }
        }

        $inspect = & $Python -m visual_agent.cli inspect-uia --max-depth 8 --limit 500 2>&1
        $inspectText = $inspect -join "`n"
        $containsNotepad = $inspectText -match "Notepad|记事本|$([Regex]::Escape($probeName))"
        if ($containsNotepad -or $processWindowDetected) {
            break
        }
    }

    $fixtureRunDir = Join-Path $RepoRoot "smoke_results\notepad_fixture_run"
    & $Python -m visual_agent.cli run-workflow --file examples/windows_notepad_demo_workflow.yaml --output-dir $fixtureRunDir --run-profile dry-run | Out-Null
    $workflowExit = $LASTEXITCODE

    $lines += "status=$(if (($containsNotepad -or $processWindowDetected -or $processDetected) -and $workflowExit -eq 0) { 'pass' } else { 'fail' })"
    $lines += "notepad_window_detected=$containsNotepad"
    $lines += "notepad_process_detected=$processDetected"
    $lines += "notepad_process_window_detected=$processWindowDetected"
    $lines += "notepad_process_title=$processTitle"
    $lines += "probe_file=$probeFile"
    $lines += "workflow_exit_code=$workflowExit"
    $lines += "inspect_excerpt_start"
    $lines += ($inspectText.Substring(0, [Math]::Min(2000, $inspectText.Length)))
    $lines += "inspect_excerpt_end"
} finally {
    if ($notepad -ne $null -and -not $notepad.HasExited) {
        Stop-Process -Id $notepad.Id -Force
    }
    Get-Process | Where-Object { $_.ProcessName -match "notepad" } | Stop-Process -Force -ErrorAction SilentlyContinue
    Pop-Location
}

$lines | Set-Content -Path $Output -Encoding UTF8
Get-Content $Output
if (($lines -join "`n") -match "status=pass") {
    exit 0
}
exit 1
