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


@pytest.fixture(autouse=True)
def _isolate_pacer_runtime_environment(tmp_path, monkeypatch):
    """Prevent a host Pacer launch from binding tests to its workspace."""
    for name in (
        "PACER_LAUNCH_ID",
        "PACER_PRELAUNCH_TASK_REQUIRED",
        "PACER_PRELAUNCH_TASK_CONTRACT_DIGEST",
        "PACER_PRELAUNCH_SOURCE_BASELINE_DIGEST",
    ):
        monkeypatch.delenv(name, raising=False)

    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    # Path.home() uses HOME on POSIX and USERPROFILE on Windows.
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("USERPROFILE", str(isolated_home))
    # Temporary Git repositories must not depend on a developer's global config.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Pacer Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "pacer-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Pacer Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "pacer-test@example.invalid")
