from __future__ import annotations

import json

from visual_agent.cli import main
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


def ready_workspace(tmp_path, monkeypatch):
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    return workspace


def test_mission_start_status_and_list_facade(tmp_path, capsys, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)

    code = main(
        [
            "mission",
            "start",
            "--goal",
            "Fix checkout total display",
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    mission_id = payload["mission"]["mission_id"]

    status_code = main(["mission", "status", "--mission", mission_id, "--workspace-root", str(workspace.root), "--format", "json"])
    status_payload = json.loads(capsys.readouterr().out)
    list_code = main(["mission", "list", "--workspace-root", str(workspace.root), "--format", "json"])
    list_payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "preview"
    assert status_code == 0
    assert status_payload["status"] == "preview"
    assert list_code == 0
    assert list_payload[0]["mission_id"] == mission_id


def test_mission_queue_facade_submits_preview_mission(tmp_path, capsys, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    main(
        [
            "mission",
            "start",
            "--goal",
            "Fix checkout total display",
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--format",
            "json",
        ]
    )
    mission_id = json.loads(capsys.readouterr().out)["mission"]["mission_id"]

    code = main(["mission", "queue", "--mission", mission_id, "--workspace-root", str(workspace.root), "--format", "json"])
    queue_item = json.loads(capsys.readouterr().out)

    assert code == 0
    assert queue_item["mission_id"] == mission_id
    assert queue_item["status"] == "pending"


def test_mission_worker_facade_uses_queue_worker(tmp_path, capsys, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    main(
        [
            "mission",
            "start",
            "--goal",
            "Fix checkout total display",
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--format",
            "json",
        ]
    )
    mission_id = json.loads(capsys.readouterr().out)["mission"]["mission_id"]
    main(["mission", "queue", "--mission", mission_id, "--workspace-root", str(workspace.root), "--format", "json"])
    capsys.readouterr()

    def fake_runner(**kwargs):
        return {
            "status": "verified",
            "stop_reason": "verified",
            "message": "Mission verified by fake runner.",
            "final_report_path": str(workspace.root / "missions" / kwargs["resume_mission_id"] / "final_report.md"),
        }

    monkeypatch.setattr("visual_agent.chief_queue.run_chief_mission", fake_runner)

    code = main(["mission", "worker", "--workspace-root", str(workspace.root), "--run-once", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "run_once_completed"
    assert payload["processed_items"] == 1
    assert payload["runs"][0]["queue_item"]["status"] == "success"


def test_mission_memory_facade_outputs_project_memory(tmp_path, capsys, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    main(
        [
            "mission",
            "start",
            "--goal",
            "Fix checkout total display",
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--format",
            "json",
        ]
    )
    capsys.readouterr()

    code = main(["mission", "memory", "--goal", "checkout total", "--workspace-root", str(workspace.root), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["entry_count"] == 1
    assert payload["entries"][0]["objective"] == "Fix checkout total display"
