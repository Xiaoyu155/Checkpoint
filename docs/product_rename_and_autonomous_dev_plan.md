# Product Rename And Autonomous Development Plan

Date: 2026-07-02
Status: working plan

## 1. Why `Checkpoint` Is No Longer Enough

`Checkpoint` is a good name for the verification engine. It says: stop, check,
record evidence.

It is no longer a good name for the whole product.

The product direction is now larger:

> A user states an outcome. The system turns that outcome into engineering work,
> allocates model/runtime budget, dispatches coding agents, verifies results,
> repairs failures, corrects direction, and reports back only when the work
> needs a human decision.

That is not just a checkpoint. It is an AI development operations system.

The old product answered: "Did this workflow pass?"

The new product must answer:

- What should be built?
- Is the request clear enough to spend tokens on?
- Which agent/model should do which part?
- How many rounds is this worth?
- When should the system stop, repair, downgrade, escalate, or ask the user?
- What evidence proves the result?
- What changed, what risk remains, and what should happen next?

## 2. Naming Recommendation

### Recommended Working Name: `DevPacer`

Meaning:

- `Dev`: software/product development.
- `Pacer`: controls speed, cadence, budget, and direction.

This name fits the product better than orchestration-heavy names like
`Conductor`, `Chief`, or `Loopwright` because the differentiator is not "more
agents." The differentiator is pacing the development loop so work proceeds
without waste.

`DevPacer` says:

- do not let the user babysit the terminal;
- do not let agents spend unlimited tokens;
- keep the work moving at the right speed;
- slow down or stop when the evidence is weak;
- escalate only when a human decision is genuinely needed.

### Brand Architecture

- Public product: `DevPacer`
- Verification engine: `Checkpoint`
- CLI for now: keep `checkpoint` until the rename is technically cheap
- Future CLI alias: `devpacer`
- Core loop name: `mission`
- Execution records: `mission.json`, `rounds.jsonl`, `verification.json`

This avoids a risky big-bang rename. The product can say:

> DevPacer is the AI development operator. Checkpoint is its local verification
> engine.

### Names To Avoid

Quick public search showed close conflicts or confusing overlap for:

- `DevHelm`
- `CodeSteward`
- `AgentWright`
- `Loopwright`
- `Conductor` / `CodeConductor`
- `SpecPilot`
- `Chief` / `ChiefLoop`
- `ForgeLoop` / `LoopForge`

These names are too close to existing AI coding or orchestration projects. They
also over-emphasize agents, loops, or command hierarchy instead of controlled
delivery.

`DevPacer` still needs a real trademark/domain check before public launch, but
it is the best working name from the current set.

## 3. Product Thesis

Official coding-agent vendors will keep making Codex, Claude Code, Gemini CLI,
Cursor, and similar products more powerful. They are not responsible for teaching
every user how to operate those tools economically.

That gap is the product.

DevPacer should not compete with Codex or Claude Code. It should make them
usable by people who do not know:

- how to write implementation prompts;
- how to define acceptance criteria;
- when to use a strong model versus a cheap/fast model;
- when to run one agent versus multiple isolated attempts;
- when a task is too vague to dispatch;
- when to stop a loop before it wastes another hour;
- when a visual/world-model reviewer like Gemini is needed;
- how to read verification evidence.

The user should not have to learn every agent. DevPacer learns the agents and
operates them under policy.

## 4. The Core Product Loop

The product should become a background, multi-round development loop:

1. **Intake**
   - Convert user language into a structured mission.
   - Ask at most a few blocking questions.
   - Refuse to dispatch if "done" is not verifiable.

2. **Planning**
   - Produce an implementation plan in coding-agent language.
   - Select workflows or request new verification coverage.
   - Estimate cost, time, risk, and likely number of rounds.

3. **Budgeting**
   - Set max rounds, wall-clock limit, token/cost cap, model tiers, and approval
     gates.
   - Stop automatically when the mission exceeds the policy.

4. **Dispatch**
   - Create an isolated worktree.
   - Run one coding worker by default.
   - Use Codex for implementation first.
   - Use Gemini as a read-only visual/world-model reviewer, not a coding worker.

5. **Verification**
   - Run Checkpoint workflows and project tests.
   - Treat inspection-only as partial evidence.
   - Treat coverage gaps as a stop condition, not success.

6. **Repair**
   - Build a repair brief from failure evidence.
   - Re-dispatch once or for N bounded rounds.
   - Keep every round in durable records.

