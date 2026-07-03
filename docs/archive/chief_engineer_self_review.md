# AI Development Chief Engineer Self-Review

Date: 2026-07-01

## Summary

This report reviews the first technical reproduction of the "AI development chief engineer" direction.

The core product idea is no longer just "connect Codex, Claude Code, and other coding tools." The stronger direction is:

> The user talks to Checkpoint. Checkpoint acts as the chief engineer, turns the user's objective into an executable engineering package, dispatches coding agents as workers, and accepts or rejects the result with product evidence.

The current implementation is only the first slice of that direction. It adds a chief-engineer planning layer, not a full agent runtime.

## External Reference Signal

The direction is not imaginary. Similar technical patterns already exist in public projects:

- OpenHands and Agent Canvas prove that coding agents can be driven through a product-facing control surface.
- Vibe Kanban, Emdash, clideck, and agent-orchestrator prove demand for multi-agent workspaces, terminal/session management, and git worktree isolation.
- BMAD-METHOD and Squad show that role-based AI software development workflows are more valuable than raw multi-agent spawning.
- claude-flow and Ruflo show that swarm-style orchestration exists, but also show the risk of making agent count the product instead of delivery quality.

The useful lesson is clear: Checkpoint should not compete as "one more multi-agent panel." The differentiation should be verified delivery.

## What Was Built

This iteration added a minimal chief-engineer planner:

- `src/visual_agent/chief_engineer.py`
- `checkpoint chief-plan`
- `tests/test_chief_engineer.py`
- Documentation entry in `docs/coding_agents.md`

The command:

```powershell
checkpoint chief-plan --goal "Fix checkout total display" --workspace-root .agent-workspace --repo-root . --format markdown
```

produces a task package containing:

- user objective
- git changed files
- selected verification workflows
- workflow coverage status
- acceptance criteria
- Codex / Claude Code worker command templates
- verification commands
- risks and next actions
- a handoff prompt for worker agents

The implementation deliberately reuses existing Checkpoint primitives:

- `discover_workflows`
- `affected_workflows`
- `changed_files`
- `codex_coverage_summary`
- existing `codex-check` and `verify-now` commands

This is the correct direction technically. The chief-engineer layer should not duplicate the verification engine.

## Verification Results

Targeted tests passed:

```text
tests/test_chief_engineer.py: 4 passed
CLI/codex-check related tests: 7 passed
```

Manual command execution also worked with local source import:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m visual_agent.cli chief-plan --goal "复现总工调度 AI coding agents 并用 Checkpoint 验收" --workspace-root .agent-workspace --repo-root . --format markdown
```

The output correctly identified the current repository state as:

```text
Status: needs_workflow_coverage
Coverage: fallback_only
```

This is an important positive signal. The new planner did not blindly say the task was ready. It exposed a real verification coverage problem.

## Current Checkpoint Failure State

The required fast Checkpoint verification was run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m visual_agent.cli codex-check --workspace-root .agent-workspace --repo-root . --format markdown
```

It failed for existing workspace reasons:

- `alipay_miniprogram_browser_acceptance` failed because visible text did not satisfy the expected text contract.
- `checkout_verification` failed because the demo HTML path was missing under `.agent-workspace/examples/web/`.
- `desktop_ocr_real_acceptance` failed because the OCR workflow was missing the `engine` input.
- Coverage was `fallback_only`, meaning selected workflows were broad fallbacks rather than precise workflow coverage for the changed files.

The latest status file changed to failing:

```text
Status: FAILING
Failing:
- desktop_ocr_real_acceptance_probe: observe_before [KNOWN]
```

This does not invalidate the new planner, but it does show that the current workspace cannot yet serve as a clean product acceptance baseline.

## Technical Self-Critique

### 1. The implementation is still plan-only

`chief-plan` creates a structured task package, but it does not yet:

- create git worktrees
- launch Codex or Claude Code
- stream worker logs
- detect worker completion
- compare worker diffs
- choose a winner
- route failed verification evidence back to a worker

So the product claim must remain: "chief-engineer planning layer," not "full multi-agent orchestration."

### 2. Worker commands are templates, not adapters

The current command templates are intentionally simple:

```text
codex "..."
claude "..."
```

This is useful for manual testing, but it is not a robust runtime. A real implementation needs a worker adapter layer that can handle:

- executable discovery
- PTY process management
- working directory setup
- session IDs
- timeout and cancellation
- output redaction
- completion detection
- retry policy

The public projects that manage CLI coding agents usually converge on PTY/session management. Checkpoint should probably do the same, but only after the plan artifact is stable.

### 3. Worktree lifecycle is missing

The plan says "git worktree per agent before merging," but no worktree is created yet.

A safe implementation needs:

- branch naming rules
- workspace cleanup rules
- dirty tree detection
- conflict detection
- artifact collection per worktree
- merge or cherry-pick strategy

Without this, parallel agents can still collide in one working tree.

### 4. Coverage status is advisory, not a hard gate

The CLI currently returns non-zero only for `blocked`, such as a missing workspace. It still exits zero for `needs_workflow_coverage`.

