# Release Announcement Draft

Use this file as the starting point for GitHub Releases, developer communities, social posts, and launch notes.

Repository: https://github.com/Xiaoyu155/Checkpoint

## 100-Character Pitch

Checkpoint is the local acceptance layer for AI coding assistants: AI writes code, Checkpoint proves it still works.

## Short Post

AI coding assistants can write code. Checkpoint verifies whether the product still works.

Checkpoint is a local-first verification layer for Codex, Claude Code, Cursor, VS Code, and MCP clients. It runs repeatable workflows after code changes, checks pages and user flows, and returns structured failure evidence an agent can use for the next repair.

GitHub: https://github.com/Xiaoyu155/Checkpoint

## Launch Post

AI coding assistants are getting fast enough to change entire features in minutes. That creates a new problem: after the code is changed, who verifies the product still works?

That is why we built Checkpoint.

Checkpoint is a local-first acceptance layer for AI coding assistants. It gives Codex, Claude Code, Cursor, VS Code, Claude Desktop, and other MCP clients a repeatable way to run product workflows after code changes.

It can validate browser flows, forms, redirects, dashboard text, error states, and no-regression contracts. It runs locally, keeps reports and screenshots on your machine, and returns structured failure evidence instead of vague terminal logs.

The goal is simple:

AI writes the change. Checkpoint runs the acceptance check.

Highlights:

- Repeatable YAML workflows for product acceptance checks.
- `dry-run`, `supervised`, and `approved` execution profiles.
- DOM-first browser validation, Windows UIA, OCR, and visual fallback paths.
- MCP tools for coding agents.
- Structured failure output with failed step, actual observation, report paths, and repair hints.
- Local reports, screenshots, audit trails, queues, and quality gates.

GitHub: https://github.com/Xiaoyu155/Checkpoint

## GitHub Release Draft

Title:

```text
Checkpoint Developer Preview: Local Acceptance Checks for AI Coding Agents
```

Body:

```markdown
Checkpoint is a local-first verification layer for AI coding assistants.

This developer preview focuses on one core loop:

1. A coding agent changes code.
2. Checkpoint runs repeatable local workflows.
3. The agent receives structured pass/fail evidence.
4. The code or workflow is fixed intentionally.

### Highlights

- CLI command: `checkpoint`
- MCP server for Codex, Claude Code, Cursor, VS Code, and Claude Desktop
- Repeatable YAML workflows
- Browser DOM validation, Windows UIA, OCR, and visual fallback paths
- `dry-run`, `supervised`, and `approved` execution profiles
- Local reports, screenshots, audit trails, queues, and quality gates
- AI-readable failure diagnostics
- Bootstrap onboarding with `doctor` and demo smoke checks

### Quick Start

```powershell
git clone https://github.com/Xiaoyu155/Checkpoint.git
cd Checkpoint
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Step all
.\.venv\Scripts\checkpoint.exe codex-check --workspace-root .agent-workspace --repo-root . --run-profile dry-run --format markdown
```

### Docs

- Quickstart: `docs/quickstart.md`
- Coding agents: `docs/for-coding-agents.md`
- Demo output: `docs/demo-output.md`
- Failure demo: `docs/failure-demo.md`
- MCP integration: `docs/mcp-integration.md`
```

## Comment Reply

If someone asks "how is this different from Playwright?":

```text
Playwright is a browser automation framework. Checkpoint is an acceptance layer for AI coding agents: it wraps workflows, permissions, reports, failure diagnostics, MCP tools, queues, and local audit trails so an agent can verify a change and understand what failed.
```

If someone asks "why not just unit tests?":

```text
Unit tests are still necessary. Checkpoint covers the product-behavior layer: page text, forms, redirects, user flows, error states, and visible regressions after an AI coding assistant changes code.
```

If someone asks "does it require cloud APIs?":

```text
No. The default path is local-first. DOM/browser workflows and dry-run validation work locally; OCR/VLM and cloud execution are optional fallback or advanced paths.
```
