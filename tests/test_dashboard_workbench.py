from __future__ import annotations

import time
from pathlib import Path

from visual_agent.dashboard import (
    DASHBOARD_HTML,
    _launch_snapshot,
    build_dashboard_data,
    start_workbench_mission,
)
from visual_agent.chief_queue import submit_mission_queue_item
from visual_agent.missions import create_mission, default_budget_policy
from visual_agent.programs import create_program_from_plan
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


def test_workbench_execute_defaults_to_manual_merge(tmp_path, monkeypatch):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "status": "verified",
            "stop_reason": "verified",
            "mission": {"mission_id": "m1"},
        }

    monkeypatch.setattr("visual_agent.chief_run.run_chief_mission", fake_run)
    result = start_workbench_mission(
        workspace_root=ws.root,
        repo_root=str(tmp_path),
        goal="修复一个明确问题",
        test_command="pytest -q",
        agent="codex",
        execute=True,
    )
    assert result["ok"] is True
    _wait_launch(result["launch_id"])
    assert captured["merge"] is False


def test_workbench_auto_detects_test_command_when_empty(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}', encoding="utf-8")
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"status": "preview", "stop_reason": "preview_only", "mission": {"mission_id": "m1"}}

    monkeypatch.setattr("visual_agent.chief_run.run_chief_mission", fake_run)
    result = start_workbench_mission(
        workspace_root=ws.root,
        repo_root=str(tmp_path),
        goal="修复一个明确问题",
        test_command="",
        agent="codex",
        execute=False,
    )

    assert result["test_command"] == "npm test"
    _wait_launch(result["launch_id"])
    assert captured["test_command"] == "npm test"


def test_dashboard_data_exposes_workbench_fields(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    data = build_dashboard_data(ws.root)
    assert "installed_agents" in data
    assert "launches" in data
    assert "repo_root" in data


def test_dashboard_data_exposes_mission_queue_context(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    mission = create_mission(
        workspace_root=ws.root,
        objective="Fix checkout",
        repo_root=tmp_path,
        plan_id="p1",
        budget_policy=default_budget_policy(),
        mission_id="m1",
        status="preview",
    )
    submit_mission_queue_item(
        workspace_root=ws.root,
        mission_id=mission["mission_id"],
        agent="codex",
        test_command="npm test",
        merge_policy="auto",
    )

    data = build_dashboard_data(ws.root)

    assert data["queue"][0]["agent"] == "codex"
    assert data["queue"][0]["test_command"] == "npm test"
    assert data["queue"][0]["merge_policy"] == "auto"


def test_dashboard_data_exposes_programs(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = tmp_path / "plan.md"
    plan.write_text("- [ ] Implement voice overlay\n", encoding="utf-8")
    program = create_program_from_plan(source_file=plan, workspace_root=ws.root, repo_root=tmp_path)

    data = build_dashboard_data(ws.root)

    assert data["programs"][0]["program_id"] == program["program_id"]


def test_dashboard_html_has_workbench_form():
    assert "托管队列" in DASHBOARD_HTML
    assert "项目托管" in DASHBOARD_HTML
    assert "/api/mission/start" in DASHBOARD_HTML
    assert "startMission" in DASHBOARD_HTML


def test_dashboard_html_has_kanban_board():
    # The workbench is a kanban board (Vibe Kanban-style): four columns and a
    # one-click merge for verified missions.
    for column in ("待办", "进行中", "待验收", "已完成"):
        assert column in DASHBOARD_HTML
    assert "/api/mission/merge" in DASHBOARD_HTML
    assert "mergeMission" in DASHBOARD_HTML
    assert "diffSection" in DASHBOARD_HTML


def test_dashboard_data_missions_carry_board_fields(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    create_mission(
        workspace_root=ws.root,
        objective="Fix checkout",
        repo_root=tmp_path,
        plan_id="p1",
        budget_policy=default_budget_policy(),
        mission_id="m1",
        status="verified",
    )

    data = build_dashboard_data(ws.root)

    mission = data["missions"][0]
    assert mission["board_column"] == "in_review"
    assert mission["can_merge"] is True
    assert mission["merge_state"] == ""
