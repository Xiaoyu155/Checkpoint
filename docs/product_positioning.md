# Product Positioning

## One-Line Positioning

DevPacer is a local-first AI development operator that turns user intent into a
bounded engineering mission, allocates coding-agent resources, runs verified
development loops, and stops only with evidence or a human decision request.

Checkpoint remains the local verification engine inside DevPacer.

## Brand Architecture

- Public product: `DevPacer`
- Verification engine: `Checkpoint`
- Current CLI: `checkpoint`
- Future CLI alias: `devpacer`
- Product unit: `mission`
- Verification unit: `workflow`

This avoids a risky big-bang rename. The product message is:

> DevPacer operates the development loop. Checkpoint proves what happened.

## What It Is

Official coding-agent vendors keep making Codex, Claude Code, Gemini CLI,
Cursor, and similar tools stronger. They are not responsible for teaching every
user how to spend models, permissions, context, and verification economically.

That gap is the product.

DevPacer accepts a user outcome, sharpens it into a verifiable mission, chooses
agent/model/permission settings, dispatches a worker under budget, verifies the
result with Checkpoint, repairs bounded failures, and records why it stopped.

## Core Principles

- Verified delivery is the product. More agents are not the product.
- A vague request must be clarified before tokens are spent.
- Budget, model tier, and stop conditions are product state, not prompt vibes.
- Codex is the first executable implementation worker.
- Gemini is a read-only multimodal inspection lane for screenshots, visual
  evidence, and world-model review; it is not a default coding worker.
- Checkpoint workflows remain local-first, auditable, permissioned, and
  repeatable.
- Every autonomous loop must leave durable records: `mission.json`,
  `budget.json`, `rounds.jsonl`, and `final_report.md`.

## What Checkpoint Does

Checkpoint is the verification and local automation engine:

- browser and Windows desktop workflow execution;
- DOM / UIA / OCR / VLM evidence collection;
- YAML workflow persistence;
- dry-run / supervised / approved permission profiles;
- structured failures, screenshots, reports, and repair evidence packs;
- `codex-check` gates for coding-agent changes.

Checkpoint should stay narrow and trustworthy. DevPacer builds the development
operator loop on top of it.

## First Product Use Cases

- Refuse to dispatch an unclear request and ask the minimum blocking questions.
- Build a saved chief plan with acceptance criteria and agent-readable worker
  prompts.
- Run `chief-run` to create a mission, preview the worktree/worker command, and
  stop safely if coverage or permissions are weak.
- Execute one Codex worker under budget, then run Checkpoint verification.
- Generate a repair brief and allow one bounded repair round.
- Show mission status later without requiring the user to keep watching a
  terminal.

## Differentiation

DevPacer is not another multi-agent dashboard. Its differentiator is controlled
delivery:

- it translates user language into coding-agent language;
- it decides when not to spend tokens;
- it chooses model and permission posture from versioned capability profiles;
- it verifies with product evidence, not agent self-reporting;
- it stops on repeated failure, coverage gaps, scope drift, or permission needs.

## Primary User

Developers and operators who want AI coding tools to reduce development time
without learning every model, CLI flag, permission mode, worktree pattern, and
verification workflow themselves.

The user should not babysit the terminal. DevPacer should run until the mission
is verified or a real decision is needed.

## Non-Goals

- It is not a hosted RPA SaaS.
- It is not a replacement for Codex, Claude Code, Gemini, Cursor, Playwright, or
  UI Automation.
- It is not a tool for bypassing approval, audit trails, or workflow evidence.
- It is not a default multi-agent coding competition system.
- It should not auto-merge code without a policy and human-approved release
  path.
