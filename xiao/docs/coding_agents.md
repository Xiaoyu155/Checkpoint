# Coding Agents

Checkpoint is useful to coding agents because it gives them a local execution
surface with durable workflows, explicit permission profiles, and auditable
reports. The agent can reason about the task, while Checkpoint performs the
browser or desktop workflow and records what happened.

## Generate The Brief

Run this from the repository root:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli coding-agent-brief --client codex --workspace-root .agent-workspace --format markdown
```

Use `--client claude-code`, `--client cursor`, or `--client vscode` for
client-specific wording and MCP config shape.

## What The Agent Should Do

1. Read the coding agent brief.
2. Connect the MCP server using the generated config.
3. Run `mcp-smoke` before using the tools for real work.
4. Prefer existing workflows over ad hoc browser actions.
5. Run workflows as `dry-run` unless a human explicitly approves escalation.
6. Use `get_session_context` when resuming work in a new chat.
7. Read `get_run_report` before claiming success when a specific run id matters.
8. Use `summarize_latest_failure` before reading a full failed report.
9. Use `run_verification` after code changes when verification-tagged workflows exist.
10. Use `get_workspace_dashboard` before and after risky changes.

## Chief Engineer Plan

Before dispatching multiple coding agents, turn the user's objective into a
single task package:

```powershell
checkpoint chief-plan --goal "Fix checkout total display" --workspace-root .agent-workspace --repo-root . --format markdown
```

The plan includes selected workflows, coverage gaps, per-agent worker commands
(model + sandbox + approval, sourced from capability profiles), domain-specific
acceptance criteria, and the verification commands that must pass before any
worker claims the task is done.

Extra flags:

- `--interview` surfaces clarifying questions to sharpen a vague objective. A
  goal with no verifiable definition of done returns status
  `needs_clarification` and refuses to emit a worker handoff — that refusal is
  intentional, it avoids wasting a coding-agent run.
- `--answer "..."` (repeatable) feeds answers back in; they become acceptance
  criteria and satisfy the clarity gate.
- `--save` persists the plan to `<workspace>/chief_plans/<plan_id>/plan.json`.
  Resume with `checkpoint chief-plans list` and `checkpoint chief-plans show <id>`.

## DevPacer Mission Run

Use the high-level mission facade for everyday operation:

```powershell
checkpoint mission start --goal "Fix checkout total display" --workspace-root .agent-workspace --repo-root .
checkpoint mission import --file docs/dev_plan.md --workspace-root .agent-workspace --repo-root .
checkpoint mission import --file docs/dev_plan.md --workspace-root .agent-workspace --repo-root . --create --queue
checkpoint mission status --mission <mission_id> --workspace-root .agent-workspace
checkpoint mission list --workspace-root .agent-workspace
checkpoint mission queue --mission <mission_id> --workspace-root .agent-workspace
checkpoint mission worker --workspace-root .agent-workspace --watch --poll-seconds 10
checkpoint mission memory --goal "Fix checkout total" --workspace-root .agent-workspace
```

`mission start` is preview-only unless `--execute` or `--background` is passed.
`mission import` is also non-executing: by default it parses a markdown
development plan into mission drafts and saves an import record under
`<workspace>/mission_imports/<import_id>/import.json`. Add `--create` to create
preview missions for extracted drafts, and add `--queue` to submit those preview
missions to the local queue. It still does not run Codex directly; a worker must
be started explicitly.
The lower-level `chief-*` commands remain available for debugging and audit.

`chief-run` is the first bounded autonomous loop. It creates a DevPacer mission,
saves `mission.json`, `budget.json`, `rounds.jsonl`, and `final_report.md`, then
uses Checkpoint as the verification engine.

Preview first:

```powershell
checkpoint chief-run --goal "Fix checkout total display" --workspace-root .agent-workspace --repo-root .
```

By default this is a dry-run. It builds or loads a plan, records a mission, and
previews the Codex worker command and worktree. It refuses to run when the goal
is unclear, workflow coverage is weak, or permissions are missing.

Execute only when the plan and coverage are acceptable:

```powershell
checkpoint chief-run --goal "Fix checkout total display" --workspace-root .agent-workspace --repo-root . --execute --run-profile supervised --max-rounds 2
```

The first executable loop supports one Codex implementation round plus one
automatic repair round. It stops with a durable reason such as `verified`,
`needs_clarification`, `coverage_gap`, `same_failure_repeated`,
`budget_exhausted`, `permission_required`, or `worker_error`.

Inspect saved missions:

```powershell
checkpoint chief-missions list --workspace-root .agent-workspace
checkpoint chief-missions show <mission_id> --workspace-root .agent-workspace
```

Resume a previewed mission without rebuilding the plan:

```powershell
checkpoint chief-run --resume <mission_id> --workspace-root .agent-workspace --execute --run-profile supervised
```

Start a mission in the background and return immediately:

```powershell
checkpoint chief-run --goal "Fix checkout total display" --workspace-root .agent-workspace --repo-root . --background --run-profile supervised
```

Check status later:

```powershell
checkpoint chief-status --mission <mission_id> --workspace-root .agent-workspace
```

Background mode writes `background.json` and stdout/stderr logs under
`<workspace>/missions/<mission_id>/logs/`. It uses the same
`chief-run --resume <mission_id> --execute` path as foreground execution; there
is no second execution engine.

`chief-status` also reconciles background health:

- if the background worker has completed, it reports `exit_code`,
  `completed_at`, result status, and stop reason from `background.json`;
- if the process is still alive and inside budget, it reports `running`;
- if the process exceeds the mission wall-clock budget, it terminates the
  process, marks the mission `budget_exhausted`, and appends a
  `background_health` round;
- if the process disappears without a completion receipt, it marks the mission
  `worker_error` and updates `final_report.md`.

## DevPacer Mission Queue

Use the mission queue when the user has already reviewed a mission preview and
wants it to run unattended later:

```powershell
checkpoint chief-queue submit --mission <mission_id> --workspace-root .agent-workspace --run-profile supervised
checkpoint chief-queue list --workspace-root .agent-workspace
checkpoint chief-worker --workspace-root .agent-workspace --run-once
```

`chief-queue submit` only accepts missions in `created` or `preview` state by
default. Use `--force` only after reviewing the mission, because completed,
blocked, or already-running missions are not normally runnable. The queue
record lives under `<workspace>/mission_queue/queue.json`.

`chief-worker` claims one pending mission atomically and then runs the same
`chief-run --resume <mission_id> --execute` path. There is no second execution
engine. Use watch mode for a simple local daemon:

```powershell
checkpoint chief-worker --workspace-root .agent-workspace --watch --poll-seconds 10
```

The queue marks an item `success` only when the mission result is `verified`;
other stop reasons are recorded as `failed` with the mission stop reason and
final report path.

## Autonomous Programs

Use autonomous mode when Pacer should own task import, prioritization, quota
allocation, delegated implementation, repair, and queue management:

```powershell
checkpoint autopilot --file docs/dev_plan.md --workspace-root .agent-workspace --repo-root . --autonomous
checkpoint mission worker --workspace-root .agent-workspace --watch
```

`--autonomous` imports every actionable task instead of stopping at the normal
12-task cap, removes the default sequential dependency chain, ignores Pacer's
conservative 45-minute quota reserve and 82% pause threshold, and sends each
internal task through `delegated` mode. Missions receive an eight-round budget
with up to seven repair rounds and longer wall/worker timeouts. These are
process-health bounds, not exploration or task-scope limits.

Autonomous mode does not bypass production/external-access classification,
credential requirements, restricted-path and test-tamper checks, verification,
or the manual merge gate. Those protect the repository and external systems;
they are independent of Pacer's freedom to plan and spend the available model
quota.

Because autonomous execution always uses an isolated worktree, callers may add
`--allow-dirty` to overlay the current dirty repository state into that
worktree. This is explicit because ignored secret-like files must never be
copied by default. Pacer never cleans or resets the source checkout, and merge
remains manual.

## DevPacer Project Memory

Project memory is derived from durable evidence (`missions/`, `chief_plans/`,
`rounds.jsonl`, and final reports). It is not an LLM-generated recollection.

```powershell
checkpoint chief-memory --workspace-root .agent-workspace --goal "Fix checkout total"
```

`chief-memory` ranks relevant previous missions, summarizes stop reasons,
failed signatures, final reports, and recommendations such as "add workflow
coverage before dispatching similar work" or "inspect the repeated failure
before spending another run." `chief-plan` also reads this memory and injects a
short project-memory block into worker handoff prompts, so a new mission can
reuse evidence from earlier missions.

Memory use is auditable. Saved plans expose `project_memory.usage`, including
`retrieval_invoked`, `injected_memory_ids`, and injected character counts. The
actual dispatch path regenerates the bounded memory block and records
`dispatch_injected` / `dispatch_memory_ids`; completed dispatches copy this into
`dispatches.jsonl`. This distinguishes "memory was indexed" from "the worker
actually received this memory in its prompt."

Run the deterministic checkout mission demo when auditing the loop without
spending coding-agent tokens:

```powershell
checkpoint chief-run-demo --workspace-root .agent-workspace --format markdown
```

The demo creates an isolated git repo under `.agent-workspace/demo_repos/`,
commits a green checkout baseline, commits a `Next Step` regression, launches
the same dispatch path with a deterministic local worker, runs real supervised
Checkpoint verification, fails once, repairs once, then verifies. It proves the
mission loop and worktree verification path without calling a remote coding
model.

## Agent Capabilities

Checkpoint keeps versioned capability profiles for each coding agent
(`src/visual_agent/agent_profiles/*.yaml`): models, sandbox/permission modes,
parallelism, and the features people most often miss.

```powershell
checkpoint agents doctor
```

`agents doctor` probes which agents are installed locally (and their versions)
and lists the capabilities you may not be using — headless mode, per-run
sandbox flags, parallel worktrees/subagents, MCP servers, hooks. Use
`checkpoint agents show codex` (or `claude-code`) to print a full profile. When
an agent ships a new release, update its YAML profile — no code change needed.

Gemini is intentionally modeled as a multimodal inspection lane, not as the
default coding worker. Use `checkpoint agents show gemini` to inspect its
profile. In chief-engineer plans Gemini should review screenshots, visual
evidence, and acceptance gaps; it should not create a competing code diff unless
a human deliberately changes that policy.

## Chief Dispatch

Preview the saved plan before any worker process is launched:

```powershell
checkpoint chief-dispatch --workspace-root .agent-workspace --plan <plan_id> --dry-run --format markdown
```

Dispatch refuses `needs_clarification`, missing workspace, and weak workflow
coverage by default. It also refuses to execute from a dirty repository unless
`--allow-dirty` is explicit.

The only executable coding adapter in this phase is Codex:

```powershell
checkpoint chief-dispatch --workspace-root .agent-workspace --plan <plan_id> --execute --run-profile supervised --format markdown
```

On execution, Checkpoint creates a git worktree, runs `codex exec`, writes
`chief_plans/<plan_id>/workers.jsonl`, then runs `codex-check` and writes
`chief_plans/<plan_id>/verification.json`. Add `--auto-repair-once` only for a
supervised experiment: if verification fails, Checkpoint builds a repair prompt
from the latest failure evidence and sends it once to the same Codex worktree.

## Verification Profiles

`codex-check` runs in `dry-run` by default, which skips real interactions. For a
workflow that asserts post-interaction state (for example a checkout confirmation
after clicking "Place Order"), dry-run cannot prove the result, so it is reported
as inspection-only, not a pass. Run interactive product-acceptance with a real
profile:

```powershell
checkpoint codex-check --workspace-root .agent-workspace --repo-root . --run-profile supervised
```

Add `--strict` in CI to exit non-zero on a coverage gap (uncovered or
fallback-only changes), not only on outright failures.

## Useful Prompts

```text
Use Checkpoint to list workflows, run `verify-now`,
then summarize the report.
```

```text
Use Checkpoint to validate every workflow before suggesting changes.
```

```text
If a workflow fails, use get_run_report and list_run_artifacts before editing
code.
```

```text
If a workflow fails, use summarize_latest_failure first. Only read the full
report if the summary does not contain enough detail.
```

```text
After changing code, run verification workflows and summarize the pass/fail
report.
```

```text
Use Checkpoint to get the workspace dashboard, find the latest failed run,
and explain the failure diagnosis.
```

## Safety Rules

- Treat missing auth state as a blocker.
- Do not bypass login or scrape protected data.
- Do not print secrets from inputs, cookies, tokens, or model credentials.
- Do not request `approved` run_profile unless the workspace policy and the
  human both allow it.
- Treat `truncated: true` as a signal to use `list_run_artifacts` or a more
  specific tool rather than asking for a larger MCP response.
- Use the run report as the source of truth.
