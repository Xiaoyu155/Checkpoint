# Product Positioning

## One-Line Positioning

Checkpoint is a local-first automation execution layer for AI assistants, giving Claude Code, Codex, Cursor, and other MCP clients a safe way to run browser and desktop workflows with audit trails, structured failure output, and repeatable verification loops.

## What It Is

AI assistants decide what should happen. Checkpoint executes the workflow locally, records what happened, enforces permissions, and produces reports that can be reviewed, queued, rerun, or promoted into regression tests.

## Core Principles

- Local-first: workflows, screenshots, inputs, auth-state metadata, queue state, and reports stay on the user machine by default.
- Structured-first: DOM and Windows UIA are preferred before OCR or VLM fallback.
- Contract-first: `structured_failure`, `get_failure_details`, `verify_workflow`, and `context-snapshot` give agents stable machine-readable state instead of ad hoc logs.
- Auditable: each run can write JSON/Markdown reports, screenshots, failure diagnosis, selector evidence, GUI action events, and quality-gate history.
- Permissioned: dry-run, supervised, and approved profiles keep generated automation from silently performing real actions.
- Workflow-centric: YAML workflows are versionable, reviewable, reusable, and queueable.

## Differentiation

Browser-only MCP servers are useful for interactive browsing. Checkpoint targets durable local automation:

- Browser plus Windows desktop automation.
- Workflow persistence rather than one-off tool calls.
- Safety profiles and target existence checks.
- Queue, worker, retry, migration, and report history.
- Failure diagnosis with screenshots, DOM excerpts, selector summaries, OCR/VLM evidence, and rerun suggestions.
- Known framework noise can be classified explicitly, so repeated demo issues such as Next.js hydration mismatch do not get mislabeled as generic regressions.
- Secret scanning for recording output and workspace artifacts.

## Primary User

Developers and operators who want AI assistants to run repeatable local workflows without sending sensitive browser or desktop state to a cloud automation service.

## First Use Cases

- Run a local dry-run browser workflow and inspect the report.
- Record a browser flow and convert sensitive values into input templates.
- Queue approved dry-run workflows for repeated execution.
- Check external sample readiness before using real accounts.
- Expose workflows to MCP clients while keeping audit data local.
- Separate known framework noise from real workflow regressions in reports and MCP payloads.

## Non-Goals

- It is not a hosted RPA SaaS.
- It is not a replacement for Playwright or UI Automation.
- It is not an autonomous agent that bypasses user approval.
- It is not intended to hide or skip audit trails.

