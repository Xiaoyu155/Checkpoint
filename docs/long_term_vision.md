# Long-Term Vision

This document captures the interface boundaries that should stay stable while the implementation evolves.

## DevPacer mission contract

DevPacer is the product-level autonomous development operator. Checkpoint is the
verification engine underneath it.

The durable mission contract should stay stable:

- `mission.json` describes the objective, status, stop reason, repo, workspace,
  plan id, current round, and product/engine names.
- `budget.json` describes max rounds, wall-clock limits, worker limits, model
  policy, and approval policy.
- `rounds.jsonl` records every planning, preview, worker, verification, and
  repair round as append-only evidence.
- `background.json` records the background worker process, logs, completion
  receipt, exit code, and health reconciliation state.
- `final_report.md` is the human handoff artifact.

Mission queue contract:

- `mission_queue/queue.json` is the local unattended-run queue.
- Queue items reference an existing `mission_id`; they do not duplicate plans,
  budgets, or execution logic.
- A worker marks an item `success` only when the resumed mission returns
  `verified`; every other stop reason remains reviewable as `failed`.
- Queue workers must execute the same `chief-run --resume <mission_id>
  --execute` path as foreground/background runs.
- File locking is enough for the local single-machine product. Do not expand
  this into a distributed scheduler until local mission reliability is proven.

Stable stop reasons:

- `verified`
- `needs_clarification`
- `coverage_gap`
- `budget_exhausted`
- `same_failure_repeated`
- `scope_drift`
- `permission_required`
- `worker_error`
- `inspection_only`

Background health states:

- `running`: process is alive and still inside mission budget.
- `completed`: process wrote a completion receipt with an exit code.
- `timeout`: process exceeded `max_wall_minutes` and was terminated.
- `orphaned`: pid disappeared before a completion receipt was written.

The CLI may keep the `checkpoint` executable name while the product name moves
to DevPacer. User-facing docs should describe `Checkpoint` as the verification
engine, not the whole autonomous development product.

## Workflow versioning

Checkpoint workflows use `schema_version` to describe the wire format of a workflow file and `version` to describe the workflow revision.

Planned compatibility rules:

1. `schema_version` only changes when the on-disk workflow schema changes in a backward-incompatible way.
2. `version` changes when the workflow logic changes but the schema stays compatible.
3. `min_runtime_version` is advisory for older runtimes and should be treated as a compatibility floor.
4. Existing runtimes should reject unsupported schema versions with a clear, machine-readable error.
5. Migration helpers should be able to upgrade `v1` workflows to newer schema versions without losing tags, visibility, or author metadata.

## Multi-model interface

The generation pipeline keeps a stable model adapter boundary:

- The workflow generator accepts a model identifier or provider-prefixed model string.
- The adapter resolves the backend provider and model name independently from the prompt logic.
- Anthropic, OpenAI, Gemini, and the existing OpenAI-compatible provider family now share the same completion path through `src/visual_agent/llm_providers.py`.
- Future providers should only need a new adapter in `src/visual_agent/llm_providers.py`, not a rewrite of the workflow generator.

## Stable Agent Contract

The agent-facing output surface should stay stable even when implementation details change:

- `structured_failure` remains the machine-readable failure contract for runs, MCP clients, and repair flows.
- `schema_version` on structured failure and workflow artifacts is the compatibility gate.
- Known framework noise may be classified as `known_issue` instead of being collapsed into generic assertion failures.
- `verify_workflow`, `get_failure_details`, `get_visual_status`, and `context-snapshot` should stay aligned so agents can resume work without parsing raw logs.

## Compatibility goals

- Keep YAML schema parsing strict and deterministic.
- Keep prompt selection and model selection separate.
- Preserve output quality checks regardless of the chosen model provider.
- Avoid hardcoding provider-specific behavior in workflow synthesis or generation call sites.

