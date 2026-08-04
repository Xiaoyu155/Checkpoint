from __future__ import annotations

import json
from pathlib import Path

from visual_agent.chief_plans_store import (
    append_dispatch_record,
    append_worker_record,
    save_plan,
    save_verification,
)
from visual_agent.mission_journey import build_mission_journey, save_mission_journey
from visual_agent.missions import create_mission, default_budget_policy, save_mission


def test_verified_mission_binds_routing_memory_managed_and_acceptance(tmp_path: Path) -> None:
    workspace, mission_id = _seed_verified_mission(tmp_path, memory_injected=True)

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)
    phases = {item["id"]: item for item in journey["phases"]}

    assert journey["status"] == "verified_pending_delivery"
    assert journey["continuity_status"] == "connected_pending_delivery"
    assert journey["can_claim_verified"] is True
    assert journey["can_claim_delivered"] is False
    assert phases["routing"]["status"] == "passed"
    assert phases["memory"]["status"] == "passed"
    assert phases["managed"]["status"] == "passed"
    assert phases["acceptance"]["status"] == "passed"
    assert phases["delivery"]["status"] == "ready"

    saved = save_mission_journey(workspace, mission_id, journey)
    assert Path(saved["path"]).is_file()


def test_executed_mission_blocks_when_selected_memory_never_reaches_worker(tmp_path: Path) -> None:
    workspace, mission_id = _seed_verified_mission(tmp_path, memory_injected=False)

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)
    phases = {item["id"]: item for item in journey["phases"]}

    assert journey["status"] == "blocked"
    assert journey["continuity_status"] == "broken"
    assert journey["can_claim_verified"] is False
    assert phases["memory"]["status"] == "blocked"
    assert "memory_dispatch_chain_broken" in journey["reason_codes"]


def test_unclean_worker_does_not_override_verified_acceptance(tmp_path: Path) -> None:
    workspace, mission_id = _seed_verified_mission(
        tmp_path,
        memory_injected=True,
        worker_status="failed",
    )

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)
    managed = next(item for item in journey["phases"] if item["id"] == "managed")

    assert journey["status"] == "verified_pending_delivery"
    assert journey["can_claim_verified"] is True
    assert managed["status"] == "passed"
    assert managed["reason_codes"] == ["managed_worker_unclean_but_accepted"]
    assert "强验收已接管最终结论" in managed["summary"]


def test_verified_mission_surfaces_budget_exhaustion_without_rewriting_acceptance(
    tmp_path: Path,
) -> None:
    workspace, mission_id = _seed_verified_mission(
        tmp_path,
        memory_injected=True,
        budget_status="exhausted",
    )

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)
    managed = next(item for item in journey["phases"] if item["id"] == "managed")

    assert journey["can_claim_verified"] is True
    assert managed["status"] == "passed"
    assert managed["details"]["budget_status"] == "exhausted"
    assert "managed_budget_exhausted_after_completion" in managed["reason_codes"]
    assert "budget=exhausted" in managed["summary"]


def test_routing_request_mismatch_breaks_the_mission_chain(tmp_path: Path) -> None:
    workspace, mission_id = _seed_verified_mission(tmp_path, memory_injected=True)

    mission = json.loads((workspace / "missions" / mission_id / "mission.json").read_text(encoding="utf-8"))
    append_dispatch_record(
        workspace,
        mission["plan_id"],
        {
            "mission_id": mission_id,
            "resolved_provider": "unexpected-relay",
            "resolved_model": "gpt-test",
            "worker_attempts": 1,
            "project_memory_usage": {
                "memory_mode": "enabled",
                "selected_entries": 1,
                "injected_memory_ids": ["mission:prior"],
                "dispatch_injected": True,
                "dispatch_memory_ids": ["mission:prior"],
            },
            "managed_runtime": {
                "routing_evidence": {
                    "request": {"provider": "expected-relay", "model": "gpt-test"}
                }
            },
            "verdict": "pass",
            "status": "verified",
        },
    )

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)
    routing = next(item for item in journey["phases"] if item["id"] == "routing")

    assert routing["status"] == "blocked"
    assert routing["reason_codes"] == ["routing_request_mismatch"]
    assert journey["can_claim_verified"] is False


def test_missing_routing_identity_is_reported_as_an_evidence_gap(tmp_path: Path) -> None:
    workspace, mission_id = _seed_verified_mission(
        tmp_path,
        memory_injected=True,
        merge_status="merged",
        resolved_provider="",
    )

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)
    routing = next(item for item in journey["phases"] if item["id"] == "routing")

    # Acceptance is independent of routing evidence, so the mission is still
    # verified — but an unproven routing chain must not read as passed, and
    # Pacer must not claim the result was delivered on that basis.
    assert routing["status"] == "incomplete"
    assert routing["reason_codes"] == ["routing_identity_missing"]
    assert journey["can_claim_verified"] is True
    assert journey["can_claim_delivered"] is False
    assert "证据不完整" in journey["summary"]


