# Execution-Layer Benchmark Results (2026-07)

## Status

**Frozen direct-Codex B1 evidence exists, but no attempt is accepted as a benchmark PASS yet.**

- Direct Codex accepted: 0/5
- Direct Codex attempted: B1 only, 6 append-only attempts
- Pacer tracked (pre-change) completed: 0/5
- Pacer tracked (post-change) completed: 0/5
- Pacer delegated completed: 0/5
- Default-mode decision: pending P4 A/B evidence

Do not calculate G1 or select a default mode until all compared lanes use the
same frozen source snapshot and all required task rows have accepted evidence.
`direct-B1-20260710-06` produced the correct one-line patch and the operator
gate passed, but the Codex invocation timed out at 600 seconds without a
`turn.completed` usage event, so it remains `INTERRUPTED` rather than accepted.

## Frozen run context

Use `context-20260710-06` for the current direct-Codex evidence. Older contexts
are preserved because they explain failed setup attempts, but they should not be
mixed into final A/B scoring.

| Field | Value |
|---|---|
| Context | `.runs/execution-benchmark/context-20260710-06/context.json` |
| Context id / SHA-256 | `execution-20260710-e858309-06` / `1234608bb68ad0244145c743b37c9cef0cf550f2f8bfe5b9d5e6df688b917c1d` |
| Harness bundle SHA-256 | `3abb1a028129f7022b25c0606fe6af3e6c3245e5a383de434c8e4d50a03f06b8` |
| Target snapshot commit | `e8583099bd8ab073d578fcbe0c6199ab289bcb0e` |
| Target snapshot SHA-256 | `ed37503430fab7d3c20af2228651cdc070d8afcbffbf1abb609702f039054495` |
| Target seed patch SHA-256 | `bfec72342daef270757f32b51f76e2970ea8e778e756f1a8665d544b158b32d2` |
| Target seed patch artifact | `.runs/execution-benchmark/context-20260710-06/targets/common/common_seed.patch` |
| Orchestrator revision / pre wheel | `e8583099bd8ab073d578fcbe0c6199ab289bcb0e` / `6272DC3453D9B684BE6CF440B067EF43AC677392F31075334C11B4B96665EF7D` |
| Codex CLI version | `codex-cli 0.144.0` |
| Codex model/provider | `gpt-5.6-sol` / frozen secret-free `custom` provider |
| Reasoning effort | `ultra` |
| Sandbox | CLI `workspace-write`; runtime `windows.sandbox='elevated'` |
| Approval policy | `never` |
| User config policy | `--ignore-user-config`, with explicit provider/runtime replay |
| Trusted project policy | only the synthetic run target root is trusted |
| Run operator/date | local operator, 2026-07-10 |

The target snapshot must be identical for every compared lane. Record the
orchestrator revision separately so pre-change and post-change Pacer can run
against that same target from independent worktrees or built wheels. If either
tree is dirty, save and hash the exact patch bytes; a commit hash alone is not
an adequate comparison baseline.

## Task inventory

| ID | Type | Definition | Current evidence |
|---|---|---|---|
| B1 | Single-file small fix | `b1_dashboard_worker_status_signature.json` | Baseline gate reproduces failure; direct attempts 01-06 preserved; no accepted PASS |
| B2 | Cross-file feature | `b2_agents_doctor_installed_only.json` | Defined only |
| B3 | Historical execution gap | `b3_repair_raw_failure_evidence.json` | Defined from plan D4; no task run |
| B4 | Exploration required | `b4_background_worker_pid_identity.json` | Defined only; no file hints in worker prompt |
| B5 | Repair chain | `b5_repair_round_secret_redaction.json` | Seed/private-verifier contract reproduces a real failure; no Codex lane run |

## Comparable results

Use one row per task and lane. Token fields must come from JSONL usage events or
be marked `UNAVAILABLE`; never estimate them from output length.

