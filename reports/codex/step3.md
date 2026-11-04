# Step 3: Command security

- Command verification rejects string commands, validates argv, executes without a shell, retains the existing timeout, and compares acceptance-file snapshots before and after execution. CLI and MCP string inputs are validated and parsed before passing argv to the executor.
- `pacer task` no longer adds `bypassPermissions` by default; callers can explicitly pass `--permission-mode bypassPermissions`.
- `python -m pytest -q tests/test_codex_command_security.py tests/test_mcp_server.py tests/test_verification_status.py`: 20 passed. The earlier combined run had 29 passed and one failed legacy test that expects default bypass. A first attempt referenced a nonexistent test file and ran no tests.
- Existing callers of the public Python API that pass strings must migrate to argv. Shell expressions are intentionally rejected.
- Follow-up: migrated the indirect baseline probe caller to safe parsing. `tests/test_acceptance_discrimination.py` plus the security tests: 14 passed, 8 failed; the failing legacy probe cases use `python script.py`, which the standard-runner safety policy rejects. This is a compatibility limitation requiring further design.

## Regression repair

- Parse commands into argv before validating runners. Repository-local Python scripts and quoted interpreter paths are accepted, while shell operators, redirection, and eval flags remain prohibited. Execution remains shell-free and reports child exit codes.
- The acceptance exam now uses the same argv validation. The pytest child environment test executes a repository-local pytest file.
- Removed the stale gateway import, hardcoded health endpoints, desktop link, extension default and token-market allowlist entry. Pool checks use the configured non-Google endpoint and cache results by base URL; missing or malformed Grok endpoints are offline.
- The task-launch test now checks that permission bypass is not injected. Provider tests inject example endpoints explicitly.
- Targeted verification after the cache-key and SSH-test follow-ups: 89 passed. The same-environment `main` baseline was 1115 passed, 27 failed, 1 warning in 772.06 seconds; the branch comparison remains pending.
- The first branch full run had 38 failures. Eleven were absent from the main baseline: legitimate argv paths with shell metacharacters were rejected, or tests still assumed shell execution, Python `-c`, or an implicit gateway. These cases were repaired or updated to exercise the same behavior with explicit safe configuration; the corresponding targeted run passed 32 tests. A final full comparison remains required.
