# Checkpoint

[![CI](https://github.com/Xiaoyu155/Checkpoint/actions/workflows/ci.yml/badge.svg)](https://github.com/Xiaoyu155/Checkpoint/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

AI coding assistants can write code. Checkpoint verifies whether the product still works.

Checkpoint is a local-first verification layer for Codex, Claude Code, Cursor, VS Code, Claude Desktop, and other MCP clients. It gives agents repeatable workflows, safe execution profiles, reports, screenshots, audit trails, and machine-readable failure diagnostics after code changes.

**AI writes the change. Checkpoint runs the acceptance check.**

## Why Checkpoint

AI coding assistants are fast, but speed creates a new quality gap: they can finish a patch without proving the UI, workflow, or product promise still works. Checkpoint closes that gap by turning acceptance checks into local, repeatable workflows an agent can run and understand.

Use it when you want an agent to verify:

- Login, forms, redirects, checkout, dashboards, and data displays.
- Exact UI copy, success states, error states, and no-regression contracts.
- Browser DOM flows first, then Windows UIA, OCR, or visual fallback when needed.
- Failure evidence that is useful to the next repair attempt, not just a raw terminal log.

## 60-Second Start

```powershell
git clone https://github.com/Xiaoyu155/Checkpoint.git
cd Checkpoint
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Step all
.\.venv\Scripts\checkpoint.exe workspace-run --root .agent-workspace --workflow local_html_form_workflow --inputs-file demo_login.json --run-profile dry-run --format markdown
```

For AI coding assistants, copy [docs/for-coding-agents.md](docs/for-coding-agents.md) into the agent context.

## Example Failure Signal

When a page regression happens, Checkpoint returns structured evidence an agent can act on:

```text
[verify-impl] Result: fail
[verify-impl] Failed at assert_checkout_button (assert_text)
  Actual: Text not found in observation: Proceed to Checkout
  Fix: Check the changed UI copy or update the workflow contract intentionally.
```

See [docs/demo-output.md](docs/demo-output.md) for real CLI output and [docs/failure-demo.md](docs/failure-demo.md) for the full local demo.

## What It Does

- Runs YAML workflows with validation and preflight checks.
- Uses structured providers first: DOM, Windows UIA, OCR, then VLM fallback.
- Defaults to safe execution through `dry-run`, `supervised`, and `approved` profiles.
- Stores reports, screenshots, queue state, auth-state metadata, and GUI action history locally.
- Exposes high-level workflow tools through MCP.
- Provides CLI, Tkinter GUI, queue worker, regression export, and quality gates.

## What It Is Not

Checkpoint is not a browser toy, screenshot demo, or replacement for unit tests. It is the acceptance layer that runs after code changes and before you trust the result.

## What Works Out of the Box

After running `bootstrap.ps1`, the following are ready with no extra configuration:

| Capability | Status | Notes |
| --- | --- | --- |
| DOM browser automation | **Ready** | Playwright Chromium installed by bootstrap |
| YAML workflow execution | **Ready** | dry-run, supervised, approved profiles |
| MCP server | **Ready** | Codex, Claude Code, Cursor, VS Code, Claude Desktop |
| Run reports and audit logs | **Ready** | Screenshots, failure diagnosis, queue |
| Windows UIA desktop automation | **Ready** | Windows only, no extra install needed |

The following require additional setup:

| Capability | Status | How to Enable |
| --- | --- | --- |
| Visual fallback (VLM) — cloud | Needs API key | Add key to `model_api_keys.txt`, see [VLM setup](docs/vlm_setup.md) |
| Visual fallback (VLM) — local | Needs model | `pip install torch transformers` + download model |
| OCR text extraction | Needs Tesseract | `pip install pytesseract` + install Tesseract binary |

**Practical note:** Most browser and desktop automation workflows work without VLM or OCR. VLM is only used as a fallback when DOM and UIA selectors cannot locate a target — which is uncommon for well-structured pages. Check your setup with:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli doctor
```

Look for the `perception` section in the output to see which providers are active on your machine.

## Install

From PyPI after release:

```powershell
pip install visual-agent
python -m playwright install chromium
```

From a source checkout on Windows:

```powershell
git clone https://github.com/Xiaoyu155/Checkpoint.git
cd Checkpoint
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Step all
```

The bootstrap script checks Python, creates or reuses `.venv`, installs core dependencies, installs `[web,mcp]` extras, installs Playwright Chromium into `.pw-browsers`, initializes `.agent-workspace`, writes MCP client config examples, and runs a local onboarding smoke check.

Checkpoint's preferred CLI command is `checkpoint`. The legacy command `visual-agent` and `python -m visual_agent.cli` remain supported because the Python package is still named `visual-agent`.

## Quick Start

From a source checkout, create a workspace and run a fixed contract workflow:

```powershell
.\.venv\Scripts\checkpoint.exe init --root .agent-workspace
.\.venv\Scripts\checkpoint.exe workspace-run --root .agent-workspace --workflow local_html_form_workflow --inputs-file demo_login.json --run-profile dry-run --format markdown
.\.venv\Scripts\checkpoint.exe show-status --workspace-root .agent-workspace
```

That is the recommended production loop: encode product promises in a workflow, then rerun that workflow after each code change.

`verify-impl` is useful for drafting or exploring a workflow from git diff context. In large repositories, prefer `--no-untracked` and pass an explicit app URL or fixture when possible:

```powershell
.\.venv\Scripts\checkpoint.exe verify-impl --workspace-root .agent-workspace --task-description "Verify login redirects" --base-url http://127.0.0.1:5173 --run-profile dry-run --format markdown --no-untracked
.\.venv\Scripts\checkpoint.exe verify-impl --workspace-root .agent-workspace --task-description "Verify login fixture" --base-url fixtures/login_demo.html --run-profile dry-run --format markdown --no-untracked
```

For real work, use one `.agent-workspace` per project. Multiple Codex/Cursor windows can use Checkpoint at the same time as long as each window points at its own project workspace.

For fast checks, target the relevant workflow instead of running every visual
contract:

```powershell
.\.venv\Scripts\checkpoint.exe verify --workspace-root .agent-workspace --workflow checkout_verification --wait-lock --format markdown
```

Run the supervised browser demo path with real Playwright fill/click actions:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli demo-workspace-check --root .agent-workspace --overwrite --run-profile supervised --format markdown
```

### Verification Loop

Checkpoint's core value is detecting regressions automatically after code
changes. After the dry-run demo passes, try the verification loop:

1. Run `workspace-run --workflow checkout_verification` - all green.
2. Change one button label in `examples/web/checkout_verification_demo.html`.
3. Run again - Checkpoint reports the exact text mismatch in the failing step.
4. Read `context-snapshot` - a <= 500-token summary tells you what changed and
   where to look.
5. Fix the label, run again - green.

See [docs/quickstart.md](docs/quickstart.md) for the full walkthrough.
## Example Workflows

Public starter workflows live under [`workflows/examples`](workflows/examples):

- [`auth`](workflows/examples/auth): login, redirect, register, logout, password reset.
- [`forms`](workflows/examples/forms): contact, search, filters, multi-step forms, inline edit.
- [`navigation`](workflows/examples/navigation): home smoke, tabs, breadcrumbs, pagination, deep links.
- [`ecommerce`](workflows/examples/ecommerce): product list/detail, cart, checkout, order confirmation.
- [`states`](workflows/examples/states): empty, loading, error, success toast, offline fallback.
- [`admin`](workflows/examples/admin): dashboard, data table, create/edit/delete records.
- [`mobile_h5`](workflows/examples/mobile_h5): 375x812 mobile H5 starter flows.
- [`demo-app`](examples/demo-app): Vue 3 + Vite demo with smoke and regression workflows.
- [`nextjs-demo`](examples/nextjs-demo): Next.js SSR demo with smoke and regression workflows.

For WeChat Mini Program work, see
[docs/miniprogram_verification.md](docs/miniprogram_verification.md). Checkpoint
can capture the DevTools simulator region and, with OCR or VLM configured,
assert real page text instead of only checking the DevTools shell.

Inspect the workspace:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-dashboard --root .agent-workspace --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-reports --root .agent-workspace
```

Open the local GUI:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-gui --root .agent-workspace
```

Run release checks:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli install-check --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-smoke --workspace-root .agent-workspace --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate --profile ci --workspace-root .agent-workspace --run --fail-on-secret-leak
```

## MCP Setup

Use the dedicated [MCP integration guide](docs/mcp-integration.md) for copy-ready Cursor, Claude Code, VS Code, and Claude Desktop configuration.

Recommended agent startup path:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli context-snapshot --workspace-root .agent-workspace --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli show-status --workspace-root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-smoke --workspace-root .agent-workspace --format markdown
```

Generate local MCP client configuration:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-client-config --workspace-root .agent-workspace --client cursor --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-client-config --workspace-root .agent-workspace --client claude-desktop --format markdown
```

Smoke-test the MCP adapter:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-smoke --workspace-root .agent-workspace --format markdown
```

Available MCP tools:

- `list_workflows`
- `validate_workflow`
- `run_workflow`
- `get_run_report`
- `list_run_artifacts`
- `get_workspace_dashboard`
- `get_latest_failure`
- `summarize_latest_failure`
- `get_failure_details`
- `get_visual_status`
- `verify_workflow`
- `run_verification`
- `generate_workflow`

Generate a coding-agent brief:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli coding-agent-brief --client codex --workspace-root .agent-workspace --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli coding-agent-brief --client vscode --workspace-root .agent-workspace --format markdown
```

Generate editor integrations or export a workflow to Playwright Test:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli generate-integrations --root . --workspace-root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli export-to-playwright workflows\examples\auth\login_basic.yaml --output login_basic.spec.ts
```

For the standard 5-minute onboarding path, start with [`examples/demo-app`](examples/demo-app) and the workflow suite under [`examples/demo-app/workflows`](examples/demo-app/workflows).

Safety defaults:

- `run_workflow` defaults to `dry-run`.
- `approved` requires `workspace.json` `mcp.approved_workflows`.
- `mcp.max_run_profile` caps the effective execution profile.
- MCP calls are written to `gui/actions.jsonl`.
- Artifact paths are constrained to the workspace.
- Reports are scrubbed before MCP JSON or Markdown output.
- MCP responses are budgeted for coding agents; oversized reports and lists return compact summaries with truncation metadata.

## Comparison

| Capability | Playwright MCP | Windows-MCP | Checkpoint |
| --- | --- | --- | --- |
| Browser automation | Yes | No | Yes |
| Windows UIA | No | Yes | Yes |
| Persistent YAML workflows | No | No | Yes |
| Permission profiles | No | Limited | Yes |
| Audit reports | Partial | Partial | Yes |
| Failure diagnostics | Partial | Limited | Yes |
| Queue and worker | No | No | Yes |
| Regression export | No | No | Yes |
| Local-first execution | Yes | Yes | Yes |

## Docs

- [English Quickstart](docs/quickstart.md)
- [Workflow schema](docs/workflow-schema.md)
- [Long-term vision](docs/long_term_vision.md)
- [MCP integration](docs/mcp-integration.md)
- [For coding agents](docs/for-coding-agents.md)
- [Demo output](docs/demo-output.md)
- [Failure demo](docs/failure-demo.md)
- [Agent handoff guide](docs/agent_handoff.md)
- [Codex usage guide](docs/codex.md)
- [MCP Server README](README_MCP.md)
- [Claude Desktop MCP setup](docs/mcp_claude_desktop.md)
- [Cursor MCP setup](docs/mcp_cursor.md)
- [Claude Code MCP setup](docs/mcp_claude_code.md)
- [VS Code MCP setup](docs/mcp_vscode.md)
- [Checkpoint for Codex](docs/codex.md)
- [Checkpoint for VS Code](docs/vscode.md)
- [JetBrains plugin spec](docs/jetbrains-plugin-spec.md)
- [Release checklist](docs/release_checklist.md)
- [Release announcement draft](docs/release_announcement.md)
- [CI/CD](docs/ci-cd.md)
- [Product positioning](docs/product_positioning.md)
- [Marketplace API spec](docs/marketplace-api.md)
- [Terms](docs/terms.md)
- [Security policy](SECURITY.md)
- [Example workflows](workflows/examples)

## Development

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Run focused MCP tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_mcp_server.py -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for local development and pull request guidance.

## Status

Current stage: developer preview / engineering alpha.

The workflow runtime, audit chain, MCP adapter, local dry-run demo, GUI console, and quality gates are working. The next maturity work is packaging, real-world smoke testing, GUI polish, and broader external-account validation.
