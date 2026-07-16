from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_model_credentials(tmp_path_factory, monkeypatch):
    """Point credential discovery at a nonexistent file for every test.

    ``resolve_cheap_backend`` falls back to the repo's own ``model_api_keys.txt``
    (the developer's real keys). Missions now auto-run goal grounding, so
    without this guard a unit test could silently place a real network call to
    MiMo/DeepSeek. Tests that need credentials set the env var themselves.
    """
    missing = tmp_path_factory.mktemp("credentials") / "no-credentials.txt"
    monkeypatch.setenv("CHECKPOINT_MODEL_CREDENTIALS", str(missing))


@pytest.fixture(autouse=True)
def _isolate_playwright_browsers_path():
    """Keep PLAYWRIGHT_BROWSERS_PATH from leaking between tests.

    ``ensure_playwright_browsers_path`` mutates ``os.environ`` directly as a
    process-wide side effect. A test that points it at a temporary cache would
    otherwise poison later tests (a stale path makes the browser capability look
    unavailable, so preflight/recorder checks fail). Snapshot and restore it
    around every test.
    """
    sentinel = object()
    original = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", sentinel)
    try:
        yield
    finally:
        if original is sentinel:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = original