7. **Direction Correction**
   - Detect repeated failures, scope drift, oversized diffs, wrong files, or
     weak acceptance.
   - Downgrade, switch model, ask for clarification, or stop.

8. **Handoff**
   - Return a concise status report only when:
     - the mission is verified;
     - the budget is exhausted;
     - a human approval is required;
     - product direction is ambiguous.

## 5. Budget Policy

Budgeting must become first-class product state, not a prompt instruction.

Each mission should carry:

```json
{
  "max_rounds": 3,
  "max_wall_minutes": 60,
  "max_worker_minutes": 45,
  "max_repair_rounds": 1,
  "model_policy": {
    "implementation": "strong",
    "repair": "strong",
    "classification": "fast",
    "visual_review": "multimodal"
  },
  "approval_policy": {
    "allow_file_edits": true,
    "allow_network": false,
    "allow_destructive_commands": false,
    "require_human_for_merge": true
  }
}
```

The system should spend cheap/fast model capacity on classification, routing,
summaries, and evidence compression. It should reserve strong coding models for
actual implementation and hard repair. Multimodal models should only inspect
screenshots and UI evidence when structured evidence is insufficient.

## 6. Agent Language

The product must stop sending casual chat to coding agents.

Every worker prompt should be a task packet with stable sections:

- Objective
- Non-goals
- Allowed files / forbidden files
- Existing evidence
- Acceptance criteria
- Verification commands
- Budget and stop conditions
- Failure handling rules
- Reporting format

This is how Codex, Claude Code, and similar tools "understand" work: not through
better vibes, but through crisp constraints and machine-readable outputs.

## 7. Direction Correction Rules

DevPacer should correct direction automatically when signals are clear:

- Same failed workflow twice -> stop and produce a human-readable diagnosis.
- Diff touches files outside allowed scope -> stop or ask approval.
- Verification coverage missing -> stop and request/generate workflow coverage.
- Inspection-only after interactive task -> rerun with supervised profile or
  mark incomplete.
- Strong model fails twice -> switch strategy, not just retry the same prompt.
- Fast model gives weak plan -> escalate planning to strong model.
- Gemini visual review contradicts DOM/OCR conclusion -> escalate to human with
  screenshots and both interpretations.

The goal is not endless autonomy. The goal is bounded autonomy that knows when
it is no longer useful.

## 8. Next Development Order

### Phase A: Rename Without Breaking The Engine

- Keep `checkpoint` CLI working.
- Add product docs that introduce `DevPacer`.
- Add CLI aliases later, not now.
- Rename user-facing docs first:
  - "Checkpoint" -> verification engine
  - "DevPacer" -> autonomous development operator

Done when a new reader understands why both names exist.

### Phase B: Mission State - first version landed 2026-07-02

Add:

- `.agent-workspace/missions/<mission_id>/mission.json`
- `.agent-workspace/missions/<mission_id>/rounds.jsonl`
- `.agent-workspace/missions/<mission_id>/budget.json`
- `.agent-workspace/missions/<mission_id>/final_report.md`

Current `chief_plans/<plan_id>` can be migrated or wrapped. Do not throw it
away; it is the seed of mission state.

Current status:

- `src/visual_agent/missions.py` writes mission, budget, round, and final report
  records.
- `checkpoint chief-missions list/show` can inspect saved missions across
  terminal restarts.
- Continue/resume from the last round is not implemented yet; current missions
  are durable and inspectable, not resumable.

Done when a mission can survive terminal restarts and continue from its last
round.

### Phase C: Autonomous Round Runner - first version landed 2026-07-02

Add a command:

```powershell
checkpoint chief-run --goal "..." --max-rounds 3 --max-wall-minutes 60 --budget-policy default
```

Behavior:

1. Build or load a chief plan.
2. Save mission state.
3. Dry-run dispatch preview.
4. If gates pass, execute Codex worker.
5. Run Checkpoint verification.
6. If failed, generate repair brief.
7. Re-dispatch until pass, budget exhausted, or blocked.

Current status:

- `checkpoint chief-run --goal ...` builds/saves a plan, creates a mission,
  records a dispatch preview, and stops safely by default.
- `--execute` launches the existing Codex adapter through `chief-dispatch`.
- `--max-rounds 2` supports the first bounded loop: one implementation round
  plus one automatic repair round when verification fails.
