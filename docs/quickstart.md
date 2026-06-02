# Visual Agent Quickstart

Visual Agent is a local-first automation runtime for AI assistants. It runs browser and desktop workflows with permissions, audit trails, screenshots, failure diagnostics, queues, and reports stored on your machine.

## Install

```powershell
cd "D:\longxia agent"
.\.venv\Scripts\python.exe -m pip install -e .[web]
```

Install Playwright Chromium only if you want to run headed browser recording or live browser workflows:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

## Create A Workspace

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli init-workspace --root .agent-workspace --overwrite
```

The workspace contains example workflows, input templates, report folders, queue state, and local audit artifacts.

## Run A Dry-Run Demo

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-run --root .agent-workspace --workflow local_html_form_workflow --inputs-file demo_login.json
```

This uses a local HTML fixture and dry-run actions. It does not submit real data to an external service.

## Inspect Reports

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-report-index --root .agent-workspace --rebuild
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-reports --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-report-detail --root .agent-workspace --run-id <run-id> --format markdown
```

Reports are written to `reports/<run-id>.json` and `reports/<run-id>.md`.

## Record A Browser Workflow

Recording is explicit and local. Use `--headless` for smoke tests, or omit it for interactive recording.

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-record-browser --root .agent-workspace --url file:///<absolute-path-to-login-demo.html> --save-as recorded/login --headless --assert-text "Login"
```

Recorded password/token-like values are converted into `input.*` references and scanned again before workflow/report output.

## Open The GUI

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-gui --root .agent-workspace
```

The GUI exposes workflows, runs, queue tasks, input templates, auth states, external sample readiness, reports, and GUI action history.

## Install And MCP Smoke

Before connecting an MCP client, generate the local checks and run the in-process tool smoke:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli install-check --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli demo-workspace-check --root .agent-workspace --overwrite --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-client-config --workspace-root .agent-workspace --client cursor --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-smoke --workspace-root .agent-workspace --format markdown
```

## Run Quality Checks

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate --profile local --workspace-root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate --profile ci --workspace-root .agent-workspace --run --fail-on-secret-leak
```

The secret leak gate scans text artifacts under `reports/`, `runs/`, and `artifacts/` and only stores redacted previews.
