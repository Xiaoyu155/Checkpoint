# VS Code Marketplace Publish Checklist

## Before You Publish

- Confirm `package.json` has `publisher`, `version`, `icon`, `categories`, `keywords`, `repository`, and `license`.
- Confirm `displayName` is `Checkpoint` and the public description matches the product copy.
- Confirm `icons/visual-agent.png` is a 128x128 PNG and looks correct on light and dark backgrounds.
- Confirm `README.md` includes install steps, quick start, commands, and settings.
- Capture marketplace screenshots for the Workflows sidebar, Verify Now output, Product Issues panel, and Latest Failure panel.
- Confirm `LICENSE` is present.
- Confirm the version bump is intentional before packaging.

## Local Build

```powershell
cd vscode-extension
npm install
npm run compile
npm test
```

`npm test` is the required extension acceptance gate. It verifies parser behavior,
CLI bridge invocation, and command wiring for `Checkpoint: Verify Now` and
`Checkpoint: Show Product Issues`.

From the repository root, run the product release smoke gate before packaging:

```powershell
.\.venv\Scripts\checkpoint.exe release-smoke --run --workspace-root .agent-workspace --format markdown
```

Also check `docs/public_launch_checklist.md` before the first public publish so
GitHub, PyPI, and VS Code launch metadata stay aligned.

## Package

Install VSCE when needed:

```powershell
npm install -g @vscode/vsce
```

Create the package:

```powershell
vsce package
```

Inspect the generated `.vsix` before publishing. Install it into a clean VS Code profile for one packaging smoke pass:

- `Checkpoint: Init Workspace`
- `Checkpoint: Verify Now`
- `Checkpoint: Show Product Issues`

## Marketplace Listing Copy

Prepare the public listing text in `MARKETPLACE_LISTING.md` before the first publish.

## Publisher Setup

1. Create or verify the Visual Studio Marketplace publisher account.
2. Create an Azure DevOps Personal Access Token with Marketplace publish scope.
3. Log in locally:

```powershell
vsce login visual-agent
```

## Publish

```powershell
vsce publish
```

For a specific version bump:

```powershell
vsce publish patch
```

## Post-Publish

- Attach the generated `.vsix` to the matching GitHub preview release.
- Install the Marketplace extension into a clean VS Code profile.
- Run `Checkpoint: Init Workspace`.
- Run `Checkpoint: Verify Current Change`.
- Run `Checkpoint: Generate Workflow from Description`.
- Run `Checkpoint: Open Examples`.
- Verify the Python environment warning appears when `visualAgent.pythonPath` is invalid.
