from __future__ import annotations

from visual_agent.hourly_budget import build_hourly_plan, effective_reserve_minutes, estimate_remaining_window_minutes, quota_used_percentage


def test_estimate_remaining_window_minutes_from_quota_snapshot() -> None:
    snapshot = {"rate_limits": {"five_hour": {"used_percentage": 50}}}

    assert estimate_remaining_window_minutes(snapshot) == 150


def test_estimate_remaining_window_minutes_from_provider_snapshot() -> None:
    snapshot = {"providers": {"codex": {"rate_limits": {"five_hour": {"used_percentage": 60}}}}}

    assert estimate_remaining_window_minutes(snapshot) == 120
    assert quota_used_percentage(snapshot) == 60.0


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


def test_hourly_plan_scales_reserve_for_one_hour_supervision() -> None:
    tasks = [{"task_id": "task-001", "objective": "Implement discovery page polish", "worker_tier": "strong", "estimated_strong_minutes": 45}]

    plan = build_hourly_plan(tasks=tasks, quota_snapshot={"rate_limits": {"five_hour": {"used_percentage": 0}}}, hours=1, reserve_minutes=45)

    assert effective_reserve_minutes(60, 45) == 10
    assert plan["reserve_minutes"] == 10
    assert plan["requested_reserve_minutes"] == 45
    assert plan["scheduled"][0]["task_id"] == "task-001"
    assert plan["scheduled"][0]["mode"] == "strong_worker"


def test_unrestricted_quota_mode_schedules_all_internal_tasks_when_hot() -> None:
    tasks = [
        {"task_id": "task-001", "objective": "Refactor scheduler", "worker_tier": "strong", "estimated_strong_minutes": 240},
        {"task_id": "task-002", "objective": "Research and update docs", "worker_tier": "doc", "estimated_minutes": 30},
        {"task_id": "task-003", "objective": "Deploy production", "worker_tier": "strong", "risk": "external"},
    ]

    plan = build_hourly_plan(
        tasks=tasks,
        quota_snapshot={"rate_limits": {"five_hour": {"used_percentage": 99}}},
        hours=1,
        quota_mode="unrestricted",
    )

    assert plan["quota_mode"] == "unrestricted"
    assert plan["reserve_minutes"] == 0
    assert [item["task_id"] for item in plan["scheduled"]] == ["task-001", "task-002"]
    assert all(item["mode"] == "delegated_worker" for item in plan["scheduled"])
    assert plan["deferred"] == []
    assert plan["blocked"][0]["task_id"] == "task-003"
