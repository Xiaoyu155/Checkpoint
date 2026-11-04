# P9-P12 local hardening acceptance

Baseline: `320d554bb9862b728211679de4f2e924d55b7ece`. Existing uncommitted changes to the Gemini bridge, its tests, the Gemini CLI timeout, and `pacer_bounded_runner_tmp.py` are excluded from this commit.

All implementation and testing in this round was local. No remote model API, local model proxy, or model CLI was invoked. Every pytest invocation targeted one file, used the requested Python 3.11 interpreter, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `PYTHONPATH=src`, and an external 9-second hard timeout. No full-suite run was performed.

| Item | Implemented contract | Targeted test | pytest / process wall |
| --- | --- | --- | --- |
| P9 | Event and byte thresholds, atomic folded checkpoint, bounded retained journal, multi-instance file-change detection, and projection of task contract, invariants and focus | 1,012 events including preloaded history, alternating writers, oversized event and Canvas integration | 3 passed, 0.52 s / 0.922 s |
| P10 | One ordered route resolver with family, timeout, cooldown and reason; explicit cascade order takes precedence; infra/quota opens circuit; verification strikes stay with the writer | Cascade, de-duplication, configured timeout, 429 failover, cooldown and task-error behavior | 3 passed, 0.19 s / 0.594 s |
| P11 | Reviewer discovery reads configuration without synchronous network probing; structured skip reasons are logged; missing or unsuccessful independent reviewers fail closed with `ReviewerUnavailableError` | Exceptions, missing and same-family reviewers, unknown coder family and explicit reviewer without a coder | 3 passed, 0.19 s / 0.687 s |
| P12 | Registered aliases and anchored identifiers replace substring family detection; unknown proxy identity cannot satisfy independence | Seven model/provider cases including misleading meta/glm strings and explicit WorkBuddy identity | 2 passed, 0.19 s / 2.766 s |
| Replay | Isolated parent/fix Git archives; same fixed test fixture tree and matching pytest config; genuine parent failure and fixed pass; P6 SEARCH/REPLACE bait rejection | Synthetic Git history; parent red, child green, accepted tamper = 0 | 1 passed, 1.18 s / 1.563 s |

Final combined acceptance: `tests/test_p9_to_p12_hardening.py --durations=10` **13 passed in 1.77 s**, process wall **2.188 s**; `tests/test_p1_to_p8_hardening.py` **27 passed in 3.73 s**, process wall **4.500 s**. Related targeted checks also passed: task review 5, orchestrator API/cascade 7, provider reviewer discovery 3, Canvas 6.

The two formerly parameterized sets were consolidated into loops to reduce per-test setup overhead; all four reviewer scenarios and seven model-name scenarios are still checked. During development, several runs hit the external 9-second timeout and one replay child hit its original 3-second timeout. The replay child timeout was increased to 6 seconds, and final combined and isolated runs above passed. Wall times vary with host load; these measurements are not a guarantee for a different machine or a busy system.

The replay harness accepts Pacer SEARCH/REPLACE bait patches, validates hexadecimal commit IDs and ancestry, rejects differing root pytest configuration, and uses fixed-commit test fixtures against both snapshots. It does not execute or apply the bait: it asks P6 to reject its candidate content before landing. Test commands execute local repository code in temporary directories, not an OS sandbox; callers must use trusted historical test code. The harness itself does not make cloud calls.

The checkpoint bounds the retained event journal, not the size of a single current prompt or other value that must remain semantically intact in the snapshot. Unknown model families remain ineligible for independent-review certification. An explicitly configured reviewer is discoverable without a network probe; an actual later invocation can still fail, in which case acceptance remains blocked.