def test_acceptance_reports_execution_not_the_workflow_run_profile(tmp_path: Path) -> None:
    workspace, mission_id = _seed_verified_mission(tmp_path, memory_injected=True)

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)
    acceptance = next(item for item in journey["phases"] if item["id"] == "acceptance")

    # A test command that really ran must not be labelled `dry-run`.
    assert acceptance["details"]["executed"] is True
    assert acceptance["details"]["exit_code"] == 0
    assert "run_profile" not in acceptance["details"]


def test_non_discriminating_gate_cannot_claim_the_objective_was_met(tmp_path: Path) -> None:
    workspace, mission_id = _seed_verified_mission(
        tmp_path,
        memory_injected=True,
        acceptance_grade={
            "tier": "regression_clear",
            "reason_code": "acceptance_gate_not_discriminating",
            "message": "验收命令在改动前就已经通过。",
            "discriminating": False,
        },
    )

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)
    acceptance = next(item for item in journey["phases"] if item["id"] == "acceptance")

    assert acceptance["status"] == "incomplete"
    assert acceptance["details"]["acceptance_tier"] == "regression_clear"
    assert acceptance["details"]["gate_discriminating"] is False
    assert journey["can_claim_verified"] is False
    assert journey["status"] == "regression_clear"
    assert "证明不了" in journey["next_action"] or "没弄坏" in journey["next_action"]
    # A weak gate is weak evidence, not a broken chain.
    assert not any(item["status"] == "broken" for item in journey["links"])


def test_discriminating_gate_still_claims_verified(tmp_path: Path) -> None:
    workspace, mission_id = _seed_verified_mission(
        tmp_path,
        memory_injected=True,
        acceptance_grade={
            "tier": "verified",
            "reason_code": "acceptance_gate_discriminating",
            "message": "验收命令在改动前是失败的。",
            "discriminating": True,
        },
    )

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)
    acceptance = next(item for item in journey["phases"] if item["id"] == "acceptance")

    assert acceptance["status"] == "passed"
    assert journey["can_claim_verified"] is True


def test_records_without_grading_keep_their_historical_reading(tmp_path: Path) -> None:
    workspace, mission_id = _seed_verified_mission(tmp_path, memory_injected=True)

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)
    acceptance = next(item for item in journey["phases"] if item["id"] == "acceptance")

    # Missions recorded before grading existed are not retroactively downgraded.
    assert acceptance["status"] == "passed"
    assert acceptance["details"]["acceptance_tier"] == ""
    assert journey["can_claim_verified"] is True


def test_preview_is_in_progress_instead_of_a_broken_chain(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    saved = save_plan(
        {
            "objective": "Prepare greeting change",
            "status": "ready",
            "worker_tracks": [{"id": "track_1_codex", "agent": "codex", "track_kind": "implementation"}],
            "project_memory": {
                "usage": {
                    "memory_mode": "enabled",
                    "selected_entries": 1,
                    "injected_memory_ids": ["mission:prior"],
                }
            },
        },
        workspace_root=workspace,
    )
    mission = create_mission(
        workspace_root=workspace,
        objective="Prepare greeting change",
        repo_root=repo,
        plan_id=saved["plan_id"],
        budget_policy=default_budget_policy(),
        status="preview",
    )

    journey = build_mission_journey(
        workspace_root=workspace,
        mission_id=mission["mission_id"],
    )

    assert journey["status"] == "in_progress"
    assert journey["continuity_status"] == "in_progress"
    assert not any(item["status"] == "broken" for item in journey["links"])


def test_running_background_worker_is_reported_active_before_worker_record(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    saved = save_plan(
        {
            "objective": "Prepare parser change",
            "status": "ready",
            "worker_tracks": [{"id": "track_1_codex", "agent": "codex", "track_kind": "implementation"}],
        },
        workspace_root=workspace,
    )
    mission = create_mission(
        workspace_root=workspace,
        objective="Prepare parser change",
        repo_root=repo,
        plan_id=saved["plan_id"],
        budget_policy=default_budget_policy(),
        status="running",
    )

    journey = build_mission_journey(
        workspace_root=workspace,
        mission_id=mission["mission_id"],
        progress={
            "stage": "worker_running",
            "activity": "worker_executing",
            "background_alive": True,
        },
    )
    managed = next(item for item in journey["phases"] if item["id"] == "managed")

    assert managed["status"] == "active"
    assert "worker=running" in managed["summary"]


def test_merged_mission_is_a_completed_product_journey(tmp_path: Path) -> None:
    workspace, mission_id = _seed_verified_mission(
        tmp_path,
        memory_injected=True,
        merge_status="merged",
    )

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)

    assert journey["status"] == "completed"
    assert journey["can_claim_delivered"] is True


def test_verified_mission_explains_blocked_delivery_next_action(tmp_path: Path) -> None:
    workspace, mission_id = _seed_verified_mission(
        tmp_path,
        memory_injected=True,
        merge_status="skipped",
    )

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)
    delivery = next(item for item in journey["phases"] if item["id"] == "delivery")

    assert journey["status"] == "verified_pending_delivery"
    assert journey["can_claim_verified"] is True
    assert journey["can_claim_delivered"] is False
    assert delivery["status"] == "blocked"
    assert "交付被阻塞" in journey["next_action"]


