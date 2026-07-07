## Checkpoint / DevPacer

Read `.visual-agent-status.md` before planning fixes.

Use Checkpoint after UI, workflow, mission, queue, or verification-related code
changes.

- Workspace root: `.agent-workspace`
- Fast check: `python -m visual_agent.cli codex-check --workspace-root .agent-workspace --repo-root .`
- Real interaction check when product behavior matters: `python -m visual_agent.cli codex-check --workspace-root .agent-workspace --repo-root . --run-profile supervised`
- Include slow visual/OCR workflows only when needed: add `--include-slow`
- Resume context in a new chat: `python -m visual_agent.cli context-snapshot --workspace-root .agent-workspace --format markdown`
- Read compact failure details before opening full reports: `python -m visual_agent.cli summarize-latest-failure --workspace-root .agent-workspace --format json`

Product boundaries:

- DevPacer is the mission/orchestration layer.
- Checkpoint is the verification engine.
- Default executable coding worker is Codex.
- Gemini is a multimodal inspection lane, not a default coding worker.
- Do not treat `dry-run` / `inspection_only` as product acceptance.
