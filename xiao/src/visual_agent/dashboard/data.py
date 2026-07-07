"""Data aggregation and caching layer for the dashboard.

Provides TTL-based caching for expensive operations like agents_doctor()
and full dashboard data assembly, eliminating redundant subprocess calls
and disk I/O on every frontend poll.
"""

from __future__ import annotations

import json
import subprocess
import time
import threading
from pathlib import Path
from typing import Any

from ..agent_capabilities import agents_doctor
from ..chief_plans_store import list_plans, load_plan, load_verification, load_worker_records, plan_dir
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
        mission["efficiency"] = _mission_efficiency(root, mission)
        mission["pacer_evidence"] = _mission_pacer_evidence(root, mission)
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
        "work_traces": _build_work_traces(root, missions=missions, plans=plans, queue=queue, programs=programs),
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


def _mission_efficiency(root: Path, mission: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(mission.get("plan_id") or "")
    records = load_worker_records(root, plan_id) if plan_id else []
    metrics = compute_mimo_efficiency(records or [])
    metrics["worker_runs"] = len([record for record in records or [] if isinstance(record, dict)])
    actual_worker_seconds = 0.0
    actual_cost_usd = 0.0
    actual_cost_available = False
    for record in records or []:
        if not isinstance(record, dict):
            continue
        actual_worker_seconds += float(record.get("elapsed_seconds") or 0.0)
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        if usage.get("cost_usd") is not None:
            actual_cost_available = True
            actual_cost_usd += float(usage.get("cost_usd") or 0.0)
    metrics["actual_worker_seconds"] = round(actual_worker_seconds, 3)
    metrics["actual_worker_minutes"] = round(actual_worker_seconds / 60.0, 2)
    metrics["actual_cost_usd"] = round(actual_cost_usd, 4)
    metrics["actual_cost_available"] = actual_cost_available
    metrics["actual_cost_label"] = f"${actual_cost_usd:.4f}" if actual_cost_available else "未回传"
    metrics["actual_task_seconds"] = _mission_elapsed_seconds(root, mission)
    metrics["actual_task_minutes"] = round(float(metrics["actual_task_seconds"] or 0.0) / 60.0, 2)
    return metrics


def _mission_pacer_evidence(root: Path, mission: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(mission.get("plan_id") or "").strip()
    mission_id = str(mission.get("mission_id") or "").strip()
    records = load_worker_records(root, plan_id) if plan_id else []
    verification = load_verification(root, plan_id) if plan_id else None
    verification = verification if isinstance(verification, dict) else {}
    latest_worker = records[-1] if records else {}
    backend = latest_worker.get("backend") if isinstance(latest_worker.get("backend"), dict) else {}
    log_path = str(latest_worker.get("log_path") or "").strip()
    report_path = root / "missions" / mission_id / "final_report.md" if mission_id else Path()
    command = ""
    if isinstance(verification.get("command_verification"), dict):
        command = str(verification["command_verification"].get("command") or "").strip()
    if not command:
        command = str(verification.get("command") or "").strip()
    return {
        "worker_status": str(latest_worker.get("status") or ""),
        "worker_command": str(latest_worker.get("command") or ""),
        "worker_exit_code": latest_worker.get("exit_code"),
        "worker_seconds": latest_worker.get("elapsed_seconds"),
        "worker_records": len(records),
        "agent": str(latest_worker.get("agent") or mission.get("agent") or ""),
        "backend": str(backend.get("name") or ""),
        "model": str(backend.get("model") or ""),
        "worktree": str(latest_worker.get("cwd") or ""),
        "log_path": log_path,
        "log_tail": _read_worker_log_tail(Path(log_path)) if log_path else "",
        "verification_verdict": str(verification.get("verdict") or ""),
        "verification_command": command,
        "verification_path": str(plan_dir(root, plan_id) / "verification.json") if plan_id else "",
        "merge_state": str(mission.get("merge_state") or ""),
        "final_report": str(report_path) if report_path else "",
    }


def _read_worker_log_tail(path: Path, *, max_chars: int = 1600) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_chars * 3))
            data = handle.read()
        return data.decode("utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""


def _build_work_traces(
    root: Path,
    *,
    missions: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    queue: list[dict[str, Any]],
    programs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compact, UI-ready ledger of everything the workbench has touched."""
    traces: list[dict[str, Any]] = []

    def add(
        *,
        kind: str,
        status: str,
        title: str,
        detail: str = "",
        path: str = "",
        timestamp: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        traces.append(
            {
                "kind": kind,
                "status": status,
                "title": title[:180],
                "detail": detail[:800],
                "path": path,
                "timestamp": timestamp,
                "meta": meta or {},
            }
        )

    for launch in sorted(_launch_snapshot(), key=lambda item: str(item.get("started_at") or ""), reverse=True)[:10]:
        launch_id = str(launch.get("launch_id") or "")
        state = str(launch.get("state") or "starting")
        goal = str(launch.get("goal") or "未命名任务")
        add(
            kind="launch",
            status=state,
            title=f"launch {launch_id}: {goal}",
            detail=str(launch.get("error") or launch.get("stop_reason") or launch.get("status") or ""),
            timestamp=str(launch.get("started_at") or ""),
            meta={
                "launch_id": launch_id,
                "mission_id": launch.get("mission_id") or "",
                "execute": bool(launch.get("execute")),
                "test_command": launch.get("test_command") or "",
                "state_path": launch.get("state_path") or "",
            },
        )

    for mission in sorted(missions, key=_mission_sort_key, reverse=True)[:20]:
        mission_id = str(mission.get("mission_id") or "")
        status = str(mission.get("status") or "unknown")
        stop_reason = str(mission.get("stop_reason") or "")
        ev = mission.get("pacer_evidence") if isinstance(mission.get("pacer_evidence"), dict) else {}
        summary = _mission_trace_summary(root, mission)
        detail_parts = [
            f"状态={status}",
            f"停止原因={stop_reason}" if stop_reason else "",
            f"验收={ev.get('verification_verdict')}" if ev.get("verification_verdict") else "",
            f"后端={ev.get('backend') or ev.get('agent')}" if (ev.get("backend") or ev.get("agent")) else "",
        ]
        add(
            kind="mission",
            status=status,
            title=f"mission {mission_id}: {mission.get('objective') or '未命名任务'}",
            detail=" | ".join(part for part in detail_parts if part),
            path=str(summary.get("report_path") or ev.get("log_path") or ""),
            timestamp=str(mission.get("updated_at") or mission.get("created_at") or ""),
            meta={
                **summary,
                "mission_id": mission_id,
                "plan_id": mission.get("plan_id") or "",
                "agent": mission.get("agent") or ev.get("agent") or "",
                "backend": ev.get("backend") or "",
                "model": ev.get("model") or "",
                "verification_command": ev.get("verification_command") or "",
                "log_path": ev.get("log_path") or "",
                "worktree": ev.get("worktree") or "",
            },
        )

    plan_ids_with_missions = {str(m.get("plan_id") or "") for m in missions if m.get("plan_id")}
    for plan in sorted(
        plans,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or item.get("saved_at") or ""),
        reverse=True,
    )[:12]:
        plan_id = str(plan.get("plan_id") or "")
        if not plan_id or plan_id in plan_ids_with_missions:
            continue
        records = load_worker_records(root, plan_id) or []
        latest = records[-1] if records else {}
        backend = latest.get("backend") if isinstance(latest.get("backend"), dict) else {}
        timestamp = str(
            latest.get("recorded_at")
            or plan.get("updated_at")
            or plan.get("created_at")
            or plan.get("saved_at")
            or ""
        )
        add(
            kind="plan",
            status=str(latest.get("status") or plan.get("status") or "planned"),
            title=f"plan {plan_id}: {plan.get('objective') or '未命名计划'}",
            detail=str(latest.get("command") or ""),
            path=str(latest.get("log_path") or plan_dir(root, plan_id)),
            timestamp=timestamp,
            meta={
                "plan_id": plan_id,
                "worker_records": len(records),
                "agent": latest.get("agent") or "",
                "backend": backend.get("name") or "",
                "model": backend.get("model") or "",
            },
        )

    for item in queue[:8]:
        add(
            kind="queue",
            status=str(item.get("status") or "queued"),
            title=f"queue {item.get('mission_id') or ''}: {item.get('objective') or '队列任务'}",
            detail=str(item.get("stop_reason") or ""),
            meta={"mission_id": item.get("mission_id") or "", "agent": item.get("agent") or ""},
        )

    for program in programs[:6]:
        add(
            kind="program",
            status=str(program.get("status") or "program"),
            title=f"program {program.get('program_id') or ''}: {program.get('objective') or '项目托管'}",
            detail=f"{program.get('task_count') or 0} 个任务",
            path=str(program.get("source_file") or ""),
            meta={"program_id": program.get("program_id") or "", "task_count": program.get("task_count") or 0},
        )

    for line in _dashboard_error_tail()[-8:]:
        add(kind="log", status="error", title="dashboard log", detail=line)

    return traces[:60]


def _mission_sort_key(mission: dict[str, Any]) -> str:
    return str(mission.get("updated_at") or mission.get("created_at") or mission.get("mission_id") or "")


def _mission_trace_summary(root: Path, mission: dict[str, Any]) -> dict[str, Any]:
    mission_id = str(mission.get("mission_id") or "").strip()
    if not mission_id:
        return {"rounds": 0, "latest_round": "", "report_exists": False, "report_path": ""}
    mission_dir = root / "missions" / mission_id
    rounds_path = mission_dir / "rounds.jsonl"
    rounds = 0
    latest_round = ""
    if rounds_path.exists():
        try:
            for line in rounds_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                rounds += 1
                try:
                    item = json.loads(line)
                    latest_round = f"{item.get('type') or 'round'}:{item.get('status') or ''}".rstrip(":")
                except json.JSONDecodeError:
                    latest_round = "unparseable"
        except OSError:
            pass
    report_path = mission_dir / "final_report.md"
    return {
        "rounds": rounds,
        "latest_round": latest_round,
        "report_exists": report_path.exists(),
        "report_path": str(report_path) if report_path.exists() else "",
    }


def _dashboard_error_tail(max_lines: int = 40) -> list[str]:
    path = Path.home() / ".pacer" / "logs" / "dashboard.log"
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    except OSError:
        return []


def _mission_elapsed_seconds(root: Path, mission: dict[str, Any]) -> float:
    mission_id = str(mission.get("mission_id") or "")
    if mission_id:
        background_path = root / "missions" / mission_id / "background.json"
        if background_path.exists():
            try:
                payload = json.loads(background_path.read_text(encoding="utf-8"))
                started = _parse_iso_timestamp(str(payload.get("started_at") or ""))
                completed = _parse_iso_timestamp(str(payload.get("completed_at") or ""))
                if started and completed and completed >= started:
                    return round(completed - started, 3)
            except (OSError, ValueError, TypeError):
                pass
    created = _parse_iso_timestamp(str(mission.get("created_at") or ""))
    updated = _parse_iso_timestamp(str(mission.get("updated_at") or ""))
    if created and updated and updated >= created:
        return round(updated - created, 3)
    return 0.0


def _parse_iso_timestamp(value: str) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime

        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


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
