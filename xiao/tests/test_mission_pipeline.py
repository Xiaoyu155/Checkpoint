from __future__ import annotations

import json

import pytest

from visual_agent.managed_state import RevisionConflict
from visual_agent.mission_pipeline import (
    MissionPipeline,
    SpecValidationError,
    SpecValidator,
    write_mission_state,
)


def test_spec_validator_requires_scope_plan_test():
    validator = SpecValidator()

    with pytest.raises(SpecValidationError) as excinfo:
        validator.validate({"scope": ["src"], "plan": ["edit"], "test": ["pytest -q"], "risk": ["low"]})

    assert excinfo.value.field == "spec.rollback"
    assert excinfo.value.to_response()["error_code"] == "spec_validation_failed"


def test_mission_pipeline_writes_state_json(tmp_path):
    spec = SpecValidator().validate(
        {
            "scope": ["src/visual_agent"],
            "plan": ["Wrap workbench launch"],
            "test": ["python -m pytest tests/test_mission_pipeline.py -q"],
            "risk": ["Low-risk wrapper change."],
            "rollback": ["Revert wrapper change."],
        }
    )
    pipeline = MissionPipeline(tmp_path / ".agent-workspace", launch_id="launch-1")

    state = pipeline.begin(spec=spec, execute=True, request={"goal": "Wrap workbench launch"})
    pipeline.attach_mission(state, "mission-1")
    pipeline.transition(state, "EXECUTING", "worker_started")
    pipeline.transition(state, "VERIFYING", "verification_started")
    pipeline.transition(
        state,
        "VERIFIED",
        "test_complete",
        status="verified",
        stop_reason="verified",
        managed_runtime={
            "budget_status": "within_budget",
            "budget": {"status": "within_budget"},
            "routing_evidence": {"policy_match": True, "decision_id": "route-1"},
            "retry": {"retry": False, "status": "not_needed"},
        },
    )

    launch_payload = json.loads(pipeline.path.read_text(encoding="utf-8"))
    mission_payload = json.loads((tmp_path / ".agent-workspace" / "missions" / "mission-1" / "state.json").read_text(encoding="utf-8"))
    assert launch_payload["current_state"] == "VERIFIED"
    assert mission_payload["mission_id"] == "mission-1"
    assert mission_payload["context"]["spec"]["plan"] == ["Wrap workbench launch"]
    assert mission_payload["managed"]["state"] == "SUCCEEDED"
    assert mission_payload["managed"]["terminal"] is True
    assert mission_payload["reliability"]["budget_status"] == "within_budget"
    assert mission_payload["reliability"]["routing_evidence"]["decision_id"] == "route-1"


def test_pipeline_rejects_direct_verified_and_stale_revision(tmp_path) -> None:
    spec = SpecValidator().derive_request_spec(
        goal="Fix checkout",
        repo_root=tmp_path,
        test_command="python -m pytest -q",
        agent="codex",
        execute=True,
    )
    pipeline = MissionPipeline(tmp_path / ".agent-workspace", launch_id="launch-cas")
    state = pipeline.begin(spec=spec, execute=True, request={"goal": "Fix checkout"})

    with pytest.raises(ValueError, match="invalid pipeline transition"):
        pipeline.transition(
            state,
            "VERIFIED",
            "untrusted_shortcut",
            status="verified",
            stop_reason="verified",
        )

    pipeline.transition(state, "EXECUTING", "worker_started")
    with pytest.raises(RevisionConflict):
        pipeline.transition(
            state,
            "VERIFYING",
            "stale_verification",
            expected_revision=0,
        )


def test_pipeline_idempotency_key_is_stable_across_launches(tmp_path) -> None:
    spec = SpecValidator().derive_request_spec(
        goal="Fix checkout",
        repo_root=tmp_path,
        test_command="python -m pytest -q",
        agent="codex",
        execute=True,
    )
    request = {"goal": "Fix checkout", "repo_root": str(tmp_path)}
    first = MissionPipeline(tmp_path / ".agent-workspace", launch_id="launch-a").begin(
        spec=spec,
        execute=True,
        request=request,
    )
    second = MissionPipeline(tmp_path / ".agent-workspace", launch_id="launch-b").begin(
        spec=spec,
        execute=True,
        request=request,
    )

    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["managed"]["idempotency_key"] == second["managed"]["idempotency_key"]


