# Visual Agent for VS Code

Visual Agent can be used from VS Code through MCP-compatible agent extensions
or by running the CLI in the integrated terminal.

## Generate A VS Code MCP Config

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-client-config --client vscode --workspace-root .agent-workspace --format markdown
```

The generated config is intended for a project-local `.vscode/mcp.json` style
setup. If your VS Code MCP extension uses a different config shape, keep the
same command, args, cwd, and `PYTHONPATH` values.

## Generate An Agent Brief

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli coding-agent-brief --client vscode --workspace-root .agent-workspace --format markdown
```

## Useful Agent Requests

```text
Use Visual Agent to inspect workspace health, list workflows, dry-run the
local form workflow, and summarize the report.
```

```text
Use Visual Agent to fetch the latest failed workflow report and explain the
recovery suggestion before editing code.
```

## Tools To Prefer

- `get_workspace_dashboard`
- `list_workflows`
- `validate_workflow`
- `run_workflow`
- `get_run_report`
- `get_latest_failure`
- `list_run_artifacts`
