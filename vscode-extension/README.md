# Visual Agent for VS Code

Visual Agent adds a local verification panel for projects that use the `visual-agent` Python package.

## Features

- Activity Bar view for recent passing and failing workflows.
- Status Bar summary for the current Visual Agent session.
- Commands to run all verification workflows, run affected workflows, and show the latest failure.
- Optional run-on-save for affected workflow checks.
- Placeholder commands for workflow generation and cloud connection.

## Requirements

Install Visual Agent in the workspace Python environment:

```powershell
pip install -e .[web,mcp,desktop]
```

The extension calls:

```powershell
python -m visual_agent.cli
```

Set `visualAgent.pythonPath` if your project uses a virtual environment.

## Development

```powershell
npm run compile
npm test
```

`npm test` compiles the extension and runs lightweight Node tests for the AI verification status parser.

## Settings

- `visualAgent.workspaceRoot`: workspace data directory, default `.agent-workspace`.
- `visualAgent.runOnSave`: run affected checks after file saves.
- `visualAgent.autoRunTags`: tags used for run-on-save checks.
- `visualAgent.pythonPath`: Python interpreter path.
