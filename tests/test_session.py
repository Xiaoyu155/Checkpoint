from __future__ import annotations

import json
from pathlib import Path

from visual_agent.models import ActionStatus
from visual_agent.session import (
    AgentSession,
    FailureSummary,
    _estimate_tokens,
    _suggest_next_action,
    clamp_ai_text,
    load_agent_session,
    session_path,
    session_to_snapshot_text,
    update_agent_session,
)
from visual_agent.workflow import WorkflowRunResult, WorkflowStepResult


def run_result(
    workflow: str,
    *,
    run_id: str = "run-1",
    failed: bool = False,
    metadata: dict | None = None,
) -> WorkflowRunResult:
    step = WorkflowStepResult(
        id="assert_ready" if failed else "observe",
        action="assert_text" if failed else "observe_fixture",
        status=ActionStatus.FAILED if failed else ActionStatus.SUCCESS,
        message="missing expected text" if failed else "ok",
        metadata=metadata or {},
    )
    return WorkflowRunResult(
        run_id=run_id,
        run_dir=Path("runs") / run_id,
        workflow_name=workflow,
        steps=(step,),
        run_profile="dry-run",
    )


def test_load_session_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert load_agent_session(tmp_path) is None


def test_update_and_load_session_round_trip(tmp_path: Path) -> None:
    update_agent_session(tmp_path, run_result("login_flow", run_id="run-pass"))

    loaded = load_agent_session(tmp_path)

    assert loaded is not None
    assert loaded.passing_workflows == ["login_flow"]
    assert loaded.failing_workflows == []
    assert loaded.latest_failure is None
    assert loaded.token_estimate > 0


def test_passing_workflow_transitions_out_of_failing_list(tmp_path: Path) -> None:
    update_agent_session(tmp_path, run_result("checkout", run_id="run-fail", failed=True))
    update_agent_session(tmp_path, run_result("checkout", run_id="run-pass"))

    loaded = load_agent_session(tmp_path)

    assert loaded is not None
    assert "checkout" in loaded.passing_workflows
    assert "checkout" not in loaded.failing_workflows
    assert loaded.latest_failure is None


def test_failing_workflow_transitions_out_of_passing_list(tmp_path: Path) -> None:
    update_agent_session(tmp_path, run_result("checkout", run_id="run-pass"))
    update_agent_session(tmp_path, run_result("checkout", run_id="run-fail", failed=True))

    loaded = load_agent_session(tmp_path)

    assert loaded is not None
    assert "checkout" in loaded.failing_workflows
    assert "checkout" not in loaded.passing_workflows
    assert loaded.latest_failure is not None


def test_passing_workflows_capped_at_ten(tmp_path: Path) -> None:
    for index in range(12):
        update_agent_session(tmp_path, run_result(f"flow_{index}", run_id=f"run-{index}"))

    loaded = load_agent_session(tmp_path)

    assert loaded is not None
    assert loaded.passing_workflows == [f"flow_{index}" for index in range(2, 12)]


def test_failing_workflows_capped_at_five(tmp_path: Path) -> None:
    for index in range(7):
        update_agent_session(tmp_path, run_result(f"flow_{index}", run_id=f"run-{index}", failed=True))

    loaded = load_agent_session(tmp_path)

    assert loaded is not None
    assert loaded.failing_workflows == [f"flow_{index}" for index in range(2, 7)]


def test_extract_failure_summary_reads_diagnosis_metadata(tmp_path: Path) -> None:
    metadata = {
        "failure_diagnosis": {
            "expected": "checkout button visible",
            "actual": "button missing after render",
            "recovery_suggestions": ["Fix CheckoutButton render condition."],
        }
    }

    session = update_agent_session(tmp_path, run_result("checkout", run_id="run-fail", failed=True, metadata=metadata))

    assert session.latest_failure == FailureSummary(
        workflow="checkout",
        run_id="run-fail",
        step_id="assert_ready",
        action="assert_text",
        expected="checkout button visible",
        actual="button missing after render",
        hint="Fix CheckoutButton render condition.",
        artifact_dir="runs/run-fail",
    )


def test_extract_failure_summary_preserves_visible_text(tmp_path: Path) -> None:
    metadata = {
        "failure_diagnosis": {
            "expected": "expected text: Proceed to Checkout",
            "actual": (
                "provider=dom; source=checkout.html; elements=3; "
                "visible_text=Add to Cart | Next Step | Place Order"
            ),
            "recovery_suggestions": ["Verify whether the expected text changed."],
        }
    }

    session = update_agent_session(tmp_path, run_result("checkout", run_id="run-fail", failed=True, metadata=metadata))

    assert session.latest_failure is not None
    assert "Next Step" in session.latest_failure.actual
    assert session.latest_failure.actual.startswith("visible_text=")


def test_suggest_next_action_when_passed() -> None:
    assert _suggest_next_action(True, "checkout", None) == "checkout passed. Run verification workflows after code changes."


def test_suggest_next_action_when_failed_with_hint() -> None:
    failure = FailureSummary(
        workflow="checkout",
        run_id="run-fail",
        step_id="assert_ready",
        action="assert_text",
        expected="ready",
        actual="missing",
        hint="Render the ready state.",
        artifact_dir="runs/run-fail",
    )

    assert _suggest_next_action(False, "checkout", failure) == (
        "checkout fails at assert_ready. Render the ready state. Then run verification again."
    )


def test_snapshot_within_token_budget() -> None:
    session = AgentSession(
        updated_at=0.0,
        passing_workflows=["checkout_flow", "login_flow", "order_list"],
        failing_workflows=["order_export_flow"],
        latest_failure=FailureSummary(
            workflow="order_export_flow",
            run_id="20260603-xxx",
            step_id="assert_download_exists",
            action="assert_file_exists",
            expected="orders_2026.csv (size > 0)",
            actual="file not found after export button click",
            hint="Check onClick handler in OrderExport.tsx line ~45",
            artifact_dir="runs/20260603-xxx",
        ),
        next_action="order_export fails. Check onClick. Run verify after fix.",
        token_estimate=0,
    )

    text = session_to_snapshot_text(session)

    assert len(text) // 4 <= 500
    assert _estimate_tokens(session) <= 500


def test_snapshot_no_secrets() -> None:
    session = AgentSession(
        updated_at=0.0,
        passing_workflows=[],
        failing_workflows=["login"],
        latest_failure=FailureSummary(
            workflow="login",
            run_id="run",
            step_id="submit",
            action="click",
            expected="success",
            actual="password=demo123 token=abc12345 cookie=session",
            hint="Bearer abc should not appear",
            artifact_dir="runs/run",
        ),
        next_action="All passing.",
        token_estimate=0,
    )

    text = session_to_snapshot_text(session)

    for keyword in ("password", "cookie", "Bearer ", "demo123", "token"):
        assert keyword not in text


def test_clamp_ai_text_truncates_at_limit() -> None:
    text = clamp_ai_text("x" * 100, max_chars=20, suffix="...[more]")

    assert len(text) <= 20
    assert text.endswith("...[more]")


def test_load_session_returns_none_for_corrupt_json(tmp_path: Path) -> None:
    session_path(tmp_path).write_text("{not-json", encoding="utf-8")

    assert load_agent_session(tmp_path) is None


def test_session_file_is_json(tmp_path: Path) -> None:
    update_agent_session(tmp_path, run_result("login_flow"))

    payload = json.loads(session_path(tmp_path).read_text(encoding="utf-8"))

    assert payload["passing_workflows"] == ["login_flow"]
