# Checkpoint For Coding Agents

Use this when giving Checkpoint to Codex, Claude Code, Cursor, VS Code agents, or any coding assistant that can run local shell commands.

## Role

Checkpoint is the local acceptance layer for code changes. It verifies product behavior after an agent edits code.

Do not treat Checkpoint as the coding assistant. The coding assistant writes or fixes code; Checkpoint runs repeatable local workflows and returns evidence.

## Startup

From the Checkpoint checkout:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Step all
checkpoint doctor
checkpoint workspace-status --root .agent-workspace
```

From the project being edited:

```powershell
checkpoint init --root .agent-workspace
checkpoint show-status --workspace-root .agent-workspace
checkpoint context-snapshot --workspace-root .agent-workspace --format markdown
```

Use one `.agent-workspace` per project.

## After Every Code Change

Run the project's normal tests first. Then run Checkpoint:

```powershell
checkpoint codex-check --workspace-root .agent-workspace --repo-root . --run-profile dry-run --format markdown
```

If the change touches a page, form, redirect, dashboard, data display, or user workflow, run:

```powershell
checkpoint verify-impl --task-description "Verify the changed user behavior" --workspace-root .agent-workspace --repo-root . --run-profile dry-run --no-untracked --format markdown
```

If the app URL is known, pass it explicitly:

```powershell
checkpoint verify-impl --task-description "Verify login redirects to dashboard" --base-url http://127.0.0.1:5173 --workspace-root .agent-workspace --repo-root . --run-profile dry-run --no-untracked --format markdown
```

## Existing Workflow Path

Prefer existing workflows when a stable product contract already exists:

```powershell
checkpoint workspace-run --root .agent-workspace --workflow <workflow-name-or-path> --run-profile dry-run --format markdown
```

Validate workflow quality before trusting a new draft:

```powershell
checkpoint workflow-lint --file .agent-workspace/workflows/<workflow>.yaml --format markdown
```

## How To Read Failures

When Checkpoint fails, inspect these fields first:

- `failed_step`: the workflow step that broke.
- `actual`: what Checkpoint observed.
- `fix_hint`: likely next repair direction.
- `quality gaps`: missing or weak assertions.
- `report_path`: full evidence trail.

Fix the product code when the workflow describes an intended user promise. Update the workflow only when the product requirement intentionally changed.

## Safety Rules

- Default to `dry-run`.
- Do not use `semi-auto` or `approved` unless the human explicitly allows real actions.
- Do not print, commit, or paste secrets, cookies, tokens, storage-state values, or private data.
- Do not share one `.agent-workspace` across unrelated projects.
- Use `--no-untracked` in large repositories unless untracked files are part of the task.

## MCP Order

When using MCP, call tools in this order:

1. `get_visual_status`
2. `get_workspace_dashboard`
3. `list_workflows`
4. `verify_workflow` or `run_verification`
5. `summarize_latest_failure`
6. `get_failure_details`

Use compact status and failure tools before reading full reports.
