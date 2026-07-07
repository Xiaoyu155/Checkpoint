from __future__ import annotations

import json
import os
import pytest

from visual_agent.cli import main
from visual_agent.chief_queue import (
    _acquire_worker_lock,
    _release_worker_lock,
    _worker_lock_path,
    claim_next_mission_queue_item,
    list_mission_queue_items,
    load_mission_queue_item,
    run_mission_queue_worker,
    submit_mission_queue_item,
)
from visual_agent.missions import create_mission, default_budget_policy


def create_preview_mission(tmp_path, *, mission_id: str = "mission-1", status: str = "preview"):
    workspace = tmp_path / ".agent-workspace"
    mission = create_mission(
        workspace_root=workspace,
        objective="Fix checkout total",
        repo_root=tmp_path,
        plan_id="plan-1",
        budget_policy=default_budget_policy(max_rounds=2, max_wall_minutes=30),
        mission_id=mission_id,
        status=status,
    )
    return workspace, mission


def test_submit_mission_queue_item_lists_pending(tmp_path) -> None:
    workspace, mission = create_preview_mission(tmp_path)

    item = submit_mission_queue_item(workspace_root=workspace, mission_id=mission["mission_id"], priority=5)
    payload = list_mission_queue_items(workspace)

    assert item.status == "pending"
    assert payload["pending_items"] == 1
    assert payload["entries"][0]["queue_id"] == item.queue_id
    assert payload["entries"][0]["priority"] == 5


def test_chief_queue_cli_submit_and_list(tmp_path, capsys) -> None:
    workspace, mission = create_preview_mission(tmp_path)

    submit_code = main(
        [
            "chief-queue",
            "submit",
            "--workspace-root",
            str(workspace),
            "--mission",
            mission["mission_id"],
            "--format",
            "json",
        ]
    )
    submit_out = capsys.readouterr().out
    list_code = main(["chief-queue", "list", "--workspace-root", str(workspace), "--format", "json"])
    list_out = capsys.readouterr().out

    assert submit_code == 0
    assert '"status": "pending"' in submit_out
    assert list_code == 0
    assert '"pending_items": 1' in list_out


def test_submit_mission_queue_item_refuses_non_runnable_without_force(tmp_path) -> None:
    workspace, mission = create_preview_mission(tmp_path, status="verified")

    with pytest.raises(RuntimeError, match="not runnable"):
        submit_mission_queue_item(workspace_root=workspace, mission_id=mission["mission_id"])

    forced = submit_mission_queue_item(workspace_root=workspace, mission_id=mission["mission_id"], force=True)

    assert forced.status == "pending"


def test_submit_mission_queue_item_refuses_duplicate_active_item(tmp_path) -> None:
    workspace, mission = create_preview_mission(tmp_path)
    submit_mission_queue_item(workspace_root=workspace, mission_id=mission["mission_id"])

    with pytest.raises(RuntimeError, match="already queued"):
        submit_mission_queue_item(workspace_root=workspace, mission_id=mission["mission_id"])


def test_claim_next_mission_queue_item_prevents_double_claim(tmp_path) -> None:
    workspace, mission = create_preview_mission(tmp_path)
    submitted = submit_mission_queue_item(workspace_root=workspace, mission_id=mission["mission_id"])

    claimed = claim_next_mission_queue_item(workspace)
    second = claim_next_mission_queue_item(workspace)

    assert claimed is not None
    assert claimed.queue_id == submitted.queue_id
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert second is None
    saved = load_mission_queue_item(workspace, submitted.queue_id)
    assert saved is not None
    assert saved.status == "running"


