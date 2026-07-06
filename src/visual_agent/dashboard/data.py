"""Data aggregation and caching layer for the dashboard.

Provides TTL-based caching for expensive operations like agents_doctor()
and full dashboard data assembly, eliminating redundant subprocess calls
and disk I/O on every frontend poll.
"""

from __future__ import annotations

import subprocess
import time
import threading
from pathlib import Path
from typing import Any

from ..agent_capabilities import agents_doctor
from ..chief_plans_store import list_plans, load_plan, load_worker_records
from ..chief_queue import list_mission_queue_items
from ..missions import list_missions
from ..mimo_efficiency import compute_mimo_efficiency
from ..programs import list_programs
from ..workbench_board import attach_board_fields


# ---------------------------------------------------------------------------
# Generic TTL cache
# ---------------------------------------------------------------------------

_SENTINEL = object()


class _TTLCache:
    """Thread-safe, TTL-based cache for any value."""

    def __init__(self, ttl: float = 60.0):
        self._ttl = ttl
        self._ts: float = 0.0
        self._data: Any = _SENTINEL
        self._lock = threading.Lock()

    def is_valid(self) -> bool:
        with self._lock:
            return self._data is not _SENTINEL and (time.monotonic() - self._ts) < self._ttl

    def get(self) -> Any:
        """Return cached data. Caller must check is_valid() first."""
        with self._lock:
            return None if self._data is _SENTINEL else self._data

    def set(self, data: Any) -> None:
        with self._lock:
            self._data = data
            self._ts = time.monotonic()

    def invalidate(self) -> None:
        with self._lock:
            self._data = _SENTINEL
            self._ts = 0.0


# ---------------------------------------------------------------------------
# Cached agents_doctor — eliminates subprocess calls on every poll
# ---------------------------------------------------------------------------

_agents_cache = _TTLCache(ttl=60.0)


def get_agents_cached() -> list[dict[str, Any]]:
    """Return agents_doctor() results, cached for 60 seconds."""
    if _agents_cache.is_valid():
        return _agents_cache.get()  # type: ignore[return-value]
    result = agents_doctor()
    _agents_cache.set(result)
    return result


# ---------------------------------------------------------------------------
# Cached backend token reads — eliminates repeated file I/O
# ---------------------------------------------------------------------------

_token_cache: dict[str, _TTLCache] = {}
_token_lock = threading.Lock()

TOKEN_TTL = 300.0  # 5 minutes


def get_backend_token_cached(name: str, *, load_fn: Any = None) -> str | None:
    """Return a backend token with 5-minute caching."""
    with _token_lock:
        if name not in _token_cache:
            _token_cache[name] = _TTLCache(ttl=TOKEN_TTL)
        cache = _token_cache[name]

    if cache.is_valid():
        return cache.get()  # type: ignore[return-value]

    token = load_fn(name) if load_fn else None
    cache.set(token)
    return token


# ---------------------------------------------------------------------------
# Cached dashboard data — eliminates redundant disk I/O on rapid polls
# ---------------------------------------------------------------------------

_data_caches: dict[str, _TTLCache] = {}
_data_cache_lock = threading.Lock()


def invalidate_dashboard_data_cache(root: Path | str | None = None) -> None:
    """Drop cached dashboard data for one workspace or for all workspaces."""
    with _data_cache_lock:
        if root is None:
            _data_caches.clear()
            return
        key = str(Path(root).expanduser().resolve())
        _data_caches.pop(key, None)


def build_dashboard_data_cached(root: Path) -> dict[str, Any]:
    """Build dashboard data with 2-second TTL caching."""
    cache_key = str(Path(root).expanduser().resolve())
    with _data_cache_lock:
        cache = _data_caches.setdefault(cache_key, _TTLCache(ttl=2.0))

    if cache.is_valid():
        return cache.get()  # type: ignore[return-value]

    data = _build_dashboard_data_uncached(root)
    cache.set(data)
    return data


