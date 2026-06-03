# Visual Agent MCP Server

Turn AI assistant commands into auditable local workflows.

Playwright MCP gives you a browser remote control. Windows-MCP gives you a desktop remote control. Visual Agent MCP gives you a local workflow runtime with permissions, audit trails, reports, and failure recovery.

## What You Get

| Feature | Playwright MCP | Windows-MCP | Visual Agent MCP |
| --- | --- | --- | --- |
| Browser automation | yes | no | yes |
| Windows desktop UIA | no | yes | yes |
| Workflow YAML persistence | no | no | yes |
| dry-run / supervised / approved | no | no | yes |
| Screenshot and failure diagnosis | partial | partial | yes |
| Audit log for calls | no | no | yes |
| Regression test export | no | no | yes |
| Local-first execution | yes | yes | yes |

## Install

```powershell
.\.venv\Scripts\python.exe -m pip install -e .[mcp]
```

Before connecting a client, run the release check plan and MCP smoke tests:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli release-check --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-client-config --workspace-root .agent-workspace --client cursor --format json
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-smoke --workspace-root .agent-workspace --format markdown
.\.venv\Scripts\python.exe -m pytest tests\test_mcp_server.py tests\e2e\test_e2e_mcp.py -q
```

## Claude Desktop

Example config:

```json
{
  "mcpServers": {
    "visual-agent": {
      "command": "visual-agent-mcp",
      "args": []
    }
  }
}
```

For source checkouts, use `examples/mcp_config/claude_desktop_config.json` and set `PYTHONPATH` to this repository's `src` directory.

## Tools

Workflow tools:

- `list_workflows`: list available workflows and latest run status. Large lists are truncated with `omitted_count`.
- `validate_workflow`: run workflow validation and preflight checks without execution.
- `run_workflow`: run a workflow. Defaults to `dry-run`.
- `get_run_report`: return markdown or redacted JSON for a completed run. Large reports are truncated and include `report_hint`.
- `list_run_artifacts`: list reports, screenshots, downloads, and run artifacts under the workspace. Large lists are truncated with `omitted_count`.

AI context tools:

- `summarize_latest_failure`: return a compact latest-failure summary for coding agents.
- `get_session_context`: return a compact session snapshot for resuming work in a new chat.
- `run_verification`: run workflows tagged `verification` and return an AI-ready pass/fail report.

Compatibility and dashboard tools:

- `get_workspace_dashboard`: return workspace health, queue, reports, and quality status.
- `get_latest_failure`: return the latest failed workflow report and diagnosis. Prefer `summarize_latest_failure` when token budget matters.

## Response Budgets

MCP responses are designed for coding agents and are budgeted by default:

| Tool/output | Budget | Behavior when large |
| --- | --- | --- |
| `summarize_latest_failure` | about 400 tokens | Returns one compact failure summary |
| `get_session_context` | about 500 tokens | Returns a snapshot, not full reports |
| `run_verification` | about 800 tokens | Passed workflows are summarized by name |
| `get_run_report` | about 2000 tokens | Returns a truncated summary plus `report_hint` |
| Any MCP response | about 2000 tokens | Final fallback returns a compact summary |

Use `list_run_artifacts` to locate full local report files when a response is truncated.

## Safety Defaults

- `run_workflow` defaults to `dry-run`.
- `approved` is rejected unless the workflow is listed in `workspace.json` under `mcp.approved_workflows`.
- `mcp.max_run_profile` limits the highest profile MCP can use.
- MCP calls are audited in `gui/actions.jsonl` when `mcp.audit_all_calls` is true.
- Reports are scrubbed before JSON responses.
- Secret-like values are scrubbed before MCP output.
- Artifact paths must stay inside the workspace.
- `workspace_root` rejects `..` path traversal and must be under an allowed local root.
- MCP does not need cloud browser infrastructure; workflow reports, screenshots, queue data, auth-state metadata, and GUI action history stay under the local workspace.
- Secret-like strings in reports can be checked with `quality-gate --fail-on-secret-leak`.

Example `workspace.json` section:

```json
{
  "mcp": {
    "approved_workflows": [],
    "audit_all_calls": true,
    "max_run_profile": "supervised"
  }
}
```

## Example Prompts

- "List my workflows."
- "Validate the order entry workflow."
- "Run local_html_form_workflow as a dry-run."
- "Show the report for run 20260602-123456-abcd1234."
- "Show the workspace dashboard and summarize any attention items."
- "Fetch the latest failed workflow report and explain the recovery suggestion."
- "Call get_session_context and tell me the current Visual Agent state."
- "Summarize the latest failure without reading the full report."
- "Run verification workflows after my code change."

## Client Config Files

Ready-to-edit examples live under `examples/mcp_config/`:

- `claude_desktop_config.json`
- `cursor_mcp.json`

You can also generate a config for the current checkout:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-client-config --workspace-root .agent-workspace --client cursor --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-client-config --workspace-root .agent-workspace --client claude-desktop --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-client-config --workspace-root .agent-workspace --client vscode --format markdown
```

Keep the configured workspace path local and avoid pointing MCP clients at directories that contain real credentials unless the workflow policy and audit settings are already reviewed.
