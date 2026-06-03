# Visual Agent

Local-first workflow automation for AI assistants.

Visual Agent lets Claude Code, Codex, Cursor, Claude Desktop, and other MCP clients run browser and desktop workflows on your machine with permission profiles, audit trails, screenshots, reports, queues, and failure diagnostics.

It is not another one-step browser remote control. It is a local execution layer for repeatable, reviewable workflows.

## What It Does

- Runs YAML workflows with validation and preflight checks.
- Uses structured providers first: DOM, Windows UIA, OCR, then VLM fallback.
- Defaults to safe execution through `dry-run`, `supervised`, and `approved` profiles.
- Stores reports, screenshots, queue state, auth-state metadata, and GUI action history locally.
- Exposes high-level workflow tools through MCP.
- Provides CLI, Tkinter GUI, queue worker, regression export, and quality gates.

## What Works Out of the Box

After running `bootstrap.ps1`, the following are ready with no extra configuration:

| Capability | Status | Notes |
| --- | --- | --- |
| DOM browser automation | **Ready** | Playwright Chromium installed by bootstrap |
| YAML workflow execution | **Ready** | dry-run, supervised, approved profiles |
| MCP server (10 tools) | **Ready** | Codex, Claude Code, Cursor, VS Code, Claude Desktop |
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

From a source checkout on Windows:

```powershell
git clone <your-repo-url> visual-agent
cd visual-agent
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

The bootstrap script checks Python, creates or reuses `.venv`, installs core dependencies, installs `[web,mcp]` extras, installs Playwright Chromium into `.pw-browsers`, initializes `.agent-workspace`, and writes MCP client config examples.

## Quickstart

Run the local dry-run demo:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli demo-workspace-check --root .agent-workspace --format markdown
```

### Verification Loop

Visual Agent's core value is detecting regressions automatically after code
changes. After the dry-run demo passes, try the verification loop:

1. Run `workspace-run --workflow checkout_verification` - all green.
2. Change one button label in `examples/web/checkout_verification_demo.html`.
3. Run again - Visual Agent reports the exact text mismatch in the failing step.
4. Read `context-snapshot` - a <= 500-token summary tells you what changed and
   where to look.
5. Fix the label, run again - green.

See [docs/quickstart.md](docs/quickstart.md) for the full walkthrough.

For WeChat Mini Program work, see
[docs/miniprogram_verification.md](docs/miniprogram_verification.md). Visual
Agent can capture the DevTools simulator region and, with OCR or VLM configured,
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
- `get_session_context`
- `run_verification`

Generate a coding-agent brief:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli coding-agent-brief --client codex --workspace-root .agent-workspace --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli coding-agent-brief --client vscode --workspace-root .agent-workspace --format markdown
```

Safety defaults:

- `run_workflow` defaults to `dry-run`.
- `approved` requires `workspace.json` `mcp.approved_workflows`.
- `mcp.max_run_profile` caps the effective execution profile.
- MCP calls are written to `gui/actions.jsonl`.
- Artifact paths are constrained to the workspace.
- Reports are scrubbed before MCP JSON or Markdown output.
- MCP responses are budgeted for coding agents; oversized reports and lists return compact summaries with truncation metadata.

## Comparison

| Capability | Playwright MCP | Windows-MCP | Visual Agent |
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
- [MCP Server README](README_MCP.md)
- [Claude Desktop MCP setup](docs/mcp_claude_desktop.md)
- [Cursor MCP setup](docs/mcp_cursor.md)
- [Claude Code MCP setup](docs/mcp_claude_code.md)
- [VS Code MCP setup](docs/mcp_vscode.md)
- [Visual Agent for Codex](docs/codex.md)
- [Visual Agent for VS Code](docs/vscode.md)
- [Release checklist](docs/release_checklist.md)
- [Product positioning](docs/product_positioning.md)
- [Example workflows](examples/workflows/README.md)

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
