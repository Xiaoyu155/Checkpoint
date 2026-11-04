# P1-P8 hardening acceptance

Baseline: `9a49c57`. Existing working changes to gemini_bridge.py, its tests, the Gemini CLI timeout, and pacer_bounded_runner_tmp.py were preserved and excluded from this commit.

## HTTP assistance evidence

Credentials were parsed from the two supplied files in memory. Requests used direct HTTP, not local model CLIs. No credentials are embedded in this report or helper.

| Requested channel | Concurrent ping observation | Outcome |
| --- | --- | --- |
| Opus 5.5, paid key 1 | 1.984 s, HTTP 403 on repeat | unavailable |
| Opus 5.5, paid key 2 | 1.922 s, HTTP 403 on repeat | unavailable |
| Gemini 4 | 1.656 s, HTTP 403 on repeat | unavailable |
| Gemini 3.8 Flash Tiered | 3.391 s, no usable generated text; alternate OpenAI route returned 404 | unavailable |

Nine independent function/scenario requests were then attempted concurrently with ThreadPoolExecutor(max_workers=12), max_tokens=4096, and a 10-second socket timeout. All nine timed out (11.625-12.813 s elapsed, including connection overhead). Zero AST-valid generated blocks were obtained or applied. Full 4096-token output support is therefore unverified. Implementation was completed locally by Codex. The credential-free replay helper is outside the repository at ../../pacer_external_hardening.py.

## Acceptance matrix

| Item | Implemented behavior | Regression evidence |
| --- | --- | --- |
| P1 | Retain ConnectionError/OSError handling; cap terminal queue publication at min(run deadline, now + 1 second) | Full 256-item queue and Windows 10053 heartbeat abort |
| P2 | Retain monotonic request deadlines and idle heartbeat; additionally bound every SSE socket write by remaining time | Nonstream timeout, stream timeout, three SSE protocols, shrinking write timeouts |
| P3 | Retain executor submission exception cleanup and lock release | Submission rejection plus record-write failure leaves lock available |
| P4 | Retain authorization before admin POST dispatch and internal management-token check | Unauthorized POST never reaches admin handler |
| P5 | Retain explicit failed amendment response and persistent failure receipt | Primary exception includes pending followup in response and run record |
| P6 | Freeze all test fixtures and nested pytest configs; prevent snapshot rebasing; reject worker test/config edits and escape calls before writes; scan before pytest collection | Real collection-ID baseline tests; hash restoration; alias, getattr, decorator escape detection; rejection before patch landing |
| P7 | Remove remaining staged/rule-based pytest string construction; carry sys.executable and file paths as argv through verification | Existing decomposition assertions check argv and its display; physical pytest executes a filename containing spaces, dollar sign and backtick |
| P8 | Keep existing >=25% focus reservation; scale excerpt capacity with budget and retain whole source lines; preserve complete patch format; explicitly reject budgets below 128 tokens | 128/256/512/1500-token cases, oversized context and complete patch markers |

## Targeted verification

Interpreter: C:/Users/xiaoyu/AppData/Local/Programs/Python/Python311/python.exe.
Every invocation used PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, PYTHONPATH=src, one test file, and a 9-second parent timeout. No full-suite pytest run was performed.

| File / selection | Result | pytest time / parent wall time |
| --- | --- | --- |
| tests/test_p1_to_p8_hardening.py | 27 passed | 3.60 s / 4.391 s |
| tests/test_subagent_orchestrator.py, verification_only_goal or numbered_independent or stage_markers or parallel_pytest_verification or test_edit_permission | 9 passed | 0.49 s / 0.937 s |
| tests/test_tamper_guard.py | 3 passed | 5.06 s / 5.953 s |
| tests/test_canvas_manager.py | 6 passed | 1.70 s / 5.469 s |
| tests/test_pacer_model_protocols.py | 4 passed | 2.55 s / 3.156 s |
| tests/test_pacer_model.py, disconnect or amendment or stream or follow or queued | 5 passed | 2.81 s / 3.610 s |
| tests/test_model_admin.py | 3 passed | 1.76 s / 2.420 s |

57 targeted tests passed across these invocations. The main P1-P8 acceptance file completes under 15 seconds; cumulative module-validation time is longer. No claim of whole-suite acceptance or successful remote model generation is made.

The existing verifier test was updated from an exact shell-display string assertion to exact interpreter/argv and serialized-display assertions, because argv is now the executable contract. No existing test was skipped, deleted or weakened.

Socket/request deadlines bound gateway waiting and publication. They do not forcibly terminate an engine already executing; the workspace remains locked until that engine stops writing. Static AST guards are an integrity gate, not an OS sandbox for arbitrary Python execution.
