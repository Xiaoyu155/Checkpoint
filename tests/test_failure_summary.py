from __future__ import annotations

import json
from pathlib import Path

from visual_agent.failure_summary import build_failure_summary
from visual_agent.session import AgentSession, FailureSummary, session_path


def write_session(workspace: Path, session: AgentSession) -> None:
    session_path(workspace).write_text(
        json.dumps(
            {
                "updated_at": session.updated_at,
                "passing_workflows": session.passing_workflows,
                "failing_workflows": session.failing_workflows,
                "latest_failure": session.latest_failure.__dict__ if session.latest_failure else None,
                "next_action": session.next_action,
                "token_estimate": session.token_estimate,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def failure() -> FailureSummary:
    return FailureSummary(
        workflow="checkout",
        run_id="run-fail",
        step_id="assert_ready",
        action="assert_text",
        expected="checkout ready",
        actual="ready text missing",
        hint="Render the ready state.",
        artifact_dir="runs/run-fail",
    )


def test_build_failure_summary_returns_no_failure_when_no_session(tmp_path: Path) -> None:
    assert build_failure_summary(tmp_path) == {"status": "no_failure", "message": "No recent failures found."}


def test_build_failure_summary_returns_no_failure_when_no_latest_failure(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        AgentSession(
            updated_at=0.0,
            passing_workflows=["checkout"],
            failing_workflows=[],
            latest_failure=None,
            next_action="All passing.",
            token_estimate=1,
        ),
    )

    assert build_failure_summary(tmp_path)["status"] == "no_failure"


def test_build_failure_summary_returns_found_with_correct_fields(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        AgentSession(
            updated_at=0.0,
            passing_workflows=[],
            failing_workflows=["checkout"],
            latest_failure=failure(),
            next_action="Fix checkout.",
            token_estimate=1,
        ),
    )

    summary = build_failure_summary(tmp_path)

    assert summary["status"] == "found"
    assert summary["workflow"] == "checkout"
    assert summary["run_id"] == "run-fail"
    assert summary["failed_step"] == {"id": "assert_ready", "action": "assert_text"}
    assert summary["expected"] == "checkout ready"
    assert summary["actual"] == "ready text missing"
    assert summary["hint"] == "Render the ready state."
    assert summary["artifacts"] == "runs/run-fail"
    assert "checkout" in summary["suggested_next_prompt"]


def test_build_failure_summary_scrubs_secrets_in_output(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        AgentSession(
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
                hint="Bearer abcdefghijklmnop",
                artifact_dir="runs/run",
            ),
            next_action="Fix login.",
            token_estimate=1,
        ),
    )

    text = json.dumps(build_failure_summary(tmp_path), ensure_ascii=False)

    for keyword in ("demo123", "abc12345", "session", "abcdefghijklmnop"):
        assert keyword not in text


def test_build_failure_summary_within_token_budget(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        AgentSession(
            updated_at=0.0,
            passing_workflows=[],
            failing_workflows=["checkout"],
            latest_failure=FailureSummary(
                workflow="checkout",
                run_id="run-fail",
                step_id="assert_ready",
                action="assert_text",
                expected="x" * 500,
                actual="y" * 500,
                hint="z" * 500,
                artifact_dir="runs/run-fail",
            ),
            next_action="Fix checkout.",
            token_estimate=1,
        ),
    )

    summary = build_failure_summary(tmp_path, max_chars=1600)

    assert len(json.dumps(summary, ensure_ascii=False)) <= 2000
    assert len(str(summary["suggested_next_prompt"])) <= 800


def test_build_failure_summary_token_estimate_is_accurate(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        AgentSession(
            updated_at=0.0,
            passing_workflows=[],
            failing_workflows=["checkout"],
            latest_failure=failure(),
            next_action="Fix checkout.",
            token_estimate=1,
        ),
    )

    summary = build_failure_summary(tmp_path)

    assert summary["token_estimate"] == len(summary["suggested_next_prompt"]) // 4