| Task | Lane | Repair strategy | First verification | Total turns | Wall seconds | Input tokens | Cached input | Output tokens | Reasoning tokens | Final verdict | Session id evidence | Artifact link |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|
| B1 | direct-codex-01 | n/a | FAIL | 0 | 12.718 | 0 | 0 | 0 | 0 | FAIL / setup launcher failure | - | `.runs/execution-benchmark/direct-codex-20260710-01/runs/direct-B1-20260710-01/manifest.json` |
| B1 | direct-codex-02 | n/a | PASS | 0 | 1293.625 | 0 | 0 | 0 | 0 | FAIL / `codex_exec_failed`; no `turn.completed` | - | `.runs/execution-benchmark/direct-codex-20260710-01/runs/direct-B1-20260710-02/manifest.json` |
| B1 | direct-codex-03 | n/a | FAIL | 0 | 57.797 | 0 | 0 | 0 | 0 | FAIL / provider missing after config isolation | - | `.runs/execution-benchmark/direct-codex-20260710-01/runs/direct-B1-20260710-03/manifest.json` |
| B1 | direct-codex-04 | n/a | FAIL | 0 | 16.938 | 0 | 0 | 0 | 0 | FAIL / TOML quoting rejected by CLI shim | - | `.runs/execution-benchmark/direct-codex-20260710-01/runs/direct-B1-20260710-04/manifest.json` |
| B1 | direct-codex-05 | n/a | FAIL | 1 | 256.266 | 560635 | 525056 | 4477 | 1390 | FAIL / sandbox read-only; no changes | `019f4b4f-0778-76b0-afa3-59838c8e1b94` | `.runs/execution-benchmark/direct-codex-20260710-01/runs/direct-B1-20260710-05/manifest.json` |
| B1 | direct-codex-06 | n/a | PASS | 0 | 616.985 | 0 | 0 | 0 | 0 | INTERRUPTED / gate PASS, worker timeout | `019f4b67-630f-70f0-be50-6b6781dffb79` | `.runs/execution-benchmark/direct-codex-20260710-01/runs/direct-B1-20260710-06/manifest.json` |
| B2 | direct-codex | n/a | NOT_RUN | 0 | - | - | - | - | - | NOT_RUN | - | - |
| B3 | direct-codex | n/a | NOT_RUN | 0 | - | - | - | - | - | NOT_RUN | - | - |
| B4 | direct-codex | n/a | NOT_RUN | 0 | - | - | - | - | - | NOT_RUN | - | - |
| B5 | direct-codex | interactive | NOT_RUN | 0 | - | - | - | - | - | NOT_RUN | - | - |
| B1 | pacer-tracked-pre | configured | NOT_RUN | 0 | - | - | - | - | - | NOT_RUN | - | - |
| B2 | pacer-tracked-pre | configured | NOT_RUN | 0 | - | - | - | - | - | NOT_RUN | - | - |
| B3 | pacer-tracked-pre | configured | NOT_RUN | 0 | - | - | - | - | - | NOT_RUN | - | - |
| B4 | pacer-tracked-pre | configured | NOT_RUN | 0 | - | - | - | - | - | NOT_RUN | - | - |
| B5 | pacer-tracked-pre | resume/fresh-control | NOT_RUN | 0 | - | - | - | - | - | NOT_RUN | - | - |

Append `pacer-tracked-post` and `pacer-delegated` rows only when those lanes are
implemented and run. Keep failed and stopped runs; they are benchmark evidence,
not rows to discard.

Use append-only run manifests with one of `NOT_RUN`, `INVALID_SETUP`,
`INTERRUPTED`, `PASS`, or `FAIL`. A retry adds a row; it never overwrites the
interrupted or invalid attempt that preceded it.

## Per-run evidence checklist

- [ ] Frozen commit and dirty-patch digest match the comparison group.
- [ ] Exact objective and task-definition hash saved.
- [ ] Full worker argv saved with secrets removed.
- [ ] Resolved model, effort, sandbox, and approval policy saved.
- [ ] JSONL stdout and stderr saved separately.
- [ ] `thread_id` and every turn's usage saved when available.
- [ ] Verification command, exit code, stdout, and stderr saved.
- [ ] Changed-file list and final diff saved.
- [ ] First-pass and final verdict recorded independently.
- [ ] Human intervention recorded rather than folded into model success.

## Metrics and decisions

After all required runs exist:

```text
first_pass_rate = tasks_with_first_verification_pass / tasks_attempted
G1_ratio = pacer_first_pass_rate / direct_codex_first_pass_rate
```

G1 passes when `G1_ratio >= 0.90`, provided the denominator is non-zero and the
same five task definitions and frozen snapshot were used. Report absolute rates
beside the ratio; a ratio alone can hide weak performance in both lanes.

The P4 default-mode decision must also compare final verified rate, total turns,
wall time, token usage, out-of-scope changes, and human interventions. Do not
select delegated or tracked mode solely from token cost.
