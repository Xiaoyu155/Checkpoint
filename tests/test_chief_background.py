from __future__ import annotations

import json

from datetime import datetime, timedelta, timezone

from visual_agent.chief_background import (
    inspect_background_state,
    run_background_worker,
    save_background_record,
    start_background_chief_run,
)
from visual_agent.chief_run import mission_status_payload, run_chief_mission
from visual_agent.missions import load_mission, load_rounds, save_mission
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


def test_start_background_chief_run_records_process(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]

    captured = {}

    class FakeProcess:
        pid = 43210

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("visual_agent.chief_background.subprocess.Popen", fake_popen)

    payload = start_background_chief_run(workspace_root=workspace.root, mission_id=mission_id)

    assert payload["status"] == "background_started"
    assert payload["background"]["pid"] == 43210
    assert "chief-background-worker" in captured["argv"]
    assert "--mission" in captured["argv"]
    assert mission_id in captured["argv"]
    background_json = workspace.root / "missions" / mission_id / "background.json"
    saved = json.loads(background_json.read_text(encoding="utf-8"))
    assert saved["pid"] == 43210
    assert saved["status"] == "running"


def test_run_background_worker_writes_completion_receipt(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]
    save_background_record(workspace.root, mission_id, {"status": "running", "pid": 123})

    def fake_run(**_kwargs):
        return {"status": "verified", "stop_reason": "verified", "final_report_path": "report.md"}

    monkeypatch.setattr("visual_agent.chief_background.run_chief_mission", fake_run)

    payload = run_background_worker(workspace_root=workspace.root, mission_id=mission_id)

    assert payload["status"] == "verified"
    saved = json.loads((workspace.root / "missions" / mission_id / "background.json").read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert saved["exit_code"] == 0
    assert saved["result_status"] == "verified"


def test_inspect_background_state_marks_orphaned_process_as_worker_error(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]
    save_background_record(workspace.root, mission_id, {"status": "running", "pid": 999, "started_at": datetime.now(timezone.utc).isoformat()})

    state = inspect_background_state(
        workspace_root=workspace.root,
        mission_id=mission_id,
        process_probe=lambda _pid: {"alive": False, "exit_code": None},
    )

    assert state["status"] == "orphaned"
    mission = load_mission(workspace.root, mission_id)
    assert mission is not None
    assert mission["stop_reason"] == "worker_error"
    rounds = load_rounds(workspace.root, mission_id)
    assert rounds[-1]["type"] == "background_health"


def test_inspect_background_state_times_out_and_marks_budget_exhausted(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        max_wall_minutes=1,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]
    mission = load_mission(workspace.root, mission_id)
    assert mission is not None
    mission["budget_policy"]["max_wall_minutes"] = 1
    save_mission(workspace.root, mission)
    old_start = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    save_background_record(workspace.root, mission_id, {"status": "running", "pid": 999, "started_at": old_start})

    state = inspect_background_state(
        workspace_root=workspace.root,
        mission_id=mission_id,
        process_probe=lambda _pid: {"alive": True, "exit_code": None},
        terminator=lambda _pid: True,
    )

    assert state["status"] == "timeout"
    assert state["exit_code"] == 124
    mission = load_mission(workspace.root, mission_id)
    assert mission is not None
    assert mission["stop_reason"] == "budget_exhausted"


def test_mission_status_reconciles_missing_background_process(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]
    save_background_record(workspace.root, mission_id, {"status": "running", "pid": 999, "started_at": datetime.now(timezone.utc).isoformat()})
    monkeypatch.setattr("visual_agent.chief_background.process_status", lambda _pid: {"alive": False, "exit_code": None})

    payload = mission_status_payload(workspace_root=workspace.root, mission_id=mission_id)

    assert payload["background"]["status"] == "orphaned"
    assert payload["stop_reason"] == "worker_error"
    assert "final_report_path" in payload
    assert (workspace.root / "missions" / mission_id / "final_report.md").exists()
