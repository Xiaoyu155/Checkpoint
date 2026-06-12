# Checkpoint Framework

This project is organized around a stable automation kernel:

```text
Observation -> Target -> Selector -> Action -> Workflow -> Audit -> Report
```

## Core Types

- `Observation`: structured state from a provider such as DOM, UIA, screen, OCR, or vision.
- `Target`: semantic description of what to operate on.
- `LocationEvidence`: why a target was resolved, including provider, confidence, bounds, and handle.
- `ActionResult`: audited result of a click, type, paste, or future action.
- `Workflow`: YAML/JSON workflow composed of auditable steps.

## Providers

Current providers:

- `observe_html`: deterministic local HTML provider for tests and demos.
- `observe_fixture`: replay saved observations as regression tests.
- `observe_dom`: Playwright web DOM provider.
- `observe_browser`: persistent Playwright page for native `locator.click/fill`.
- `observe_uia`: Windows UI Automation provider.
- `observe_ocr`: OCR text-box provider for screenshots or images.
- `observe_vision`: VLM-style screen-state description provider.
- `observe_screen`: screenshot provider.

Provider rule:

> Prefer structured observations first. Vision and screenshots are fallback layers.

Providers are registered through `ProviderRegistry`, so new sources can be added
without changing the workflow runtime dispatch loop.

## Workflow Safety

- Workflows default to dry-run.
- Real clicks require `--allow-click`.
- Browser workflows with `observe_browser` execute DOM-native Playwright
  `locator.click/fill` when a selector handle exists; coordinates are fallback.
- Browser workflows record response/request-failure events for `assert_response`.
- Browser workflows can save downloads under `.runs/<run-id>/downloads/`
  with `expect_download`, then verify them with `assert_file_exists`.
- Browser workflows can load `storage_state` in `observe_browser` and save
  current session state with `save_storage_state`.
- DOM targets can constrain matches by table row text with `row_text`,
  `row_contains_text`, or `row_text_regex`.
- DOM targets can constrain matches by table column header with
  `column_header`, `column_contains_text`, or `column_text_regex`.
- DOM targets can constrain matches by nearby text with `near_text`,
  `near_contains_text`, or `near_text_regex`.
- DOM targets can constrain matches to a dialog/scope with `scope_role`,
  `scope_text`, or `scope_contains_text`.
- `type` and `paste` values are masked in audit metadata.
- Steps marked `sensitive: true` store only salted SHA-256.
- Every run writes `.runs/<run-id>/workflow_result.json`.
- Every step writes `.runs/<run-id>/<step-id>.json`.
- Every workflow run writes `state.json` for checkpoint/resume.
- `run-workflow` and `workspace-run` create a root `workflow.lock` by default,
  so two runs do not concurrently operate the same desktop/browser resource.
  Stale locks are replaced after the configured TTL.
- `--queue-when-locked` turns lock contention into a bounded local wait queue
  and records wait time/attempts in `run_queue`.
- Failed steps include `metadata.failure_diagnosis` with expected state,
  observed state, screenshot artifact when available, deterministic recovery
  suggestions, and structured failure classification.
- Known framework noise can be labeled explicitly as `known_issue` so repeated
  demo warnings do not get mistaken for generic regressions.
- When a screenshot artifact exists, failure diagnosis performs a best-effort
  OCR pass and stores the result under `failure_diagnosis.evidence.ocr`.
- The same failure path also performs a best-effort VLM pass and stores the
  result under `failure_diagnosis.evidence.vision`.
- Planner-generated drafts must pass `workspace-check-plan` before execution;
  the check never grants execution rights and always requires dry-run.
- High-risk planner capabilities such as `save_storage_state` are blocked unless
  explicitly allowed by a human-controlled flag.

## Common Commands