- Stop reasons are written into `mission.json` and summarized in
  `final_report.md`.
- `checkpoint chief-run-demo` now creates an isolated checkout demo repo,
  seeds a `Next Step` regression, runs the same mission/dispatch path with a
  deterministic local worker, fails supervised Checkpoint verification once,
  repairs once, and finishes `verified`. This gives auditors a no-token,
  reproducible proof of the loop.
- Worktree verification now prepares a workspace inside the worktree and stores
  records back in the original workspace. This fixes a real correctness risk:
  local HTML workflows must verify the worker's worktree files, not the original
  checkout.
- `checkpoint chief-run --resume <mission_id>` can continue a saved preview
  mission without rebuilding the plan or losing old round records.
- `checkpoint chief-run --background` starts the same resume/execute path in a
  background process and returns immediately; `checkpoint chief-status
  --mission <id>` reads mission status, rounds, final report, pid, and logs.
- Background execution now writes a completion receipt with `exit_code`,
  `completed_at`, result status, and stop reason. `chief-status` reconciles
  health: alive processes stay `running`, processes that exceed
  `max_wall_minutes` are terminated and marked `budget_exhausted`, and missing
  processes without a receipt are marked `worker_error`.
- Current real-repo behavior is intentionally conservative: this dirty
  development tree stops on `coverage_gap` unless `--allow-coverage-gap` is
  explicit.

Done when one command can run a bounded multi-round loop without the user
watching the terminal.

### Phase D: Stop Conditions And Budget Enforcement

Add hard stop reasons:

- `verified`
- `needs_clarification`
- `coverage_gap`
- `budget_exhausted`
- `same_failure_repeated`
- `scope_drift`
- `permission_required`
- `worker_error`

Done when every stopped mission explains why it stopped and what the user should
do next.

### Phase E: Background Mode And Mission Queue - first version landed 2026-07-02

Add:

```powershell
checkpoint chief-queue submit --mission <id>
checkpoint chief-queue list --workspace-root .agent-workspace
checkpoint chief-worker --workspace-root .agent-workspace --watch
checkpoint chief-status --mission <id>
```

The user should be able to leave. The system should write reports and return
with a decision request, not raw logs.

Current status:

- `checkpoint chief-run --background` starts one mission in a background
  process and writes a completion receipt.
- `checkpoint chief-status --mission <id>` reconciles process health and budget
  exhaustion.
- `checkpoint chief-queue submit --mission <id>` queues an already-created
  mission. It refuses non-runnable mission states by default.
- `checkpoint chief-worker --run-once` atomically claims one pending mission and
  executes the existing `chief-run --resume <id> --execute` path.
- `checkpoint chief-worker --watch` is the simple local daemon form. It is not a
  distributed scheduler; it is a safe unattended local worker.
- `checkpoint mission start/status/list/show/queue/worker/memory/import` is the
  high-level user-facing facade. It keeps the same execution paths underneath
  (`chief-run --resume <id> --execute` for work), but gives users one command
  family instead of several `chief-*` commands.
- `checkpoint mission import --file <plan.md>` parses a Markdown development
  plan into mission drafts and saves an auditable import record. Add `--create`
  to create preview missions, and `--queue` to submit those preview missions to
  the existing queue. Import never executes Codex directly.

Done when a mission can run unattended inside strict budget and approval policy.

### Phase F: UI / Dashboard Later

Only after the headless loop is correct:

- show active missions;
- show current round;
- show budget used;
- show pass/fail evidence;
- show approval requests.

Do not build the UI before the loop is trustworthy.

## 9. Immediate Next Sprint

The next sprint should still not be Phase 5 parallel comparison yet.

Harden the single-mission operator first.

Target implementation:

- Run a real Codex token worker in a clean demo repository once, with explicit
  human approval.
- Add queue status/retry/cancel only if the JSON queue proves useful in daily
  use; do not build a large scheduler prematurely.
- Keep Gemini as optional read-only inspection after verification failure or
  visual uncertainty.
- Keep default execution to one Codex worker until the single-worker loop is
  boringly reliable.

Acceptance:

- Seed a known defect in the demo page.
- Run one command with `max_rounds=2`.
- Round 1 fails verification.
- Repair brief is generated.
- Round 2 repairs.
- Verification passes.
- Final report explains cost/rounds/evidence.

This is the real product milestone: not more docs, not more agents, but a
bounded development loop that can finish work while the user is away.
