# VS Code MCP Setup

Visual Agent exposes an MCP server that can be used by VS Code agent tooling.

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
