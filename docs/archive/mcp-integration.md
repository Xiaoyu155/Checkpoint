# MCP Integration

Checkpoint exposes local workflow automation to MCP clients through:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.mcp_server
```

The server is local-first. It reads and writes artifacts under the configured `.agent-workspace` and returns redacted, budgeted payloads for coding agents.

## Agent Startup Order

Before asking an agent to run or fix workflows, give it this startup order:

1. Read `.visual-agent-status.md` if it exists.
2. Run `context-snapshot` for compact current state.
3. Use MCP tools in this order: `get_visual_status`, `get_workspace_dashboard`, `list_workflows`, then `verify_workflow`, `run_verification`, or `run_workflow`.

CLI equivalents:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli context-snapshot --workspace-root .agent-workspace --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli show-status --workspace-root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-smoke --workspace-root .agent-workspace --format markdown
```

## Copy-Ready Config

Use absolute paths in real client config. Replace `D:\\path\\to\\project` with your project path.

### Cursor

`.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "visual-agent": {
      "command": "D:\\path\\to\\project\\.venv\\Scripts\\python.exe",
      "args": ["-m", "visual_agent.mcp_server"],
      "cwd": "D:\\path\\to\\project"
    }
  }
}
```

### Claude Code

Project-scoped command:

```powershell
claude mcp add visual-agent --scope project --cwd D:\path\to\project -- D:\path\to\project\.venv\Scripts\python.exe -m visual_agent.mcp_server
```

Equivalent JSON:

```json
{
  "mcpServers": {
    "visual-agent": {
      "command": "D:\\path\\to\\project\\.venv\\Scripts\\python.exe",
      "args": ["-m", "visual_agent.mcp_server"],
      "cwd": "D:\\path\\to\\project"
    }
  }
}
```

### VS Code

`.vscode/mcp.json`:

```json
{
  "servers": {
    "visual-agent": {
      "type": "stdio",
      "command": "D:\\path\\to\\project\\.venv\\Scripts\\python.exe",
      "args": ["-m", "visual_agent.mcp_server"],
      "cwd": "D:\\path\\to\\project"
    }
  }
}
```

## Core Tools

- `list_workflows`: discover workspace workflows.
- `validate_workflow`: validate workflow YAML and preflight requirements.
- `run_workflow`: run a workflow, defaulting to `dry-run`.
- `verify_workflow`: run one workflow and return pass/fail with `structured_failure`.
- `get_run_report`: return a completed report.
- `list_run_artifacts`: list screenshots and run artifacts.
- `get_workspace_dashboard`: compact workspace health.
- `get_latest_failure`: latest failed report.
- `summarize_latest_failure`: token-efficient failure summary.
- `get_failure_details`: latest `StructuredFailure` JSON.
- `get_visual_status`: structured `.visual-agent-status.md`.
- `run_verification`: run verification-tagged workflows.
- `generate_workflow`: generate workflow YAML from a description.

Recommended first tools for coding agents:

- `get_visual_status`
- `get_workspace_dashboard`
- `list_workflows`
- `verify_workflow`
- `run_verification`
- `summarize_latest_failure`
- `get_failure_details`

## Safety Defaults

- `run_workflow` defaults to `dry-run`.
- `approved` runs require explicit workspace policy.
- Reports and MCP payloads are scrubbed for secrets.
- Artifact paths are constrained to the workspace.
- Long reports are compacted with truncation metadata.

## Client Guides

- [Codex](codex.md)
- [Claude Code](mcp_claude_code.md)
- [Claude Desktop](mcp_claude_desktop.md)
- [Cursor](mcp_cursor.md)
- [VS Code](mcp_vscode.md)

