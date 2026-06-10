# Checkpoint for VS Code

Checkpoint adds a local verification panel for projects that use the `visual-agent` Python package.

## Install

1. Install the Python package from the repository root:

```powershell
pip install -e .[web,mcp,desktop]
```

2. Install extension dependencies for local development:

```powershell
cd vscode-extension
npm install
npm run compile
```

3. Open this folder in VS Code and run the extension from the Extension Development Host, or package it with `npm run package`.

Set `visualAgent.pythonPath` when your project uses a virtual environment, for example `.\\.venv\\Scripts\\python.exe`.

## Quick Start

Open the Checkpoint activity bar view.

Screenshot placeholder: add `images/quickstart.png` before publishing. It should show the Workflows sidebar with example workflows and the status bar quick action menu.

Use one of these commands:

- `Checkpoint: Init Workspace`
- `x-agent: Verify Implementation`
- `Checkpoint: Generate Workflow from Description`
- `Checkpoint: New Workflow`
- `Checkpoint: Open Examples`
- `Checkpoint: Run All Workflows`

## Commands

- `visualAgent.quickActions`: open status bar quick actions.
- `visualAgent.initWorkspace`: initialize `.agent-workspace` without opening a terminal.
- `visualAgent.runAll`: run all verification workflows.
- `visualAgent.runLiveSupervised`: run live supervised workflows.
- `visualAgent.runAffected`: run affected workflows.
- `visualAgent.verifyCurrentChange`: generate and run verification for the current git diff.
- `visualAgent.verifyImplementation`: command palette entry for `x-agent: Verify Implementation`; leave the URL blank to infer it.
- `visualAgent.showLastVerification`: show the last AI verification status.
- `visualAgent.showLatestFailure`: show latest failure details.
- `visualAgent.autoRepair`: preview or run deterministic workflow repair.
- `visualAgent.generateWorkflow`: generate workflow YAML from a description.
- `visualAgent.newWorkflow`: create a template workflow under `workflows/`.
- `visualAgent.openExamples`: show bundled example workflows in the sidebar.
- `visualAgent.copyExampleWorkflow`: copy an example workflow into the current project.
- `visualAgent.browserSmoke`: run a one-off browser smoke check.
- `visualAgent.connectCloud`: show cloud connection placeholder.
- `visualAgent.refresh`: refresh the sidebar.

## Settings

- `visualAgent.workspaceRoot`: workspace data directory, default `.agent-workspace`.
- `visualAgent.runOnSave`: run affected checks after file saves.
- `visualAgent.autoRunTags`: tags used for run-on-save checks.
- `visualAgent.pythonPath`: Python interpreter path.
- `visualAgent.enableTelemetry`: opt-in telemetry switch, default `false`.
- `visualAgent.maxNotificationChars`: maximum CLI output shown in notifications.

## Development

```powershell
npm run compile
npm test
```

`npm test` compiles the extension and runs lightweight Node tests for the AI verification status parser.

