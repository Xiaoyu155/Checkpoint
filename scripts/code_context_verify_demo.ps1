param(
    [string]$DemoRoot = ".demo-workspaces\code-context-verify",
    [string]$Python = "python",
    [switch]$Keep
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DemoPath = Join-Path $RepoRoot $DemoRoot
$Workspace = Join-Path $DemoPath ".agent-workspace"
$Page = Join-Path $DemoPath "app\profile\page.tsx"
$Fixture = Join-Path $Workspace "fixtures\profile.html"
$DemoParent = Split-Path $DemoPath -Parent
New-Item -ItemType Directory -Force -Path $DemoParent | Out-Null
$ResolvedDemoParent = (Resolve-Path $DemoParent).Path
if (-not ($ResolvedDemoParent -eq $RepoRoot -or $ResolvedDemoParent.StartsWith("$RepoRoot\"))) {
    throw "DemoRoot must resolve inside the repository: $DemoRoot"
}

if ((Test-Path $DemoPath) -and -not $Keep) {
    Remove-Item -LiteralPath $DemoPath -Recurse -Force
}
New-Item -ItemType Directory -Force -Path (Split-Path $Page -Parent) | Out-Null

Push-Location $DemoPath
try {
    & $Python -m visual_agent.cli init-workspace --root $Workspace --overwrite --no-demo | Out-Null
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $Fixture -Parent) | Out-Null
    @'
<form action="/profile">
  <label for="displayName">Display name</label>
  <input id="displayName" name="displayName" value="Demo User">
  <button type="submit">Save profile</button>
</form>
<p>Profile saved successfully</p>
<p>Demo User</p>
'@ | Set-Content -Path $Fixture -Encoding UTF8

    @'
export default function ProfilePage() {
  return <form><input name="displayName" /></form>;
}
'@ | Set-Content -Path $Page -Encoding UTF8

    git init | Out-Null
    git config core.autocrlf false | Out-Null
    git add app/profile/page.tsx | Out-Null
    git -c user.email=demo@example.com -c user.name=Demo commit -m "initial profile form" | Out-Null

    @'
import { redirect } from "next/navigation";

async function saveProfile(formData: FormData) {
  "use server";
  redirect("/profile");
}

export default function ProfilePage() {
  return (
    <form action={saveProfile}>
      <label htmlFor="displayName">Display name</label>
      <input id="displayName" name="displayName" required minLength="3" />
      <button type="submit">Save profile</button>
      <p>Profile saved successfully</p>
      <p>Display name is required</p>
      <p>{profile.displayName}</p>
    </form>
  );
}
'@ | Set-Content -Path $Page -Encoding UTF8

    Write-Host "== generate-from-diff dry run =="
    & $Python -m visual_agent.cli generate-from-diff `
        --workspace-root $Workspace `
        --repo-root $DemoPath `
        --task-description "Verify profile form saves and displays the updated profile name" `
        --base-url "fixtures/profile.html" `
        --dry-run `
        --no-untracked
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    Write-Host ""
    Write-Host "== verify-impl dry run =="
    & $Python -m visual_agent.cli verify-impl `
        --workspace-root $Workspace `
        --repo-root $DemoPath `
        --task-description "Verify profile form saves and displays the updated profile name" `
        --base-url "fixtures/profile.html" `
        --run-profile dry-run `
        --min-quality-score 0 `
        --no-untracked `
        --format markdown
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    Write-Host ""
    Write-Host "== VS Code status markdown =="
    & $Python -m visual_agent.cli agent-status --workspace-root $Workspace --format markdown
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    Write-Host ""
    Write-Host "Demo workspace: $DemoPath"
} finally {
    Pop-Location
}
