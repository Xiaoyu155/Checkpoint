from __future__ import annotations

import json
from pathlib import Path

from visual_agent.verification_status import (
    STATUS_SCHEMA_VERSION,
    enrich_verification_payload,
    normalize_verification_status,
    status_file_payload,
    write_verification_status,
)


def test_enrich_verification_payload_adds_report_paths_and_next_action(tmp_path) -> None:
    payload = enrich_verification_payload(
        {
            "result": "pass",
            "workflow_name": "login_verification",
            "workflow_path": str(tmp_path / "workflows" / "login.yaml"),
            "run_id": "run-123",
            "quality_score": 0.82,
            "quality": {"score": 0.82, "gaps": [], "recommendation": "Workflow quality is good."},
            "inputs_path": str(tmp_path / "inputs" / "login_inputs.json"),
            "inputs_source": "generated_template",
            "message": "ok",
        },
        workspace_root=tmp_path,
    )

    status = normalize_verification_status(payload)

    assert payload["schema_version"] == STATUS_SCHEMA_VERSION
    assert Path(payload["report_path"]).parts[-2:] == ("reports", "run-123.json")
    assert Path(payload["report_markdown_path"]).parts[-2:] == ("reports", "run-123.md")
    assert "get_run_report" in payload["report_hint"]
    assert status.result == "pass"
    assert status.next_action.startswith("Implementation verified")
    assert status.quality is not None
    assert status.quality.score == 0.82
    assert status.inputs_source == "generated_template"
    assert status.inputs_path and status.inputs_path.endswith("login_inputs.json")
    assert status.report_hint and "run-123" in status.report_hint


def test_normalize_verification_status_preserves_failed_step_fix_hint() -> None:
    status = normalize_verification_status(
        {
            "schema_version": 1,
            "result": "fail",
            "workflow_name": "login_verification",
            "quality_score": 0.7,
            "failed_step": {
                "id": "assert_success",
                "action": "assert_text",
                "expected": "Dashboard",
                "actual": "Login failed",
                "fix_hint": "Render Dashboard after login.",
            },
        }
    )

    assert status.result == "fail"
    assert status.failed_step is not None
    assert status.failed_step.id == "assert_success"
    assert status.next_action == "Render Dashboard after login."


def test_next_action_for_assert_text_fail_is_actionable() -> None:
    status = normalize_verification_status(
        {
            "schema_version": 1,
            "result": "fail",
            "workflow_name": "profile_verification",
            "quality_score": 0.7,
            "failed_step": {
                "id": "assert_success",
                "action": "assert_text",
                "expected": "Profile saved successfully",
                "actual": "保存成功 操作完成",
            },
        }
    )

    assert "Profile saved successfully" in status.next_action
    assert "当前页面" in status.next_action
    assert "repair-workflow" in status.next_action


def test_next_action_for_needs_improvement_includes_score_threshold_and_gaps() -> None:
    status = normalize_verification_status(
        {
            "schema_version": 1,
            "result": "needs_workflow_improvement",
            "workflow_name": "weak_verification",
            "quality_score": 0.42,
            "min_quality_score": 0.6,
            "quality": {
                "score": 0.42,
                "gaps": ["no success state assertion"],
                "recommendation": "Add wait_for_text after submit.",
            },
        }
    )

    assert "0.42" in status.next_action
    assert "0.6" in status.next_action
    assert "no success state assertion" in status.next_action
    assert "Add wait_for_text" in status.next_action


