# Summary

Work is incomplete and must not be treated as a passing acceptance gate.

- Regression repair comparison: same-environment `main` had 1115 passed / 27 failed; `codex-work` had 1124 passed / 27 failed. JUnit comparison found no new failing test identities. See step3 for details.

- Baseline `pytest -q`: 1114 passed, 28 failed, 1 warning, 793.22 s.
- Full `pytest -q` after Steps 0-3: 1094 passed, 55 failed, 1 warning, 640.33 s. This run preceded the baseline-probe caller follow-up; that follow-up's targeted tests report 14 passed and 8 failed. The final tree has not had a further full run.
- Steps 1 and 2 are partial: old gateway service is absent, some legacy tests depend on removed defaults, and direct-write/inter-wave tampering needs further validation. Step 3 changes the verifier API and some pre-existing script-based acceptance commands are rejected.
- Steps 4 and 5 are not implemented; see their reports. No GPU/CPU comparison or zero-false-positive result is available.
- `provider_runtime` and `model_router` were not changed. No credentials were added to commits.
