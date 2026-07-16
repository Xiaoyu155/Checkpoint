from __future__ import annotations

import os

from visual_agent.playwright_env import (
    ensure_playwright_browsers_path,
    find_local_playwright_browsers_path,
    playwright_chromium_executable,
)


def create_fake_chromium_cache(path) -> None:
    executable = path / "chromium-1217" / "chrome-win64" / "chrome.exe"
    executable.parent.mkdir(parents=True)
    executable.write_text("fake chromium", encoding="utf-8")


def test_find_local_playwright_browsers_path_uses_workspace_parent(tmp_path) -> None:
    browsers = tmp_path / ".pw-browsers"
    create_fake_chromium_cache(browsers)
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()

    assert find_local_playwright_browsers_path(workspace) == browsers


def test_ensure_playwright_browsers_path_sets_env_from_local_cache(tmp_path, monkeypatch) -> None:
    browsers = tmp_path / ".pw-browsers"
    create_fake_chromium_cache(browsers)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    selected = ensure_playwright_browsers_path(tmp_path)

    assert selected == browsers
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers)
    assert selected is not None


def test_configured_path_without_chromium_self_heals_to_local_cache(tmp_path, monkeypatch) -> None:
    # A stale/partial PLAYWRIGHT_BROWSERS_PATH must not shadow a working cache.
    partial = tmp_path / "stale"
    (partial / ".links").mkdir(parents=True)
    good = tmp_path / ".pw-browsers"
    create_fake_chromium_cache(good)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(partial))

    selected = ensure_playwright_browsers_path(tmp_path)

    assert selected == good
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(good)


def test_configured_path_without_chromium_and_no_local_cache_is_cleared(tmp_path, monkeypatch) -> None:
    partial = tmp_path / "stale"
    (partial / ".links").mkdir(parents=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(partial))

    selected = ensure_playwright_browsers_path(tmp_path)

    # Cleared so Playwright falls back to its own default (e.g. system cache).
    assert selected is None
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ


def test_incomplete_local_cache_does_not_shadow_default_playwright_cache(tmp_path, monkeypatch) -> None:
    browsers = tmp_path / ".pw-browsers"
    (browsers / ".links").mkdir(parents=True)
    (browsers / "ffmpeg-1011").mkdir()
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    selected = ensure_playwright_browsers_path(tmp_path)

    assert selected is None
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ
    assert playwright_chromium_executable(browsers) is None
