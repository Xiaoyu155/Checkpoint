# Coding Agents

Checkpoint is useful to coding agents because it gives them a local execution
surface with durable workflows, explicit permission profiles, and auditable
reports. The agent can reason about the task, while Checkpoint performs the
browser or desktop workflow and records what happened.

## Generate The Brief

Run this from the repository root:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli coding-agent-brief --client codex --workspace-root .agent-workspace --format markdown
```

Use `--client claude-code`, `--client cursor`, or `--client vscode` for
client-specific wording and MCP config shape.

## What The Agent Should Do

1. Read the coding agent brief.
2. Connect the MCP server using the generated config.
3. Run `mcp-smoke` before using the tools for real work.
4. Prefer existing workflows over ad hoc browser actions.
5. Run workflows as `dry-run` unless a human explicitly approves escalation.
6. Use `get_session_context` when resuming work in a new chat.
7. Read `get_run_report` before claiming success when a specific run id matters.
8. Use `summarize_latest_failure` before reading a full failed report.
9. Use `run_verification` after code changes when verification-tagged workflows exist.
10. Use `get_workspace_dashboard` before and after risky changes.

## Useful Prompts

```text
Use visual-agent to list workflows, run local_html_form_workflow as a dry-run,
then summarize the report.
```

```text
Use visual-agent to validate every workflow before suggesting changes.
```

```text
If a workflow fails, use get_run_report and list_run_artifacts before editing
code.
```

```text
If a workflow fails, use summarize_latest_failure first. Only read the full
report if the summary does not contain enough detail.
```

```text
After changing code, run verification workflows and summarize the pass/fail
report.
```

```text
Use visual-agent to get the workspace dashboard, find the latest failed run,
and explain the failure diagnosis.
```

## Safety Rules

- Treat missing auth state as a blocker.
- Do not bypass login or scrape protected data.
- Do not print secrets from inputs, cookies, tokens, or model credentials.
- Do not request `approved` run_profile unless the workspace policy and the
  human both allow it.
- Treat `truncated: true` as a signal to use `list_run_artifacts` or a more
  specific tool rather than asking for a larger MCP response.
- Use the run report as the source of truth.

