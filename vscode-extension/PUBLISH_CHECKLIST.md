# VS Code Marketplace Publish Checklist

## Prerequisites

- Confirm `package.json` has `publisher`, `version`, `icon`, `categories`, `keywords`, `repository`, and `license`.
- Confirm `icons/visual-agent.png` is a 128x128 PNG.
- Confirm `README.md` includes install steps, quick start, commands, settings, and screenshots.
- Confirm `LICENSE` is present.

## Build

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

Inspect the generated `.vsix` before publishing.

## Publisher Setup

1. Create or verify the Visual Studio Marketplace publisher account.
2. Create an Azure DevOps Personal Access Token with Marketplace publish scope.
3. Login locally:

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
- Run `Checkpoint: Open Examples`.
- Run `Checkpoint: New Workflow`.
- Run `Checkpoint: Generate Workflow from Description`.
- Verify the Python environment warning appears when `visualAgent.pythonPath` is invalid.

