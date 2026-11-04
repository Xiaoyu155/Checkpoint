# Step 2: Bug fixes (partial)

- A missing token-exchange module now results in a clear gateway-unavailable message instead of an import traceback; the removed server itself is not restored. The parsed gateway namespace is accepted when the server is present.
- Windows executable names are recognized even when parsed on a non-Windows host. Generated pytest fallback commands use the running interpreter in the MCP server and verification profiles.
- A rejected protected patch now fails its task and increments the false-green count. Snapshot restoration skips unchanged files, avoiding a Windows read-only error. Without torch, strategy search now performs CPU replay and holdout validation.
- `python -m pytest -q tests/test_codex_step2.py tests/test_verification_profiles.py`: 19 passed. Earlier broader run: 60 passed, 1 failed; after correcting restoration, the remaining legacy witness test still expects a specific trivial-test message, whereas the immutable-test guard rejects the patch first. `tests/test_vnext_strategy_search.py`: 13 passed in the subsequent run.
- Remaining: the actual gateway implementation is absent from this repository; other generated acceptance command paths require audit. Direct-write and inter-wave tamper handling still need broader verification.
