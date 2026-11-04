# Step 3: Command security

- Command verification rejects string commands, validates argv, executes without a shell, retains the existing timeout, and compares acceptance-file snapshots before and after execution. CLI and MCP string inputs are validated and parsed before passing argv to the executor.
- `pacer task` no longer adds `bypassPermissions` by default; callers can explicitly pass `--permission-mode bypassPermissions`.
- `python -m pytest -q tests/test_codex_command_security.py tests/test_mcp_server.py tests/test_verification_status.py`: 20 passed. The earlier combined run had 29 passed and one failed legacy test that expects default bypass. A first attempt referenced a nonexistent test file and ran no tests.
- Existing callers of the public Python API that pass strings must migrate to argv. Shell expressions are intentionally rejected.
- Follow-up: migrated the indirect baseline probe caller to safe parsing. `tests/test_acceptance_discrimination.py` plus the security tests: 14 passed, 8 failed; the failing legacy probe cases use `python script.py`, which the standard-runner safety policy rejects. This is a compatibility limitation requiring further design.
