# Checkpoint for VS Code

Checkpoint can be used from VS Code through MCP-compatible agent extensions
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

## Verify Current Change

The bundled VS Code extension contributes `Checkpoint: Verify Current Change`.
It prompts for a task description and a base URL or fixture path, then runs
`verify-impl --format markdown`, refreshes the sidebar/status bar, and opens the
latest verification markdown.

Use dry-run first unless the workflow needs real browser actions.

## Useful Agent Requests

```text
Use Checkpoint to inspect workspace health, list workflows, dry-run the
local form workflow, and summarize the report.
```

```text
Use Checkpoint to fetch the latest failed workflow report and explain the
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
- `summarize_latest_failure`
- `get_session_context`
- `run_verification`

Prefer `get_session_context` when resuming work and `summarize_latest_failure`
when a failure summary is enough. Full reports and large lists are budgeted for
MCP output and may return `truncated: true` with a local report or artifact
hint.

