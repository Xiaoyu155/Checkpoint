from __future__ import annotations

import json

from visual_agent.chief_queue import list_mission_queue_items
from visual_agent.cli import main
from visual_agent.missions import load_mission, save_mission
from visual_agent.program_scheduler import advance_program_for_mission, start_program, sync_program_tasks
from visual_agent.programs import create_program_from_plan, load_program
from visual_agent.workspace import init_workspace


def test_start_program_creates_preview_and_queues_first_task(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = tmp_path / "plan.md"
    plan.write_text("- [ ] Implement voice overlay\n- [ ] Update docs\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    program = create_program_from_plan(source_file=plan, workspace_root=workspace.root, repo_root=tmp_path, sequential=True)

    payload = start_program(workspace_root=workspace.root, program_id=program["program_id"], hours=5)

    assert payload["queued_items"]
    assert payload["queued_items"][0]["task_id"] == "task-001"
    saved = load_program(workspace.root, program["program_id"])
    assert saved["tasks"][0]["status"] == "queued"
    assert saved["tasks"][1]["status"] == "pending"
    assert list_mission_queue_items(workspace.root)["pending_items"] == 1


def _sequential_program(tmp_path, tasks: str = "- [ ] Implement voice overlay\n- [ ] Update docs\n"):
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = tmp_path / "plan.md"
    plan.write_text(tasks, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    program = create_program_from_plan(source_file=plan, workspace_root=workspace.root, repo_root=tmp_path, sequential=True)
    return workspace, program


def _mark_first_task_mission(workspace_root, program_id, status: str, stop_reason: str = "") -> str:
    saved = load_program(workspace_root, program_id)
    mission_id = str(saved["tasks"][0]["mission_id"])
    mission = load_mission(workspace_root, mission_id)
    mission["status"] = status
    if stop_reason:
        mission["stop_reason"] = stop_reason
    save_mission(workspace_root, mission)
    return mission_id


def test_sync_program_tasks_marks_verified(tmp_path) -> None:
    workspace, program = _sequential_program(tmp_path)
    start_program(workspace_root=workspace.root, program_id=program["program_id"], hours=5)
    _mark_first_task_mission(workspace.root, program["program_id"], "verified")

    synced = sync_program_tasks(workspace_root=workspace.root, program_id=program["program_id"])

    assert synced["updated"] == [{"task_id": "task-001", "status": "verified"}]
    saved = load_program(workspace.root, program["program_id"])
    assert saved["tasks"][0]["status"] == "verified"


def test_sync_program_tasks_marks_failed_with_reason(tmp_path) -> None:
    workspace, program = _sequential_program(tmp_path)
    start_program(workspace_root=workspace.root, program_id=program["program_id"], hours=5)
    _mark_first_task_mission(workspace.root, program["program_id"], "stopped", stop_reason="worker_error")

    synced = sync_program_tasks(workspace_root=workspace.root, program_id=program["program_id"])

    assert synced["updated"][0]["status"] == "failed"
    saved = load_program(workspace.root, program["program_id"])
    assert saved["tasks"][0]["status"] == "failed"
    assert saved["tasks"][0]["block_reason"] == "worker_error"


def test_start_program_advances_past_verified_task(tmp_path) -> None:
    """Sequential programs must not stall after the first task verifies (V5 finding)."""
    workspace, program = _sequential_program(tmp_path)
    start_program(workspace_root=workspace.root, program_id=program["program_id"], hours=5)
    _mark_first_task_mission(workspace.root, program["program_id"], "verified")

    payload = start_program(workspace_root=workspace.root, program_id=program["program_id"], hours=5)

    queued_ids = [item["task_id"] for item in payload["queued_items"]]
    assert "task-002" in queued_ids
    saved = load_program(workspace.root, program["program_id"])
    assert saved["tasks"][0]["status"] == "verified"
    assert saved["tasks"][1]["status"] == "queued"


def test_advance_program_for_mission_queues_next_task(tmp_path) -> None:
    workspace, program = _sequential_program(tmp_path)
    start_program(workspace_root=workspace.root, program_id=program["program_id"], hours=5)
    mission_id = _mark_first_task_mission(workspace.root, program["program_id"], "verified")

    advanced = advance_program_for_mission(workspace_root=workspace.root, mission_id=mission_id)

    assert advanced is not None
    assert advanced["program_id"] == program["program_id"]
    assert [q.get("task_id") for q in advanced["queued_items"]] == ["task-002"]


def test_verified_blocked_mission_terminates_program_without_queuing_dependency(tmp_path) -> None:
    workspace, program = _sequential_program(tmp_path)
    start_program(workspace_root=workspace.root, program_id=program["program_id"], hours=5)
    mission_id = _mark_first_task_mission(
        workspace.root,
        program["program_id"],
        "verified_blocked",
        stop_reason="worker_toolchain_violation",
    )

    advanced = advance_program_for_mission(workspace_root=workspace.root, mission_id=mission_id)

    assert advanced is not None
    assert advanced["status"] == "blocked"
    assert advanced["queued_items"] == []
    saved = load_program(workspace.root, program["program_id"])
    assert saved["status"] == "blocked"
    assert saved["tasks"][0]["status"] == "blocked"
    assert saved["tasks"][0]["terminal_outcome"] == "verified_blocked"
    assert saved["tasks"][1]["status"] == "pending"


def test_sync_promotes_failed_task_when_retried_mission_verifies(tmp_path) -> None:
    """V5 live finding: a task that failed earlier must flip to verified after a retry succeeds."""
    workspace, program = _sequential_program(tmp_path)
    start_program(workspace_root=workspace.root, program_id=program["program_id"], hours=5)
    mission_id = _mark_first_task_mission(workspace.root, program["program_id"], "stopped", stop_reason="worker_error")
    sync_program_tasks(workspace_root=workspace.root, program_id=program["program_id"])
    saved = load_program(workspace.root, program["program_id"])
    assert saved["tasks"][0]["status"] == "failed"

    # The mission is retried through the queue and verifies.
    mission = load_mission(workspace.root, mission_id)
    mission["status"] = "verified"
    mission["stop_reason"] = "verified"
    save_mission(workspace.root, mission)

    advanced = advance_program_for_mission(workspace_root=workspace.root, mission_id=mission_id)

    assert advanced is not None
    saved = load_program(workspace.root, program["program_id"])
    assert saved["tasks"][0]["status"] == "verified"
    assert "block_reason" not in saved["tasks"][0]
    assert [q.get("task_id") for q in advanced["queued_items"]] == ["task-002"]


def test_sync_never_regresses_verified_task(tmp_path) -> None:
    workspace, program = _sequential_program(tmp_path)
    start_program(workspace_root=workspace.root, program_id=program["program_id"], hours=5)
    mission_id = _mark_first_task_mission(workspace.root, program["program_id"], "verified")
    sync_program_tasks(workspace_root=workspace.root, program_id=program["program_id"])

    # Later mission state changes must not un-verify the task.
    mission = load_mission(workspace.root, mission_id)
    mission["status"] = "stopped"
    mission["stop_reason"] = "worker_error"
    save_mission(workspace.root, mission)
    sync_program_tasks(workspace_root=workspace.root, program_id=program["program_id"])

    saved = load_program(workspace.root, program["program_id"])
    assert saved["tasks"][0]["status"] == "verified"


def test_advance_program_for_mission_ignores_unknown_mission(tmp_path) -> None:
    workspace, program = _sequential_program(tmp_path)
    start_program(workspace_root=workspace.root, program_id=program["program_id"], hours=5)

    assert advance_program_for_mission(workspace_root=workspace.root, mission_id="not-a-mission") is None


def test_autopilot_cli_creates_program_and_queue(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = tmp_path / "plan.md"
    plan.write_text("- [ ] Implement voice overlay\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    code = main(
        [
            "autopilot",
            "--file",
            str(plan),
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["program_id"]
    assert payload["queued_items"]
