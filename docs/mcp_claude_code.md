# Claude Code MCP Setup

This guide connects Claude Code to Visual Agent through a local stdio MCP server.

## 1. Bootstrap Visual Agent

```powershell
cd "D:\longxia agent"
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

## 2. Add The MCP Server

Claude Code supports local stdio MCP servers with `claude mcp add`.

Use your actual checkout path:

```powershell
claude mcp add --transport stdio --scope local visual-agent -- "D:\longxia agent\.venv\Scripts\python.exe" -m visual_agent.mcp_server --workspace-root "D:\longxia agent\.agent-workspace"
```

If you prefer a project-scoped shared config, create `.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "visual-agent": {
      "command": "D:\\longxia agent\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "visual_agent.mcp_server",
        "--workspace-root",
        "D:\\longxia agent\\.agent-workspace"
      ],
      "cwd": "D:\\longxia agent",
      "env": {
        "PYTHONPATH": "D:\\longxia agent\\src"
      }
    }
  }
}
```

Project-scoped MCP configs may require approval when Claude Code loads them.

## 3. Verify In Claude Code

List configured servers:

```powershell
claude mcp list
```

Inside Claude Code, open:

```text
/mcp
```

Expected result:

- `visual-agent` appears as connected or pending approval.
- The server exposes workflow tools plus `get_session_context`, `summarize_latest_failure`, and `run_verification`.

## 4. Test A Workflow

Ask Claude Code:

```text
Use visual-agent to list workflows in D:\longxia agent\.agent-workspace.
```

Then:

```text
Use visual-agent to run local_html_form_workflow as a dry-run with inputs_file demo_login.json.
```

Expected result:

- The call returns a `run_id`.
- The report is available through `get_run_report`.
- The MCP call is audited in `.agent-workspace\gui\actions.jsonl`.
- If a report is too large, MCP returns a compact truncated response with a local report or artifact hint.

## Troubleshooting

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-smoke --workspace-root .agent-workspace --format markdown
claude mcp get visual-agent
```

If the server is pending, approve it from `/mcp`. If the server cannot start, check that the Python path, `cwd`, and `PYTHONPATH` all point at this checkout.

## References

- Claude Code MCP documentation: https://code.claude.com/docs/en/mcp
- Claude Code CLI reference: https://docs.anthropic.com/en/docs/claude-code/cli-usage