def _build_dashboard_data_uncached(root: Path) -> dict[str, Any]:
    """Original dashboard data assembly (uncached, called by cache layer)."""
    missions = attach_board_fields(root, list_missions(root))
    for mission in missions:
        if mission.get("board_column") == "pending_merge":
            mission["board_column"] = "in_review"
    plans = list_plans(root)

    try:
        raw_queue = list_mission_queue_items(root).get("entries", [])
    except Exception:
        raw_queue = []

    mission_map = {str(m.get("mission_id") or ""): m for m in missions if m.get("mission_id")}
    queue: list[dict[str, Any]] = []
    for item in raw_queue:
        mid = str(item.get("mission_id") or "")
        enriched = dict(item)
        m = mission_map.get(mid) or {}
        enriched["objective"] = str(m.get("objective") or m.get("goal") or "")
        queue.append(enriched)

    try:
        programs = list_programs(root)
    except Exception:
        programs = []

    agents = get_agents_cached()

    counts: dict[str, int] = {}
    for mission in missions:
        key = mission.get("status") or "unknown"
        counts[key] = counts.get(key, 0) + 1

    installed_agents = [str(a.get("agent")) for a in agents if isinstance(a, dict) and a.get("installed")]

    try:
        from ..agent_backends import resolve_backend_by_name
        bugteam_available = resolve_backend_by_name("bugteam") is not None
        mimo_available = resolve_backend_by_name("mimo") is not None
    except Exception:
        bugteam_available = False
        mimo_available = False

    return {
        "product": "Pacer",
        "orchestrator": "Pacer",
        "bugteam_available": bugteam_available,
        "mimo_available": mimo_available,
        "engine": "Checkpoint",
        "workspace_root": str(root),
        "repo_root": str(root.parent),
        "status": _read_status(root),
        "value": _build_value(root, missions, plans),
        "agents": agents,
        "installed_agents": installed_agents,
        "missions": missions,
        "mission_counts": counts,
        "plans": plans,
        "queue": queue,
        "programs": programs,
        "launches": _launch_snapshot(),
        "worker": worker_status(),
    }


def _read_status(root: Path) -> dict[str, Any]:
    candidates = [
        root.parent / ".visual-agent-status.md",
        root / ".visual-agent-status.md",
    ]
    for path in candidates:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="ignore")
            state = "unknown"
            for line in text.splitlines():
                if line.lower().startswith("## status:"):
                    state = line.split(":", 1)[1].strip()
                    break
            return {"state": state, "path": str(path), "raw": text[:2000]}
    return {"state": "none", "path": "", "raw": ""}


def _build_value(root: Path, missions: list[dict[str, Any]], plans: list[dict[str, Any]]) -> dict[str, Any]:
    verified = sum(1 for m in missions if str(m.get("status") or "") == "verified")
    tier_counts = {"cheap": 0, "standard": 0, "strong": 0}
    spent_usd = 0.0
    saved_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    real_runs = 0
    all_worker_records: list[dict[str, Any] | None] = []

    for summary in plans:
        plan_id = summary.get("plan_id")
        if not plan_id:
            continue
        payload = load_plan(root, plan_id) or {}
        for track in payload.get("worker_tracks") or []:
            tier = str((track or {}).get("tier") or "")
            if tier in tier_counts:
                tier_counts[tier] += 1
        records = load_worker_records(root, plan_id) or []
        all_worker_records.extend(records)
        for record in records:
            usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
            cost = float(usage.get("cost_usd") or 0.0)
            if usage.get("cost_usd") is not None:
                real_runs += 1
                if usage.get("cost_is_savings"):
                    saved_usd += cost
                else:
                    spent_usd += cost
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)

    routed_tasks = tier_counts["cheap"] + tier_counts["standard"]
    return {
        "missions_total": len(missions),
        "verified": verified,
        "tier_counts": tier_counts,
        "downgraded_tasks": routed_tasks,
        "spent_usd": round(spent_usd, 4),
        "saved_usd": round(saved_usd, 4),
        "real_worker_runs": real_runs,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "mimo_efficiency": compute_mimo_efficiency(all_worker_records),
    }


# ---------------------------------------------------------------------------
# Launch and worker state — shared with server.py
# ---------------------------------------------------------------------------

_LAUNCHES: dict[str, dict[str, Any]] = {}
_LAUNCH_LOCK = threading.Lock()

_WORKER_PROC: subprocess.Popen[bytes] | None = None
_WORKER_LOCK = threading.Lock()


def _launch_snapshot() -> list[dict[str, Any]]:
    with _LAUNCH_LOCK:
        return [dict(item) for item in _LAUNCHES.values()]


def worker_status() -> dict[str, Any]:
    global _WORKER_PROC
    with _WORKER_LOCK:
        if _WORKER_PROC is None:
            return {"running": False, "pid": None}
        if _WORKER_PROC.poll() is not None:
            _WORKER_PROC = None
            return {"running": False, "pid": None}
        return {"running": True, "pid": _WORKER_PROC.pid}