That is acceptable for a planning command, but it is not acceptable for CI or automatic dispatch. Before adding auto-dispatch, we need either:

- a `--strict` option that exits non-zero for `needs_workflow_coverage`, or
- a separate `chief-dispatch` command that refuses to run when coverage is weak.

### 5. The acceptance criteria are generic

The current acceptance criteria are useful but broad:

- objective must be satisfied
- tests/build should pass
- Checkpoint verification must not fail
- inspection-only is partial evidence

A better chief engineer must infer domain-specific acceptance criteria from:

- user objective
- changed files
- existing workflow descriptions
- latest failure summaries
- product issue groups
- previous run history

For example, "fix checkout total display" should produce criteria about totals, currency, discounts, and confirmation state, not only generic test passing.

### 6. The plan is not yet durable product state

The command can write `--output`, but there is no first-class workspace plan directory or index.

A real workflow needs something like:

```text
.agent-workspace/chief_plans/<plan_id>/plan.json
.agent-workspace/chief_plans/<plan_id>/workers.jsonl
.agent-workspace/chief_plans/<plan_id>/verification.json
```

Without durable plan state, the product cannot resume orchestration or audit what happened.

### 7. Existing workspace quality blocks demos

The current workspace is not clean enough for a convincing demo because `codex-check` fails before the new chief-engineer loop can prove value.

Before showing this to users, we need a stable demo workspace where:

- the default workflow passes
- a seeded defect fails
- a worker fix can be verified
- the final report is clear

## Product Self-Critique

The strongest product insight is that users do not want to learn Codex, Claude Code, worktrees, test commands, and visual verification. They want to say what they want and have the system manage the engineering process.

The risk is that we accidentally build an engineer-facing orchestration toy instead of a user-facing delivery system.

The product should be judged by these questions:

- Can a non-expert user state a goal without writing a perfect prompt?
- Does the system know when the task is not ready to dispatch?
- Does it choose or generate the right verification?
- Does it prevent "AI says done" without evidence?
- When verification fails, does it explain and route the failure back into repair?

The first implementation only touches the second and fourth questions. It is not enough yet.

## Recommended Next Development Path

### Phase 1: Make plans durable

Add:

- `checkpoint chief-plan --save`
- workspace plan storage
- plan IDs
- plan detail/list commands
- strict status option

Done when a plan can be resumed in a new terminal or chat.

### Phase 2: Add dry-run dispatch

Add:

- `checkpoint chief-dispatch --plan <id> --dry-run`
- generated worktree names
- worker command preview
- verification command preview
- no process launching yet

Done when a user can see exactly what would run before approving agent execution.

### Phase 3: Add one real worker adapter

Start with Codex only.

Add:

- executable detection
- PTY launch
- output capture
- timeout
- cancellation
- worker run report

Do not add Claude Code until the adapter interface is stable.

### Phase 4: Add verification feedback loop

After a worker exits:

- run `codex-check`
- parse verdict
- summarize latest failure if needed
- generate a repair prompt
- optionally redispatch once

Done when a failed workflow automatically becomes a useful repair brief.

### Phase 5: Add comparison and merge review

For multiple workers:

- compare diffs
- compare verification evidence
- identify riskier changes
- recommend one candidate
- never auto-merge without explicit policy

Done when parallel workers produce a reviewable decision, not just more terminal output.

## Immediate Cleanup Before Next Demo

The current workspace should be cleaned before using this as a product demo:

- fix or remove stale `checkout_verification` fixture path
- provide required OCR inputs or mark the OCR workflow slow/manual
- add precise `affects` paths to verification workflows
- create one focused chief-engineer demo workflow
- rerun `codex-check` until the baseline is passing

## Bottom Line

The direction is valid, and the first implementation is intentionally scoped correctly. It creates a chief-engineer task package by reusing Checkpoint's existing verification intelligence.

However, it is not yet agent orchestration. The next serious milestone is not more planning text. It is durable plan state plus safe dry-run dispatch. Only after that should we launch real Codex or Claude Code worker processes.

The product should keep one rule:

> More agents are not the product. Verified delivery is the product.

## 2026-07-02 Addendum

Phase 1 and Phase 2 are now implemented: plans are durable, vague goals are
blocked before dispatch, agent capabilities live in YAML profiles, and
`agents doctor/show/list` exposes local Codex / Claude Code / Gemini capability
state.

Phase 3 and Phase 4 now have a first executable slice:

- `chief-dispatch --plan <id> --dry-run` previews worktree path, branch,
  worker command, verification command, and record paths.
- `chief-dispatch --execute` is intentionally narrow: only the Codex coding
  adapter can run. It creates a git worktree, runs `codex exec`, captures logs,
  writes `workers.jsonl`, runs `codex-check`, and writes `verification.json`.
- Failed verification builds a repair brief from latest failure evidence; with
  `--auto-repair-once`, the same Codex worktree receives one repair prompt and
  verification runs again.

Gemini was added, but not as a default coding worker. It is a
`multimodal_inspection` lane for screenshots, visual evidence, and product
semantics. This preserves the rule above: Checkpoint can use Gemini's world
model without becoming a multi-agent coding panel.
