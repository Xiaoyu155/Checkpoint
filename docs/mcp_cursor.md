# Cursor MCP Setup

This guide connects Cursor to Checkpoint through a local stdio MCP server.

## 1. Bootstrap Checkpoint

```powershell
cd "D:\longxia agent"
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

This writes:

```text
examples\mcp_config\cursor_mcp.json
```

## 2. Add Cursor MCP Config

For a project-local setup, create or update:

```text
.cursor\mcp.json
```

Copy the `mcpServers.visual-agent` entry from `examples\mcp_config\cursor_mcp.json`.

Example:

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

## 3. Reload Cursor

Reload the Cursor window or restart Cursor so it reads the MCP configuration.

## 4. Test The Server

In Cursor chat, ask:

```text
Use Checkpoint to validate local_html_form_workflow.
```

Then ask:

```text
Run local_html_form_workflow as a dry-run and show me the report id.
```

Expected result:

- Cursor sees the Checkpoint MCP tools.
- Validation returns `valid: true`.
- The run returns a `run_id`.
- A report is written under `.agent-workspace\reports`.
- Compact context tools such as `get_session_context`, `summarize_latest_failure`, and `run_verification` are available for coding-agent loops.
- Oversized reports and artifact lists return `truncated: true` with a local path hint instead of flooding the chat.

## Troubleshooting

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-smoke --workspace-root .agent-workspace --format markdown
.\.venv\Scripts\python.exe -m pytest tests\test_mcp_server.py -q
```

If Cursor does not show the tools, verify `.cursor\mcp.json`, restart Cursor, and confirm all paths are absolute.

## References

- Cursor MCP documentation: https://docs.cursor.com/context/model-context-protocol

