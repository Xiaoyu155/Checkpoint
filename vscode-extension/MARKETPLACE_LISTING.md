# Checkpoint Marketplace Listing Draft

## Short Description

Checkpoint adds a local verification panel for AI coding assistants. Run product workflows, inspect structured failures, and keep verification context across coding sessions.

## Tagline

Local-first verification runtime for AI agents.

## Long Description

Checkpoint gives AI assistants a repeatable local execution layer for verified workflows. It is designed for Codex, Cursor, Claude Code, Claude Desktop, and other MCP-compatible clients that need more than terminal output.

Use Checkpoint to:

- initialize a project workspace without leaving VS Code
- generate or run workflows for browser, desktop, and implementation checks
- inspect structured failures, product issue groups, reports, screenshots, and audit trails
- resume work with compact session context instead of raw logs
- keep verification runs local, permissioned, and reviewable

Checkpoint is built around local workspace state, not opaque browser control. It stores the important artifacts where your project already lives and exposes them through the extension, CLI, and MCP tools.

Try the repository demo with `scripts/public_demo_case.ps1` to see a checkout workflow pass, catch a deliberate button-copy regression, then pass again after restoration.

## Key Features

- Local workspace initialization
- Workflow library and workflow generation
- Dry-run, supervised, and approved execution profiles
- Structured failure summaries and detailed diagnostics
- Reports, screenshots, queue state, and audit history
- MCP-friendly tools for coding agents
- Cloud marketplace and remote run support when enabled

## First-Time User Flow

1. Install the extension.
2. Run `Checkpoint: Init Workspace`.
3. Run `Checkpoint: Verify Now`, `Checkpoint: Verify Implementation`, or open an existing workflow.
4. Inspect the report, product issue group, and failure details in the sidebar.
5. Resume from `get_session_context` or `Checkpoint: Show Last AI Verification`.

## Screenshot Captions

- Checkpoint activity bar with the workflow list open
- Quick Actions menu showing Init Workspace and Verify Implementation
- Sidebar report summary after a verification run
- Product Issues panel grouping repeated failed assertions
- Failure details panel with structured diagnosis

## Release Notes Snippet

Checkpoint is now available in VS Code with local workspace initialization, workflow-driven verification, structured failure summaries, and MCP-friendly context tools for AI coding assistants.
