# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project uses semantic versioning during developer preview.

## [0.1.0] - 2026-06-03

### Added

- Workflow runtime for YAML/JSON workflows with schema versioning.
- Structured-first providers for browser DOM, Windows UIA, OCR, VLM, fixtures, screen, and local HTML.
- Permission profiles: `dry-run`, `supervised`, and `approved`.
- Runtime validation, strict validation, and preflight checks.
- Run reports in JSON and Markdown with screenshots, artifacts, downloads, selector evidence, DOM excerpts, and failure diagnostics.
- Browser recording into workflow drafts with selector self-checks, input templates, redaction, and failure archives.
- Workspace layer with workflows, inputs, fixtures, runs, reports, queues, quality gates, and regression tests.
- Queue backends, worker mode, SQLite migration, and JSON rollback.
- MCP server with workflow-level tools: list, validate, run, report, and artifacts.
- MCP AI context tools: `summarize_latest_failure`, `get_session_context`, and `run_verification`.
- Agent session persistence in `agent_session.json` for compact pass/fail state and latest-failure recovery hints.
- Verification workflows via `tags: [verification]` and the `verify` CLI command.
- MCP safety controls: workspace path checks, approved workflow whitelist, max run profile cap, report scrubbing, artifact path limits, and audit logging.
- MCP response budgeting for coding agents, including report truncation, compact failure summaries, session snapshots, verification summaries, and large-list truncation metadata.
- Tkinter workspace GUI with workflow/run/queue views, report detail, input template editor, auth-state controls, readiness panels, action history, async jobs, and check buttons.
- Quality gates, secret scan, risk policy checks, CI templates, release-check, install-check, mcp-client-config, mcp-smoke, and demo-workspace-check.
- Bootstrap scripts for Python checks, virtual environment setup, dependency install, Playwright browser install, workspace initialization, and MCP config generation.

### Changed

- MCP documentation now lists all 10 current tools and explains compact context usage.
- Large MCP reports and artifact/workflow lists now return truncation metadata instead of unbounded responses.
- Coding-agent guidance now recommends compact context tools before full report reads when token budget matters.

### Known Gaps

- The product is still a developer preview, not a packaged end-user app.
- Real external-account validation remains intentionally gated.
- Windows UIA and VLM paths need more real-world smoke testing.
- GUI layout is functional but still engineering-console oriented.
- Optional OCR/VLM dependencies may be absent; DOM workflows remain ready without them.
