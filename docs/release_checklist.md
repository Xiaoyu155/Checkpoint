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

## Workspace Demo

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli init-workspace --root .agent-workspace --overwrite
.\.venv\Scripts\python.exe -m visual_agent.cli demo-workspace-check --root .agent-workspace --overwrite --format markdown
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

## MCP Smoke

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-client-config --workspace-root .agent-workspace --client cursor --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli mcp-smoke --workspace-root .agent-workspace --format markdown
.\.venv\Scripts\python.exe -m pytest tests\test_mcp_server.py
```

## Security

- [ ] No plaintext passwords, cookies, tokens, or API keys in reports.
- [ ] No real auth-state files committed.
- [ ] `.agent-secrets/`, `.agent-auth/`, `.agent-workspace/`, and local credential files remain ignored.
- [ ] External samples use sandbox/staging/test account environments only.

## Documentation

- [ ] README quickstart still runs.
- [ ] `docs/quickstart.md` matches current CLI flags.
- [ ] `README_MCP.md` lists current MCP tools and safety defaults.
- [ ] `examples/workflows/README.md` reflects available example groups.

## Release Notes

- [ ] Summarize new workflow/runtime features.
- [ ] Summarize safety and audit changes.
- [ ] List known requirements such as Playwright browser installation.
- [ ] Mention any skipped live-account validation and its prerequisites.
