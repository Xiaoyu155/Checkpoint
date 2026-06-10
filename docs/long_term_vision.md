# Long-Term Vision

This document captures the interface boundaries that should stay stable while the implementation evolves.

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

