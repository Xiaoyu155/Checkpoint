# Checkpoint Quickstart

Checkpoint is a local-first acceptance layer for AI coding assistants. It runs
browser and desktop workflows after code changes, with permissions, audit
trails, screenshots, failure diagnostics, queues, and reports stored on your
machine.

## Install

Run the bootstrap script from the project root. It sets up the virtual
environment, installs dependencies, installs Playwright Chromium, initializes
`.agent-workspace`, writes example MCP client configs, then runs `doctor` and
an offline demo workflow as an onboarding smoke check.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

If you only want to rerun the final onboarding check:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Step smoke
```

To install manually into an existing virtual environment:

```powershell
pip install -e .[web,mcp]
python -m playwright install chromium
```

The product is Checkpoint. The preferred CLI command is `checkpoint`; the
legacy `visual-agent` command and `python -m visual_agent.cli` still work for
compatibility with existing scripts.

If you are connecting a coding assistant, start with
[for-coding-agents.md](for-coding-agents.md).

## First Three Commands

From a fresh checkout, use these commands in order to run a fixed contract
workflow:

```powershell
checkpoint init --root .agent-workspace
checkpoint workspace-run --root .agent-workspace --workflow local_html_form_workflow --inputs-file demo_login.json --run-profile dry-run --format markdown
checkpoint show-status --workspace-root .agent-workspace
```

This is the recommended loop for real work: write the page or workflow promises
as assertions, then rerun the same workflow after each code change.

`verify-impl` can draft or explore a workflow from git diff context. In large
repositories, use `--no-untracked` and pass an explicit app URL or fixture when
possible:

```powershell
checkpoint verify-impl --workspace-root .agent-workspace --task-description "Verify login redirects" --base-url http://127.0.0.1:5173 --run-profile dry-run --format markdown --no-untracked
checkpoint verify-impl --workspace-root .agent-workspace --task-description "Verify login fixture" --base-url fixtures/login_demo.html --run-profile dry-run --format markdown --no-untracked
```

## Verify Your Setup

```powershell
checkpoint doctor
```

Look for `"dom_browser": true` in the `perception` section. OCR and VLM are
optional - most workflows work without them.

## Run A Dry-Run Demo

```powershell
checkpoint workspace-run --root .agent-workspace --workflow local_html_form_workflow --inputs-file demo_login.json --format markdown
```

This opens a local HTML fixture and runs the workflow in dry-run mode. No
external service is contacted. Reports are written to
`.agent-workspace/reports/`.

## Multi-Project Workspaces

Use one workspace per project. Do not share one `.agent-workspace` across
unrelated repos or coding-agent windows.

```text
D:\project-a\.agent-workspace
D:\project-b\.agent-workspace
D:\project-c\.agent-workspace
```

From each project root:

```powershell
checkpoint init --root .agent-workspace
checkpoint show-status --workspace-root .agent-workspace
```

`show-status` shows the workspace root, project root, and current failure or
pass state so an agent can confirm it is using the right project before running
checks.

## Resume In A New Chat

Checkpoint stores the working context in the project workspace, not in the
chat window. After reopening Codex or starting a new chat, run:

```powershell
checkpoint context-snapshot --workspace-root .agent-workspace --format markdown
```

MCP clients should call `get_session_context` first. The snapshot shows recent
passes, latest failure, and the next suggested action.

## Fast Verification

Run only the workflow that protects the code you changed:

```powershell
python -m visual_agent.cli verify --workspace-root .agent-workspace --workflow checkout_verification --wait-lock --format markdown
```

Use broad tag verification when you really want the full local contract suite:

```powershell
python -m visual_agent.cli verify --workspace-root .agent-workspace --tags verification --max-workflows 10 --wait-lock --format markdown
```

Keep OCR/VLM workflows in their own tags, such as `visual` or `miniprogram`,
and run them explicitly. They are much slower than DOM/UIA/file checks because
they capture windows and extract visual text.

When a visual workflow brings a target window to the foreground, Checkpoint
minimizes that target window after capture and restores the previous foreground
window by default. Use `post_capture: keep` only when a workflow intentionally
needs to leave the target window open.

## Verification Loop Demo

This demo shows the core loop: write a verification workflow, make a breaking
change, let Checkpoint detect it, read the compact diagnosis, fix the code,
and confirm the green pass.

**Step 1 - run verification (all green):**

```powershell
python -m visual_agent.cli workspace-run --root .agent-workspace --workflow checkout_verification --run-profile dry-run
```

Expected: 7 steps, all `success`.

**Step 2 - simulate a developer breaking the UI copy:**

In `examples/web/checkout_verification_demo.html`, change:

```html
<button id="checkout-btn" ...>Proceed to Checkout</button>
```

to:

```html
<button id="checkout-btn" ...>Next Step</button>
```

**Step 3 - run verification (regression detected):**

```powershell
python -m visual_agent.cli workspace-run --root .agent-workspace --workflow checkout_verification --run-profile dry-run
```

Expected: step `assert_checkout_button` fails with
`Text not found in observation: Proceed to Checkout`.

**Step 4 - read the AI-ready diagnosis:**

```powershell
python -m visual_agent.cli context-snapshot --workspace-root .agent-workspace --format markdown
```

Output is under 500 tokens. It shows the failed step, the expected text, and
the actual visible text (`next step`) so you know exactly what to fix.

**Step 5 - fix the code and re-verify:**

Revert the button text to `Proceed to Checkout`, then run step 3 again.
Expected: 7 steps, all `success`. `context-snapshot` shows
`Status: 0 failing / 1 passing`.

## Path Convention for Workspace Workflows

When a workflow runs via `workspace-run`, the working directory is the
**workspace root** (e.g. `.agent-workspace/`). Paths in workflow YAML files
are relative to that directory.

- Reference a file one level up in the project:
  `path: ../examples/web/my_page.html`
- Reference a file inside the workspace:
  `path: fixtures/my_fixture.json`

When running with `run-workflow --file /absolute/path/to/workflow.yaml`, the
working directory is the **project root**, so use `examples/web/my_page.html`
directly.

## Inspect Reports

```powershell
python -m visual_agent.cli workspace-reports --root .agent-workspace
python -m visual_agent.cli workspace-report-detail --root .agent-workspace --run-id <run-id> --format markdown
```

## Connect an MCP Client

Generate the config snippet for your client:

```powershell
python -m visual_agent.cli mcp-client-config --workspace-root .agent-workspace --client cursor --format markdown
python -m visual_agent.cli mcp-client-config --workspace-root .agent-workspace --client claude-code --format markdown
```

Run the in-process smoke check before connecting:

```powershell
python -m visual_agent.cli mcp-smoke --workspace-root .agent-workspace --format markdown
```

Once connected, use `get_session_context` to resume work,
`summarize_latest_failure` for compact failure diagnosis, and
`run_verification` after code changes.

## Run Quality Checks

```powershell
python -m visual_agent.cli quality-gate --profile local --workspace-root .agent-workspace
python -m visual_agent.cli quality-gate --profile ci --workspace-root .agent-workspace --run --fail-on-secret-leak
```

