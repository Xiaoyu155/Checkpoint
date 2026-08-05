from __future__ import annotations

import json

import pytest

from visual_agent.pacer_host import (
    _host_duration_lines,
    host_end_summary,
    retryable_stopped_missions,
)


# 2026-08-06 那次真实托管的形状：申请 5 小时，45 分钟 HOST_DRAINED，
# 8 个 verified、3 个 same_failure_repeated 被原样丢下。
LAUNCHED = ["m-ok-1", "m-fail-1", "m-fail-2", "m-refused", "m-quota"]

ROWS = [
    {"mission_id": "m-ok-1", "status": "verified", "stop_reason": "verified", "objective": "改空态"},
    {"mission_id": "m-fail-1", "status": "stopped", "stop_reason": "same_failure_repeated", "objective": "统一字号"},
    {"mission_id": "m-fail-2", "status": "stopped", "stop_reason": "verification_failed", "objective": "改按钮"},
    {"mission_id": "m-refused", "status": "stopped", "stop_reason": "evidence_rejected", "objective": "改字重"},
    {"mission_id": "m-quota", "status": "stopped", "stop_reason": "quota_exhausted", "objective": "别的活"},
]


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("visual_agent.pacer_host.list_missions", lambda _root: ROWS)
    return tmp_path


def test_failed_missions_are_picked_up_instead_of_abandoned(workspace) -> None:
    pending = retryable_stopped_missions(workspace, LAUNCHED)

    # 手里还有几小时预算却丢下明知没做成的事，等于把托管缩水成一次派工。
    assert {item["mission_id"] for item in pending} == {"m-fail-1", "m-fail-2"}


def test_a_refusal_is_never_retried(workspace) -> None:
    pending = retryable_stopped_missions(workspace, LAUNCHED)

    # 重试拒绝类失败，正是昨天刚修掉的"让人白费功夫"。
    assert all(item["mission_id"] != "m-refused" for item in pending)


def test_a_quota_block_is_not_retried_either(workspace) -> None:
    # 额度用尽缺的是额度，不是次数；立刻重试只会再撞一次墙。
    assert all(item["mission_id"] != "m-quota" for item in retryable_stopped_missions(workspace, LAUNCHED))


def test_a_verified_mission_is_never_redone(workspace) -> None:
    assert all(item["mission_id"] != "m-ok-1" for item in retryable_stopped_missions(workspace, LAUNCHED))


def test_retries_stop_after_the_allowed_number_of_attempts(workspace) -> None:
    once = retryable_stopped_missions(workspace, LAUNCHED, retry_counts={"m-fail-1": 1}, max_retries=1)

    assert {item["mission_id"] for item in once} == {"m-fail-2"}
    assert retryable_stopped_missions(workspace, LAUNCHED, retry_counts={"m-fail-1": 1, "m-fail-2": 1}) == []


def test_missions_from_other_sessions_are_left_alone(workspace) -> None:
    # 只捡本轮自己启动的，不去动别人的任务。
    assert retryable_stopped_missions(workspace, ["m-ok-1"]) == []


def test_the_summary_states_requested_and_actual_duration() -> None:
    summary = host_end_summary(
        requested_hours=5.0, started_at_monotonic=0.0, now_monotonic=45 * 60, reason="goals_and_retries_exhausted"
    )

    assert summary["requested_seconds"] == 18000.0
    assert summary["elapsed_seconds"] == 2700.0
    assert summary["ended_early"] is True


def test_a_run_that_used_its_time_is_not_flagged_as_early() -> None:
    summary = host_end_summary(
        requested_hours=1.0, started_at_monotonic=0.0, now_monotonic=59 * 60, reason="deadline"
    )

    assert summary["ended_early"] is False


def test_the_user_is_told_plainly_that_five_hours_became_forty_five_minutes() -> None:
    lines = "\n".join(
        _host_duration_lines(
            host_end_summary(
                requested_hours=5.0,
                started_at_monotonic=0.0,
                now_monotonic=45 * 60,
                reason="goals_and_retries_exhausted",
                unfinished=[{"stop_reason": "same_failure_repeated", "objective": "统一字号"}],
            )
        )
    )

    # 申请 5 小时拿到 45 分钟却看不出差别，本身就是一句谎。
    assert "45 分钟" in lines
    assert "300 分钟" in lines
    assert "提前结束" in lines
    assert "仍未做成：1 项" in lines


def test_nothing_is_printed_when_the_run_has_no_summary() -> None:
    assert _host_duration_lines(None) == []
    assert _host_duration_lines({}) == []


def test_the_summary_survives_a_json_round_trip() -> None:
    summary = host_end_summary(
        requested_hours=2.0, started_at_monotonic=0.0, now_monotonic=600.0, reason="goals_and_retries_exhausted"
    )

    assert json.loads(json.dumps(summary))["ended_early"] is True
