from __future__ import annotations

from visual_agent.hourly_budget import build_hourly_plan, estimate_remaining_window_minutes


def test_estimate_remaining_window_minutes_from_quota_snapshot() -> None:
    snapshot = {"rate_limits": {"five_hour": {"used_percentage": 50}}}

    assert estimate_remaining_window_minutes(snapshot) == 150


def test_hourly_plan_preserves_reserve_and_defers_strong_when_hot() -> None:
    tasks = [
        {"task_id": "task-001", "objective": "Implement voice page", "worker_tier": "strong", "estimated_strong_minutes": 60},
        {"task_id": "task-002", "objective": "Update README", "worker_tier": "cheap", "estimated_minutes": 15},
    ]
    snapshot = {"rate_limits": {"five_hour": {"used_percentage": 90}}}

    plan = build_hourly_plan(tasks=tasks, quota_snapshot=snapshot, hours=5)

    assert [item["task_id"] for item in plan["scheduled"]] == ["task-002"]
    assert plan["deferred"][0]["task_id"] == "task-001"


def test_hourly_plan_schedules_strong_inside_safe_window() -> None:
    tasks = [{"task_id": "task-001", "objective": "Implement voice page", "worker_tier": "strong", "estimated_strong_minutes": 60}]

    plan = build_hourly_plan(tasks=tasks, quota_snapshot={"rate_limits": {"five_hour": {"used_percentage": 10}}}, hours=5)

    assert plan["scheduled"][0]["mode"] == "strong_worker"