Validate:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli validate-workflow --file examples/local_html_form_workflow.yaml
.\.venv\Scripts\python.exe -m visual_agent.cli validate-workflow --file examples/minimal_testable_workflow.yaml --strict
.\.venv\Scripts\python.exe -m visual_agent.cli preflight-workflow --file examples/minimal_testable_workflow.yaml --strict
```

Strict validation is intended for production readiness checks. It requires at
least one observation, at least one verification assertion, sensitive flags for
secret-like inputs, and explicit confirmation for high-risk actions.

Workflow files should declare their DSL contract:

```yaml
schema_version: 1
min_runtime_version: "0.1.0"
name: my_workflow
version: 1
```

The runtime writes `workflow_schema_version` and `runtime_version` into every
`workflow_result.json`, and run reports expose both fields.

`run-workflow` and `workspace-run` execute preflight by default. Preflight
combines workflow validation with capability availability checks, and blocks a
workflow when it uses an unavailable provider/action such as `observe_uia` on a
machine without UIA installed. Use `--skip-preflight` only for controlled
debugging.

Run locking is also enabled by default:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/minimal_testable_workflow.yaml --lock-ttl-seconds 600
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/minimal_testable_workflow.yaml --queue-when-locked --lock-wait-seconds 60 --lock-poll-seconds 0.5
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/minimal_testable_workflow.yaml --no-lock
```

`--no-lock` should be reserved for deterministic tests or controlled debugging,
not for real desktop/browser automation.

Run dry-run demo:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/local_html_form_workflow.yaml --inputs-file examples/inputs/demo_login.json
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/minimal_testable_workflow.yaml --run-profile dry-run
```

Run browser DOM-native demo:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH='D:\longxia agent\.pw-browsers'
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/browser_form_workflow.yaml --inputs-file examples/inputs/demo_login.json --run-profile approved
```

Run profiles:

- `dry-run`: default; all mutating actions are skipped.
- `supervised`: allows low/medium-risk real actions, blocks high-risk actions.
- `approved`: allows high-risk actions only when the step declares `require_confirm: true`.

`--allow-click` remains as a compatibility alias for `--run-profile approved`.

Run browser network assertion demo:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH='D:\longxia agent\.pw-browsers'
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/browser_network_workflow.yaml --allow-click
```

Run browser download assertion demo:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH='D:\longxia agent\.pw-browsers'
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/browser_download_workflow.yaml --allow-click
```

Run browser auth-state demo:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH='D:\longxia agent\.pw-browsers'
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/browser_auth_save_workflow.yaml --allow-click
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/browser_auth_restore_workflow.yaml --allow-click
```

Run browser table row/column demo:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH='D:\longxia agent\.pw-browsers'
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/browser_table_row_workflow.yaml --allow-click
```

Run browser business-backend combo demo:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH='D:\longxia agent\.pw-browsers'
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/browser_business_backend_workflow.yaml --run-profile supervised
```

List runs:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli list-runs --limit 5
.\.venv\Scripts\python.exe -m visual_agent.cli report-run --run-dir .runs\<run-id>
.\.venv\Scripts\python.exe -m visual_agent.cli report-run --run-dir .runs\<run-id> --format markdown
```

`report-run` emits a schema-versioned run report with step status, elapsed
seconds, providers, targets, artifacts, downloads, and failure diagnosis. The
same data can be rendered as Markdown for human review.

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Run failure diagnosis demo:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/failure_diagnosis_workflow.yaml
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/ocr_failure_diagnosis_workflow.yaml
```

Run OCR mock demo:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/ocr_mock_workflow.yaml
```

Run VLM mock demo:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/vision_mock_workflow.yaml
```

## Workspace Layer

A workspace turns loose files into a manageable automation project:

```text
workspace/
  workspace.json
  workflows/
  inputs/
  fixtures/
  runs/
  reports/
  regression_tests/
