## Pacer Multi-Model Coding & Verification Engine

Use Pacer for automated coding, diff patching, and test verification.

- Pacer backends health check:
  `python -m visual_agent.cli backends`
- Fast automated execution (Stateless Canvas + AST Indexer + Multi-model Patching):
  `python -m visual_agent.cli auto "<goal>" --workspace-root .`
- High-speed streaming query with xAI Grok (Sub2API):
  `python -m visual_agent.cli grok "<prompt>"`
- Verification check:
  `python -m visual_agent.cli verify`
- Full test suite:
  `python -m pytest -q`

Product boundaries:

- Pacer is the orchestration and execution engine.
- Fast coding and diff patch workers: xAI Grok (Sub2API) and Gemini Flash.
- High-complexity fallback and deep architecture: Codex Plus.
- Domestic model worker gateway: WorkBuddy (DeepSeek / Kimi / GLM).
- Strict guardrails: Local AST indexing (<20ms), transactional SEARCH/REPLACE diffs, test tamper guards, zero local offline models.
