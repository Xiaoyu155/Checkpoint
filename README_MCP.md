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

## Local License Metadata

Visual Agent remains local-first by default. Paid feature boundaries are visible through `visual_agent.licensing.check_feature()`, but `require_feature()` is still non-blocking while cloud billing and remote validation are inactive.

For development and future activation testing, local license metadata can be provided with environment variables:

```powershell
$env:VISUAL_AGENT_LICENSE_TIER = "pro"        # free | pro | team | enterprise
$env:VISUAL_AGENT_LICENSE_SEATS = "3"
$env:VISUAL_AGENT_LICENSE_EXPIRES_AT = "4102444800"
$env:VISUAL_AGENT_LICENSE_KEY = "..."
```

Or with a JSON file at `%USERPROFILE%\.visual-agent\license.json`, `$env:VISUAL_AGENT_HOME\license.json`, or `$env:VISUAL_AGENT_LICENSE_FILE`:

```json
{
  "tier": "pro",
  "seats": 2,
  "expires_at": 4102444800,
  "license_key": "..."
}
```

Expired licenses are treated as `free` for feature checks. License keys are only tracked as present/absent in local metadata.

Local usage counters are stored in `.agent-workspace/agent_session.json` and are visible in `context-snapshot`, `get_session_context`, and:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli usage-status --workspace-root .agent-workspace --format markdown
```

`usage-status` reports local runs this month, cloud runs used, cloud run quota/remaining, the usage reset month, current local license tier, and feature access booleans. It does not print license keys or workflow inputs.

Cloud workflow execution is explicit. The `run_remote_workflow()` API records `cloud_runs_used` only after an injected/remote client returns `status: success`; failed remote attempts do not consume cloud usage. Free tier has 5 cloud runs per month; once exceeded, cloud execution returns `status: upgrade_required` before network traffic. Pro/team/enterprise tiers have unlimited cloud runs.

Remote configuration readiness is local-only and does not probe the network. Set `VISUAL_AGENT_CLOUD_ENDPOINT`, `VISUAL_AGENT_CLOUD_API_KEY`, and optionally `VISUAL_AGENT_CLOUD_ORG`; `usage-status` reports endpoint, org, key-present status, blockers, and `network_probe: not_run` without printing the key.

`usage-status --format json` also includes `remote_request_preview`, a dry-run request shape for future cloud execution. It includes workflow metadata, run profile, cloud readiness, and redacted input summaries only; it does not send the request.

`remote_client_from_env()` is available as a testable adapter factory. Without an explicit transport it returns a blocked diagnostic and does not send network traffic. With an injected transport, responses are filtered down to `status`, `run_id`, `report_url`, and `message`; successful responses are the only path that records `cloud_runs_used`.

Use `cloud-run-plan` to preview the future remote request and adapter diagnostic without reading inputs or sending traffic:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli cloud-run-plan --workspace-root .agent-workspace --workflow checkout --inputs-file checkout.json --format markdown
```

