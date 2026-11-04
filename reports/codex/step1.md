# Step 1: Configuration and credentials

- Removed fixed Grok and Gemini gateway addresses and fixed SSH host, user, private-key path, proxy and gateway settings from the requested modules. Gemini profile defaults now disable SSH fallback; SSH settings are supplied by `PACER_GEMINI4_SSH_*` and `PACER_GEMINI4_SOCKS_*` environment variables. Missing gateway or SSH settings return an explicit error.
- Grok accepts its existing environment or Pacer credentials configuration and reports a missing endpoint. README uses an example endpoint.
- Seller credentials now come from `PACER_SELLER_KEY`, not a command-line flag. Cheap backends resolve keys exclusively from environment variables.
- Verification: `python -m pytest -q tests/test_codex_configuration.py tests/test_model_catalog.py tests/test_gemini4_vps_client.py`: 11 passed, 2 failed. Both failing legacy Gemini tests assume the removed default gateway or automatically enabled SSH fallback. Initial command named two nonexistent test files and ran no tests.
- Existing gateway entry point also references an absent example module; that is addressed in Step 2. Other modules outside the requested files still contain service-specific URLs and need separate review.
