# VS Code Marketplace Publish Checklist

## Before You Publish

- Confirm `package.json` has `publisher`, `version`, `icon`, `categories`, `keywords`, `repository`, and `license`.
- Confirm `displayName` is `Checkpoint` and the public description matches the product copy.
- Confirm `icons/visual-agent.png` is a 128x128 PNG and looks correct on light and dark backgrounds.
- Confirm `README.md` includes install steps, quick start, commands, settings, and a screenshot placeholder.
- Confirm `LICENSE` is present.
- Confirm the version bump is intentional before packaging.

## Local Build

```powershell
cd vscode-extension
npm install
npm run compile
npm test
```

## Package

Install VSCE when needed:

```powershell
npm install -g @vscode/vsce
```

Create the package:

```powershell
vsce package
```

Inspect the generated `.vsix` before publishing. Install it into a clean VS Code profile and verify:

- `Checkpoint: Init Workspace`
- `x-agent: Verify Implementation`
- `Checkpoint: Show Last AI Verification`
- `Checkpoint: Show Latest Failure`
- `Checkpoint: Open Examples`

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

- Install the Marketplace extension into a clean VS Code profile.
- Run `Checkpoint: Init Workspace`.
- Run `Checkpoint: Verify Current Change`.
- Run `Checkpoint: Generate Workflow from Description`.
- Run `Checkpoint: Open Examples`.
- Verify the Python environment warning appears when `visualAgent.pythonPath` is invalid.
