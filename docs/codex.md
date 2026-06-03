# Visual Agent for Codex

Visual Agent gives Codex a local execution layer for workflows that should be
repeatable, permissioned, and auditable. Codex can write code and reason about
the task; Visual Agent can run local browser or desktop workflows and return
redacted reports.

## Generate The Brief

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli coding-agent-brief --client codex --workspace-root .agent-workspace --format markdown
```

Give the generated brief to Codex when you want it to use local workflows.

## Recommended Loop

1. Ask Codex to call `get_workspace_dashboard`.
2. Ask Codex to call `list_workflows`.
3. Run a workflow with `run_profile=dry-run`.
4. Read `get_run_report` before accepting the result.
5. If a run fails, call `get_latest_failure`, then `list_run_artifacts`.

## Safety Defaults

- Start with `dry-run`.
- Escalate only after human approval.
- Treat the report as the source of truth.
- Do not print cookies, tokens, passwords, storage state, or model credentials.