@pytest.mark.parametrize(
    ("stop_reason", "managed_state"),
    [
        ("command_timeout", "FAILED"),
        ("provider_5xx", "FAILED"),
        ("worker_error", "CRASHED"),
        ("evidence_rejected", "FAILED"),
    ],
)
def test_standalone_mission_failures_never_become_success(
    tmp_path,
    stop_reason: str,
    managed_state: str,
) -> None:
    workspace = tmp_path / ".agent-workspace"
    write_mission_state(
        workspace,
        "mission-failure",
        current_state="DRAFT",
        event="chief_run_mission_created",
        goal="Fix checkout",
        plan_id="plan-1",
    )
    result = write_mission_state(
        workspace,
        "mission-failure",
        current_state="BLOCKED",
        event="chief_run_finished",
        status="stopped",
        stop_reason=stop_reason,
        managed_runtime={
            "budget_status": "exhausted" if stop_reason == "command_timeout" else "within_budget",
            "budget": {
                "status": "exhausted" if stop_reason == "command_timeout" else "within_budget"
            },
            "routing_evidence": {"decision_id": "route-1", "policy_match": True},
            "retry": {"retry": stop_reason in {"command_timeout", "provider_5xx"}},
        },
    )

    assert result["managed"]["state"] == managed_state
    assert result["managed"]["terminal"] is True
    assert result["current_state"] == "BLOCKED"
    assert result["reliability"]["routing_evidence"]["decision_id"] == "route-1"


def test_terminal_mission_state_allows_idempotent_same_state_update(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    first = write_mission_state(
        workspace,
        "mission-blocked",
        current_state="BLOCKED",
        event="chief_run_finished",
        status="stopped",
        stop_reason="worker_error",
    )
    second = write_mission_state(
        workspace,
        "mission-blocked",
        current_state="BLOCKED",
        event="chief_run_finished",
        status="stopped",
        stop_reason="worker_error",
        managed_runtime={
            "budget_status": "within_budget",
            "routing_evidence": {"decision_id": "route-1", "policy_match": True},
        },
    )

    assert second["current_state"] == "BLOCKED"
    assert second["revision"] == first["revision"] + 1
    assert second["reliability"]["routing_evidence"]["decision_id"] == "route-1"
    assert len(second["context"]["history"]) == 2


def test_terminal_mission_state_rejects_different_terminal_update(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    write_mission_state(
        workspace,
        "mission-blocked",
        current_state="BLOCKED",
        event="chief_run_finished",
        status="stopped",
        stop_reason="worker_error",
    )

    with pytest.raises(ValueError, match="terminal pipeline state is immutable"):
        write_mission_state(
            workspace,
            "mission-blocked",
            current_state="VERIFIED",
            event="chief_run_finished",
            status="verified",
            stop_reason="verified",
        )


def test_pipeline_retry_requires_whitelist_and_starts_new_attempt(tmp_path) -> None:
    spec = SpecValidator().derive_request_spec(
        goal="Fix checkout",
        repo_root=tmp_path,
        test_command="python -m pytest -q",
        agent="codex",
        execute=True,
    )
    pipeline = MissionPipeline(tmp_path / ".agent-workspace", launch_id="launch-retry")
    state = pipeline.begin(spec=spec, execute=True, request={"goal": "Fix checkout"})
    pipeline.transition(state, "EXECUTING", "worker_started", attempt_id="attempt-1")
    pipeline.transition(state, "VERIFYING", "verification_started", attempt_id="attempt-1")

    with pytest.raises(ValueError, match="retry whitelist"):
        pipeline.transition(
            state,
            "REPAIRING",
            "repair_requested",
            managed_runtime={
                "retry": {
                    "retry": True,
                    "status": "scheduled",
                    "failure_kind": "evidence_rejected",
                    "scheduled_at": "2026-07-15T12:00:05+00:00",
                }
            },
        )

    pipeline.transition(
        state,
        "REPAIRING",
        "repair_requested",
        managed_runtime={
            "retry": {
                "retry": True,
                "status": "scheduled",
                "failure_kind": "provider_5xx",
                "delay_seconds": 1.25,
                "scheduled_at": "2026-07-15T12:00:05+00:00",
            }
        },
    )
    assert state["managed"]["state"] == "RETRY_WAIT"
    pipeline.transition(state, "EXECUTING", "retry_started", attempt_id="attempt-2")
    assert state["managed"]["state"] == "RUNNING"
    assert state["managed"]["attempt_id"] == "attempt-2"
