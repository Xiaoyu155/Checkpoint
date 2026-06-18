# Checkpoint

[![CI](https://github.com/Xiaoyu155/Checkpoint/actions/workflows/ci.yml/badge.svg)](https://github.com/Xiaoyu155/Checkpoint/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-0.1.1-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

AI coding assistants can write code. Checkpoint verifies whether the product still works.

Checkpoint is a local-first acceptance layer for Codex, Claude Code, Cursor, VS Code, Claude Desktop, and other MCP clients. It runs repeatable product workflows after code changes, keeps reports and screenshots on your machine, and returns structured failure evidence an agent can use for the next repair.

**AI writes the change. Checkpoint runs the acceptance check.**

## 60-Second Start

```powershell
git clone https://github.com/Xiaoyu155/Checkpoint.git
cd Checkpoint
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Step all
.\.venv\Scripts\checkpoint.exe verify-now --workspace-root .agent-workspace --workflow checkout_verification --live --format markdown
```

Expected result:

```text
## Verification Report
Ran 1 workflows: 1 passed with real interaction, 0 inspection-only, 0 failed
Strict product acceptance (L3+ without blockers): 1/1

### Passed (real interaction)
checkout_verification [L4]
```

The demo opens a local checkout page, clicks the order button, asserts the confirmation state, and writes a reviewable report with screenshots and operation evidence.

## Break It On Purpose

Run the public demo loop:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\public_demo_case.ps1
```

It runs the checkout contract green, temporarily changes the checkout button copy from `Proceed to Checkout` to `Next Step`, verifies that Checkpoint catches the regression, then restores the file and verifies green again.

Failure signal shape:

```text
Failed step: assert_checkout_button
Action: assert_text
Expected: Proceed to Checkout
Actual: Text not found in observation: Proceed to Checkout
```

## Why It Exists

AI coding assistants are fast enough to edit entire features in minutes. The weak link is proving that the UI, workflow, and product promise still work afterward.

Use Checkpoint when you want an agent to verify:

- Login, forms, redirects, checkout, dashboards, and data displays.
- Exact UI copy, success states, error states, and no-regression contracts.
- Browser DOM flows first, then Windows UIA, OCR, or visual fallback when needed.
- Failure evidence that points to what broke instead of dumping raw terminal logs.

## What It Is Not

Checkpoint is not a replacement for Playwright or unit tests. It is the workflow, permission, report, failure-diagnosis, and MCP layer that lets AI coding agents run product acceptance checks locally and understand the result.

## Install

Current developer-preview path from source:

```powershell
git clone https://github.com/Xiaoyu155/Checkpoint.git
cd Checkpoint
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Step all
```

From PyPI after release:

```powershell
pip install visual-agent
python -m playwright install chromium
checkpoint doctor
```

The product is Checkpoint and the preferred CLI command is `checkpoint`. The Python package name remains `visual-agent` for compatibility.

## Daily Loop

```powershell
.\.venv\Scripts\checkpoint.exe init --root .agent-workspace
.\.venv\Scripts\checkpoint.exe verify-now --workspace-root .agent-workspace --workflow checkout_verification --live --format markdown
.\.venv\Scripts\checkpoint.exe show-status --workspace-root .agent-workspace
```

For exploratory checks from a git diff:

```powershell
.\.venv\Scripts\checkpoint.exe verify-impl --workspace-root .agent-workspace --task-description "Verify login redirects" --base-url http://127.0.0.1:5173 --run-profile dry-run --format markdown --no-untracked
```

For AI coding assistants, copy [docs/for-coding-agents.md](docs/for-coding-agents.md) into the agent context.

## What Works Out Of The Box

After `bootstrap.ps1`:

| Capability | Status |
| --- | --- |
| DOM browser automation | Ready |
| YAML workflow execution | Ready |
| MCP server | Ready |
| Local reports and screenshots | Ready |
| Windows UIA desktop automation | Ready on Windows |

Optional capabilities:

| Capability | Setup |
| --- | --- |
| Cloud VLM fallback | Add an API key to `model_api_keys.txt`; see [VLM setup](docs/vlm_setup.md) |
| Local VLM fallback | Install `torch`, `transformers`, and a local model |
| OCR text extraction | Install `pytesseract` and the Tesseract binary |

Check local readiness:

```powershell
.\.venv\Scripts\checkpoint.exe doctor
```

Look for `perception.dom_browser: true` and `playwright.ready: true` before running live browser workflows.

## Docs

- [Quickstart](docs/quickstart.md)
- [Public demo case](docs/public_demo_case.md)
- [Demo output](docs/demo-output.md)
- [Failure demo](docs/failure-demo.md)
- [Comparison](docs/comparison.md)
- [First-user feedback plan](docs/first_user_feedback_plan.md)
- [Ship now](docs/ship_now.md)
- [MCP integration](docs/mcp-integration.md)
- [Workflow schema](docs/workflow-schema.md)
- [VS Code extension](vscode-extension/README.md)
- [Public launch checklist](docs/public_launch_checklist.md)

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest -q
cd vscode-extension
npm test
```

## Status

Current stage: developer preview / engineering alpha.

The workflow runtime, MCP adapter, local checkout demo, report generation, VS Code extension wiring, and quality gates are working. The next maturity work is publishing the package and extension, adding demo media, and running first-user onboarding tests.
