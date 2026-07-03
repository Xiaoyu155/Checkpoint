from __future__ import annotations

import json
from typing import Any


DEFAULT_WINDOW_MINUTES = 300
DEFAULT_RESERVE_MINUTES = 45
DEFAULT_PAUSE_AT_USED = 82.0


def estimate_remaining_window_minutes(snapshot: dict[str, Any] | None, *, window_minutes: int = DEFAULT_WINDOW_MINUTES) -> int:
    """Approximate remaining 5h-window headroom from the latest quota snapshot."""
    if not isinstance(snapshot, dict):
        return int(window_minutes)
    five_hour = (snapshot.get("rate_limits") or {}).get("five_hour") if isinstance(snapshot.get("rate_limits"), dict) else None
    if not isinstance(five_hour, dict):
        return int(window_minutes)
    used = max(0.0, min(100.0, float(five_hour.get("used_percentage") or 0.0)))
    return max(0, int(round(float(window_minutes) * (100.0 - used) / 100.0)))


def quota_used_percentage(snapshot: dict[str, Any] | None) -> float:
    if not isinstance(snapshot, dict):
        return 0.0
    five_hour = (snapshot.get("rate_limits") or {}).get("five_hour") if isinstance(snapshot.get("rate_limits"), dict) else None
    if not isinstance(five_hour, dict):
        return 0.0
    return max(0.0, min(100.0, float(five_hour.get("used_percentage") or 0.0)))


def build_hourly_plan(
    *,
    tasks: list[dict[str, Any]],
    quota_snapshot: dict[str, Any] | None = None,
    hours: float = 5.0,
    reserve_minutes: int = DEFAULT_RESERVE_MINUTES,
    pause_at_used_percentage: float = DEFAULT_PAUSE_AT_USED,
) -> dict[str, Any]:
    """Plan ready tasks inside the current quota window.

    This is deliberately conservative: if quota is hot, strong workers stop and
    the plan keeps only cheap/research/doc work moving.
    """
    window_minutes = max(30, int(float(hours) * 60))
    remaining = min(window_minutes, estimate_remaining_window_minutes(quota_snapshot, window_minutes=DEFAULT_WINDOW_MINUTES))
    used = quota_used_percentage(quota_snapshot)
    usable_strong = max(0, remaining - max(0, int(reserve_minutes)))
    strong_allowed = used < float(pause_at_used_percentage) and usable_strong >= 20
    scheduled: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for task in tasks:
        if str(task.get("status") or "pending") not in {"pending", "ready"}:
            continue
        task_id = str(task.get("task_id") or "")
        risk = str(task.get("risk") or "medium")
        tier = str(task.get("worker_tier") or "strong")
        estimate = max(5, int(task.get("estimated_strong_minutes") or task.get("estimated_minutes") or 30))
        if risk == "external":
            blocked.append({**task, "reason": "requires external access, credentials, production system, or human decision"})
            continue
        if tier == "cheap":
            scheduled.append(_slot(task, mode="cheap_worker", minutes=max(5, int(task.get("estimated_minutes") or 20))))
            continue
        if tier in {"research", "doc"}:
            scheduled.append(_slot(task, mode="research_or_doc", minutes=max(5, int(task.get("estimated_minutes") or 15))))
            continue
        if strong_allowed and estimate <= usable_strong:
            scheduled.append(_slot(task, mode="strong_worker", minutes=estimate))
            usable_strong -= estimate
            strong_allowed = usable_strong >= 20 and used < float(pause_at_used_percentage)
            continue
        deferred.append({**task, "reason": "not enough safe 5h-window headroom for a strong worker"})

    return {
        "schema_version": 1,
        "window_minutes": window_minutes,
        "remaining_window_minutes": remaining,
        "reserve_minutes": int(reserve_minutes),
        "usable_strong_minutes": usable_strong,
        "pause_at_used_percentage": float(pause_at_used_percentage),
        "quota_used_percentage": used,
        "strong_workers_allowed": bool(strong_allowed or any(item.get("mode") == "strong_worker" for item in scheduled)),
        "scheduled": scheduled,
        "deferred": deferred,
        "blocked": blocked,
        "summary": _summary(scheduled, deferred, blocked),
    }


def hourly_plan_to_markdown(payload: dict[str, Any]) -> str:
    lines = ["## DevPacer Hourly Plan", ""]
    lines.append(
        f"5h window used: `{payload.get('quota_used_percentage', 0):.1f}%`; "
        f"remaining≈`{payload.get('remaining_window_minutes')}` min; "
        f"reserve=`{payload.get('reserve_minutes')}` min"
    )
    lines.append(f"Summary: {payload.get('summary')}")
    for title, key in (("Scheduled", "scheduled"), ("Deferred", "deferred"), ("Blocked", "blocked")):
        items = payload.get(key) if isinstance(payload.get(key), list) else []
        if not items:
            continue
        lines.extend(["", f"### {title}", ""])
        for item in items:
            label = item.get("mode") or item.get("reason") or ""
            lines.append(f"- `{item.get('task_id')}` {item.get('objective')} ({label})")
    return "\n".join(lines).rstrip()


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _slot(task: dict[str, Any], *, mode: str, minutes: int) -> dict[str, Any]:
    return {
        "task_id": task.get("task_id"),
        "objective": task.get("objective"),
        "mode": mode,
        "estimated_minutes": int(minutes),
        "agent": task.get("agent"),
        "test_command": task.get("test_command"),
    }


def _summary(scheduled: list[dict[str, Any]], deferred: list[dict[str, Any]], blocked: list[dict[str, Any]]) -> str:
    strong = sum(1 for item in scheduled if item.get("mode") == "strong_worker")
    cheap = sum(1 for item in scheduled if item.get("mode") == "cheap_worker")
    research = sum(1 for item in scheduled if item.get("mode") == "research_or_doc")
    return f"{len(scheduled)} scheduled ({strong} strong, {cheap} cheap, {research} research/doc), {len(deferred)} deferred, {len(blocked)} blocked"
