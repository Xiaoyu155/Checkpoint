# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project uses semantic versioning during developer preview.

## [Unreleased]

## [0.1.1-preview] - 2026-06-18

### Added

- Repository-facing support documentation, coding-agent onboarding, real CLI demo output, a failure demo, and release announcement drafts for GitHub visitors.
- Context-aware workflow generation from code changes via `generate_workflow_from_context`.
- Single-call implementation verification via `verify_implementation`, including generation, quality scoring, workflow execution, compact failure diagnosis, status-file updates, quality thresholds, and timeout handling.
- Git-diff workflow synthesis commands: `generate-from-diff` and `verify-impl`.
- Static context ingestion for HTML, React/JSX, Vue, Django, FastAPI, and Flask code.
- Workflow quality scoring for generated workflows, including assertion density, business assertions, success-path coverage, and actionable gaps.
- VS Code status-file integration for `.vscode-agent-status.json`, with status bar and sidebar updates for AI verification results, quality gaps, recommendations, warning states, and timeout state.
- Python verification status schema helpers for normalizing `verify_implementation` responses and writing compact VS Code status files.
- VS Code status parser tests for pass/fail/timeout/quality-gap verification status payloads.
- Generated input templates now use safe example values for common non-sensitive fields while keeping password/token/secret-like fields empty.
- Generated input templates now honor basic parsed validation rules such as min/max, min/max length, and simple fixed-length patterns.
- HTML label extraction now correctly handles `<label for="...">` appearing before its input.
- Generated workflow and implementation verification responses now include `semantic_summary` so AI tools can see parser confidence, generation method, extracted field counts, success-state counts, and parse warnings.
- Static context ingestion now extracts basic form validation rules such as required, email format, min/max length, numeric bounds, and pattern constraints from HTML, Vue templates, and React/JSX inputs.
- Static workflow synthesis now asserts dynamic data displays when parsed template variables match submitted non-sensitive input fields.

### Changed

- Public repository links, issue templates, CI workflow names, and support/security docs now use the Checkpoint brand and `Xiaoyu155/Checkpoint` repository path.
- Core CLI, MCP, and workspace modules were split below the repository's module-size governance thresholds while preserving compatibility entrypoints.
- Cloud server request handling now rejects workspace escapes and run-profile privilege escalation.
- Local onboarding now runs `doctor` and a demo smoke check through `scripts/bootstrap.ps1 -Step all`.
- `generate_workflow_from_context` and `verify_implementation` can now collect code changes from git diff when `code_changes` is omitted.
- `verify_implementation` now uses the generated input template automatically when callers do not provide explicit inputs.
- Static workflow synthesis now adds an `assert_text_contract` forbidden-text check for known parsed error messages, and workflow quality scoring recognizes that as error-state coverage.
- Workflow quality scoring now treats `text_from` assertions as data-display coverage.
- `semantic_summary` now includes parsed `data_displays` names, not only the count.
- Verification status payloads now include `inputs_path` and `inputs_source`.
- `verify_implementation` blocks low-quality generated workflows by default when quality is below `0.6`, returning `needs_workflow_improvement` instead of a weak pass/fail signal.
- `verify_implementation` supports `timeout_seconds` and returns `timeout` when the generated workflow run exceeds the budget.

### Fixed

- Loopback URLs such as `http://127.0.0.1:5173` are allowed by workflow URL validation while private and link-local SSRF targets remain blocked.
- Dry-run workflow generation falls back to the offline template when model authentication or endpoint configuration is missing.

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
