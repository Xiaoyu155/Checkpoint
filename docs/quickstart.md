# Checkpoint / DevPacer Quickstart

> **This page covers the AI coding assistant orchestrator use case.**
> For browser/UI workflow automation, see `docs/framework.md`.

## Five-minute walkthrough

See **`docs/五分钟上手.md`** for a step-by-step guide verified end-to-end on 2026-07-04 (7 minutes from install to first `verified` task).

## Minimum commands (copy-paste)

```powershell
# 1. Install (once)
pip install -e .

# 2. Verify agents are available
checkpoint agents doctor

# 3. Init your project workspace (once per project)
cd <your-project>
checkpoint init --root .agent-workspace

# 4. Preview a task (no code changed, no quota used)
checkpoint mission start --goal "Add a slugify function and tests" `
  --test-command "pytest -q" --agent codex

# 5. Execute (runs Codex in an isolated branch, verifies with pytest)
checkpoint mission start --goal "Add a slugify function and tests" `
  --test-command "pytest -q" --agent codex --execute --merge

# 6. Watch status
checkpoint mission list
```

## Queue multiple tasks

```powershell
# Submit tasks to queue, then start a worker daemon
checkpoint mission start --goal "..." --test-command "pytest -q" --queue
checkpoint mission worker --watch
checkpoint dashboard    # browser kanban
```

## Useful reference

| Command | What it does |
|---------|-------------|
| `checkpoint agents doctor` | Check which AI agents are installed and their versions |
| `checkpoint mission list` | Show all missions and their status |
| `checkpoint quota` | Show Claude 5h/7d quota usage |
| `checkpoint chief-run-demo` | Deterministic end-to-end demo (no quota needed) |
| `checkpoint mission worker --watch` | Queue daemon — picks up pending missions automatically |
| `checkpoint dashboard` | Browser kanban board |

## Stop reasons explained

When a mission stops without `verified`, the output shows a `stop_reason`.
Common reasons and fixes:

| Stop reason | Fix |
|-------------|-----|
| `coverage_gap` | Add `--test-command "pytest -q"` (or your test command) |
| `permission_required` | Run `git status`; commit or stash dirty files |
| `same_failure_repeated` | Open `final_report.md`; the AI tried twice and the same test failed — refine the goal |
| `quota_exhausted` | Wait for quota reset or add `--agent claude-code` |
| `worker_error` | Run `checkpoint agents doctor`; confirm agent is installed |
| `needs_clarification` | Make the `--goal` more specific — include a testable acceptance criterion |
