from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from visual_agent.chief_plans_store import append_worker_record, save_plan, save_verification
from visual_agent.commercial_config import CommercialConfig
from visual_agent.dashboard.api import (
    get_model_config,
    refine_goal_intake,
    retry_mission,
    run_chat,
    save_model_config as save_dashboard_model_config,
    set_active_workspace,
)
from visual_agent.dashboard import (
    DASHBOARD_HTML,
    _bind_dashboard_server,
    build_dashboard_data,
    build_five_pillars_data,
    build_mission_detail,
    start_worker,
    stop_worker,
)
from visual_agent.mission_progress import save_mission_progress
from visual_agent.missions import default_budget_policy, load_mission, missions_dir, save_mission
from visual_agent.user_profile import LocalUserProfile
from visual_agent.workbench_model_config import WorkbenchModelConfig


def _verification_summary(
    run_id: str,
    *,
    launch_id: str = "launch-dashboard",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "pacer_verification_batch",
        "source_tool": "run_pacer_verification",
        "policy_version": 1,
        "run_id": run_id,
        "launch_id": launch_id,
        "status": "passed",
        "requested_steps": 1,
        "executed_steps": 1,
        "skipped_steps": [],
        "passed": 1,
        "failed": 0,
        "timed_out": 0,
        "not_applicable": 0,
        "step_classes": ["test"],
        "records": [
            {
                "status": "passed",
                "command": ["python", "-m", "pytest", "-q"],
                "exit_code": 0,
            }
        ],
    }


def test_dashboard_data_on_empty_workspace(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    data = build_dashboard_data(workspace)

    assert data["product"] == "Pacer"
    assert data["orchestrator"] == "Pacer"
    assert data["engine"] == "Checkpoint"
    assert data["missions"] == []
    assert data["plans"] == []
    assert data["value"]["verified"] == 0
    assert data["value"]["tier_counts"] == {"cheap": 0, "standard": 0, "strong": 0}
    assert data["value"]["spent_usd"] == 0.0
    me = data["value"]["mimo_efficiency"]
    assert me["mimo_runs"] == 0
    assert me["saved_usd"] == 0.0
    assert me["saved_quota_percent"] == 0.0
    assert me["saved_minutes"] == 0.0
    assert me["efficiency_gain_percent"] == 0
    assert me["capability_score"] == 0.0
    assert "summary" in data["subscription_quota"]
    assert "level" in data["subscription_quota"]["summary"]
    assert "promotion_readiness" in data
    assert "checks" in data["promotion_readiness"]
    assert "core_readiness" in data
    assert "checks" in data["core_readiness"]
    assert any(check["id"] == "intake" for check in data["core_readiness"]["checks"])
    assert "labels" in me
    assert {a["agent"] for a in data["agents"]} >= {"codex", "claude-code"}


def test_five_pillars_dashboard_uses_reconciled_orphan_status(tmp_path, monkeypatch) -> None:
    from visual_agent.pacer_launch_context import (
        initialize_active_launch,
        read_reconciled_active_launch,
    )

    workspace = tmp_path / ".agent-workspace"
    native = workspace / "pacer_native"
    manifest = native / "launches" / "launch-orphan.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    (native / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(tmp_path.resolve()),
                "recorded_at": "2026-07-13T10:00:00+00:00",
                "goal": "show orphan",
                "summary": "launcher disappeared",
                "verification": "not run",
                "status": "failed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={
            "launch_id": "launch-orphan",
            "repo_root": str(tmp_path.resolve()),
            "launcher_pid": 424242,
        },
    )
    probes: list[int] = []

    def reconciled(root):
        return read_reconciled_active_launch(
            root,
            process_probe=lambda pid: probes.append(pid) or False,
            reconcile_interval_seconds=0,
        )

    monkeypatch.setattr("visual_agent.pacer_support.read_reconciled_active_launch", reconciled)
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "subscription"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_five_pillars_data(workspace)

    assert probes == [424242]
    assert payload["program"]["lifecycle_status"] == "orphaned"
    assert payload["active_launch"]["lifecycle_status"] == "orphaned"
    assert payload["active_launch"]["liveness"]["monitoring"] is False