```

Commands:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli init --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli show-status --workspace-root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli verify-impl --workspace-root .agent-workspace --task-description "Verify the current change" --run-profile dry-run --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-validate --root .agent-workspace --strict
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-planner-context --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-check-plan --root .agent-workspace --file workflows/local_html_form_workflow.yaml
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-run --root .agent-workspace --workflow local_html_form_workflow --inputs-file demo_login.json
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-reports --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-report-index --root .agent-workspace --rebuild
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-report-index --root .agent-workspace --failed-only
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-report-detail --root .agent-workspace --run-id <run-id> --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-tag-report --root .agent-workspace --run-id <run-id> --review-status needs_fix --tag selector --note "needs selector update"
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-report-tags --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-export-regression-fixture --root .agent-workspace --run-id <failed-run-id>
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-promote-regression --root .agent-workspace --run-id <failed-run-id>
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-regression-tests --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-run-regression-tests --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-queue-submit --root .agent-workspace --workflow local_html_form_workflow --inputs-file demo_login.json
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-queue-run-next --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-dashboard --root .agent-workspace --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-gui --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli external-samples-check
.\.venv\Scripts\python.exe -m visual_agent.cli external-samples-readiness --workspace-root .
.\.venv\Scripts\python.exe -m visual_agent.cli external-samples-readiness --workspace-root . --require-live-auth
.\.venv\Scripts\python.exe -m visual_agent.cli external-sample-run-plan --workspace-root .agent-workspace --sample-id external_ecommerce_orders_readonly
.\.venv\Scripts\python.exe -m visual_agent.cli external-sample-run-plan --workspace-root .agent-workspace --sample-id external_ecommerce_orders_readonly --require-live-auth
.\.venv\Scripts\python.exe -m visual_agent.cli external-sample-run --workspace-root .agent-workspace --sample-id external_ecommerce_orders_readonly --run-profile dry-run
.\.venv\Scripts\python.exe -m visual_agent.cli external-sample-batch-report --workspace-root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli external-sample-batch-report-index --workspace-root .agent-workspace --rebuild
.\.venv\Scripts\python.exe -m visual_agent.cli external-sample-batch-rerun-submit --workspace-root .agent-workspace --report-id external-samples-...
.\.venv\Scripts\python.exe -m visual_agent.cli auth-state-plan --source path\to\storage_state.json --name seller-sandbox-state --workspace-root .
.\.venv\Scripts\python.exe -m visual_agent.cli auth-state-import --source path\to\storage_state.json --name seller-sandbox-state --workspace-root .
.\.venv\Scripts\python.exe -m visual_agent.cli auth-state-inspect --path .agent-auth\seller-sandbox-state.json
.\.venv\Scripts\python.exe -m visual_agent.cli auth-state-probe --path .agent-auth\seller-sandbox-state.json --url https://seller.sandbox.example.com/probe --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli model-credentials-inspect --source model_api_keys.txt --preferred openai --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli model-api-probe-plan --source model_api_keys.txt --preferred openai --base-url https://api.example.test --endpoint /v1/models --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli model-api-probe-plan --source model_api_keys.txt --preferred openai --run --timeout-seconds 20 --max-completion-tokens 64 --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate --profile local --workspace-root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate --profile ci --workspace-root .agent-workspace --run
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate --profile ci --workspace-root .agent-workspace --run --fail-on-risk-policy-error
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate-index --workspace-root .agent-workspace --rebuild
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate-index --workspace-root .agent-workspace --strict-policy-failed true
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate-reports --workspace-root .agent-workspace --strict-policy-failed true
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate-index --workspace-root .agent-workspace --strict-policy-failed true --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate-reports --workspace-root .agent-workspace --strict-policy-failed true --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli install-ci-templates --root . --workspace-root .agent-workspace --overwrite
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-risk-policy-check --root .agent-workspace
.\scripts\quality_gate.ps1 -Profile local
```

