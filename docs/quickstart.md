# Visual Agent Quickstart

Visual Agent is a local-first automation runtime for AI assistants. It runs
browser and desktop workflows with permissions, audit trails, screenshots,
failure diagnostics, queues, and reports stored on your machine.

## Install

Run the bootstrap script from the project root. It sets up the virtual
environment, installs dependencies, installs Playwright Chromium, initialises
`.agent-workspace`, and writes example MCP client configs.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

To install manually into an existing virtual environment:

```powershell
pip install -e .[web,mcp]
python -m playwright install chromium
python -m visual_agent.cli init-workspace --root .agent-workspace
```

## Verify Your Setup

```powershell
python -m visual_agent.cli doctor
```

Look for `"dom_browser": true` in the `perception` section. OCR and VLM are
optional - most workflows work without them.

## Run A Dry-Run Demo

```powershell
python -m visual_agent.cli workspace-run --root .agent-workspace --workflow local_html_form_workflow --inputs-file demo_login.json
```

This opens a local HTML fixture and runs the workflow in dry-run mode. No
external service is contacted. Reports are written to
`.agent-workspace/reports/`.

## Verification Loop Demo

This demo shows the core loop: write a verification workflow, make a breaking
change, let Visual Agent detect it, read the compact diagnosis, fix the code,
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