def test_dashboard_data_uses_repo_inferred_from_external_workspace_ledger(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "external" / ".agent-workspace"
    native = workspace / "pacer_native"
    repo.mkdir()
    native.mkdir(parents=True)
    (native / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(repo.resolve()),
                "recorded_at": "2026-07-13T20:00:00+00:00",
                "goal": "dashboard external workspace",
                "summary": "repo inferred",
                "verification": "review",
                "status": "failed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )

    data = build_dashboard_data(workspace)

    assert data["repo_root"] == str(repo.resolve())
    assert data["pacer_support"]["memory"]["latest"]["goal"] == "dashboard external workspace"


def test_five_pillars_uses_dispatch_provider_and_upstream_memory(tmp_path, monkeypatch) -> None:
    tasks = [
        {"task_id": "task-001", "mission_id": "mission-one", "status": "verified"},
        {"task_id": "task-002", "mission_id": "mission-two", "status": "verified"},
    ]
    program = {
        "program_id": "program-five",
        "objective": "Five pillar proof",
        "status": "completed",
        "updated_at": "2026-07-12T00:00:00+00:00",
        "source_plan": "plan.md",
        "source_plan_sha256": "abc123",
        "tasks": tasks,
    }
    monkeypatch.setattr("visual_agent.dashboard.api.list_programs", lambda _root: [{"program_id": "program-five"}])
    monkeypatch.setattr("visual_agent.dashboard.api.load_program", lambda _root, _program_id: program)
    monkeypatch.setattr(
        "visual_agent.dashboard.api.load_mission",
        lambda _root, mission_id: {"mission_id": mission_id, "plan_id": f"plan-{mission_id}", "status": "verified"},
    )
    monkeypatch.setattr("visual_agent.dashboard.api.load_worker_records", lambda _root, _plan_id: [{"resolved_model": "gpt-5.5"}])
    monkeypatch.setattr(
        "visual_agent.dashboard.api._load_verification_payload",
        lambda _root, _plan_id: {"verdict": "pass", "command_verification": {"command": "pytest tests", "verdict": "pass"}},
    )

    def fake_dispatches(path: Path) -> list[dict]:
        memory_ids = ["mission:mission-one"] if "mission-two" in str(path) else []
        return [{
            "resolved_provider": "custom",
            "resolved_model": "gpt-5.5",
            "worker_attempts": 1,
            "repair_rounds": 0,
            "project_memory_usage": {"dispatch_memory_ids": memory_ids},
        }]

    monkeypatch.setattr("visual_agent.dashboard.api._read_jsonl", fake_dispatches)

    payload = build_five_pillars_data(tmp_path)

    assert payload["program"]["provider"] == "custom"
    assert payload["program"]["model"] == "gpt-5.5"
    assert payload["program"]["upstream_memory_id"] == "mission:mission-one"
    assert [item["status"] for item in payload["pillars"]] == ["passed"] * 5


def test_five_pillars_page_and_api_are_served(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/five-pillars") as response:
            html = response.read().decode("utf-8")
        with urllib.request.urlopen(base + "/api/five-pillars") as response:
            payload = json.loads(response.read().decode("utf-8-sig"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert "Pacer 五项闭环" in html
    assert 'id="pillarList"' in html
    assert 'id="telemetryMetric"' in html
    assert payload["ok"] is True
    assert payload["program"] is None
    assert "五项闭环实证" in DASHBOARD_HTML


def test_five_pillars_prefers_native_pacer_evidence(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    native = workspace / "pacer_native"
    native.mkdir(parents=True)
    run_id = "20260713-140000-dash1234"
    (native / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(tmp_path.resolve()),
                "recorded_at": "2026-07-13T14:00:00+00:00",
                "goal": "dashboard native proof",
                "summary": "complete",
                "verification": f"run_id={run_id}",
                "status": "completed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = native / "commands" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(_verification_summary(run_id)),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )

    payload = build_five_pillars_data(workspace)

    assert payload["mode"] == "native"
    assert payload["program"]["objective"] == "dashboard native proof"
    assert payload["program"]["verification_verdict"] == "passed"
    assert [item["status"] for item in payload["pillars"][:4]] == ["passed"] * 4


def test_five_pillars_rejects_legacy_passed_summary_and_stale_mechanical_green(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / ".agent-workspace"
    native = workspace / "pacer_native"
    native.mkdir(parents=True)
    run_id = "20260713-140100-legacy"
    (native / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(tmp_path.resolve()),
                "recorded_at": "2026-07-13T14:01:00+00:00",
                "goal": "legacy false green",
                "summary": "old summary lacks provenance",
                "verification": f"run_id={run_id}",
                "status": "completed",
                "evidence_level": "verified_batch",
                "batch_run_id": run_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = native / "commands" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "passed",
                "requested_steps": 1,
                "executed_steps": 1,
                "passed": 1,
                "failed": 0,
                "timed_out": 0,
                "not_applicable": 0,
            }
        ),
        encoding="utf-8",
    )
    (native / "active_launch.json").write_text(
        json.dumps(
            {
                "launch_id": "legacy-launch",
                "status": "completed",
                "pillars": {
                    name: {"active": True, "state": "verified"}
                    for name in ("routing", "memory", "managed", "acceptance", "dogfood")
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key"},
    )

    payload = build_five_pillars_data(workspace)
    pillars = {item["id"]: item for item in payload["pillars"]}

    assert payload["program"]["verification_verdict"] == "evidence incomplete"
    assert payload["support"]["commands"]["verified_runs"] == 0
    assert pillars["managed"]["status"] == "attention"
    assert pillars["acceptance"]["status"] == "attention"
    assert pillars["dogfood"]["status"] == "attention"
    assert "可信验收校验" in pillars["acceptance"]["evidence"]


def test_five_pillars_mechanical_launch_blocks_legacy_false_green(tmp_path, monkeypatch) -> None:
    from visual_agent.pacer_launch_context import write_launch_liveness

    workspace = tmp_path / ".agent-workspace"
    native = workspace / "pacer_native"
    native.mkdir(parents=True)
    run_id = "20260713-140000-legacygreen"
    (native / "history.jsonl").write_text(
        json.dumps({
            "repo_root": str(tmp_path.resolve()), "recorded_at": "2026-07-13T14:00:00+00:00",
            "goal": "legacy green", "summary": "complete", "verification": f"run_id={run_id}",
            "status": "completed", "evidence_level": "verified_batch", "batch_run_id": run_id,
        }) + "\n",
        encoding="utf-8",
    )
    run_dir = native / "commands" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps({"run_id": run_id, "status": "passed", "executed_steps": 1, "passed": 1}),
        encoding="utf-8",
    )
    (native / "active_launch.json").write_text(
        json.dumps({
            "launch_id": "current-launch",
            "status": "running",
            "pillars": {
                name: {"active": False, "state": "not_verified"}
                for name in ("routing", "memory", "managed", "acceptance", "dogfood")
            },
        }),
        encoding="utf-8",
    )
    write_launch_liveness(
        workspace,
        "current-launch",
        {
            "state": "stalled",
            "monitoring": True,
            "lifecycle_status": "running",
            "destructive_action": False,
        },
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    payload = build_five_pillars_data(workspace)
    assert [item["status"] for item in payload["pillars"]] == ["attention"] * 5
    assert all("launch=current-launch" in item["evidence"] for item in payload["pillars"])
    assert payload["program"]["lifecycle_status"] == "running"
    assert payload["program"]["liveness_state"] == "stalled"
    assert payload["active_launch"]["liveness"]["destructive_action"] is False


def test_five_pillars_rejects_failed_outcome_even_when_bound_batch_passed(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    native = workspace / "pacer_native"
    native.mkdir(parents=True)
    previous_run_id = "20260713-140000-completed"
    run_id = "20260713-141000-failed"
    (native / "history.jsonl").write_text(
        "\n".join(
            json.dumps(item)
            for item in (
                {
                    "repo_root": str(tmp_path.resolve()),
                    "recorded_at": "2026-07-13T14:00:00+00:00",
                    "goal": "historical completed outcome",
                    "summary": "previously completed",
                    "verification": f"run_id={previous_run_id}",
                    "status": "completed",
                },
                {
                    "repo_root": str(tmp_path.resolve()),
                    "recorded_at": "2026-07-13T14:10:00+00:00",
                    "goal": "failed native outcome",
                    "summary": "verification ran but the outcome failed",
                    "verification": f"run_id={run_id}",
                    "status": "failed",
                    "evidence_level": "verified_batch",
                    "batch_run_id": run_id,
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    for passed_run_id in (previous_run_id, run_id):
        run_dir = native / "commands" / passed_run_id
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps({"run_id": passed_run_id, "status": "passed", "executed_steps": 1, "passed": 1}),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )

    payload = build_five_pillars_data(workspace)
    pillars = {item["id"]: item for item in payload["pillars"]}

    assert payload["program"]["status"] == "failed"
    assert payload["program"]["verification_verdict"] == "evidence incomplete"
    assert pillars["managed"]["status"] == "attention"
    assert pillars["acceptance"]["status"] == "attention"
    assert "状态为 failed" in pillars["acceptance"]["evidence"]
    assert pillars["dogfood"]["status"] == "attention"


def test_five_pillars_uses_bound_batch_instead_of_unrelated_latest_run(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    native = workspace / "pacer_native"
    native.mkdir(parents=True)
    bound_run_id = "20260713-142000-bound"
    (native / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(tmp_path.resolve()),
                "recorded_at": "2026-07-13T14:20:00+00:00",
                "goal": "completed native outcome",
                "summary": "bound verification passed",
                "verification": f"run_id={bound_run_id}",
                "status": "completed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summaries = (
        _verification_summary(bound_run_id),
        {"run_id": "20260713-143000-unrelated", "status": "failed", "executed_steps": 1, "failed": 1},
    )
    for summary in summaries:
        run_dir = native / "commands" / summary["run_id"]
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )

    payload = build_five_pillars_data(workspace)
    pillars = {item["id"]: item for item in payload["pillars"]}

    assert payload["program"]["program_id"] == bound_run_id
    assert payload["program"]["verification_verdict"] == "passed"
    assert pillars["acceptance"]["status"] == "passed"


def test_five_pillars_rejects_unmatched_batch_even_when_latest_run_passed(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    native = workspace / "pacer_native"
    native.mkdir(parents=True)
    (native / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(tmp_path.resolve()),
                "recorded_at": "2026-07-13T14:30:00+00:00",
                "goal": "unmatched native outcome",
                "summary": "claims a missing batch",
                "verification": "run_id=missing-run",
                "status": "completed",
                "evidence_level": "verified_batch",
                "batch_run_id": "missing-run",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = native / "commands" / "20260713-144000-unrelated"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps({"run_id": run_dir.name, "status": "passed", "executed_steps": 1, "passed": 1}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )

    payload = build_five_pillars_data(workspace)
    pillars = {item["id"]: item for item in payload["pillars"]}

    assert payload["program"]["program_id"] == "missing-run"
    assert payload["program"]["verification_verdict"] == "evidence incomplete"
    assert pillars["acceptance"]["status"] == "attention"
    assert "missing-run" in pillars["acceptance"]["evidence"]


def test_five_pillars_rejects_conflicted_canonical_and_legacy_batch(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    canonical = workspace / "pacer_native"
    legacy = tmp_path / "pacer_native"
    canonical.mkdir(parents=True)
    legacy.mkdir(parents=True)
    run_id = "20260713-145000-conflict"
    (canonical / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(tmp_path.resolve()),
                "recorded_at": "2026-07-13T14:50:00+00:00",
                "goal": "conflicted dashboard outcome",
                "summary": "must not pass",
                "verification": f"run_id={run_id}",
                "status": "completed",
                "evidence_level": "verified_batch",
                "batch_run_id": run_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    for native, status in ((canonical, "passed"), (legacy, "failed")):
        run_dir = native / "commands" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": status,
                    "executed_steps": 1,
                    "passed": 1 if status == "passed" else 0,
                    "failed": 1 if status == "failed" else 0,
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )

    payload = build_five_pillars_data(workspace)
    pillars = {item["id"]: item for item in payload["pillars"]}

    assert payload["support"]["storage"]["status"] == "inconsistent"
    assert payload["support"]["storage"]["conflicted_run_ids"] == [run_id]
    assert payload["support"]["commands"]["latest"]["status"] == "passed"
    assert payload["program"]["verification_verdict"] == "evidence incomplete"
    assert pillars["managed"]["status"] == "attention"
    assert pillars["acceptance"]["status"] == "attention"


def test_dashboard_api_rejects_cross_site_post(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/chat"
    try:
        request = urllib.request.Request(
            url,
            data=b'{"message":"test"}',
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:  # pragma: no cover - defensive failure path
            raise AssertionError("cross-site POST was not rejected")
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_api_requires_json_content_type(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/api/chat"
    try:
        request = urllib.request.Request(
            url,
            data=b'{"message":"test"}',
            method="POST",
            headers={
                "Content-Type": "text/plain",
                "Origin": f"http://127.0.0.1:{port}",
            },
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 415
        else:  # pragma: no cover - defensive failure path
            raise AssertionError("text/plain POST was not rejected")
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_refuses_non_loopback_bind(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()

    import pytest

    with pytest.raises(ValueError, match="仅允许绑定"):
        _bind_dashboard_server("0.0.0.0", 0, workspace)


def test_dashboard_rejects_mission_id_path_traversal(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/mission?id=..%2F..%2Foutside",
            method="GET",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            payload = json.loads(exc.read().decode("utf-8-sig"))
            assert payload["ok"] is False
        else:  # pragma: no cover - defensive failure path
            raise AssertionError("mission_id traversal was not rejected")
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_profile_endpoint_round_trips(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PACER_PROFILE_PATH", str(tmp_path / "profile.json"))
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/profile",
            data=json.dumps({"email": "user@example.com", "display_name": "小鱼", "organization": "Pacer"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        saved = json.loads(urllib.request.urlopen(request, timeout=5).read().decode("utf-8-sig"))
        loaded = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/profile", timeout=5).read().decode("utf-8-sig"))
        assert saved["ok"] is True
        assert loaded["configured"] is True
        assert loaded["email"] == "user@example.com"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_commercial_config_endpoint_round_trips(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "commercial.json"
    monkeypatch.setenv("PACER_COMMERCIAL_CONFIG", str(config_path))
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        payload = {
            "supabase_url": "https://pacer.supabase.co",
            "supabase_anon_key": "anon-key",
            "supabase_service_role_key": "service-role",
            "google_oauth_configured": True,
            "google_client_id": "google-client",
            "google_client_secret": "google-secret",
            "stripe_publishable_key": "pk_test_123",
            "stripe_secret_key": "sk_test_123",
            "stripe_webhook_secret": "whsec_123",
            "stripe_price_id": "price_123",
            "stripe_customer_portal_url": "https://billing.stripe.com/p/session",
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/commercial-config",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        saved = json.loads(urllib.request.urlopen(request, timeout=5).read().decode("utf-8-sig"))
        loaded = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/commercial-config", timeout=5).read().decode("utf-8-sig"))

        assert saved["ok"] is True
        assert loaded["auth_configured"] is True
        assert loaded["billing_configured"] is True
        assert loaded["portal_configured"] is True
        assert loaded["stripe_secret_key"] == "****"
        assert "sk_test_123" in config_path.read_text(encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_events_stream_sends_snapshot_event(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/events", method="GET")
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.headers.get_content_type() == "text/event-stream"
            lines: list[str] = []
            for _ in range(6):
                line = response.readline().decode("utf-8").strip()
                if line:
                    lines.append(line)
                if any(item.startswith("data:") for item in lines):
                    break
            joined = "\n".join(lines)
            assert "event: snapshot" in joined
            assert "snapshot_changed" in joined
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_promotion_readiness_lists_user_required_items(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    (workspace / "missions").mkdir(parents=True)
    monkeypatch.setattr("visual_agent.dashboard.data.load_user_profile", lambda: LocalUserProfile())
    monkeypatch.setattr("visual_agent.dashboard.data.load_notification_config", lambda: None)
    monkeypatch.setattr("visual_agent.dashboard.data.load_commercial_config", lambda: CommercialConfig())
    monkeypatch.setattr("visual_agent.dashboard.data.load_workbench_model_config", lambda: WorkbenchModelConfig(api_key=""))

    data = build_dashboard_data(workspace)
    readiness = data["promotion_readiness"]

    assert readiness["score"] < 100
    assert readiness["checks"]
    assert any("邮箱" in item for item in readiness["user_required"])
    assert any(check["id"] == "relay" for check in readiness["checks"])
    assert any(check["id"] == "auth_supabase" for check in readiness["checks"])
    assert any("Stripe" in item for item in readiness["user_required"])
    usage_meter = next(check for check in readiness["checks"] if check["id"] == "stripe_usage_meter")
    assert usage_meter["status"] == "success"


def test_dashboard_promotion_readiness_marks_commercial_stack_configured(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    (workspace / "missions").mkdir(parents=True)
    monkeypatch.setattr(
        "visual_agent.dashboard.data.load_commercial_config",
        lambda: CommercialConfig(
            supabase_url="https://pacer.supabase.co",
            supabase_anon_key="anon-key",
            google_oauth_configured=True,
            google_client_id="google-client",
            stripe_publishable_key="pk_test_123",
            stripe_secret_key="sk_test_123",
            stripe_webhook_secret="whsec_123",
            stripe_price_id="price_123",
            stripe_customer_portal_url="https://billing.stripe.com/p/session",
        ),
    )

    data = build_dashboard_data(workspace)
    checks = {item["id"]: item for item in data["promotion_readiness"]["checks"]}

    assert checks["auth_supabase"]["status"] == "success"
    assert checks["google_oauth"]["status"] == "success"
    assert checks["stripe_billing"]["status"] == "success"
    assert checks["stripe_portal"]["status"] == "success"


def test_dashboard_core_readiness_focuses_on_product_loop(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    (workspace / "missions").mkdir(parents=True)
    monkeypatch.setattr(
        "visual_agent.dashboard.data.get_agents_cached",
        lambda: [{"agent": "codex", "installed": True}],
    )
    monkeypatch.setattr(
        "visual_agent.dashboard.data.worker_status",
        lambda _workspace_root: {"running": True, "pid": 1234},
    )

    data = build_dashboard_data(workspace)
    readiness = data["core_readiness"]
    checks = {item["id"]: item for item in readiness["checks"]}

    assert checks["agents"]["status"] == "success"
    assert checks["intake"]["status"] == "success"
    assert checks["worker"]["status"] == "success"
    assert "auth_supabase" not in checks
    assert "stripe_billing" not in checks
    assert readiness["score"] >= 50



def test_dashboard_chat_batch_shim_keeps_prompt_off_command_line(monkeypatch) -> None:
    captured = {}

    class Completed:
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr("shutil.which", lambda exe: r"C:\Tools\claude.cmd")

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")
        return Completed()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = run_chat({"agent": "claude-code", "message": 'test " & calc.exe & "'})

    assert result["ok"] is True
    assert captured["argv"] == [r"C:\Tools\claude.cmd", "-p"]
    assert "calc.exe" not in " ".join(captured["argv"])
    assert 'test " & calc.exe & "' in captured["input"]


def test_dashboard_data_reads_status_file(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    (tmp_path / ".visual-agent-status.md").write_text("## Status: PASSING\n", encoding="utf-8")

    data = build_dashboard_data(workspace)

    assert data["status"]["state"] == "PASSING"


def test_dashboard_work_traces_include_plan_saved_at_timestamp(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    saved_at = "2026-07-07T08:09:10+00:00"
    save_plan(
        {
            "objective": "留下带时间戳的计划痕迹",
            "status": "planned",
            "saved_at": saved_at,
        },
        workspace_root=workspace,
        plan_id="20260707-080910-plan",
    )

    data = build_dashboard_data(workspace)

    trace = next(item for item in data["work_traces"] if item["kind"] == "plan")
    assert trace["timestamp"] == saved_at
    assert "留下带时间戳的计划痕迹" in trace["title"]


def test_dashboard_mission_detail_includes_rounds_and_report(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    mission = {"mission_id": "m1", "objective": "fix checkout", "status": "verified", "stop_reason": "verified"}
    save_mission(workspace, mission)
    mdir = missions_dir(workspace) / "m1"
    (mdir / "rounds.jsonl").write_text(json.dumps({"round": 1, "type": "verification", "status": "pass"}) + "\n", encoding="utf-8")
    (mdir / "final_report.md").write_text("# Mission verified\n", encoding="utf-8")

    detail = build_mission_detail(workspace, "m1")

    assert detail["mission"]["objective"] == "fix checkout"
    assert len(detail["rounds"]) == 1
    assert "verified" in detail["final_report"]
    assert detail["live_logs"]["count"] == 0


def test_dashboard_mission_detail_includes_live_worker_log_tail(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix checkout",
        "status": "running",
        "stop_reason": "",
    }
    save_mission(workspace, mission)
    logs = workspace / "chief_plans" / "p1" / "logs"
    logs.mkdir(parents=True)
    (logs / "track-initial.log").write_text("first\nworker is editing\nlatest line\n", encoding="utf-8")

    detail = build_mission_detail(workspace, "m1")

    assert detail["live_logs"]["count"] == 1
    assert "latest line" in detail["live_logs"]["latest_tail"]
    assert detail["live_logs"]["latest_path"].endswith("track-initial.log")


def test_dashboard_mission_detail_includes_pacer_evidence(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix checkout",
        "status": "verified",
        "stop_reason": "verified",
    }
    save_mission(workspace, mission)
    log_dir = workspace / "chief_plans" / "p1" / "logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "track_1_mimo-initial.log"
    log_path.write_text("{\"stdout_tail\":\"mimo unified diff applied.\"}", encoding="utf-8")
    save_plan(
        {
            "plan_id": "p1",
            "objective": "fix checkout",
            "status": "needs_workflow_coverage",
            "verification_mode": "command",
        },
        workspace_root=workspace,
        plan_id="p1",
    )
    append_worker_record(
        workspace,
        "p1",
        {
            "schema_version": 1,
            "plan_id": "p1",
            "attempt": "initial",
            "track_id": "track_1_mimo",
            "agent": "mimo",
            "status": "completed",
            "exit_code": 0,
            "elapsed_seconds": 1.5,
            "cwd": str(tmp_path / "worktree"),
            "command": "low-cost-backend patch-worker",
            "log_path": str(log_path),
            "backend": {"name": "mimo", "model": "mimo-v2.5-pro"},
        },
    )
    save_verification(
        workspace,
        "p1",
        {
            "verdict": "pass",
            "command_verification": {"command": "cmd /c dir PACER_MIMO_SMOKE.md >nul"},
        },
    )

    detail = build_mission_detail(workspace, "m1")

    assert detail["worker_records"][0]["agent"] == "mimo"
    assert detail["verification"]["verdict"] == "pass"
    assert detail["pacer_evidence"]["worker_status"] == "completed"
    assert detail["pacer_evidence"]["backend"] == "mimo"
    assert detail["pacer_evidence"]["verification_command"] == "cmd /c dir PACER_MIMO_SMOKE.md >nul"
    assert detail["pacer_evidence"]["verification_mode"] == "command"
    assert "mimo unified diff applied" in detail["pacer_evidence"]["log_tail"]
    assert detail["progress"]["stage"] == "verified"
    assert detail["pacer_evidence"]["stage"] == "verified"


def test_dashboard_data_propagates_command_verification_mode(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix checkout",
        "status": "stopped",
        "stop_reason": "coverage_gap",
    }
    save_mission(workspace, mission)
    save_plan(
        {
            "plan_id": "p1",
            "objective": "fix checkout",
            "status": "needs_workflow_coverage",
            "verification_mode": "command",
        },
        workspace_root=workspace,
        plan_id="p1",
    )
    save_verification(
        workspace,
        "p1",
        {
            "verdict": "pass",
            "command_verification": {"command": "python -m pytest -q"},
        },
    )

    data = build_dashboard_data(workspace)

    mission_payload = data["missions"][0]
    assert mission_payload["verification_mode"] == "command"
    assert mission_payload["pacer_evidence"]["verification_mode"] == "command"


def test_dashboard_data_and_detail_propagate_activity_fields(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "run tests",
        "status": "running",
        "stop_reason": "",
    }
    save_mission(workspace, mission)
    started_at = datetime.now(timezone.utc).isoformat()
    save_mission_progress(
        workspace,
        "m1",
        stage="worker_running",
        activity="tests_running",
        activity_command="python -m pytest tests/test_dashboard.py -q",
        activity_started_at=started_at,
    )

    data = build_dashboard_data(workspace)
    mission_payload = data["missions"][0]
    detail = build_mission_detail(workspace, "m1")

    assert mission_payload["activity"] == "tests_running"
    assert mission_payload["activity_label"] == "Running tests"
    assert mission_payload["activity_command"] == "python -m pytest tests/test_dashboard.py -q"
    assert isinstance(mission_payload["activity_elapsed_seconds"], int)
    assert mission_payload["pacer_evidence"]["activity_label"] == "Running tests"
    assert detail["activity_label"] == "Running tests"
    assert detail["activity_command"] == "python -m pytest tests/test_dashboard.py -q"
    assert isinstance(detail["activity_elapsed_seconds"], int)
    assert detail["pacer_evidence"]["activity"] == "tests_running"


def test_start_worker_uses_cli_module_entrypoint(tmp_path, monkeypatch) -> None:
    captured = {}
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()

    class FakeProc:
        pid = 1234

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr("visual_agent.dashboard.api.hidden_subprocess_kwargs", lambda *, detached=False: {"creationflags": 12345})
    monkeypatch.setattr("subprocess.Popen", fake_popen)

    assert set_active_workspace(workspace)["ok"] is True
    result = start_worker(workspace)
    stop_worker()

    assert result["ok"] is True
    assert captured["cmd"][1:4] == ["-m", "visual_agent.cli", "mission"]
    assert captured["kwargs"]["creationflags"] == 12345


def test_workspace_switch_is_blocked_while_worker_owns_current_project(tmp_path, monkeypatch) -> None:
    workspace_a = tmp_path / "a" / ".agent-workspace"
    workspace_b = tmp_path / "b" / ".agent-workspace"
    workspace_a.mkdir(parents=True)
    workspace_b.mkdir(parents=True)

    class FakeProc:
        pid = 2345

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("visual_agent.dashboard.api.hidden_subprocess_kwargs", lambda *, detached=False: {})
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: FakeProc())

    assert set_active_workspace(workspace_a)["ok"] is True
    assert start_worker(workspace_a)["ok"] is True
    try:
        result = set_active_workspace(workspace_b)
        assert result["ok"] is False
        assert result["worker_workspace"] == str(workspace_a.resolve())
    finally:
        stop_worker()


def test_model_config_endpoint_redacts_and_preserves_existing_key(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    saved = save_dashboard_model_config(
        {
            "base_url": "http://127.0.0.1:8788/v1",
            "api_key": "sk-dashboard-secret",
            "model": "gpt-test",
        }
    )
    assert saved["ok"] is True

    loaded = get_model_config()
    assert loaded["api_key"] == "****"
    assert loaded["api_key_configured"] is True
    assert "sk-dashboard-secret" not in json.dumps(loaded)

    updated = save_dashboard_model_config(
        {
            "base_url": "http://127.0.0.1:8788/v1",
            "api_key": "****",
            "model": "gpt-test-2",
        }
    )
    assert updated["ok"] is True
    config_text = (tmp_path / "model_api_keys.txt").read_text(encoding="utf-8")
    assert "api_key=sk-dashboard-secret" in config_text
    assert "model=gpt-test-2" in config_text


def test_retry_mission_reuses_original_repo_and_becomes_idempotent(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    repo = tmp_path / "external-repo"
    workspace.mkdir()
    repo.mkdir()
    save_mission(
        workspace,
        {
            "mission_id": "retry-1",
            "objective": "Fix checkout",
            "repo_root": str(repo),
            "status": "failed",
            "stop_reason": "verification_failed",
            "test_command": "npm test",
            "agent": "codex",
            "merge_policy": "auto",
            "budget_policy": default_budget_policy(),
        },
    )
    captured = {}

    def fake_start(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "launch_id": "launch-retry-1"}

    monkeypatch.setattr("visual_agent.dashboard.api.start_workbench_mission", fake_start)

    first = retry_mission(workspace, "retry-1")
    second = retry_mission(workspace, "retry-1")

    assert first["ok"] is True
    assert second["ok"] is False
    assert captured["repo_root"] == str(repo)
    assert captured["test_command"] == "npm test"
    assert captured["merge_policy"] == "auto"
    assert load_mission(workspace, "retry-1")["status"] == "retried"


def test_dashboard_html_is_self_contained() -> None:
    # No external script/style URLs — the page must work offline/local-only.
    assert "<title>Pacer 工作台</title>" in DASHBOARD_HTML
    assert "http://" not in DASHBOARD_HTML.split("<script>")[0]


def test_dashboard_inline_js_has_valid_syntax(tmp_path) -> None:
    # The page JS lives in a Python string, where one swallowed backslash (e.g.
    # a \\ regex becoming \) produces a SyntaxError that kills every button on
    # the page while all API tests stay green. Parse it for real.
    import re
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    match = re.search(r"<script>(.*)</script>", DASHBOARD_HTML, re.S)
    assert match, "inline <script> block missing"
    js_file = tmp_path / "inline.js"
    js_file.write_text(match.group(1), encoding="utf-8")
    completed = subprocess.run([node, "--check", str(js_file)], capture_output=True, text=True)
    assert completed.returncode == 0, f"页面内联 JS 语法错误（会导致工作台所有按钮失灵）:\n{completed.stderr}"


def test_dashboard_html_has_mimo_efficiency_display() -> None:
    # The HTML must display low-cost backend efficiency metrics with Chinese labels.
    assert "低成本后端效率" in DASHBOARD_HTML
    assert "套餐额度" in DASHBOARD_HTML
    assert "订阅额度" in DASHBOARD_HTML
    assert "中转站" in DASHBOARD_HTML
    assert "流式工作任务" in DASHBOARD_HTML
    assert "自己的付费模型网关" in DASHBOARD_HTML
    assert "时间节省" in DASHBOARD_HTML
    assert "综合效率" in DASHBOARD_HTML
    assert "rSavedUsd" in DASHBOARD_HTML
    assert "rQuotaSave" in DASHBOARD_HTML
    assert "rSavedMin" in DASHBOARD_HTML
    assert "rEfficiency" in DASHBOARD_HTML
    assert "推广就绪" in DASHBOARD_HTML
    assert "邮箱身份" in DASHBOARD_HTML
    assert "月预算 USD" in DASHBOARD_HTML


def test_dashboard_html_has_professional_overview_strip() -> None:
    assert "工作台总览" in DASHBOARD_HTML
    assert "托管控制台" in DASHBOARD_HTML
    assert "功能导航" in DASHBOARD_HTML
    assert "开发入口" in DASHBOARD_HTML
    assert "后端节省" in DASHBOARD_HTML
    assert "pageMeta" in DASHBOARD_HTML
    assert "cockpit-layout" in DASHBOARD_HTML
    assert "ops-rail" in DASHBOARD_HTML
    assert 'data-workbench-view="relay-panel"' in DASHBOARD_HTML
    assert 'data-nav-view="mission-desk"' in DASHBOARD_HTML


def test_dashboard_app_js_exposes_execution_evidence_surface() -> None:
    app_js = Path(__file__).resolve().parents[1] / "src" / "visual_agent" / "dashboard" / "static" / "app.js"
    style_css = Path(__file__).resolve().parents[1] / "src" / "visual_agent" / "dashboard" / "static" / "style.css"
    text = app_js.read_text(encoding="utf-8")
    css = style_css.read_text(encoding="utf-8")
    assert "执行证据" in text
    assert "Worker 日志尾部" in text
    assert "verification_command" in text
    assert "pacer_evidence" in text
    assert "需求合同" in text
    assert "requirement_contract" in text
    assert "_requirementContractBlock" in text
    assert "_intakePolicyLabel" in text
    assert "focusWorkbenchPanel" in text
    assert "switchWorkbenchView" in text
    assert "收口方式" in text
    assert "/api/goal/refine" in text
    assert "目标还没收口" in text
    assert "先补验收方案" in text
    assert "saveRelayConfig" in text
    assert "renderSubscriptionQuotaPanel" in text
    assert "renderPromotionReadinessPanel" in text
    assert "saveProfileConfig" in text
    assert "打开中转站" in text
    assert "verification_mode" in text
    assert "commandMode && m.stop_reason==='coverage_gap'" in text
    assert "commandMode&&m.stop_reason==='coverage_gap'" in text
    assert "_activityBlock" in text
    assert "activity_elapsed_seconds" in text
    assert "activity-line${risk?' risk':''}" in text
    assert ".activity-line.risk" in css
    assert "dependency_install" in text
    assert "tests_running" in text


def test_dashboard_goal_intake_refines_goal_into_dialogue(tmp_path, monkeypatch) -> None:
    captured = {}

    def fake_refine_goal(goal, *, answers=None, enable_model=True):
        captured["goal"] = goal
        captured["answers"] = list(answers or [])
        captured["enable_model"] = enable_model
        return {
            "source": "model",
            "already_clear": False,
            "clarifying_questions": ["要兼容手机端吗？"],
            "suggested_goal": "把登录页改成深色主题，并保持手机端布局可用",
            "acceptance_hint": "打开登录页检查配色和移动端布局",
            "input_goal": goal,
        }

    def fake_dialogue_lines(payload, answers=None):
        return [
            "我还需要确认这些点：",
            "- 要兼容手机端吗？",
            "建议改写：把登录页改成深色主题，并保持手机端布局可用",
        ]

    monkeypatch.setattr("visual_agent.dashboard.api.refine_goal", fake_refine_goal)
    monkeypatch.setattr("visual_agent.dashboard.api.intake_dialogue_lines", fake_dialogue_lines)

    payload = refine_goal_intake({"goal": "把登录页改成暗色主题", "answers": ["保留蓝色按钮"]})

    assert captured["goal"] == "把登录页改成暗色主题"
    assert captured["answers"] == ["保留蓝色按钮"]
    assert captured["enable_model"] is True
    assert payload["ok"] is True
    assert payload["clarifying_questions"] == ["要兼容手机端吗？"]
    assert "建议改写" in "\n".join(payload["dialogue_lines"])


def test_dashboard_goal_intake_does_not_swap_selected_agent_for_auto_backend(monkeypatch) -> None:
    captured = {}

    def fake_resolve_agent_llm(agent):
        captured["agent"] = agent
        return None

    def fake_refine_goal(goal, *, answers=None, enable_model=True, **kwargs):
        captured["goal"] = goal
        captured["answers"] = list(answers or [])
        captured["enable_model"] = enable_model
        captured["kwargs"] = kwargs
        return {
            "source": "deterministic",
            "already_clear": False,
            "clarifying_questions": ["完成标准是什么？"],
            "suggested_goal": goal,
            "acceptance_hint": "",
            "input_goal": goal,
        }

    monkeypatch.setattr("visual_agent.dashboard.api._resolve_agent_llm", fake_resolve_agent_llm)
    monkeypatch.setattr("visual_agent.dashboard.api.refine_goal", fake_refine_goal)
    monkeypatch.setattr("visual_agent.dashboard.api.shutil.which", lambda _exe: None)

    payload = refine_goal_intake({"goal": "改一下登录页", "agent": "codex", "use_model": True})

    assert captured["agent"] == "codex"
    assert captured["enable_model"] is False
    assert captured["kwargs"] == {}
    assert payload["intake_policy"] == "selected_agent_unavailable"
    assert payload["model_unavailable"] is True
    assert "未切换到其他模型" in "\n".join(payload["dialogue_lines"])


def test_dashboard_goal_intake_uses_codex_read_only_cli(monkeypatch) -> None:
    captured = {}

    class Completed:
        returncode = 0
        stdout = json.dumps(
            {
                "clarifying_questions": ["完成后页面上要看到什么？"],
                "suggested_goal": "把登录页改成暗色主题，并保留蓝色提交按钮",
                "acceptance_hint": "打开登录页确认背景为暗色且提交按钮仍为蓝色",
            },
            ensure_ascii=False,
        )
        stderr = ""

    monkeypatch.setattr("visual_agent.dashboard.api._resolve_agent_llm", lambda _agent: None)
    monkeypatch.setattr("visual_agent.dashboard.api.shutil.which", lambda exe: r"C:\Tools\codex.cmd" if exe == "codex" else None)

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")
        return Completed()

    monkeypatch.setattr("visual_agent.dashboard.api.subprocess.run", fake_run)

    payload = refine_goal_intake(
        {
            "goal": "把登录页改成暗色主题",
            "agent": "codex",
            "repo_root": r"D:\Projects\app",
            "test_command": "pytest -q",
            "use_model": True,
        }
    )

    assert payload["ok"] is True
    assert payload["source"] == "selected_agent_cli"
    assert payload["intake_policy"] == "selected_agent_cli"
    assert payload["model_id"] == "codex:cli"
    assert "codex.cmd" in " ".join(captured["argv"])
    assert "read-only" in captured["argv"]
    assert captured["argv"][-1] == "-"
    assert "把登录页改成暗色主题" not in " ".join(captured["argv"])
    assert "把登录页改成暗色主题" in captured["input"]
    assert "Do not edit files" in captured["input"]
    assert "暗色主题" in payload["suggested_goal"]


def test_dashboard_html_surfaces_live_execution_logs() -> None:
    assert "实时执行日志" in DASHBOARD_HTML
    assert "live_logs" in DASHBOARD_HTML