`workspace-run` exports Run Report 2.0 to `reports/<run-id>.json` and
`reports/<run-id>.md` by default. Use `--no-report-export` only when a test or
debugging task needs to avoid report artifacts. It also refreshes
`reports/index.json`, a compact report index for GUI and Planner reads.
Human review annotations are stored separately in `reports/tags.json` and are
merged into report index entries without modifying the original report files.
Failed runs can be exported into `fixtures/regression/` plus a draft pytest file
under `reports/regression/`; this keeps failure samples as reviewable assets
before they are promoted into the permanent test suite.
Promotion writes executable workspace-level tests into `regression_tests/` and
refreshes `regression_tests/index.json`.
`workspace-run-regression-tests` executes that directory with pytest and writes
JSON/Markdown reports under `reports/regression_runs/`.
`workspace-queue-submit`, `workspace-queue-list`, `workspace-queue-cancel`,
`workspace-queue-retry`, and `workspace-queue-run-next` manage a persisted
workspace queue under `queue/tasks.json`; pending tasks are ordered by priority
and creation time, with attempts/history kept for review.
`workspace-dashboard` provides a compact console data layer for GUI work. It
combines workspace status, recent runs, report index entries, quality gate
status, regression tests, and queue tasks, and can render JSON or Markdown.
`workspace-report-detail` expands one report by run id, including compact step
rows, artifacts, downloads, annotations, and failure diagnosis for GUI detail
views or human Markdown review.
`workspace-gui` opens a tkinter desktop console using the same dashboard and
report-detail data layers. It shows summary cards, recent report selection, the
selected report Markdown, report artifacts, workspace auth-state files, and
external sample readiness without adding a third-party GUI dependency. Its Run
Dry, Run Next, Cancel, Retry, Open Artifact, Inspect Auth, Readiness, and Plan
External buttons build explicit action plans and reuse the
workspace/scheduler/path/readiness safety boundaries, with workflow runs
defaulting to dry-run.
GUI actions that mutate run, queue, readiness, auth-state, or external sample
batch/rerun state attach a `refreshed_model` snapshot. The snapshot is built
through the same console model entrypoint and retains the just-created `run_id`
or selected `batch_report_id`, so a UI client can refresh lists and details
without guessing which entity should stay selected.
The tkinter console consumes that snapshot through a shared selection-state
helper: after an action it refreshes summary card values, report, queue,
artifact, auth-state, readiness, and batch-report selectors, then shows the
selected report or batch Markdown unless the action deliberately displays a plan
or summary payload.
Button availability is computed from the same model and current selector state:
Run Next requires pending queue work, Cancel requires a pending selected task,
Retry requires a failed or canceled task, batch actions require a selected batch
report, and external-sample rerun submit requires a ready failed sample.
`examples/local_business_backend_workflow.yaml` is a deterministic business
backend fixture covering SPA state text, form fields near labels, paginated
controls, table row/column disambiguation, and dialog-scoped actions.
`examples/browser_business_backend_workflow.yaml` runs the same backend shape
through a routed Playwright page. It uses `observe_browser reuse_page` to re-read
SPA state after clicks, asserts a network response, closes a dialog, checks
pagination text, and verifies a downloaded CSV.
`examples/windows_notepad_demo_workflow.yaml` is a deterministic Windows
software fixture covering a UIA window assertion, two edit controls, and a save
button action.
OCR real-engine validation is explicit: `detect_tesseract()` checks the
`pytesseract` package, the Tesseract binary, version discovery, and runtime
errors. `observe_ocr` records that diagnostic under `engine_status` and keeps
mock OCR deterministic when the real engine is absent.
Local VLM validation follows the same pattern: `detect_vlm_backend()` checks
`qwen2-vl` or `moondream` dependencies, validates `model_path`, and records
diagnostics under `engine_status` without downloading models implicitly.
`external-samples-check` validates controlled external business-backend samples:
sample workflows must observe an external HTTPS URL, include assertions, disable
live execution, avoid inline secrets, and keep mutating steps dry-run or
confirmation-gated. Catalog entries also declare account environment, allowed
domains, storage-state policy, and download policy. `external-samples-readiness`
checks the runtime prerequisites, including whether required storage-state files
exist under the selected workspace root.
For real account 联调, `--require-live-auth` inspects only redacted
Playwright `storage_state` metadata, verifies that cookie or origin hosts match
the sample `allowed_domains`, detects empty sessions and fully expired
cookie-only sessions, and adds `auth_state_not_ready` as a blocker without
printing cookie or localStorage values.
`model-credentials-inspect` provides the same redacted inspection pattern for
local model API credential files. It defaults to preferred provider
`openai`; if that provider is missing, the command reports
`preferred_available=false` and does not silently select another key.
`model-api-probe-plan` turns that selected credential into a redacted readiness
plan for a read-only API probe. Without `--run` it records base URL, endpoint,
model, and blockers without sending secrets; with `--run` it sends one bounded
OpenAI-compatible chat health check with timeout and token limits.
The bundled catalog now covers four sandbox-style business shapes: ecommerce
orders, support ticket triage, inventory restock, and finance reconciliation
export. Local HTML fixtures live next to the catalog as reference page shapes
for deterministic route/local-server tests.
Those sample workflows keep their sandbox HTTPS URLs but use Playwright routes
to fulfill pages from local fixtures; the finance reconciliation sample also
routes a CSV download so the download path can be verified without external
network access.
The GUI readiness panel renders that same data as ready/blocked sample options,
summary cards, requirements, blockers, allowed domains, download policies, and
storage-state file existence without reading cookie or token values.
`external-sample-run-plan` and `external-sample-run` provide the guarded path
from readiness to execution: a sample cannot run while blockers remain, and the
entrypoint only accepts `dry-run` or `supervised` profiles. Ready samples are
copied into the workspace workflow tree and executed through the normal
workspace runner, so preflight, reports, locks, and audit behavior stay intact.
`external-sample-batch-plan` and `external-sample-batch-submit` extend that path
to the full catalog: ready samples can be materialized and queued through the
existing workspace queue, while blocked samples are skipped with their blockers
preserved.
`external-sample-summary` merges readiness, queue state, and latest external
sample reports by `sample_id`, giving the GUI a compact batch-result model.
`external-sample-batch-report` exports that model as JSON and Markdown under
`reports/external_samples/`, so a queued catalog run can be reviewed as one
batch artifact.
`external-sample-batch-report-index` and `external-sample-batch-reports`
summarize those historical batch artifacts with status and sample filters; the
GUI model exposes the same entries as batch report options.
`external-sample-batch-failures`, `external-sample-batch-rerun-plan`, and
`external-sample-batch-rerun-submit` make a selected batch actionable: failed
samples are summarized, only ready failures become rerun candidates, and blocked
failures keep their blockers.
Batch Markdown reports include the same operational context directly in the
artifact: batch status, failure tables, rerun command hints, blocked-sample
remediation hints, and clean-batch review notes.
The GUI model also exposes `selected_batch_report_markdown`, and the desktop
window updates the detail pane when a batch report is selected, so operators can
review batch artifacts without leaving the console.
Batch export and batch rerun GUI actions return that updated model immediately,
including the selected batch report Markdown and current queue counts.
The desktop callback keeps the selected batch report active after export or
rerun planning/submission, so operators do not need to manually reopen the
latest batch artifact.
Action feedback is normalized so callbacks consistently show either an explicit
plan/summary payload or the action message/status.
Readiness and batch detail Markdown add GUI-focused status summaries before the
long-form body. Readiness summaries group ready and blocked samples and include
remediation hints; batch summaries surface failed samples, blocked samples, and
ready rerun candidates before the full exported report.
The desktop GUI uses `safe_execute_gui_action` around callbacks. The strict
`execute_gui_action` path still raises for invalid operations, while the safe
wrapper returns `status=error`, error type/message, a recovery hint, and a
refreshed console model so the window can update after recoverable failures.
Safe callbacks append compact audit events to `gui/actions.jsonl` for both
success and error results. Events include action, status, message, a compact
plan/result payload, and omit the full refreshed model; the console model
exposes recent `gui_action_events`.
The GUI action history is also exposed as selector options and Markdown detail:
operators can open the latest action events from the desktop console, and
`list_gui_action_events` supports action/status filters for future views or CLI
reporting.
`workspace-gui-actions` exposes the same history in headless environments. It
returns JSON by default or Markdown with `--format markdown`, and supports
`--action`, `--status`, and `--limit`.
`workspace-gui-action-index` builds a Planner/CI-friendly summary over recent
GUI events, including total events, success/error counts, error rate,
per-action counts, failed actions, and compact recent error details.
Planner and CI consumers read the same signal through
`workspace-planner-context` and `quality-gate`: both expose warning-only GUI
action history risk summaries for high recent error rates and failed actions.
Risk policy is configurable from `workspace.json`:
`quality.gui_action_history.error_rate_threshold`, `history_limit`, and
`failed_action_limit` set workspace defaults, while
`quality.gui_action_history.profiles.planner/local/ci` can override those
values per consumer profile.
`workspace-risk-policy-template` prints a copyable `quality` fragment for
`workspace.json`, including GUI action thresholds, profile overrides, and
dashboard health trend attention directions.
`workspace-risk-policy-check --root <workspace>` validates the policy that is
actually applied in a workspace and reports typed issues for invalid thresholds,
unknown profiles, unsupported trend directions, and repair suggestions.
`workspace-risk-policy-plan --root <workspace>` previews a mergeable
`workspace.json` quality policy patch. By default it fills missing policy fields
without writing the manifest; `--apply` writes the patch, and `--overwrite`
allows template defaults to replace existing risk policy values.
The workspace dashboard includes the same policy check result, and invalid
policy errors raise the `workspace_risk_policy_invalid` health issue; the
desktop GUI mirrors it in a Risk Policy summary card.
Dashboard Markdown and the GUI detail model render the policy issues as a table
with level, code, path, message, and suggestion.
The desktop GUI exposes the same patch workflow as actions: Plan Policy previews
the merged quality policy without writing, while Apply Policy writes the patch
and refreshes the Risk Policy card and detail model.
Plan and Apply feedback is rendered as Markdown with apply state, changed paths,
and before/after validation status so the operator can review the result without
reading raw JSON.
GUI action history stores compact policy patch audit summaries with changed
paths and before/after validation status, while avoiding the full patch body in
the event log.
Quality gate reports include the same workspace risk policy check status, and
`quality-gate-index` aggregates policy error/warning counts for CI history
review.
By default these policy errors remain warning-only inside `quality-gate`; passing
`--fail-on-risk-policy-error` records a strict policy gate summary and marks an
executed gate failed when the workspace policy has validation errors.
`quality-gate-index` aggregates strict policy gate enabled/failed counts and
policy error totals, and each latest entry exposes whether strict policy failed.
`quality-gate-index --strict-policy-failed true|false` and
`quality-gate-reports --strict-policy-failed true|false` filter historical
reports by that strict failure state.
Both commands support `--format markdown`, rendering filters, aggregate counts,
strict policy failure totals, and report paths as CI-readable tables.
Generated CI/local quality gate templates also run
`workspace-risk-policy-check` before `quality-gate`, and
`install-ci-templates` returns copyable policy check/plan commands so a team can
wire the same check into other CI systems.
The desktop `workspace-gui` also surfaces this risk signal as a GUI Action Risk
summary card and Markdown detail, including warnings, failed actions, and
recent error recovery hints.
Risk summaries are mapped back to recent failed GUI action events through
`gui_action_risk_event_options`, so the desktop Risk button can select and show
the relevant action event detail directly.
`gui_action_history_remediation_items` deduplicates recovery hints by action,
error type, and hint text; the GUI Action Risk Markdown starts with this
remediation checklist before the raw warning and recent-error tables.
Headless consumers can use `workspace-gui-action-index --risk` to export the
same JSON/Markdown remediation checklist, and `quality-gate` includes it in
its risk summary report.
Risk summaries also include a newest-vs-older trend window. The trend compares
error rate and remediation count deltas, and classifies the result as
`improving`, `worsening`, `stable`, `mixed`, or `insufficient_history`.
Quality gate report entries and `quality-gate-index` expose the same risk
trend direction, warning count, remediation count, and aggregate trend counts
for CI history views. The same index also exposes strict policy gate failure
counts for CI history views.
`workspace-dashboard` reads those index fields and exposes quality risk warning
totals, latest risk trend direction, and strict policy gate failure counts;
dashboard Markdown also includes a filtered strict policy failure history table.
`workspace-gui` shows the same signal in the Quality Gates summary card and
provides a Strict Failures button that opens the filtered quality gate Markdown
inside the desktop detail pane.
When the latest risk trend is `worsening`, dashboard health includes
`gui_action_risk_worsening` by default. Workspaces can override the attention
policy with
`quality.gui_action_history.health.attention_trend_directions`, for example
`["worsening", "mixed"]`; dashboard JSON and Markdown expose the active policy.
`external-sample-rerun-plan` and `external-sample-rerun-submit` select failed
external samples from that summary, rerun only those that are ready, and keep
blocked failures in a skipped list with their blockers.
External sample queue tasks preserve the guarded run plan in
`metadata.external_sample`. When `workspace-queue-run-next` completes one of
those tasks, the generated workspace reports are annotated with the same
external sample block used by direct sample runs.
After a guarded external sample run, the workspace JSON report, Markdown report,
report index, and GUI report detail include an `external_sample` block with the
sample id, readiness state, requirements, blockers, allowed domains, and
storage/download policy metadata.
`auth-state-plan`, `auth-state-import`, `auth-state-inspect`, and
`auth-state-probe` support bringing an existing Playwright storage-state file
into `.agent-auth/`. They emit only redacted metadata such as cookie count,
origin count, domains, file size, and a manifest path; cookie and token values
are never printed. The probe entrypoint starts a browser context with the
storage state, routes a local readonly probe page, and reports whether the state
loaded, whether the URL domain matches, and whether session material is present.
The GUI action layer can import or inspect auth-state files through the same
redacted metadata path, so desktop users do not need to inspect raw
`storage_state` JSON.
`quality-gate` groups the current release checks into local and CI profiles.
Without `--run` it prints the planned commands; with `--run` it executes them
and writes JSON/Markdown reports under `reports/quality_gates/`. It also
refreshes `reports/quality_gates/index.json`; `quality-gate-index` can rebuild
or filter that index for GUI and Planner reads.
`install-ci-templates` installs `.github/workflows/checkpoint-quality-gate.yml`,
`scripts/quality_gate.ps1`, and `scripts/quality_gate.bat` so local and CI
release checks call the same risk policy check and `quality-gate` entrypoints.