def test_pacer_self_change_stays_pending_until_strict_dogfood_is_bound(tmp_path: Path) -> None:
    workspace, mission_id = _seed_verified_mission(tmp_path, memory_injected=True)
    mission_path = workspace / "missions" / mission_id / "mission.json"

    mission = json.loads(mission_path.read_text(encoding="utf-8"))
    repo = Path(mission["repo_root"])
    (repo / "src" / "visual_agent").mkdir(parents=True)
    (repo / ".pacer").mkdir()
    (repo / ".pacer" / "dogfood.json").write_text("{}\n", encoding="utf-8")

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)
    delivery = next(item for item in journey["phases"] if item["id"] == "delivery")

    assert journey["status"] == "verified_pending_dogfood"
    assert journey["can_claim_verified"] is True
    assert journey["can_claim_delivered"] is False
    assert delivery["status"] == "partial"
    assert delivery["details"]["pacer_repo"] is True


def _seed_verified_mission(
    tmp_path: Path,
    *,
    memory_injected: bool,
    merge_status: str = "",
    worker_status: str = "completed",
    budget_status: str = "within_budget",
    resolved_provider: str = "openai",
    acceptance_grade: dict | None = None,
) -> tuple[Path, str]:
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    memory_id = "mission:prior"
    saved = save_plan(
        {
            "objective": "Fix greeting",
            "status": "ready",
            "worker_tracks": [{"id": "track_1_codex", "agent": "codex", "track_kind": "implementation"}],
        },
        workspace_root=workspace,
    )
    mission = create_mission(
        workspace_root=workspace,
        objective="Fix greeting",
        repo_root=repo,
        plan_id=saved["plan_id"],
        budget_policy=default_budget_policy(),
        status="created",
        merge=bool(merge_status),
    )
    mission["status"] = "verified"
    mission["stop_reason"] = "verified"
    save_mission(workspace, mission)
    append_worker_record(
        workspace,
        saved["plan_id"],
        {
            "agent": "codex",
            "status": worker_status,
            "exit_code": 0 if worker_status == "completed" else 1,
            "resolved_provider": resolved_provider,
            "resolved_model": "gpt-test",
            "cwd": str(repo),
        },
    )
    usage = {
        "memory_mode": "enabled",
        "selected_entries": 1,
        "injected_memory_ids": [memory_id],
        "dispatch_injected": memory_injected,
        "dispatch_memory_ids": [memory_id] if memory_injected else [],
    }
    append_dispatch_record(
        workspace,
        saved["plan_id"],
        {
            "mission_id": mission["mission_id"],
            "resolved_provider": resolved_provider,
            "resolved_model": "gpt-test",
            "worker_attempts": 1,
            "project_memory_usage": usage,
            "managed_runtime": {
                "idempotency_key": "managed:test",
                "budget_status": budget_status,
            },
            "merge": {"status": merge_status, "commit": "abc123"} if merge_status else {},
            "verdict": "pass",
            "status": "verified",
        },
    )
    save_verification(
        workspace,
        saved["plan_id"],
        {
            "plan_id": saved["plan_id"],
            "verdict": "pass",
            "command_verification": {
                "verdict": "pass",
                "command": "python -m pytest -q",
                "exit_code": 0,
            },
            **({"acceptance": acceptance_grade} if acceptance_grade else {}),
        },
    )
    return workspace, mission["mission_id"]


def test_stopped_mission_whose_gate_passed_is_read_as_verified(tmp_path: Path) -> None:
    workspace, mission_id = _seed_verified_mission(tmp_path, memory_injected=True)
    mission_path = workspace / "missions" / mission_id / "mission.json"

    # The worker died on a trailing rate limit after the change already passed
    # acceptance, so the mission carries a stopped label over a proven result.
    mission = json.loads(mission_path.read_text(encoding="utf-8"))
    mission["status"] = "stopped"
    mission["stop_reason"] = "quota_exhausted"
    mission_path.write_text(json.dumps(mission, ensure_ascii=False), encoding="utf-8")

    journey = build_mission_journey(workspace_root=workspace, mission_id=mission_id)
    managed = next(item for item in journey["phases"] if item["id"] == "managed")

    assert managed["status"] == "passed"
    assert journey["can_claim_verified"] is True
    assert journey["status"] == "verified_pending_delivery"
