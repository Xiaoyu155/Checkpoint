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
    record_cloud_run_usage,
    session_path,
    session_to_snapshot_text,
    save_task_context,
    update_agent_session,
    workspace_session_snapshot_text,
)
from visual_agent.repair_history import append_repair_history
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
    assert loaded.runs_this_month == 1
    assert loaded.cloud_runs_used == 0
    assert loaded.usage_reset_date


def test_update_session_increments_monthly_usage(tmp_path: Path) -> None:
    update_agent_session(tmp_path, run_result("login_flow", run_id="run-1"))
    update_agent_session(tmp_path, run_result("login_flow", run_id="run-2"))

    loaded = load_agent_session(tmp_path)

    assert loaded is not None
    assert loaded.runs_this_month == 2


def test_record_cloud_run_usage_increments_cloud_only(tmp_path: Path) -> None:
    update_agent_session(tmp_path, run_result("login_flow", run_id="run-1"))
    record_cloud_run_usage(tmp_path)
    record_cloud_run_usage(tmp_path, count=2)

    loaded = load_agent_session(tmp_path)

    assert loaded is not None
    assert loaded.runs_this_month == 1
    assert loaded.cloud_runs_used == 3
    assert loaded.usage_reset_date


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


def test_snapshot_includes_usage_summary(tmp_path: Path) -> None:
    update_agent_session(tmp_path, run_result("checkout"))
    record_cloud_run_usage(tmp_path, count=2)

    text = session_to_snapshot_text(load_agent_session(tmp_path))

    assert "Usage:" in text
    assert "Local runs this month: 1" in text
    assert "Cloud runs used: 2" in text


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
    assert payload["runs_this_month"] == 1
    assert payload["cloud_runs_used"] == 0
    assert payload["usage_reset_date"]


def test_load_legacy_session_defaults_usage_fields(tmp_path: Path) -> None:
    session_path(tmp_path).write_text(
        json.dumps(
            {
                "updated_at": 0.0,
                "passing_workflows": [],
                "failing_workflows": [],
                "latest_failure": None,
                "next_action": "legacy",
                "token_estimate": 1,
            }
        ),
        encoding="utf-8",
    )

    loaded = load_agent_session(tmp_path)

    assert loaded is not None
    assert loaded.runs_this_month == 0
    assert loaded.cloud_runs_used == 0
    assert loaded.usage_reset_date == ""


def test_save_task_context_round_trip_and_snapshot(tmp_path: Path) -> None:
    session = save_task_context(
        tmp_path,
        task="Fix checkout export",
        analyzed_files=["src/checkout.py", "tests/test_checkout.py"],
        root_cause="missing click handler",
        plan="patch handler and run verify",
        tried=["ran pytest"],
    )
    loaded = load_agent_session(tmp_path)

    assert loaded is not None
    assert loaded.ai_task_context == session.ai_task_context
    text = session_to_snapshot_text(loaded)
    assert "AI Task Context" in text
    assert "Fix checkout export" in text
    assert "src/checkout.py" in text
    assert len(text) // 4 <= 500


def test_save_task_context_overwrites_previous_context(tmp_path: Path) -> None:
    save_task_context(tmp_path, task="first task")
    save_task_context(tmp_path, task="second task", plan="continue here")

    loaded = load_agent_session(tmp_path)

    assert loaded is not None
    assert loaded.ai_task_context is not None
    assert loaded.ai_task_context.task == "second task"
    assert loaded.ai_task_context.plan == "continue here"
    assert "first task" not in session_to_snapshot_text(loaded)


def test_save_task_context_scrubs_secrets(tmp_path: Path) -> None:
    save_task_context(
        tmp_path,
        task="Fix login password=demo123",
        root_cause="Bearer abcdefghijklmnop leaked",
        plan="remove token=abc12345",
    )

    text = session_to_snapshot_text(load_agent_session(tmp_path))

    for keyword in ("password", "demo123", "Bearer ", "abcdefghijklmnop", "token"):
        assert keyword not in text


def test_update_agent_session_preserves_task_context(tmp_path: Path) -> None:
    save_task_context(tmp_path, task="Fix checkout")
    update_agent_session(tmp_path, run_result("checkout"))

    loaded = load_agent_session(tmp_path)

    assert loaded is not None
    assert loaded.ai_task_context is not None
    assert loaded.ai_task_context.task == "Fix checkout"


def test_workspace_session_snapshot_includes_latest_repair(tmp_path: Path) -> None:
    update_agent_session(tmp_path, run_result("checkout", run_id="run-pass"))
    append_repair_history(
        tmp_path,
        {
            "status": "verified",
            "source": "deterministic",
            "workflow": "checkout",
            "run_id": "run-fail",
            "repair": {
                "classification": "selector_drift",
                "confidence": 0.9,
                "recommended_fix": "Update target selector.",
                "apply_supported": True,
            },
            "workflow_repair_plan": {
                "applied": True,
                "apply_requested": True,
                "verify_requested": True,
                "rollback_on_fail": False,
                "verification": {"status": "passed", "run_id": "run-verify"},
            },
        },
    )

    text = workspace_session_snapshot_text(tmp_path)

    assert "Latest Repair" in text
    assert "Status: verified" in text
    assert "Workflow: checkout" in text
    assert "Classification: selector_drift" in text
    assert "Verification: passed" in text
