# Contributing

Thanks for helping improve Visual Agent. This project is a local-first automation runtime, so changes should preserve safety, auditability, and structured-first execution.

## Local Setup

```powershell
git clone <your-repo-url> visual-agent
cd visual-agent
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

The bootstrap script creates `.venv`, installs editable dependencies, installs `[web,mcp]` extras, installs Playwright Chromium into `.pw-browsers`, initializes `.agent-workspace`, and writes MCP config examples.

## Run Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Focused checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_mcp_server.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_workflow_contracts.py -q
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate --profile local --workspace-root .agent-workspace
```

## Development Rules

- Prefer DOM/UIA/OCR/VLM structured evidence over fixed coordinates.
- Keep generated or AI-planned actions permissioned by `dry-run`, `supervised`, or `approved`.
- Do not log plaintext passwords, cookies, tokens, auth headers, API keys, or storage-state secrets.
- Keep workflow examples versioned and validate them with contract tests.
- Keep MCP tools high-level and workflow-oriented unless a lower-level tool has a clear safety boundary.
- Add tests for behavior changes that affect runtime execution, security, reports, MCP, queue state, or GUI actions.

## Pull Request Checklist

- [ ] Run focused tests for the changed area.
- [ ] Run full `pytest -q` before opening the PR.
- [ ] Update docs when CLI flags, workflows, MCP tools, or safety defaults change.
- [ ] Avoid committing `.venv`, `.pw-browsers`, `.agent-workspace`, `.agent-auth`, `.runs`, auth-state files, or credential files.
- [ ] Explain any skipped real-account validation and its prerequisites.
