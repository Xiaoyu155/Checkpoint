from __future__ import annotations

import time
from pathlib import Path

from visual_agent.dashboard import (
    DASHBOARD_HTML,
    _launch_snapshot,
    build_dashboard_data,
    start_workbench_mission,
)
from visual_agent.workspace import init_workspace


def _wait_launch(launch_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        matches = [item for item in _launch_snapshot() if item.get("launch_id") == launch_id]
        if matches and matches[0].get("state") in {"done", "error"}:
            return matches[0]
        time.sleep(0.2)
    raise AssertionError("workbench launch did not finish in time")


def test_workbench_rejects_empty_goal(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    result = start_workbench_mission(
        workspace_root=ws.root, repo_root=str(tmp_path), goal="   ",
        test_command="", agent="claude-code", execute=False,
    )
    assert result["ok"] is False
    assert "goal" in result["error"] or "目标" in result["error"]


def test_workbench_preview_launch_records_mission(tmp_path):
    # A vague goal should still launch but stop at the clarity/coverage gate,
    # proving the workbench drives the real mission machinery (no execution).
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    result = start_workbench_mission(
        workspace_root=ws.root, repo_root=str(tmp_path),
        goal="确认现有代码是否正确并给出说明",
        test_command="", agent="claude-code", execute=False,
    )
    assert result["ok"] is True
    final = _wait_launch(result["launch_id"])
    assert final["state"] == "done"
    # Preview never executes a worker; it stops with a reason, not "verified".
    assert final["status"] in {"stopped", "preview", "blocked"}


def test_dashboard_data_exposes_workbench_fields(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    data = build_dashboard_data(ws.root)
    assert "installed_agents" in data
    assert "launches" in data
    assert "repo_root" in data


def test_dashboard_html_has_workbench_form():
    assert "新建任务" in DASHBOARD_HTML
    assert "/api/mission/start" in DASHBOARD_HTML
    assert "startMission" in DASHBOARD_HTML
