from __future__ import annotations

import subprocess
import threading
from datetime import datetime, timedelta, timezone

from visual_agent.mission_progress import (
    build_mission_progress,
    load_mission_progress,
    progress_path,
    record_worker_output,
    save_mission_progress,
)
from visual_agent.missions import append_round, save_mission


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_build_mission_progress_reports_product_diff_without_runtime_noise(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "devpacer@example.local")
    _git(repo, "config", "user.name", "DevPacer")
    (repo / "src").mkdir()
    (repo / "src" / "app.js").write_text("export const value = 1;\n", encoding="utf-8")
    _git(repo, "add", "src/app.js")
    _git(repo, "commit", "-m", "baseline")

    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix app",
        "status": "background_running",
        "stop_reason": "",
        "repo_root": str(repo),
    }
    save_mission(workspace, mission)
    append_round(
        workspace,
        "m1",
        {
            "round": 0,
            "type": "dispatch_preview",
            "status": "preview",
            "payload": {"worktree": {"path": str(repo)}},
        },
    )
    save_mission_progress(workspace, "m1", stage="worker_running", worktree=str(repo))

    (repo / "src" / "app.js").write_text("export const value = 2;\n", encoding="utf-8")
    (repo / "eval").mkdir()
    (repo / "eval" / "acceptance.mjs").write_text("export const cases = [];\n", encoding="utf-8")
    (repo / "快手").mkdir()
    (repo / "快手" / "test.js").write_text("console.log('test');\n", encoding="utf-8")
    (repo / "快手" / "feature.js").write_text("export const feature = true;\n", encoding="utf-8")
    (repo / ".agent-workspace").mkdir()
    (repo / ".agent-workspace" / "run.json").write_text("noise\n", encoding="utf-8")

    progress = build_mission_progress(
        workspace_root=workspace,
        mission=mission,
        background={"status": "running", "alive": True},
    )

    assert progress["stage"] == "worker_running"
    assert progress["changed_product_files"] == ["src/app.js", "快手/feature.js"]
    assert progress["changed_product_file_count"] == 2
    assert "eval/acceptance.mjs" in progress["changed_files"]
    assert "快手/test.js" in progress["changed_files"]
    assert "快手/test.js" not in progress["changed_product_files"]
    assert ".agent-workspace/run.json" in progress["changed_files"]


