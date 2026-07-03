# Release Checklist

Run this checklist before publishing a release, demo, or public branch.

## Environment

- [ ] Use a clean virtual environment.
- [ ] Confirm Python version matches project support.
- [ ] Install web extras when browser workflows are part of the demo.

```powershell
.\.venv\Scripts\python.exe -m pip install -e .[web,mcp]
```

## Capability Checks

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli install-check --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli doctor
.\.venv\Scripts\python.exe -m visual_agent.cli atomic-capabilities
```

Confirm `doctor` reports `perception.dom_browser: true` and `playwright.ready: true`.

## Product Release Smoke

```powershell
.\.venv\Scripts\checkpoint.exe release-smoke --run --workspace-root .agent-workspace --format markdown
```

## Workspace Demo

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli init --root .agent-workspace --overwrite
.\.venv\Scripts\python.exe -m visual_agent.cli verify-impl --workspace-root .agent-workspace --task-description "Verify the current change" --run-profile dry-run --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli show-status --workspace-root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli demo-workspace-check --root .agent-workspace --overwrite --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli release-trial --workspace-root .agent-workspace --run-profile supervised --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-run --root .agent-workspace --workflow local_html_form_workflow --inputs-file demo_login.json
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-report-index --root .agent-workspace --rebuild
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-dashboard --root .agent-workspace --format markdown
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## Quality Gate

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate --profile ci --workspace-root .agent-workspace --run --fail-on-secret-leak
```

## CI Hooks

```powershell
git config core.hooksPath .githooks
.\.venv\Scripts\python.exe -m visual_agent.cli verify --workspace-root .agent-workspace --tags fast --max-workflows 5 --run-profile dry-run --wait-lock --format json
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate --profile ci --workspace-root .agent-workspace --run --fail-on-risk-policy-error --fail-on-secret-leak --ci --junit-output .runs/quality_gates/junit.xml
```

## MCP Smoke

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-client-config --workspace-root .agent-workspace --client cursor --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-smoke --workspace-root .agent-workspace --format markdown
.\.venv\Scripts\python.exe -m pytest tests\test_mcp_server.py tests\e2e\test_e2e_mcp.py -q
```

## Security

- [ ] No plaintext passwords, cookies, tokens, or API keys in reports.
- [ ] MCP compact context tools do not expose passwords, cookies, tokens, or API keys.
- [ ] Oversized MCP reports/lists return truncation metadata instead of full unbounded content.
- [ ] No real auth-state files committed.
- [ ] `.agent-secrets/`, `.agent-auth/`, `.agent-workspace/`, and local credential files remain ignored.
- [ ] External samples use sandbox/staging/test account environments only.

## Documentation

- [ ] README quickstart still runs.
- [ ] `docs/quickstart.md` matches current CLI flags.
- [ ] `docs/public_launch_checklist.md` matches current publishing steps and GitHub metadata.
- [ ] `README_MCP.md` lists current MCP tools and safety defaults.
- [ ] `examples/workflows/README.md` reflects available example groups.

## Release Notes

- [ ] Summarize new workflow/runtime features.
- [ ] Summarize safety and audit changes.
- [ ] List known requirements such as Playwright browser installation.
- [ ] Mention any skipped live-account validation and its prerequisites.

## Latest Verification

Last checked on 2026-06-10:

- Editable install with `[web,mcp]`: passed.
- `doctor`: passed for required capabilities; OCR/VLM are optional and not configured.
- `init`, `verify-impl`, `show-status`, `context-snapshot`, and `release-trial`: passed on a temporary workspace.
- Workspace demo, dashboard, MCP smoke, and CI quality gate: passed on a temporary release workspace.
- Targeted regression suites for `structured_failure`, MCP failure details, `visual_status`, and demo/browser workflows: passed.

