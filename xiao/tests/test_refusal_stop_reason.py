from __future__ import annotations

from visual_agent.chief_run import chief_run_to_markdown
from visual_agent.chief_run import _stop_reason_from_dispatch


# 2026-08-05 真实事故的记录形状：worker 改了验收用的测试文件，
# Pacer 正确拒绝验收，但把预算写成了停止原因。
TAMPERED_VERIFICATION = {
    "verdict": "fail",
    "tampered_test_files": ["test/designSystem.test.mjs"],
    "repair_brief": {
        "source": "test_tampering",
        "repairable": False,
        "message": "Worker modified 1 test file(s); the command gate refuses to run against edited tests.",
    },
}

EXHAUSTED_BUDGET = {"status": "exhausted", "max_rounds": 1}


def _dispatch(status: str, verification: dict) -> dict:
    return {"status": status, "latest_verification": verification, "verification_attempts": [verification]}


def test_a_refusal_is_never_reported_as_a_budget_problem() -> None:
    reason = _stop_reason_from_dispatch(
        _dispatch("managed_budget_exhausted", TAMPERED_VERIFICATION), EXHAUSTED_BUDGET
    )

    # 这正是实测里出的错：真因是拒绝，用户看到的却是"时间和次数用满了"，
    # 于是被指引去"加大轮数重试"——轮数从来不是原因，重试一万次也一样。
    assert reason == "evidence_rejected"


def test_the_refusal_wins_even_when_every_budget_signal_says_exhausted() -> None:
    for status in ("managed_budget_exhausted", "managed_usage_unknown", "verification_failed"):
        assert (
            _stop_reason_from_dispatch(_dispatch(status, TAMPERED_VERIFICATION), EXHAUSTED_BUDGET)
            == "evidence_rejected"
        )


def test_a_scope_violation_reports_itself_too() -> None:
    verification = {"verdict": "fail", "source": "scope_violation", "failure_kind": "scope_violation"}

    assert (
        _stop_reason_from_dispatch(_dispatch("managed_budget_exhausted", verification), EXHAUSTED_BUDGET)
        == "scope_violation"
    )


def test_tampering_is_caught_even_when_only_the_file_list_survives() -> None:
    # 有些篡改记录只留下被改的文件名，没有留下 source。
    verification = {"verdict": "fail", "tampered_test_files": ["test/a.test.js"]}

    assert (
        _stop_reason_from_dispatch(_dispatch("managed_budget_exhausted", verification), EXHAUSTED_BUDGET)
        == "evidence_rejected"
    )


def test_a_real_budget_exhaustion_is_still_reported_as_budget() -> None:
    # 反向保护：没有拒绝的时候，预算仍然要如实说出来。
    verification = {"verdict": "fail", "command_verification": {"failure_kind": "command_failed", "exit_code": 1}}

    assert (
        _stop_reason_from_dispatch(_dispatch("managed_budget_exhausted", verification), EXHAUSTED_BUDGET)
        == "budget_exhausted"
    )


def test_a_verified_run_is_never_turned_into_a_refusal() -> None:
    passing = {"verdict": "pass"}

    assert _stop_reason_from_dispatch(_dispatch("verified", passing), {"status": "ok"}) == "verified"


def _report(stop_reason: str, verification: dict) -> str:
    return chief_run_to_markdown(
        {
            "status": "stopped",
            "stop_reason": stop_reason,
            "mission": {"mission_id": "m1", "objective": "换设计 token", "plan_id": "p1", "repo_root": "D:/x"},
            "plan": {"status": "ready"},
            "rounds": [],
            "dispatch": {
                "status": "managed_budget_exhausted",
                "latest_verification": verification,
                "worktree": {"path": "D:/x/wt", "branch": "b"},
            },
        }
    )


def test_the_user_is_not_told_to_retry_a_refusal() -> None:
    report = _report("evidence_rejected", TAMPERED_VERIFICATION)

    # 给拒绝类失败提供"重试"按钮，等于把人送去做必然失败的事。
    assert "加大轮数后重试" not in report
    assert "--max-rounds" not in report
    assert "allow-test-edits" in report


def test_the_report_says_the_gate_refused_rather_than_ran_out() -> None:
    report = _report("evidence_rejected", TAMPERED_VERIFICATION)

    assert "测试" in report
    assert "时间和次数用满" not in report
