from __future__ import annotations

import time

from visual_agent.chief_queue import list_mission_queue_items
from visual_agent.missions import load_mission, save_mission
from visual_agent.programs import load_program
from visual_agent.simple_task import run_simple_managed_task, simple_result_to_markdown


def test_simple_task_runs_through_locked_autonomous_program(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    workspace = repo / ".agent-workspace"
    observed = {}

    def fake_worker(**kwargs):
        queue = list_mission_queue_items(kwargs["workspace_root"])
        item = next(entry for entry in queue["entries"] if entry["status"] == "pending")
        observed["merge_policy"] = item["merge_policy"]
        mission = load_mission(kwargs["workspace_root"], item["mission_id"])
        assert mission is not None
        mission["status"] = "verified"
        mission["stop_reason"] = "verified"
        save_mission(kwargs["workspace_root"], mission)
        return {"ran": True, "status": "run_once_completed", "mission_id": item["mission_id"]}

    payload = run_simple_managed_task(
        "修复登录错误并运行测试",
        repo_root=repo,
        workspace_root=workspace,
        worker_runner=fake_worker,
        codex_provider="relay-main",
        max_wait_seconds=10,
    )

    program = load_program(workspace, payload["program_id"])
    assert payload["status"] == "completed"
    assert observed["merge_policy"] == "auto"
    assert program is not None
    assert program["status"] == "completed"
    assert program["autonomy_policy"]["mode"] == "autonomous"
    assert program["autonomy_policy"]["closed_loop"]["memory_mode"] == "enabled"
    assert program["autonomy_policy"]["closed_loop"]["acceptance_policy"] == "strict"
    assert program["autonomy_policy"]["closed_loop"]["roadmap_mode"] == "locked"
    assert program["autonomy_policy"]["closed_loop"]["codex_provider"] == "relay-main"
    assert program["autonomy_policy"]["closed_loop"]["codex_failover_provider"] == ""
    assert program["tasks"][0]["agent"] == "codex"
    assert program["source_plan_sha256"]


def test_review_task_uses_report_acceptance_without_test_command(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    observed = {}

    def fake_worker(**kwargs):
        queue = list_mission_queue_items(kwargs["workspace_root"])
        item = next(entry for entry in queue["entries"] if entry["status"] == "pending")
        mission = load_mission(kwargs["workspace_root"], item["mission_id"])
        assert mission is not None
        observed["test_command"] = mission["test_command"]
        mission["status"] = "verified"
        mission["stop_reason"] = "verified"
        save_mission(kwargs["workspace_root"], mission)
        report_path = workspace / "missions" / item["mission_id"] / "final_report.md"
        report_path.write_text("## 审查意见\n\n结构需要收口。\n", encoding="utf-8")
        return {"ran": True, "status": "run_once_completed", "mission_id": item["mission_id"]}

    payload = run_simple_managed_task(
        "审查这个项目并给出意见",
        repo_root=repo,
        workspace_root=workspace,
        worker_runner=fake_worker,
        max_wait_seconds=10,
    )

    program = load_program(workspace, payload["program_id"])
    assert observed["test_command"] == ""
    assert program is not None
    assert program["tasks"][0]["acceptance_mode"] == "best_effort"
    assert "结构需要收口" in payload["review_report"]
    assert "结构需要收口" in simple_result_to_markdown(payload)


def test_simple_task_streams_elapsed_progress(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    workspace = repo / ".agent-workspace"
    output: list[str] = []

    def fake_worker(**kwargs):
        time.sleep(0.12)
        queue = list_mission_queue_items(kwargs["workspace_root"])
        item = next(entry for entry in queue["entries"] if entry["status"] == "pending")
        mission = load_mission(kwargs["workspace_root"], item["mission_id"])
        assert mission is not None
        mission["status"] = "verified"
        mission["stop_reason"] = "verified"
        save_mission(kwargs["workspace_root"], mission)
        return {"ran": True, "status": "run_once_completed", "mission_id": item["mission_id"]}

    payload = run_simple_managed_task(
        "整理 README",
        repo_root=repo,
        workspace_root=workspace,
        worker_runner=fake_worker,
        progress_func=output.append,
        progress_interval_seconds=0.02,
        max_wait_seconds=10,
    )

    assert payload["status"] == "completed"
    assert any(line.startswith("[00:00]") for line in output)


def test_simple_task_stops_before_program_when_project_has_no_verification(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    payload = run_simple_managed_task(
        "优化首页布局",
        repo_root=repo,
        workspace_root=repo / ".agent-workspace",
    )

    assert payload["status"] == "needs_input"
    assert payload["reason"] == "project_verification_unresolved"
    assert payload["program_id"] == ""
    assert "没有创建 Program" in simple_result_to_markdown(payload)
    assert not (repo / ".agent-workspace").exists()