Use `cloud-run` for the same safe default flow. Without `--execute`, it only prints the plan and adapter diagnostic. HTTP execution requires both `--execute` and `--transport http`, plus ready `VISUAL_AGENT_CLOUD_ENDPOINT` and `VISUAL_AGENT_CLOUD_API_KEY`. Missing config blocks before network traffic; HTTP failures, 4xx/5xx responses, invalid JSON responses, and non-success remote statuses do not record `cloud_runs_used`. Optional retries apply only to 429 and 5xx responses. Remote response output is compacted to local `schema_version`, `remote_schema_version`, `status`, `run_id`, `report_url`, and redacted `message`.

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli cloud-run --workspace-root .agent-workspace --workflow checkout --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli cloud-run --workspace-root .agent-workspace --workflow checkout --execute --transport http --timeout-seconds 30 --max-retries 1 --format markdown
```

Workspace report history is also tier gated. Free tier can query reports from the last 7 days; older reports are filtered from workspace report lists/indexes and detail/artifact queries return `status: upgrade_required` with `reason: history_window_exceeded`. Pro/team/enterprise tiers can query unlimited report history.

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
- `generate_workflow_from_context`: generate a verification workflow from changed code. If `code_changes` is omitted, Visual Agent reads git diff from `repo_root`.
- `verify_implementation`: generate a workflow from changed code, check generated workflow quality, run it, and return `pass`, `fail`, `needs_workflow_improvement`, or `timeout`.

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
| `generate_workflow_from_context` | about 500 tokens | Returns quality score, gaps, and workflow path; YAML only for dry runs |
| `verify_implementation` | about 800 tokens | Returns pass/fail/timeout with quality and compact diagnosis |
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
- "Generate a workflow from my current git diff with base_url=http://localhost:3000/login."
- "Verify this implementation from git diff with run_profile=dry-run and timeout_seconds=30."

## Code-Context Verification

After an AI coding assistant changes UI code, prefer the context-aware loop:

1. Call `generate_workflow_from_context` to inspect the generated workflow quality before execution.
2. Call `verify_implementation` to generate, quality-gate, run, and diagnose in one step.

Useful arguments:

- `workspace_root`: Visual Agent workspace, usually `.agent-workspace`.
- `task_description`: the implementation task being verified.
- `base_url`: app URL or local fixture path used as the workflow entry point.
- `code_changes`: optional explicit changed files. If omitted, Visual Agent reads git diff from `repo_root`.
- `repo_root`: git repository root for automatic diff collection.
- `min_quality_score`: default `0.6`; lower only when you intentionally accept a weak workflow.
- `timeout_seconds`: default `30`; returns `result: timeout` when exceeded.

`generate_workflow_from_context` and `verify_implementation` include `semantic_summary` with parser confidence, generation method, extracted field counts, required/sensitive field counts, validation-rule counts, success-state counts, parsed data-display names, matched/unmatched data-display names, and parse warnings. Static code-context parsing covers HTML, React/JSX, Vue, Django, FastAPI, Flask, Next.js App Router patterns such as `redirect()`, `permanentRedirect()`, `useRouter().push()`, and server-action forms, Remix route actions with `<Form>` and `redirect()`, and SvelteKit forms with `goto()`, `redirect()`, and `fail()` patterns. Responses also include a short `generation_trace` array that explains the main mapping decisions, such as fields becoming `paste input.<field>` steps, redirects becoming URL waits, error texts becoming forbidden assertions, and unmatched displays staying diagnostic-only.

When parsed template variables match submitted non-sensitive input fields, including nested expressions such as `profile.displayName` matching `displayName`, static synthesis adds `text_from: input.<field>` assertions so generated workflows verify that submitted values are rendered back to the UI. Unmatched displays stay in `semantic_summary` for diagnostics but do not generate assertions.

When parsed validation rules are available, generation also returns draft-only `negative_input_cases` with invalid inputs for rules such as required, email format, min/max, min/max length, and simple patterns. Generation also produces a separate draft negative workflow (`negative_workflow_yaml` in dry-run responses, or `negative_workflow_path` when saved) plus `negative_workflow_ready`, `negative_workflow_reason`, `negative_workflow_reset_strategy`, and redacted `negative_oracles` with parsed text/source. This draft is not added to the default success workflow. The current reset strategy is `fresh_observe_per_case`: every negative case starts with a fresh `observe_browser` on the entry URL. `verify_implementation` runs it only when `run_negative` / `--run-negative` is explicitly set, after the success-path workflow passes. Negative reports include `next_action`, redacted `oracles`, and, when a run exists, `report_path`, `report_markdown_path`, and `report_hint`; if no parsed error oracle exists, negative verification returns `skipped` with `reason: no_negative_oracle`. Error oracle extraction ignores success-like text that merely contains error keywords. Sensitive fields use empty safe values rather than example secrets.

When parsed code exposes known error messages, static synthesis adds a forbidden-text `assert_text_contract` after the success path so the generated workflow checks that successful UI states do not still show those errors.

Workflow quality payloads include assertion diagnostics for generated workflows: `data_display_assertions`, `forbidden_error_assertions`, `text_from_input_references`, and `invalid_text_from_references`. `assert_no_error` still counts as an error-path safety check, but it receives less scoring weight than explicit forbidden/error text assertions.

Saved generated workflows also get an input template. Non-sensitive examples are shaped by basic parsed validation rules such as min/max, min/max length, and simple fixed-length patterns; sensitive fields stay empty. When `verify_implementation` is called without explicit `inputs`, it automatically loads this generated template and returns `inputs_path` plus `inputs_source` so clients can see which input source was used.

`verify_implementation` writes `.vscode-agent-status.json` under the workspace so the VS Code extension can show the latest AI verification result, including semantic summary, quality gaps, recommendations, timeout state, pass/fail status, report paths, report hint, negative verification summary, and the next action. The extension's output panel and sidebar show negative status/reason/reset/oracle/report details when `negative_verification` is present.

To inspect the same status from a terminal:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli agent-status --workspace-root .agent-workspace --format markdown
```

For a one-command local demo of code-context generation, dry-run implementation verification, and status markdown output:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\code_context_verify_demo.ps1
```

The e2e suite also includes real frontend-style code-context dry-run coverage for Next.js, React component/form-table, React list-row delete confirmation, Vue, and Remix samples.

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