def test_status_file_payload_is_compact_and_includes_quality_next_action(tmp_path) -> None:
    payload = enrich_verification_payload(
        {
            "result": "needs_workflow_improvement",
            "workflow_name": "weak_verification",
            "workflow_path": str(tmp_path / "workflows" / "weak.yaml"),
            "run_id": None,
            "quality_score": 0.5,
            "quality": {
                "score": 0.5,
                "gaps": ["no success state assertion"],
                "data_display_assertions": 1,
                "forbidden_error_assertions": 1,
                "text_from_input_references": 1,
                "invalid_text_from_references": ["input.timezone"],
                "recommendation": "Add assert_text after submit.",
            },
            "semantic_summary": {
                "framework": "html",
                "confidence": 0.8,
                "generation_method": "static",
                "field_count": 1,
                "required_field_count": 1,
                "sensitive_field_count": 0,
                "validation_rule_count": 2,
                "submit_action_count": 1,
                "success_state_count": 0,
                "error_state_count": 0,
                "data_display_count": 1,
                "negative_input_case_count": 2,
                "data_displays": ["profile.displayName"],
                "matched_data_displays": ["profile.displayName"],
                "unmatched_data_displays": ["profile.unused"],
                "warnings": ["no success state found"],
            },
            "inputs_path": str(tmp_path / "inputs" / "weak_inputs.json"),
            "inputs_source": "generated_template",
            "negative_verification": {
                "requested": True,
                "status": "skipped",
                "reason": "no_negative_oracle",
                "workflow_name": "weak_verification_negative_draft",
                "workflow_path": str(tmp_path / "workflows" / "weak_negative_draft.yaml"),
                "reset_strategy": "fresh_observe_per_case",
                "oracles": [],
                "next_action": "Add or expose parsed validation error text before treating negative verification as executable.",
                "debug_blob": "drop me",
            },
            "generation_trace": ["field email -> paste input.email"],
            "message": "weak",
        },
        workspace_root=tmp_path,
    )
    compact = status_file_payload({**payload, "updated_at": 123.0})
    status = normalize_verification_status(compact)

    assert compact["schema_version"] == 1
    assert compact["result"] == "needs_workflow_improvement"
    assert compact["quality"]["gaps"] == ["no success state assertion"]
    assert status.quality is not None
    assert status.quality.data_display_assertions == 1
    assert status.quality.forbidden_error_assertions == 1
    assert status.quality.text_from_input_references == 1
    assert status.quality.invalid_text_from_references == ("input.timezone",)
    assert compact["inputs_source"] == "generated_template"
    assert compact["inputs_path"].endswith("weak_inputs.json")
    assert "report_hint" in compact
    assert compact["negative_verification"]["status"] == "skipped"
    assert compact["negative_verification"]["reason"] == "no_negative_oracle"
    assert compact["negative_verification"]["reset_strategy"] == "fresh_observe_per_case"
    assert "debug_blob" not in compact["negative_verification"]
    assert status.negative_verification is not None
    assert status.negative_verification.requested is True
    assert status.negative_verification.status == "skipped"
    assert status.negative_verification.reason == "no_negative_oracle"
    assert status.negative_verification.reset_strategy == "fresh_observe_per_case"
    assert status.negative_verification.oracles == ()
    assert compact["semantic_summary"]["field_count"] == 1
    assert status.semantic_summary is not None
    assert status.semantic_summary.framework == "html"
    assert status.semantic_summary.required_field_count == 1
    assert status.semantic_summary.validation_rule_count == 2
    assert status.semantic_summary.data_display_count == 1
    assert status.semantic_summary.negative_input_case_count == 2
    assert status.semantic_summary.data_displays == ("profile.displayName",)
    assert status.semantic_summary.matched_data_displays == ("profile.displayName",)
    assert status.semantic_summary.unmatched_data_displays == ("profile.unused",)
    assert status.semantic_summary.warnings == ("no success state found",)
    assert status.generation_trace == ("field email -> paste input.email",)
    assert compact["generation_trace"] == ["field email -> paste input.email"]
    assert "0.5" in compact["next_action"]
    assert "no success state assertion" in compact["next_action"]
    assert "Add assert_text after submit." in compact["next_action"]
    assert "workspace" not in compact


def test_normalize_verification_status_preserves_negative_verification_pass(tmp_path) -> None:
    payload = enrich_verification_payload(
        {
            "result": "pass",
            "workflow_name": "signup_verification",
            "workflow_path": str(tmp_path / "workflows" / "signup.yaml"),
            "run_id": "run-main",
            "quality_score": 0.9,
            "quality": {"score": 0.9, "gaps": [], "recommendation": "Workflow quality is good."},
            "negative_verification": {
                "requested": True,
                "status": "pass",
                "reason": "ready",
                "workflow_name": "signup_verification_negative_draft",
                "workflow_path": str(tmp_path / "workflows" / "signup_negative_draft.yaml"),
                "run_id": "run-negative",
                "run_profile": "fast",
                "reset_strategy": "fresh_observe_per_case",
                "oracles": [{"text": "Invalid email", "source": "html:text"}],
                "report_path": str(tmp_path / "reports" / "run-negative.json"),
                "report_markdown_path": str(tmp_path / "reports" / "run-negative.md"),
                "report_hint": "Use get_run_report with run_id='run-negative' for full details.",
                "next_action": "Negative verification passed.",
                "steps_passed": 3,
                "steps_total": 3,
            },
            "message": "ok",
        },
        workspace_root=tmp_path,
    )

    status = normalize_verification_status(status_file_payload(payload))

    assert status.negative_verification is not None
    assert status.negative_verification.status == "pass"
    assert status.negative_verification.run_id == "run-negative"
    assert status.negative_verification.run_profile == "fast"
    assert status.negative_verification.steps_passed == 3
    assert status.negative_verification.steps_total == 3
    assert len(status.negative_verification.oracles) == 1
    assert status.negative_verification.oracles[0].text == "Invalid email"
    assert status.negative_verification.oracles[0].source == "html:text"


def test_write_verification_status_round_trips_timeout(tmp_path) -> None:
    payload = enrich_verification_payload(
        {
            "result": "timeout",
            "workflow_name": "slow_verification",
            "workflow_path": str(tmp_path / "workflows" / "slow.yaml"),
            "run_id": None,
            "quality_score": 0.9,
            "quality": {"score": 0.9, "gaps": [], "recommendation": "Workflow quality is good."},
            "inputs_path": str(tmp_path / "inputs" / "slow_inputs.json"),
            "inputs_source": "generated_template",
            "timeout_seconds": 0,
            "message": "timed out",
        },
        workspace_root=tmp_path,
    )

    path = write_verification_status(tmp_path, payload)
    data = json.loads(path.read_text(encoding="utf-8"))
    status = normalize_verification_status(data)

    assert data["result"] == "timeout"
    assert data["inputs_source"] == "generated_template"
    assert data["inputs_path"].endswith("slow_inputs.json")
    assert "report_hint" in data
    assert status.result == "timeout"
    assert status.inputs_source == "generated_template"
    assert status.timeout_seconds == 0.0
    assert status.next_action.startswith("Workflow 执行超时")
    assert "--timeout-seconds" in status.next_action
