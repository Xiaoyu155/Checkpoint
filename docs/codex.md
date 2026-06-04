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

1. Ask Codex to call `get_session_context` when resuming an existing workspace.
2. Ask Codex to call `get_workspace_dashboard`.
3. Ask Codex to call `list_workflows`.
4. Run a workflow with `run_profile=dry-run`.
5. Read `get_run_report` before accepting the result.
6. If a run fails, call `summarize_latest_failure`, then `list_run_artifacts` if more detail is needed.
7. After code changes, call `run_verification` when verification workflows exist.

## Project Workspace Rule

Use one `.agent-workspace` per product or project. Do not share one workspace
across unrelated projects or Codex windows.

```text
D:\product-a\.agent-workspace
D:\product-b\.agent-workspace
D:\product-c\.agent-workspace
```

Before running workflows in a new Codex chat, confirm the workspace belongs to
the current project:

```powershell
python -m visual_agent.cli workspace-status --root .agent-workspace
```

The output includes `root` and `project_root`. If `project_root` is not the
project Codex is editing, stop and initialize the correct workspace.

## Resume After Reopening Codex

Codex chat memory is not the source of truth. Visual Agent stores pass/fail
state, latest failure, reports, screenshots, and audit data inside the project
workspace. In a new Codex chat, start with:

```powershell
python -m visual_agent.cli context-snapshot --workspace-root .agent-workspace --format markdown
```

MCP clients should call `get_session_context` first. If there is a failure, call
`summarize_latest_failure` before reading full reports.

## Fast Verification

Prefer targeted verification while coding:

```powershell
python -m visual_agent.cli verify --workspace-root .agent-workspace --workflow <workflow_name> --wait-lock --format markdown
```

Use broad tag verification only when you intentionally want the full local
contract suite:

```powershell
python -m visual_agent.cli verify --workspace-root .agent-workspace --tags verification --max-workflows 10 --wait-lock --format markdown
```

Keep slow visual/OCR workflows under explicit tags such as `visual`,
`desktop`, or `miniprogram` and run them when the changed code needs them.

## Visual Desktop Behavior

Visual Agent uses a global visual lock for screen/OCR/VLM/UIA foreground
operations. Multiple projects can run non-visual checks in parallel, but visual
steps are serialized so agents do not fight over the same physical desktop.

When a visual step uses `bring_to_front: true`, Visual Agent foregrounds the
target window, captures evidence, minimizes that target window by default, and
restores the previous foreground window.

To intentionally keep the target window open:

```yaml
window:
  title_contains: "Target App"
  bring_to_front: true
  post_capture: keep
```

## Keyboard Actions

`press_key` does not require a target. It is useful for global keyboard actions:

```yaml
- id: submit
  action: press_key
  keys: enter
```

`press_key`, `click`, `type`, and `paste` are mutating actions. Under
`dry-run`, they report what would happen without touching the desktop.

## Safety Defaults

- Start with `dry-run`.
- Escalate only after human approval.
- Treat the report as the source of truth.
- Do not print cookies, tokens, passwords, storage state, or model credentials.
- Prefer compact context tools over full reports when the goal is to continue coding.
