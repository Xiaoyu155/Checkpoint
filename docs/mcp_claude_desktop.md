# Claude Desktop MCP Setup

This guide connects Claude Desktop to Visual Agent through a local stdio MCP server.

## 1. Bootstrap Visual Agent

```powershell
cd "D:\longxia agent"
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

This writes a ready-to-edit config at:

```text
examples\mcp_config\claude_desktop_config.json
```

## 2. Open Claude Desktop Config

In Claude Desktop, open the developer settings and choose the option to edit the MCP configuration file. On Windows this is commonly named:

```text
claude_desktop_config.json
```

If Claude Desktop opens an existing JSON file, merge the `mcpServers.visual-agent` entry from `examples\mcp_config\claude_desktop_config.json` into it.

Example shape:

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

Use your actual checkout path.

## 3. Restart Claude Desktop

Fully quit and reopen Claude Desktop so it reloads MCP servers.

## 4. Test The Connection

Ask Claude Desktop:

```text
Use visual-agent to list my workflows.
```

Expected result:

- Claude Desktop detects the `visual-agent` MCP server.
- The available tools include `list_workflows`, `validate_workflow`, `run_workflow`, `get_run_report`, `list_run_artifacts`, `get_workspace_dashboard`, `get_latest_failure`, `summarize_latest_failure`, `get_session_context`, and `run_verification`.
- `list_workflows` returns at least `local_html_form_workflow`.

## 5. Safety Notes

- `run_workflow` defaults to `dry-run`.
- `approved` requires `workspace.json` `mcp.approved_workflows`.
- Reports are scrubbed before MCP output.
- Oversized MCP responses are truncated and include truncation metadata.
- MCP calls are audited under `.agent-workspace\gui\actions.jsonl`.
- Keep `workspace_root` pointed at a local workspace you trust.

## Troubleshooting

- Run `.\.venv\Scripts\python.exe -m visual_agent.cli mcp-smoke --workspace-root .agent-workspace --format markdown`.
- Confirm `PYTHONPATH` points to this checkout's `src` directory.
- Confirm `command` points to `.venv\Scripts\python.exe`.
- Re-run `powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Step mcp-config`.

## References

- Claude Desktop local MCP setup: https://support.anthropic.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop
- Model Context Protocol local server tutorial: https://modelcontextprotocol.io/docs/tutorials/use-local-mcp-server
