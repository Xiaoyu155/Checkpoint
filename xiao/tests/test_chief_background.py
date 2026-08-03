from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from datetime import datetime, timedelta, timezone

import pytest

from visual_agent.chief_background import (
    inspect_background_state,
    run_background_worker,
    save_background_record,
    start_background_chief_run,
)
from visual_agent.chief_run import mission_status_payload, run_chief_mission
from visual_agent.missions import append_round, load_mission, load_rounds, save_mission
from visual_agent.workspace import init_workspace


@pytest.fixture(autouse=True)
def _liveness_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Background start now probes agent liveness; keep unit tests offline-friendly."""
    monkeypatch.setattr(
        "visual_agent.provider_liveness.probe_worker_agent_liveness",
        lambda *_a, **_k: {
            "ok": True,
            "agent": "codex",
            "stop_reason": "",
            "message": "",
            "details": {},
        },
    )


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

    payload = start_background_chief_run(
        workspace_root=workspace.root,
        mission_id=mission_id,
        agents=("codex",),
        test_command="python -m pytest -q",
        allow_test_edits=True,
        merge=True,
    )

    assert payload["status"] == "background_started"
    assert payload["background"]["pid"] == 43210
    assert "chief-background-worker" in captured["argv"]
    assert "--mission" in captured["argv"]
    assert mission_id in captured["argv"]
    assert "--agent" in captured["argv"]
    assert "codex" in captured["argv"]
    assert "--test-command" in captured["argv"]
    assert "python -m pytest -q" in captured["argv"]
    assert "--allow-test-edits" in captured["argv"]
    assert "--merge" in captured["argv"]
    if os.name == "nt":
        assert captured["kwargs"]["creationflags"] & getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        assert captured["kwargs"].get("startupinfo") is not None
    background_json = workspace.root / "missions" / mission_id / "background.json"
    saved = json.loads(background_json.read_text(encoding="utf-8"))
    assert saved["pid"] == 43210
    assert saved["status"] == "running"
    assert saved["agents"] == ["codex"]
    assert saved["test_command"] == "python -m pytest -q"
    assert saved["allow_test_edits"] is True
    assert saved["merge"] is True


def test_start_background_chief_run_inherits_saved_allow_dirty(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        allow_dirty=True,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]

    captured = {}

    class FakeProcess:
        pid = 43211

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("visual_agent.chief_background.subprocess.Popen", fake_popen)

    payload = start_background_chief_run(
        workspace_root=workspace.root,
        mission_id=mission_id,
        agents=("codex",),
    )

    assert payload["status"] == "background_started"
    assert "--allow-dirty" in captured["argv"]
    assert payload["background"]["allow_dirty"] is True
    assert payload["mission"]["allow_dirty"] is True


def test_start_background_chief_run_blocks_duplicate_live_process(tmp_path, monkeypatch) -> None:
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
    launches = []

    class FakeProcess:
        pid = 43210

    def fake_popen(argv, **kwargs):
        launches.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr("visual_agent.chief_background.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "visual_agent.chief_background.process_status",
        lambda pid: {"pid": pid, "alive": pid == 43210, "exit_code": None},
    )
    monkeypatch.setattr(
        "visual_agent.chief_background.process_command_line",
        lambda pid: f"python -m visual_agent.cli chief-background-worker --mission {mission_id}"
        if pid == 43210
        else "",
    )

    first = start_background_chief_run(workspace_root=workspace.root, mission_id=mission_id)
    saved_before = (workspace.root / "missions" / mission_id / "background.json").read_text(encoding="utf-8")
    second = start_background_chief_run(workspace_root=workspace.root, mission_id=mission_id)

    assert first["status"] == "background_started"
    assert first["background"]["allow_dirty"] is False
    assert first["mission"]["allow_dirty"] is False
    assert second["status"] == "blocked"
    assert second["stop_reason"] == "background_already_running"
    worker_launches = [item for item in launches if "chief-background-worker" in " ".join(str(x) for x in item[0])]
    assert len(worker_launches) == 1
    assert (workspace.root / "missions" / mission_id / "background.json").read_text(encoding="utf-8") == saved_before


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

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"status": "verified", "stop_reason": "verified", "final_report_path": "report.md"}

    monkeypatch.setattr("visual_agent.chief_background.run_chief_mission", fake_run)

    payload = run_background_worker(
        workspace_root=workspace.root,
        mission_id=mission_id,
        agents=("codex",),
        test_command="python -m pytest -q",
        allow_test_edits=True,
        merge=True,
    )

    assert payload["status"] == "verified"
    assert captured["agents"] == ("codex",)
    assert captured["test_command"] == "python -m pytest -q"
    assert captured["allow_test_edits"] is True
    assert captured["merge"] is True
    saved = json.loads((workspace.root / "missions" / mission_id / "background.json").read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert saved["exit_code"] == 0
    assert saved["result_status"] == "verified"


def test_watchdog_writes_heartbeat(tmp_path, monkeypatch) -> None:
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
    save_background_record(
        workspace.root,
        mission_id,
        {"status": "running", "pid": 123, "started_at": datetime.now(timezone.utc).isoformat()},
    )
    progress_path = workspace.root / "missions" / mission_id / "progress.json"

    def fake_run(**_kwargs):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if progress_path.exists():
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
                if progress.get("heartbeat_at"):
                    break
            time.sleep(0.02)
        return {"status": "verified", "stop_reason": "verified", "final_report_path": "report.md"}

    monkeypatch.setattr("visual_agent.chief_background.run_chief_mission", fake_run)

    payload = run_background_worker(
        workspace_root=workspace.root,
        mission_id=mission_id,
        agents=("codex",),
        watchdog_interval_seconds=0.1,
    )

    assert payload["status"] == "verified"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["heartbeat_at"]


def test_watchdog_marks_timeout_state_before_exit(tmp_path, monkeypatch) -> None:
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
    save_background_record(workspace.root, mission_id, {"status": "running", "pid": 123, "started_at": old_start})

    terminated = []
    termination_seen = threading.Event()

    def fake_terminator(code: int) -> None:
        terminated.append(code)
        termination_seen.set()

    def fake_run(**_kwargs):
        assert termination_seen.wait(2.0)
        return {"status": "verified", "stop_reason": "verified", "final_report_path": "report.md"}

    monkeypatch.setattr("visual_agent.chief_background.run_chief_mission", fake_run)

    payload = run_background_worker(
        workspace_root=workspace.root,
        mission_id=mission_id,
        agents=("codex",),
        watchdog_interval_seconds=0.1,
        watchdog_terminator=fake_terminator,
    )

    assert terminated == [124]
    assert payload["status"] == "timeout"
    assert payload["stop_reason"] == "budget_exhausted"
    saved = json.loads((workspace.root / "missions" / mission_id / "background.json").read_text(encoding="utf-8"))
    assert saved["status"] == "timeout"
    assert saved["exit_code"] == 124
    assert saved["budget_exceeded"] is True
    progress = json.loads((workspace.root / "missions" / mission_id / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "timeout"
    assert progress["blocker"] == "budget_exhausted"
    mission = load_mission(workspace.root, mission_id)
    assert mission is not None
    assert mission["stop_reason"] == "budget_exhausted"


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
    assert mission["stop_reason"] == "worker_orphaned"
    rounds = load_rounds(workspace.root, mission_id)
    assert rounds[-1]["type"] == "background_health"


def test_inspect_background_state_treats_aborted_as_terminal(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    mission_id = "aborted-terminal"
    save_background_record(workspace.root, mission_id, {"status": "aborted", "worker_pid": 999})

    state = inspect_background_state(
        workspace_root=workspace.root,
        mission_id=mission_id,
        process_probe=lambda _pid: (_ for _ in ()).throw(AssertionError("terminal state must not probe PID")),
    )

    assert state["status"] == "aborted"
    assert state["alive"] is False
    assert state["process_state"] == "aborted"


def test_background_alive_rejects_pid_owned_by_other_mission(tmp_path, monkeypatch) -> None:
    from visual_agent.chief_background import (
        _background_record_is_alive,
        process_belongs_to_mission,
        start_background_chief_run,
    )

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
    foreign_cmd = "python -m visual_agent.cli chief-background-worker --mission OTHER-MISSION-ID"
    monkeypatch.setattr(
        "visual_agent.chief_background.process_status",
        lambda _pid: {"alive": True, "exit_code": None},
    )
    monkeypatch.setattr(
        "visual_agent.chief_background.process_command_line",
        lambda _pid: foreign_cmd,
    )
    record = {"status": "running", "pid": 4242, "worker_pid": 4242}
    assert process_belongs_to_mission(4242, mission_id, record=record) is False
    assert _background_record_is_alive(record, mission_id) is False

    save_background_record(workspace.root, mission_id, {**record, "started_at": datetime.now(timezone.utc).isoformat()})
    # Should not block as already_running; relaunch path marks stale and starts (or attempts)
    monkeypatch.setattr(
        "visual_agent.chief_background.subprocess.Popen",
        lambda *a, **k: type("P", (), {"pid": 7777})(),
    )
    payload = start_background_chief_run(
        workspace_root=workspace.root,
        mission_id=mission_id,
        agents=("codex",),
        allow_dirty=True,
    )
    assert payload["stop_reason"] != "background_already_running"
    assert payload["status"] in {"background_started", "blocked"}


def test_inspect_background_state_preserves_verified_mission_when_launcher_exits(tmp_path, monkeypatch) -> None:
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
    mission = load_mission(workspace.root, mission_id)
    assert mission is not None
    mission["status"] = "verified"
    mission["stop_reason"] = "verified"
    save_mission(workspace.root, mission)
    append_round(workspace.root, mission_id, {"round": 1, "type": "verification", "status": "pass"})
    save_background_record(workspace.root, mission_id, {"status": "running", "pid": 999, "started_at": datetime.now(timezone.utc).isoformat()})

    state = inspect_background_state(
        workspace_root=workspace.root,
        mission_id=mission_id,
        process_probe=lambda _pid: {"alive": False, "exit_code": None},
    )

    assert state["status"] == "completed"
    assert state["process_state"] == "reconciled_from_mission"
    mission = load_mission(workspace.root, mission_id)
    assert mission is not None
    assert mission["status"] == "verified"
    assert mission["stop_reason"] == "verified"


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
    assert payload["stop_reason"] == "worker_orphaned"
    assert "final_report_path" in payload
    assert (workspace.root / "missions" / mission_id / "final_report.md").exists()