def test_load_mission_progress_ignores_corrupt_utf8(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    path = progress_path(workspace, "m1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"stage":"worker_running"}\xae')

    assert load_mission_progress(workspace, "m1") == {}


def test_worker_output_progress_writes_are_atomic_under_threads(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    save_mission_progress(workspace, "m1", stage="worker_running", stop_reason="preview_only", blocker="preview_only")

    def write_output(index: int) -> None:
        record_worker_output(
            workspace,
            "m1",
            stream="stdout" if index % 2 == 0 else "stderr",
            chunk=f"chunk-{index}",
        )

    threads = [threading.Thread(target=write_output, args=(index,)) for index in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    progress = load_mission_progress(workspace, "m1")
    assert progress["stage"] == "worker_running"
    assert progress["status"] == "running"
    assert progress["stop_reason"] == ""
    assert progress["blocker"] == ""
    assert progress["needs_attention"] is False
    assert str(progress["last_output_tail"]).startswith("chunk-")


def test_running_background_progress_overrides_stale_completed_worker_record(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix app",
        "status": "running",
        "stop_reason": "",
    }
    save_mission_progress(workspace, "m1", stage="worker_running")

    progress = build_mission_progress(
        workspace_root=workspace,
        mission=mission,
        background={"status": "running", "alive": True},
        worker_records=[{"status": "completed", "exit_code": 0}],
        verification={},
        rounds=[],
    )

    assert progress["stage"] == "worker_running"


def test_current_completed_worker_during_running_background_reports_verification_running(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix app",
        "status": "running",
        "stop_reason": "",
    }
    save_mission_progress(workspace, "m1", stage="worker_running")

    progress = build_mission_progress(
        workspace_root=workspace,
        mission=mission,
        background={
            "status": "running",
            "alive": True,
            "worker_started_at": "2026-07-09T04:30:13+00:00",
        },
        worker_records=[
            {
                "status": "completed",
                "exit_code": 0,
                "recorded_at": "2026-07-09T04:41:16+00:00",
            }
        ],
        verification={},
        rounds=[],
    )

    assert progress["stage"] == "verification_running"
    assert progress["activity"] == "verification"


def test_verified_progress_is_not_attention_blocked(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix app",
        "status": "verified",
        "stop_reason": "verified",
    }

    progress = build_mission_progress(
        workspace_root=workspace,
        mission=mission,
        background={"status": "completed", "alive": False},
        worker_records=[{"status": "completed", "exit_code": 0}],
        verification={"verdict": "pass"},
        rounds=[],
    )

    assert progress["stage"] == "verified"
    assert progress["needs_attention"] is False
    assert progress["blocker"] == ""


def test_new_background_round_ignores_stale_verification_pass(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix app",
        "status": "running",
        "stop_reason": "",
    }
    save_mission_progress(workspace, "m1", stage="worker_running")

    progress = build_mission_progress(
        workspace_root=workspace,
        mission=mission,
        background={"status": "running", "alive": True},
        worker_records=[{"status": "completed", "exit_code": 0}],
        verification={"verdict": "pass"},
        rounds=[
            {"type": "verification", "status": "pass"},
            {"type": "background", "status": "started"},
            {"type": "dispatch_preview", "status": "preview"},
        ],
    )

    assert progress["stage"] == "worker_running"
    assert progress["verification_verdict"] == ""


def test_running_preview_without_background_reports_stale_worker_activity(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix app",
        "status": "running",
        "stop_reason": "",
    }
    save_mission_progress(workspace, "m1", stage="worker_running")

    progress = build_mission_progress(
        workspace_root=workspace,
        mission=mission,
        background={},
        worker_records=[],
        verification={},
        rounds=[{"type": "dispatch_preview", "status": "preview"}],
    )

    assert progress["stage"] == "worker_activity_stale"
    assert progress["needs_attention"] is True
    assert progress["blocker"] == "worker_activity_stale"


def test_worker_progress_infers_dependency_install_activity(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix app",
        "status": "running",
        "stop_reason": "",
    }
    save_mission_progress(
        workspace,
        "m1",
        stage="worker_running",
        last_output_tail="512 silly tarball no local data for accepts.tgz. Extracting by manifest.",
    )

    progress = build_mission_progress(
        workspace_root=workspace,
        mission=mission,
        background={"status": "running", "alive": True},
        worker_records=[],
        verification={},
        rounds=[],
    )

    assert progress["activity"] == "dependency_install"
    assert progress["activity_label"] == "Installing dependencies"


def test_worker_progress_infers_test_activity(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix app",
        "status": "running",
        "stop_reason": "",
    }
    save_mission_progress(
        workspace,
        "m1",
        stage="worker_running",
        last_output_tail="node --import ./test/setup.js --test test/diagnosisRisk.test.js",
    )

    progress = build_mission_progress(
        workspace_root=workspace,
        mission=mission,
        background={"status": "running", "alive": True},
        worker_records=[],
        verification={},
        rounds=[],
    )

    assert progress["activity"] == "tests_running"
    assert progress["activity_label"] == "Running tests"


def test_stack_trace_with_node_modules_is_not_dependency_install(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix app",
        "status": "running",
        "stop_reason": "",
    }
    save_mission_progress(
        workspace,
        "m1",
        stage="worker_running",
        last_output_tail=(
            "Error: failed assertion\n"
            "    at node_modules/vitest/dist/index.js:1:1\n"
            "node --test test/app.test.js\n"
        ),
    )

    progress = build_mission_progress(
        workspace_root=workspace,
        mission=mission,
        background={"status": "running", "alive": True},
        worker_records=[],
        verification={},
        rounds=[],
    )

    assert progress["activity"] == "tests_running"
    assert progress["activity_label"] == "Running tests"


def test_reported_activity_wins_over_inference(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix app",
        "status": "running",
        "stop_reason": "",
    }
    started_at = datetime.now(timezone.utc).isoformat()
    save_mission_progress(
        workspace,
        "m1",
        stage="worker_running",
        activity="verification",
        activity_command="pytest -q",
        activity_started_at=started_at,
        last_output_tail="npm ci\nextracting by manifest",
    )

    progress = build_mission_progress(
        workspace_root=workspace,
        mission=mission,
        background={"status": "running", "alive": True},
        worker_records=[],
        verification={},
        rounds=[],
    )

    assert progress["activity"] == "verification"
    assert progress["activity_command"] == "pytest -q"
    assert progress["activity_started_at"] == started_at
    assert progress["activity_elapsed_seconds"] >= 0


def test_stale_reported_activity_falls_back(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix app",
        "status": "running",
        "stop_reason": "",
    }
    save_mission_progress(
        workspace,
        "m1",
        stage="worker_running",
        activity="verification",
        activity_command="pytest -q",
        activity_started_at=(datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),
        last_output_tail="npm ci\nextracting by manifest",
    )

    progress = build_mission_progress(
        workspace_root=workspace,
        mission=mission,
        background={"status": "running", "alive": True},
        worker_records=[],
        verification={},
        rounds=[],
    )

    assert progress["activity"] == "dependency_install"
    assert progress["activity_command"] == ""
    assert progress["activity_started_at"] == ""


def test_blocked_progress_does_not_report_stale_activity(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix app",
        "status": "stopped",
        "stop_reason": "verification_environment_missing",
    }
    save_mission_progress(
        workspace,
        "m1",
        stage="worker_running",
        activity="worker_output",
        activity_started_at=datetime.now(timezone.utc).isoformat(),
        last_output_tail="preflight blocked before worker start",
    )

    progress = build_mission_progress(
        workspace_root=workspace,
        mission=mission,
        background={},
        worker_records=[],
        verification={},
        rounds=[{"type": "dispatch_preview", "status": "preflight_blocked"}],
    )

    assert progress["stage"] == "blocked"
    assert progress["activity"] == ""
    assert progress["activity_label"] == ""
    assert progress["activity_elapsed_seconds"] is None


def test_verification_failed_does_not_report_running_verification(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix app",
        "status": "running",
        "stop_reason": "",
    }

    progress = build_mission_progress(
        workspace_root=workspace,
        mission=mission,
        background={},
        worker_records=[{"status": "completed", "exit_code": 0}],
        verification={"verdict": "fail"},
        rounds=[{"type": "verification", "status": "fail"}],
    )

    assert progress["stage"] == "verification_failed"
    assert progress["activity"] == ""
    assert progress["activity_label"] == ""


def test_blank_worker_output_does_not_replace_last_meaningful_tail(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    record_worker_output(workspace, "m1", stream="stderr", chunk="npm ci --prefer-offline")
    record_worker_output(workspace, "m1", stream="stderr", chunk="\n")

    progress = load_mission_progress(workspace, "m1")

    assert progress["last_output_tail"] == "npm ci --prefer-offline"


def test_late_worker_output_does_not_reset_terminal_stage(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    save_mission_progress(
        workspace,
        "m1",
        stage="verified",
        stage_label="Verified",
        status="verified",
        stop_reason="verified",
        blocker="",
        needs_attention=False,
    )

    progress = record_worker_output(
        workspace,
        "m1",
        stream="stdout",
        chunk="late worker output after terminal state",
    )

    assert progress["stage"] == "verified"
    assert progress["stage_label"] == "Verified"
    assert progress["status"] == "verified"
    assert progress["stop_reason"] == "verified"
    assert progress["blocker"] == ""
    assert progress["needs_attention"] is False
    assert progress["last_output_tail"] == "late worker output after terminal state"
