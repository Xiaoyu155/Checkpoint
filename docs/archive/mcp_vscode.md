# VS Code MCP Setup

Checkpoint exposes an MCP server that can be used by VS Code agent tooling.

Generate the config from the repository root:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-client-config --client vscode --workspace-root .agent-workspace --format markdown
```

The important server fields are:

- `command`: the Python executable in this checkout.
- `args`: `["-m", "visual_agent.mcp_server"]`.
- `cwd`: the repository root.
- `env.PYTHONPATH`: the local `src` directory.

Keep the workspace local. Real execution remains gated by the workspace MCP
policy: `approved` runs require `workspace.json` approval and human intent.

Current MCP tools include:

- `list_workflows`
- `validate_workflow`
- `run_workflow`
- `get_run_report`
- `list_run_artifacts`
- `get_workspace_dashboard`
- `get_latest_failure`
- `summarize_latest_failure`
- `get_session_context`
- `run_verification`

Use `get_session_context` to resume work, `summarize_latest_failure` for a
compact failure diagnosis, and `run_verification` after code changes. Oversized
MCP responses are truncated and include metadata such as `truncated`,
`omitted_count`, or `report_hint`.

