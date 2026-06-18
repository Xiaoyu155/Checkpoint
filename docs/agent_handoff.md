# Agent Handoff Guide

Use this file when opening a new Codex window. The current project is
Checkpoint in `D:\longxia agent`.

## Current Mission

Build Checkpoint into a strict, evidence-driven real acceptance tool for coding
agents. The user cares about the word "real": do not claim product acceptance
from mock data, fixture-only runs, screenshots without actions, or green scripts
that avoid product behavior.

The immediate work is to expand from a minimal desktop OCR proof into broader,
more product-like acceptance tests.

## Non-Negotiable Acceptance Rules

When reporting a pass, include the run id and evidence. Do not just say it
passed.

Product acceptance must stay strict:

- no mock or synthetic evidence
- at least one real user interaction
- one valid operation receipt per counted interaction
- no invalid operation receipts
- post-action observation artifact exists
- post-action assertion is recorded
- post-interaction contract assertion exists
- at least one negative contract assertion exists
- failures are reported, not hidden

Current code exposes these as `product_acceptance_blockers`. If blockers are
non-empty, `is_product_acceptance` must be false even if the level is L3.

## Current Verified Baseline

Latest strict run:

```text
run_id: 20260616-093037-98c79bf6
run_dir: .agent-workspace\runs\20260616-093037-98c79bf6
acceptance_level: L3
is_product_acceptance: true
valid_operation_receipts: 2
invalid_operation_receipts: 0
product_acceptance_blockers: []
```

Workflow steps:

```text
observe_before          observe_ocr            success
assert_start_contract   assert_text_contract   success
type_amount             type                   success
click_approve           click_text             success
assert_result_visible   assert_text_contract   success
```

The one-command repro is:

```powershell
cd "D:\longxia agent"
.\.venv\Scripts\python.exe scripts\desktop_ocr_acceptance_probe.py
```

This opens a clean local desktop Tk window, runs real OCR, types `128` into an
amount field with a real desktop action, clicks APPROVE with a real desktop
action, checks required and forbidden text before the actions, and checks the
exact result text after approval.

The probe also runs a negative case:

```text
run_id: 20260616-093048-036cc131
run_dir: .agent-workspace\runs\20260616-093048-036cc131
status: failed as expected
failed_step: click_approve
reason: post_action_observe did not find PASSED after typing 129
```

This proves the desktop OCR + click + post-action evidence chain. It does not
prove the user's real product yet.

## Key Files

- `scripts/desktop_ocr_acceptance_probe.py`
  One-command real desktop OCR acceptance probe.

- `.agent-workspace\workflows\desktop_ocr_real_acceptance.yaml`
  Installed workflow used by the probe.

- `.agent-workspace\inputs\desktop_ocr_real_acceptance_inputs.json`
  Inputs for the probe. Includes required-before, forbidden-before, and
  required-after contract text.

- `templates\desktop_ocr_real_acceptance\`
  Installable template for the same workflow shape.

- `src\visual_agent\acceptance.py`
  Acceptance levels and strict product acceptance blockers.

- `src\visual_agent\workflow.py`
  Operation receipt generation and post-action assertion evidence.

- `src\visual_agent\reports.py`
  Report output, including product acceptance blockers.

- `tests\test_acceptance_grade.py`
  Tests that prevent weak L3 runs from being called product acceptance.

## What Was Learned

Using Chrome as the carrier for the minimal OCR page caused real environmental
noise: browser extensions and right-click menus polluted the screenshot. That
was a useful failure, not something to hide.

The current probe uses a clean Tk desktop window to isolate the toolchain. The
next step should move back toward realistic application surfaces, but only with
the same strict evidence rules.

## Next Step

Completed: the probe now covers a two-step business flow:

1. Show an editable field such as `AMOUNT`.
2. Type a value with a real desktop action.
3. Click `APPROVE`.
4. Verify the exact value appears in the result.
5. Add a negative case where the wrong value must fail.

Current target:

```text
Before:
ORDER A100
TOTAL 128
READY
AMOUNT field empty
APPROVE
forbidden: PASSED, RECEIPT

Action:
type 128 into AMOUNT
click APPROVE

After:
PASSED RECEIPT TOTAL 128
```

Next: connect the same pattern to an actual app page rather than the Tk probe.

## Verification Commands

Run focused tests after acceptance changes:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_acceptance_grade.py tests\test_templates.py::test_desktop_ocr_real_acceptance_template_installs_actionable_skeleton tests\test_workflow.py::test_workflow_runtime_click_text_records_synthetic_operation_receipt_with_post_observe -q
```

Run the strict desktop probe:

```powershell
.\.venv\Scripts\python.exe scripts\desktop_ocr_acceptance_probe.py
```

Inspect latest report:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli report-run --run-dir .agent-workspace\runs\<run_id> --format markdown
```

Preflight the workflow:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli preflight-workflow --file .agent-workspace\workflows\desktop_ocr_real_acceptance.yaml --workspace-root .agent-workspace --strict --allow-high-risk
```

## Skill Note

The user had a Codex skill warning. The local custom skill was fixed here:

```text
C:\Users\xiaoyu\.codex\skills\youyou-express-service\agents\openai.yaml
```

Its `default_prompt` now includes `$youyou-express-service` and validates.
Codex may still log warnings from marketplace plugin cache entries; do not
confuse those with this project's acceptance work.

## Working Style

Be direct and pragmatic. The user does not want vision statements. They want
small real scripts, reproducible results, strict blockers, and failures that are
allowed to fail.