def test_run_mission_queue_worker_run_once_marks_success(tmp_path) -> None:
    workspace, mission = create_preview_mission(tmp_path)
    submitted = submit_mission_queue_item(
        workspace_root=workspace,
        mission_id=mission["mission_id"],
        run_profile="supervised",
        allow_dirty=True,
    )
    captured = {}

    def fake_runner(**kwargs):
        captured.update(kwargs)
        return {
            "status": "verified",
            "stop_reason": "verified",
            "final_report_path": str(workspace / "missions" / mission["mission_id"] / "final_report.md"),
        }

    payload = run_mission_queue_worker(workspace_root=workspace, run_once=True, mission_runner=fake_runner)

    assert payload["status"] == "run_once_completed"
    assert payload["processed_items"] == 1
    assert captured["resume_mission_id"] == mission["mission_id"]
    assert captured["execute"] is True
    assert captured["dry_run"] is False
    assert captured["run_profile"] == "supervised"
    assert captured["allow_dirty"] is True
    saved = load_mission_queue_item(workspace, submitted.queue_id)
    assert saved is not None
    assert saved.status == "success"
    assert saved.last_result_status == "verified"


def test_queue_item_carries_worker_and_verification_context(tmp_path) -> None:
    workspace, mission = create_preview_mission(tmp_path)
    submit_mission_queue_item(
        workspace_root=workspace,
        mission_id=mission["mission_id"],
        agent="codex",
        test_command="npm test",
        allow_test_edits=True,
        merge_policy="auto",
    )
    captured = {}

    def fake_runner(**kwargs):
        captured.update(kwargs)
        return {"status": "verified", "stop_reason": "verified"}

    run_mission_queue_worker(workspace_root=workspace, run_once=True, mission_runner=fake_runner)

    assert captured["agents"] == ("codex",)
    assert captured["test_command"] == "npm test"
    assert captured["allow_test_edits"] is True
    assert captured["merge"] is True


def test_run_mission_queue_worker_marks_non_verified_result_failed(tmp_path) -> None:
    workspace, mission = create_preview_mission(tmp_path)
    submitted = submit_mission_queue_item(workspace_root=workspace, mission_id=mission["mission_id"])

    def fake_runner(**_kwargs):
        return {
            "status": "stopped",
            "stop_reason": "coverage_gap",
            "message": "Mission stopped because workflow coverage is missing.",
        }

    payload = run_mission_queue_worker(workspace_root=workspace, run_once=True, mission_runner=fake_runner)

    assert payload["status"] == "run_once_completed"
    saved = load_mission_queue_item(workspace, submitted.queue_id)
    assert saved is not None
    assert saved.status == "failed"
    assert saved.last_result_status == "stopped"
    assert saved.last_stop_reason == "coverage_gap"


def test_worker_lock_blocks_second_watch_worker(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir(parents=True)
    # Acquire lock as current process
    existing = _acquire_worker_lock(workspace)
    assert existing is None, "First acquire should succeed"
    # Second acquire with same PID should see an active lock
    existing2 = _acquire_worker_lock(workspace)
    assert existing2 is not None, "Second acquire should fail when lock is held"
    assert existing2.get("pid") == os.getpid()
    _release_worker_lock(workspace)
    assert not _worker_lock_path(workspace).exists()


def test_stale_worker_lock_is_overwritten(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir(parents=True)
    # Write a lock with a PID that cannot exist (max PID + 1 heuristic)
    stale = {"pid": 9999999, "started_at": "2000-01-01T00:00:00+00:00"}
    _worker_lock_path(workspace).write_text(json.dumps(stale), encoding="utf-8")
    existing = _acquire_worker_lock(workspace)
    assert existing is None, "Stale lock should be overwritten"
    _release_worker_lock(workspace)


def test_watch_worker_blocked_by_existing_lock(tmp_path) -> None:
    workspace, mission = create_preview_mission(tmp_path)
    # Plant a fake lock from a non-existent PID so it looks live
    _worker_lock_path(workspace).write_text(
        json.dumps({"pid": os.getpid(), "started_at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    result = run_mission_queue_worker(workspace_root=workspace, watch=True)
    assert result["status"] == "blocked"
    assert "worker" in result["reason"].lower()
    _release_worker_lock(workspace)
