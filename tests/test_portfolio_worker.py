from __future__ import annotations

import json
from threading import Barrier, Lock

from visual_agent.chief_queue import load_mission_queue_item, submit_mission_queue_item
from visual_agent.cli import main
from visual_agent.missions import create_mission, default_budget_policy
from visual_agent.portfolio_worker import run_portfolio_mission_worker


def test_portfolio_worker_runs_project_queues_concurrently(tmp_path) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    workspace_a, queue_a = _queued_preview_mission(project_a, mission_id="mission-a")
    workspace_b, queue_b = _queued_preview_mission(project_b, mission_id="mission-b")
    barrier = Barrier(2, timeout=3)
    lock = Lock()
    calls: list[str] = []

    def fake_runner(**kwargs):
        with lock:
            calls.append(str(kwargs["workspace_root"]))
        barrier.wait()
        return {"status": "verified", "stop_reason": "verified"}

    payload = run_portfolio_mission_worker(
        project_roots=[project_a, project_b],
        max_workers=2,
        mission_runner=fake_runner,
    )

    assert payload["status"] == "completed"
    assert payload["project_count"] == 2
    assert payload["processed_items"] == 2
    assert set(calls) == {str(workspace_a), str(workspace_b)}
    assert load_mission_queue_item(workspace_a, queue_a.queue_id).status == "success"
    assert load_mission_queue_item(workspace_b, queue_b.queue_id).status == "success"


def test_portfolio_worker_cli_reports_idle_projects(tmp_path, capsys) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()

    exit_code = main(
        [
            "portfolio-worker",
            "--project",
            str(project_a),
            "--project",
            str(project_b),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "idle"
    assert payload["project_count"] == 2
    assert payload["processed_items"] == 0


def test_portfolio_worker_watch_forwards_deadline_without_item_cap(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project-a"
    project.mkdir()
    captured = {}

    def fake_worker(**kwargs):
        captured.update(kwargs)
        return {"status": "max_seconds_reached", "processed_items": 0, "idle_polls": 5, "elapsed_seconds": 3600, "runs": []}

    monkeypatch.setattr("visual_agent.portfolio_worker.run_mission_queue_worker", fake_worker)

    payload = run_portfolio_mission_worker(project_roots=[project], watch=True, max_seconds=3600)

    assert payload["status"] == "max_seconds_reached"
    assert payload["watch"] is True
    assert payload["max_items_per_project"] is None
    assert captured["watch"] is True
    assert captured["run_once"] is False
    assert captured["max_items"] is None
    assert captured["max_seconds"] == 3600


def test_portfolio_worker_cli_accepts_watch_deadline(tmp_path, capsys, monkeypatch) -> None:
    project = tmp_path / "project-a"
    project.mkdir()
    captured = {}

    def fake_portfolio_worker(**kwargs):
        captured.update(kwargs)
        return {
            "schema_version": 1,
            "product": "DevPacer",
            "status": "max_seconds_reached",
            "project_count": 1,
            "processed_items": 0,
            "projects": [],
        }

    monkeypatch.setattr("visual_agent.portfolio_worker.run_portfolio_mission_worker", fake_portfolio_worker)

    exit_code = main(
        [
            "portfolio-worker",
            "--project",
            str(project),
            "--watch",
            "--max-seconds",
            "3600",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "max_seconds_reached"
    assert captured["watch"] is True
    assert captured["max_seconds"] == 3600
    assert captured["max_items_per_project"] is None


def _queued_preview_mission(project_root, *, mission_id: str):
    project_root.mkdir()
    workspace = project_root / ".agent-workspace"
    mission = create_mission(
        workspace_root=workspace,
        objective="Fix checkout total",
        repo_root=project_root,
        plan_id=f"plan-{mission_id}",
        budget_policy=default_budget_policy(max_rounds=2, max_wall_minutes=30),
        mission_id=mission_id,
        status="preview",
    )
    item = submit_mission_queue_item(workspace_root=workspace, mission_id=mission["mission_id"])
    return workspace.resolve(), item
