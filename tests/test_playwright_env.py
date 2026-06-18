from __future__ import annotations

import os

from visual_agent.playwright_env import ensure_playwright_browsers_path, find_local_playwright_browsers_path


def test_find_local_playwright_browsers_path_uses_workspace_parent(tmp_path) -> None:
    browsers = tmp_path / ".pw-browsers"
    (browsers / ".links").mkdir(parents=True)
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()

    assert find_local_playwright_browsers_path(workspace) == browsers


def test_ensure_playwright_browsers_path_sets_env_from_local_cache(tmp_path, monkeypatch) -> None:
    browsers = tmp_path / ".pw-browsers"
    (browsers / ".links").mkdir(parents=True)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

    selected = ensure_playwright_browsers_path(tmp_path)

    assert selected == browsers
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browsers)
    assert selected is not None
