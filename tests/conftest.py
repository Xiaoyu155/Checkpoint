from __future__ import annotations

import os

import pytest


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
