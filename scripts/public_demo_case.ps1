param(
    [string]$WorkspaceRoot = ".agent-workspace",
    [switch]$SkipBootstrap
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Checkpoint = Join-Path $RepoRoot ".venv\Scripts\checkpoint.exe"
$DemoPage = Join-Path $RepoRoot "examples\web\checkout_verification_demo.html"
$ExpectedCopy = "Proceed to Checkout"
$BrokenCopy = "Next Step"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Invoke-Checkpoint {
    param(
        [string[]]$CheckpointArgs,
        [switch]$AllowFailure
    )
    & $Checkpoint @CheckpointArgs
    $code = $LASTEXITCODE
    if ($code -ne 0 -and -not $AllowFailure) {
        throw "checkpoint failed with exit code $code"
    }
}

if (-not (Test-Path $Checkpoint)) {
    if ($SkipBootstrap) {
        throw "Checkpoint executable not found: $Checkpoint"
    }
    powershell -ExecutionPolicy Bypass -File (Join-Path $RepoRoot "scripts\bootstrap.ps1") -Step all -WorkspaceRoot $WorkspaceRoot
}

$original = Get-Content -LiteralPath $DemoPage -Raw -Encoding UTF8
if (-not $original.Contains($ExpectedCopy)) {
    throw "Demo page does not contain expected checkout copy: $ExpectedCopy"
}

Push-Location $RepoRoot
try {
    Write-Host ""
    Write-Host "[1/4] Baseline verification should pass."
    Invoke-Checkpoint -CheckpointArgs @(
        "verify-now",
        "--workspace-root", $WorkspaceRoot,
        "--workflow", "checkout_verification",
        "--live",
        "--format", "markdown"
    )

    Write-Host ""
    Write-Host "[2/4] Introduce a copy regression."
    $broken = $original.Replace($ExpectedCopy, $BrokenCopy)
    [System.IO.File]::WriteAllText($DemoPage, $broken, $Utf8NoBom)

    Write-Host ""
    Write-Host "[3/4] Verification should fail and point to assert_checkout_button."
    & $Checkpoint @(
        "verify-now",
        "--workspace-root", $WorkspaceRoot,
        "--workflow", "checkout_verification",
        "--live",
        "--format", "markdown"
    )
    $failureCode = $LASTEXITCODE
    if ($failureCode -eq 0) {
        throw "Expected the broken checkout copy to fail verification, but it passed."
    }

    Write-Host ""
    Write-Host "[4/4] Restore the page and verify green again."
    [System.IO.File]::WriteAllText($DemoPage, $original, $Utf8NoBom)
    Invoke-Checkpoint -CheckpointArgs @(
        "verify-now",
        "--workspace-root", $WorkspaceRoot,
        "--workflow", "checkout_verification",
        "--live",
        "--format", "markdown"
    )

    Write-Host ""
    Write-Host "Public demo completed: green -> detected regression -> restored green."
}
finally {
    [System.IO.File]::WriteAllText($DemoPage, $original, $Utf8NoBom)
    Pop-Location
}
