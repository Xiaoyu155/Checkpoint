# Pacer API recovery — 2026-11-01

## Workspace and backup

- Original workspace: `D:\助手codex`, application: `xiao`.
- Recovery branch: `fix/pacer-api-recovery-20261101`.
- Recovery application: `D:\pacer-recovery\20261101\worktree\xiao`.
- Starting HEAD: `e66a376`. Context snapshot: `f050a4b` (81 source/test/config files; existing work, not the new fix).
- Local backup: `D:\pacer-recovery\20261101\dirty-workspace.zip`. It contains 566 existing modified/untracked files from Git's file listing, HEAD, status, and a per-file SHA-256 manifest. ZIP integrity and every archived file hash were checked. Ignored files remain in the original workspace; this is not a full disk backup. The archive may contain private runtime data and stays local.
- Original archived files were checked again after recovery tests: zero changed.

## Process cleanup and initial failure

Stopped exactly eight Python `visual_agent.pacer_model serve` processes:
31876, 35764, 31220, 26192, 21688, 37684, 26872, 34876.
The later process check found zero matching services. No service was restarted.
This proves overlapping services existed; it does not identify which process wrote a historical file or displayed a console window.

Native pytest on the original workspace returned **42 passed, 1 failed**:
`test_auto_help_advertises_fast_local_mode` did not catch argparse's normal `SystemExit(0)`.

## Changes

- API writer now resolves the existing provider configuration and calls `run_llm_completion`, passing the current stateless prompt, credentials, URL, endpoint, and model. It returns complete text rather than a health-probe preview (8192 output-token budget, 90-second request timeout).
- Missing configuration and empty responses fail explicitly. Provider errors reach the retry/circuit-breaker code.
- Seven retry transitions use `_next_cascade_backend`. Explicit `PACER_CODER_CASCADE` order constrains dispatch as well as retries, including old task backends restored from saved plans.
- Removed the extra Gemini 3.8 fallback outside the configured list and the forced Codex-to-WorkBuddy replacement.
- An explicitly empty cascade keeps the current backend and adds no fallback. An unset variable uses the existing default list. A single `api` entry retries `api` only.
- Defined the module logger used when a resumed task's file fingerprints mismatch.
- Corrected the CLI help test to expect exit code zero; pinned the older retry test to its two mocked backends.

## Validation

Run from the recovery application directory:

```powershell
python -m pytest tests/test_subagent_orchestrator.py tests/test_cli.py tests/test_api_cascade_recovery.py tests/test_stateless_canvas_engine.py tests/test_llm_providers.py tests/test_subprocess_window.py tests/test_five_core_runtime_regressions.py -q --tb=short --junitxml=D:/pacer-recovery/20261101/pytest-results.xml
```

The new regression cases cover long completions, empty content, provider errors and cooldown, missing credentials, configured retry order, stale task backends, explicit Codex selection, zero-diff retry rejection, a real completion adapter with mocked HTTP transport applying a patch on disk, and fingerprint mismatch logging followed by reexecution.

Final result: **95 passed in 18.84 seconds**. JUnit results are stored outside the repository at the path in the command above. `git diff --check` passed.

No paid live model completion was used as acceptance evidence. No account, subscription, or new API key was purchased/configured. This does not establish that the user's provider currently has quota.

To constrain writing to the API backend in a PowerShell session:

```powershell
Set-Location D:\pacer-recovery\20261101\worktree\xiao
$env:PACER_CODER_CASCADE = 'api'
```

Existing provider configuration still supplies the API credentials and model. The cascade controls coder dispatch; separate planning/review paths retain their own policies. Continue to use direct editing and native pytest for Pacer repairs.

## Remaining issues outside these fixes

- No cross-process single-instance guard was added; manually starting multiple services remains possible.
- Historical delayed writes and console flashing were not reproduced or attributed.
- Local indexing and a generic local success string do not prove a complete semantic audit. This patch does not claim to solve local audit false positives.
- `pacer verify` diagnostic presentation was bypassed, not repaired.
- The entire repository test suite was not used as an acceptance gate for this isolated repair.
