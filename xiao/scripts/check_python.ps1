param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

function Write-Ok($Message) {
    Write-Host "[OK] $Message"
}

function Write-Fail($Message) {
    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

try {
    $versionOutput = & $Python --version 2>&1
} catch {
    Write-Fail "Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/windows/ and enable 'Add python.exe to PATH'."
    exit 1
}

if ($LASTEXITCODE -ne 0) {
    Write-Fail "Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/windows/ and enable 'Add python.exe to PATH'."
    exit 1
}

$versionText = ($versionOutput | Select-Object -First 1).ToString()
if ($versionText -notmatch "Python\s+(\d+)\.(\d+)\.(\d+)") {
    Write-Fail "Could not parse Python version from: $versionText"
    exit 1
}

$major = [int]$Matches[1]
$minor = [int]$Matches[2]
$patch = [int]$Matches[3]

if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
    Write-Fail "Python $major.$minor.$patch is too old. Install Python 3.10 or newer from https://www.python.org/downloads/windows/."
    exit 1
}

Write-Ok "Python $major.$minor.$patch is supported."
exit 0
