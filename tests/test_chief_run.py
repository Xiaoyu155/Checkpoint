from __future__ import annotations

from pathlib import Path

from visual_agent.chief_run import mission_status_payload, run_chief_mission
from visual_agent.missions import load_mission, load_rounds
from visual_agent.workspace import init_workspace


def write_verification_workflow(workspace, name: str, *, affects: str = "src/payment/") -> None:
    workspace.workflows_dir.joinpath(f"{name}.yaml").write_text(
        "schema_version: 1\n"
        f"name: {name}\n"
        "version: 1\n"
        "affects:\n"
        f"  - {affects}\n"
        "tags:\n"
        "  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_ocr\n"
        "    mock_text: ready\n",
        encoding="utf-8",
    )


def preview_payload(**_kwargs):
    return {
        "schema_version": 1,
        "status": "preview",
        "worker": {"track_id": "track_1_codex", "agent": "codex", "command": "codex exec ..."},
        "worktree": {"path": "worktree", "branch": "checkpoint/plan/track", "created": False},
        "verification": {"command": "checkpoint codex-check", "run_profile": "dry-run"},
    }


def verified_payload(**kwargs):
    if not kwargs.get("execute"):
        return preview_payload(**kwargs)
    attempt = {
        "verdict": "pass",
        "run_profile": "dry-run",
        "passed": 1,
        "inspection_only": 0,
        "failed": 0,
        "total": 1,
        "results": [{"name": "checkout", "status": "passed"}],
    }
    return {
        "schema_version": 1,
        "status": "verified",
        "worker_record": {"status": "completed"},
        "verification_attempts": [attempt],
        "latest_verification": attempt,
    }


def repeated_failure_payload(**kwargs):
    if not kwargs.get("execute"):
        return preview_payload(**kwargs)
    attempt = {
        "verdict": "fail",
        "run_profile": "dry-run",
        "passed": 0,
        "inspection_only": 0,
        "failed": 1,
        "total": 1,
        "results": [
            {
                "name": "checkout",
                "status": "failed",
                "failed_step": "assert_total",
                "message": "expected total 128",
            }
        ],
    }
    return {
        "schema_version": 1,
        "status": "verification_failed",
        "worker_record": {"status": "completed"},
        "verification_attempts": [attempt, attempt],
        "latest_verification": attempt,
    }


def ready_workspace(tmp_path, monkeypatch):
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    return workspace


def test_chief_run_dry_run_saves_preview_mission(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    payload = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )

    assert payload["status"] == "preview"
    assert payload["stop_reason"] == "preview_only"
    mission = payload["mission"]
    assert mission["product"] == "DevPacer"
    assert Path(payload["final_report_path"]).exists()
    rounds = load_rounds(workspace.root, mission["mission_id"])
    assert rounds[0]["type"] == "dispatch_preview"


def test_chief_run_blocks_vague_goal_before_dispatch(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    def should_not_dispatch(**_kwargs):
        raise AssertionError("dispatch should not run for vague goals")

    payload = run_chief_mission(
        goal="改一下",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=should_not_dispatch,
    )

    assert payload["status"] == "stopped"
    assert payload["stop_reason"] == "needs_clarification"
    mission = load_mission(workspace.root, payload["mission"]["mission_id"])
    assert mission is not None
    assert mission["status"] == "stopped"


def test_chief_run_execute_verified(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    payload = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        execute=True,
        dry_run=False,
        dispatch_runner=verified_payload,
    )

    assert payload["status"] == "verified"
    assert payload["stop_reason"] == "verified"
    rounds = load_rounds(workspace.root, payload["mission"]["mission_id"])
    assert rounds[-1]["status"] == "pass"


def test_chief_run_execute_stops_on_repeated_failure(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    payload = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        execute=True,
        dry_run=False,
        max_rounds=2,
        dispatch_runner=repeated_failure_payload,
    )

    assert payload["status"] == "stopped"
    assert payload["stop_reason"] == "same_failure_repeated"
    rounds = load_rounds(workspace.root, payload["mission"]["mission_id"])
    assert rounds[-1]["failed_signature"] == "checkout|assert_total|expected total 128"


def test_chief_run_resume_executes_existing_preview_mission(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]

    resumed = run_chief_mission(
        workspace_root=workspace.root,
        resume_mission_id=mission_id,
        execute=True,
        dry_run=False,
        dispatch_runner=verified_payload,
    )

    assert resumed["status"] == "verified"
    assert resumed["mission"]["mission_id"] == mission_id
    rounds = load_rounds(workspace.root, mission_id)
    assert [item["round"] for item in rounds] == [0, 1, 2]
    assert rounds[-1]["status"] == "pass"


def test_mission_status_payload_reports_next_action(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )

    payload = mission_status_payload(workspace_root=workspace.root, mission_id=preview["mission"]["mission_id"])

    assert payload["status"] == "preview"
    assert "chief-run --resume" in payload["message"]
    assert payload["rounds"][0]["type"] == "dispatch_preview"