`workspace-planner-context` returns workflow refs, input file metadata, fixture
metadata, recent run summaries, report index entries, and atomic capabilities
without reading input file contents.

`workspace-check-plan` validates a proposed workflow draft against static
workflow rules, planner-visible atomic capabilities, risk policy, workspace path
boundaries, observation presence, and verification assertions. It returns a JSON
check result only; it does not run the workflow.

`workspace-planner-draft` builds a planner-safe prompt from the same workspace
context and can ask the preferred model provider, currently `openai`, for a
workflow YAML draft. The command is plan-only by default; with `--run` it sends a
bounded chat request, parses the YAML response, normalizes common model step
shapes such as `params:` and `name:`, then immediately runs
`workspace-check-plan`. It never executes the workflow. It writes a workflow only
when `--save-as <name>` is explicit, the draft is valid, and the target remains
under workspace `workflows/`; existing files require `--overwrite`.
`--preview-save` uses the same validation and path checks, but returns a unified
diff without writing the file. The desktop console exposes the same path through
Preview Draft for the selected workflow, and Generate Draft can call the
preferred model provider before entering the same no-write diff preview.

## Template Catalog

Templates are stored under `templates/<template-id>/`:

```text
template.json
workflow.yaml
inputs.json
fixture.html
```

Current templates:

- `login_form`: basic web login form.
- `order_entry`: ERP order entry flow.
- `ecommerce_download`: ecommerce order export flow.
- `external_readonly_probe`: readonly external URL observe/assert probe.

Install and run:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli templates
.\.venv\Scripts\python.exe -m visual_agent.cli install-template --root .agent-workspace --template order_entry
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-run --root .agent-workspace --workflow order_entry --inputs-file order_entry_inputs.json
.\.venv\Scripts\python.exe -m visual_agent.cli install-template --root .agent-workspace --template external_readonly_probe
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-run --root .agent-workspace --workflow external_readonly_probe --inputs-file external_readonly_probe_inputs.json
```

## Extension Points

The runtime exposes capabilities through:

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli capabilities
.\.venv\Scripts\python.exe -m visual_agent.cli atomic-capabilities
.\.venv\Scripts\python.exe -m visual_agent.cli doctor
```

`atomic-capabilities` returns planner-visible capabilities with input schema,
output schema, dry-run support, risk level, and dependency availability.

To add a new provider:

1. Return an `Observation`.
2. Preserve provider identity in `Observation.provider`.
3. Register it on a `ProviderRegistry`.
4. Add a selector strategy only if the provider needs custom matching.
5. Add fixture-based regression tests.

OCR note: `observe_ocr` supports deterministic `mock_text` for tests and can
use `pytesseract` when the Python package and Tesseract binary are installed.

To add a new action:

1. Add a method to `DesktopActions` or a provider-specific action executor.
2. Return `ActionResult`.
3. Register it on `ActionDispatcher`.
4. Keep dry-run behavior first.
5. Add workflow dispatch and tests.

