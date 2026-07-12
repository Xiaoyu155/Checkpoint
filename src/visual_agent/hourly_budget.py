from __future__ import annotations

import json
from typing import Any


DEFAULT_WINDOW_MINUTES = 300
DEFAULT_RESERVE_MINUTES = 45
DEFAULT_PAUSE_AT_USED = 82.0
MIN_STRONG_WORKER_MINUTES = 20


def estimate_remaining_window_minutes(snapshot: dict[str, Any] | None, *, window_minutes: int = DEFAULT_WINDOW_MINUTES) -> int:
    """Approximate remaining 5h-window headroom from the latest quota snapshot."""
    if not isinstance(snapshot, dict):
        return int(window_minutes)
    used = _window_used_percentage(snapshot, "five_hour")
    if used is None:
        return int(window_minutes)
    return max(0, int(round(float(window_minutes) * (100.0 - used) / 100.0)))


def quota_used_percentage(snapshot: dict[str, Any] | None) -> float:
    if not isinstance(snapshot, dict):
        return 0.0
    used = _window_used_percentage(snapshot, "five_hour")
    return 0.0 if used is None else used


def build_hourly_plan(
    *,
    tasks: list[dict[str, Any]],
    quota_snapshot: dict[str, Any] | None = None,
    hours: float = 5.0,
    reserve_minutes: int = DEFAULT_RESERVE_MINUTES,
    pause_at_used_percentage: float = DEFAULT_PAUSE_AT_USED,
    quota_mode: str = "conservative",
) -> dict[str, Any]:
    """Plan ready tasks inside the current quota window.

    This is deliberately conservative: if quota is hot, strong workers stop and
    the plan keeps only cheap/research/doc work moving.
    """
    normalized_quota_mode = str(quota_mode or "conservative").strip().lower()
    unrestricted = normalized_quota_mode == "unrestricted"
    window_minutes = max(30, int(float(hours) * 60))
    remaining = min(window_minutes, estimate_remaining_window_minutes(quota_snapshot, window_minutes=DEFAULT_WINDOW_MINUTES))
    used = quota_used_percentage(quota_snapshot)
    requested_reserve = 0 if unrestricted else max(0, int(reserve_minutes))
    effective_reserve = effective_reserve_minutes(remaining, requested_reserve)
    usable_strong = max(0, remaining - effective_reserve)
    strong_allowed = used < float(pause_at_used_percentage) and usable_strong >= MIN_STRONG_WORKER_MINUTES
    scheduled: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for task in tasks:
        if str(task.get("status") or "pending") not in {"pending", "ready"}:
            continue
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
            mode = "delegated_worker" if unrestricted else "research_or_doc"
            scheduled.append(_slot(task, mode=mode, minutes=max(5, int(task.get("estimated_minutes") or 15))))
            continue
        if unrestricted:
            scheduled.append(_slot(task, mode="delegated_worker", minutes=estimate))
            continue
        if strong_allowed and estimate <= usable_strong:
            scheduled.append(_slot(task, mode="strong_worker", minutes=estimate))
            usable_strong -= estimate
            strong_allowed = usable_strong >= MIN_STRONG_WORKER_MINUTES and used < float(pause_at_used_percentage)
            continue
        deferred.append({**task, "reason": "not enough safe 5h-window headroom for a strong worker"})

    return {
        "schema_version": 1,
        "quota_mode": normalized_quota_mode,
        "window_minutes": window_minutes,
        "remaining_window_minutes": remaining,
        "reserve_minutes": effective_reserve,
        "requested_reserve_minutes": requested_reserve,
        "usable_strong_minutes": usable_strong,
        "pause_at_used_percentage": float(pause_at_used_percentage),
        "quota_used_percentage": used,
        "strong_workers_allowed": bool(
            unrestricted or strong_allowed or any(item.get("mode") == "strong_worker" for item in scheduled)
        ),
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


def effective_reserve_minutes(available_minutes: int, requested_reserve_minutes: int) -> int:
    """Scale the reserve to the actual supervision window.

    The default 45 minute reserve is appropriate for a 5h quota window, but it
    makes 1h dogfood/autopilot runs unable to start normal 45 minute tasks.
    Keep the requested reserve for large windows and cap it to one sixth of
    the available time for shorter windows.
    """
    available = max(0, int(available_minutes))
    requested = max(0, int(requested_reserve_minutes))
    if available <= 0 or requested <= 0:
        return 0
    scaled = max(0, available // 6)
    return min(requested, scaled)


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
    delegated = sum(1 for item in scheduled if item.get("mode") == "delegated_worker")
    return f"{len(scheduled)} scheduled ({strong} strong, {cheap} cheap, {research} research/doc, {delegated} delegated), {len(deferred)} deferred, {len(blocked)} blocked"


def _window_used_percentage(snapshot: dict[str, Any], window_name: str) -> float | None:
    blocks: list[dict[str, Any]] = []
    rate_limits = snapshot.get("rate_limits") if isinstance(snapshot.get("rate_limits"), dict) else {}
    block = rate_limits.get(window_name) if isinstance(rate_limits, dict) else None
    if isinstance(block, dict):
        blocks.append(block)
    providers = snapshot.get("providers") if isinstance(snapshot.get("providers"), dict) else {}
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        limits = provider.get("rate_limits") if isinstance(provider.get("rate_limits"), dict) else {}
        provider_block = limits.get(window_name) if isinstance(limits, dict) else None
        if isinstance(provider_block, dict):
            blocks.append(provider_block)
    used_values = [_used_percentage(block) for block in blocks]
    used_values = [value for value in used_values if value is not None]
    if not used_values:
        return None
    # The scheduler is provider-agnostic today, so use the hottest matching
    # window as a conservative global signal.
    return max(used_values)


def _used_percentage(block: dict[str, Any]) -> float | None:
    used = block.get("used_percentage")
    if isinstance(used, (int, float)):
        return max(0.0, min(100.0, float(used)))
    remaining = block.get("remaining_percentage")
    if isinstance(remaining, (int, float)):
        return max(0.0, min(100.0, 100.0 - float(remaining)))
    return None
