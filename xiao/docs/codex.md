# Checkpoint for Codex

Checkpoint gives Codex a local execution layer for workflows that should be
repeatable, permissioned, and auditable. Codex can write code and reason about
the task; Checkpoint can run local browser or desktop workflows and return
redacted reports.

## Generate The Brief

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli coding-agent-brief --client codex --workspace-root .agent-workspace --format markdown
```

Give the generated brief to Codex when you want it to use local workflows.
The first AGENTS.md rule should be: read `.visual-agent-status.md` for current
verification state before planning fixes.

## Connect Codex

From the project root, initialize the workspace and write Codex instructions:

```powershell
python -m visual_agent.cli connect codex --workspace-root .agent-workspace
```

For MCP clients, connect the platform directly:

```powershell
python -m visual_agent.cli connect claude-code --workspace-root .agent-workspace
python -m visual_agent.cli connect cursor --workspace-root .agent-workspace
```

## Recommended Loop

1. Ask Codex to call `get_session_context` when resuming an existing workspace.
2. Ask Codex to call `get_workspace_dashboard`.
3. Ask Codex to call `list_workflows`.
4. Run a workflow with `run_profile=dry-run`.
5. Read `get_run_report` before accepting the result.
6. If a run fails, call `summarize_latest_failure`, then `list_run_artifacts` if more detail is needed.
7. After code changes, call `run_verification` when verification workflows exist.
8. When no workflow exists for a UI change, call `generate_workflow_from_context` or run `verify-impl` so Checkpoint generates a workflow from the current code diff.

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
python -m visual_agent.cli show-status --workspace-root .agent-workspace
```

The output includes the workspace root, project root, and the latest failure
or pass state. If `project_root` is not the project Codex is editing, stop and
initialize the correct workspace.

## Resume After Reopening Codex

Codex chat memory is not the source of truth. Checkpoint stores pass/fail
state, latest failure, reports, screenshots, and audit data inside the project
workspace. In a new Codex chat, start with:

```powershell
python -m visual_agent.cli context-snapshot --workspace-root .agent-workspace --format markdown
```

MCP clients should call `get_session_context` first. If there is a failure, call
`summarize_latest_failure` before reading full reports.

For the fastest local check, read the project root `.visual-agent-status.md` or
run:

```powershell
python -m visual_agent.cli show-status --workspace-root .agent-workspace --format markdown
```

## Fast Verification

Prefer targeted verification while coding:

```powershell
python -m visual_agent.cli verify --workspace-root .agent-workspace --workflow <workflow_name> --wait-lock --format markdown
```

For Codex, prefer the git-diff-aware check. It reads changed files, selects
workflows whose `affects` paths overlap, skips `slow` workflows by default, and
returns compact output:

```powershell
python -m visual_agent.cli codex-check --workspace-root .agent-workspace
```

When Codex has just implemented a new UI flow and no existing workflow covers
it, generate from the code diff:

```powershell
python -m visual_agent.cli generate-from-diff --workspace-root .agent-workspace --task-description "Verify login redirects to dashboard" --base-url http://localhost:3000/login --dry-run
```

For the one-call implementation loop, use:

```powershell
python -m visual_agent.cli verify-impl --workspace-root .agent-workspace --task-description "Verify login redirects to dashboard" --base-url http://localhost:3000/login --run-profile dry-run --timeout-seconds 30
```

`verify-impl` generates a workflow from git diff, scores its assertion quality,
blocks weak workflows by default below `0.6`, runs the generated workflow when
quality is acceptable, and writes `.vscode-agent-status.json` for the VS Code
extension.

To include visual/OCR-heavy contracts:

```powershell
python -m visual_agent.cli codex-check --workspace-root .agent-workspace --include-slow
```

Use broad tag verification only when you intentionally want the full local
contract suite:

```powershell
python -m visual_agent.cli verify --workspace-root .agent-workspace --tags verification --max-workflows 10 --wait-lock --format markdown
```

Keep slow visual/OCR workflows under explicit tags such as `visual`,
`desktop`, or `miniprogram` and run them when the changed code needs them.

To make diff-aware selection precise, add `affects` to workflows:

```yaml
name: checkout_verification
tags: [verification]
affects:
  - src/payment/
  - templates/checkout.html
```

## Real Interaction Checks

Codex must not treat unit tests as a substitute for product interaction. When a
task changes UI, forms, navigation, checkout, auth, or visible copy, run at
least one Checkpoint workflow that actually observes and operates the UI.

Use `dry-run` first to validate selectors and safety, then use `supervised` for
real low/medium-risk clicks and typing:

```powershell
python -m visual_agent.cli run-workflow --file examples/workflows/form-fill/browser_form_workflow.yaml --inputs-file examples/inputs/demo_login.json --run-profile dry-run
python -m visual_agent.cli run-workflow --file examples/workflows/form-fill/browser_form_workflow.yaml --inputs-file examples/inputs/demo_login.json --run-profile supervised
```

For browser pages, prefer `observe_browser` plus semantic targets. Checkpoint
captures the DOM, finds controls by label/text/role/selector, and executes
native Playwright `click`/`fill` actions. This is faster and more reliable than
OCR for web UI:

```yaml
- id: observe
  action: observe_browser
  url: http://localhost:3000
- id: fill_name
  action: paste
  target:
    label: 用户名
    role: input
  value: demo
- id: submit
  action: click
  target:
    text: 登录
    role: button
- id: reread
  action: observe_browser
  reuse_page: true
- id: assert_result
  action: assert_product_contract
  required_sections: ["首页"]
  no_error_state: true
```

For desktop, mini program simulators, or canvas-like UI where DOM is not
available, use OCR-based actions:

```yaml
- id: buy
  action: click_text
  text: 购买服务
  window_title_candidates: ["微信开发者工具", "Chrome"]
- id: fill
  action: paste
  target:
    text: 输入框
    preferred: [ocr]
  value: demo
```

Window title aliases such as `window_title_contains` and
`window_title_candidates` are treated as target-window capture parameters.
Checkpoint foregrounds the target, captures it, minimizes it after capture,
and restores the previously active window unless `post_capture: keep` is set.

## Visual Desktop Behavior

Checkpoint uses a global visual lock for screen/OCR/VLM/UIA foreground
operations. Multiple projects can run non-visual checks in parallel, but visual
steps are serialized so agents do not fight over the same physical desktop.

When a visual step uses `bring_to_front: true`, Checkpoint foregrounds the
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

