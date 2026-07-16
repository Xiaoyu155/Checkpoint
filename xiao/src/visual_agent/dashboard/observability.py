"""Safe, launch-scoped observability payloads for the local Pacer dashboard."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..pacer_pillars import assess_five_pillars, assess_pillar


_LAUNCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_LAUNCHES = 50
_MAX_TIMELINE_PAGE = 200
_DETAIL_CACHE_TTL_SECONDS = 1.0
_DETAIL_CACHE_LOCK = threading.Lock()
_DETAIL_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any], dict[str, Any]]] = {}


class ObservabilityRequestError(ValueError):
    """A client-visible observability request error with an HTTP status."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = int(status)


def list_observability_launches(
    workspace_root: str | Path,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """List Pacer launches without scanning Codex rollout bodies."""
    root = Path(workspace_root).expanduser().resolve()
    selected_limit = _bounded_integer(limit, name="limit", minimum=1, maximum=_MAX_LAUNCHES)
    launch_dir = root / "pacer_native" / "launches"
    rows: list[dict[str, Any]] = []
    try:
        candidates = sorted(
            (
                path
                for path in launch_dir.glob("*.json")
                if not path.name.endswith(".rollout-baseline.json")
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        candidates = []
    for path in candidates:
        payload = _read_json(path)
        launch_id = str(payload.get("launch_id") or "")
        if not _valid_identifier(launch_id, _LAUNCH_ID_RE) or path.stem != launch_id:
            continue
        rows.append(_launch_summary(payload))
        if len(rows) >= selected_limit:
            break
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "launches": rows,
        "count": len(rows),
    }


def get_observability_launch(
    workspace_root: str | Path,
    launch_id: str,
) -> dict[str, Any]:
    """Build one launch detail from its immutable baseline and current rollouts."""
    manifest, rollout = _load_launch_rollout(workspace_root, launch_id)
    launch = _launch_summary(manifest, rollout=rollout)
    raw_sessions = rollout.get("sessions") if isinstance(rollout.get("sessions"), list) else []
    raw_agents = rollout.get("agents") if isinstance(rollout.get("agents"), list) else []
    sessions = [_public_session(item) for item in raw_sessions if isinstance(item, dict)]
    agents = [_public_session(item) for item in raw_agents if isinstance(item, dict)]
    return {
        "ok": True,
        "launch": launch,
        "sessions": sessions,
        "agents": agents,
        "evidence": _pillar_evidence(manifest),
        "warnings": list(rollout.get("warnings") or []),
    }


def get_observability_timeline(
    workspace_root: str | Path,
    *,
    launch_id: str,
    session_id: str,
    cursor: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Return a bounded metadata-only page from one launch-owned session."""
    selected_session = _validated_identifier(session_id, _SESSION_ID_RE, name="session_id")
    selected_cursor = _bounded_integer(cursor, name="cursor", minimum=0, maximum=10_000_000)
    selected_limit = _bounded_integer(limit, name="limit", minimum=1, maximum=_MAX_TIMELINE_PAGE)
    manifest, rollout = _load_launch_rollout(workspace_root, launch_id)
    sessions = rollout.get("sessions") if isinstance(rollout.get("sessions"), list) else []
    session = next(
        (item for item in sessions if str(item.get("session_id") or "") == selected_session),
        None,
    )
    if session is None:
        raise ObservabilityRequestError("该 session 不属于指定的 Pacer launch。", status=404)
    raw_events = rollout.get("events") if isinstance(rollout.get("events"), list) else []
    events = [
        _public_event(item, index=index)
        for index, item in enumerate(raw_events)
        if isinstance(item, dict) and str(item.get("session_id") or "") == selected_session
    ]
    total = len(events)
    page = events[selected_cursor : selected_cursor + selected_limit]
    next_cursor = selected_cursor + len(page)
    if next_cursor >= total:
        next_cursor = None
    return {
        "ok": True,
        "launch_id": str(manifest.get("launch_id") or ""),
        "session_id": selected_session,
        "events": page,
        "cursor": selected_cursor,
        "next_cursor": next_cursor,
        "total": total,
    }


def _load_launch_rollout(
    workspace_root: str | Path,
    launch_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(workspace_root).expanduser().resolve()
    selected_id = _validated_identifier(launch_id, _LAUNCH_ID_RE, name="launch_id")
    configured_home = str(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve())
    cache_key = (os.path.normcase(str(root)), selected_id, os.path.normcase(configured_home))
    now = time.monotonic()
    with _DETAIL_CACHE_LOCK:
        cached = _DETAIL_CACHE.get(cache_key)
        if cached is not None and cached[0] >= now:
            return cached[1], cached[2]

    manifest_path = root / "pacer_native" / "launches" / f"{selected_id}.json"
    baseline_path = root / "pacer_native" / "launches" / f"{selected_id}.rollout-baseline.json"
    manifest = _read_json(manifest_path)
    if not manifest or str(manifest.get("launch_id") or "") != selected_id:
        raise ObservabilityRequestError("找不到该 Pacer launch。", status=404)
    rollout = _build_rollout_detail(manifest, _read_json(baseline_path))
    with _DETAIL_CACHE_LOCK:
        _DETAIL_CACHE[cache_key] = (now + _DETAIL_CACHE_TTL_SECONDS, manifest, rollout)
        if len(_DETAIL_CACHE) > 64:
            expired = [key for key, value in _DETAIL_CACHE.items() if value[0] < now]
            for key in expired or [next(iter(_DETAIL_CACHE))]:
                _DETAIL_CACHE.pop(key, None)
    return manifest, rollout


def _build_rollout_detail(manifest: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    if not baseline:
        return {
            "status": "baseline_unavailable",
            "usage": _manifest_usage(manifest),
            "sessions": [],
            "agents": [],
            "warnings": ["该 launch 没有可用的 rollout baseline；仅展示结束时聚合账。"],
        }
    sessions_root = _trusted_sessions_root(baseline.get("sessions_root"))
    files = baseline.get("files") if isinstance(baseline.get("files"), dict) else {}
    try:
        from ..rollout_observability import build_launch_observability

        return build_launch_observability(
            sessions_root=sessions_root,
            baseline_files={str(key): max(0, int(value)) for key, value in files.items()},
            repo_root=str(manifest.get("project_root") or manifest.get("repo_root") or ""),
            started_at=str(manifest.get("started_at") or baseline.get("captured_at") or ""),
            completed_at=str(manifest.get("completed_at") or ""),
            baseline_captured_at=str(baseline.get("captured_at") or ""),
            launch_id=str(manifest.get("launch_id") or ""),
        )
    except (OSError, TypeError, ValueError) as exc:
        return {
            "status": "rollout_unavailable",
            "usage": _manifest_usage(manifest),
            "sessions": [],
            "agents": [],
            "warnings": [f"rollout 明细不可用：{type(exc).__name__}"],
        }


def _launch_summary(payload: dict[str, Any], *, rollout: dict[str, Any] | None = None) -> dict[str, Any]:
    telemetry = payload.get("rollout_telemetry") if isinstance(payload.get("rollout_telemetry"), dict) else {}
    detail = rollout if isinstance(rollout, dict) else {}
    detail_status = str(detail.get("status") or "")
    usage = (
        _public_usage(detail.get("usage"))
        if detail_status in {"ok", "partial"} and isinstance(detail.get("usage"), dict)
        else _manifest_usage(payload)
    )
    sessions = detail.get("sessions") if isinstance(detail.get("sessions"), list) else []
    agents = detail.get("agents") if isinstance(detail.get("agents"), list) else []
    runtime = telemetry.get("runtime") if isinstance(telemetry.get("runtime"), dict) else {}
    project_root = str(payload.get("project_root") or payload.get("repo_root") or "")
    project_name = Path(project_root).name if project_root else ""
    compactions = telemetry.get("compactions") if isinstance(telemetry.get("compactions"), dict) else {}
    return {
        "launch_id": str(payload.get("launch_id") or ""),
        "status": str(payload.get("status") or "unknown"),
        "started_at": str(payload.get("started_at") or ""),
        "completed_at": str(payload.get("completed_at") or ""),
        "elapsed_seconds": _number(payload.get("elapsed_seconds")),
        "project_name": project_name,
        "runtime": {
            "provider": str(runtime.get("provider") or ""),
            "model": str(runtime.get("model") or ""),
            "reasoning_effort": str(runtime.get("reasoning_effort") or ""),
        },
        "usage": usage,
        "session_count": len(sessions) if rollout is not None else int(telemetry.get("source_files") or 0),
        "agent_count": len(agents) if rollout is not None else int((telemetry.get("agents") or {}).get("total") or 0),
        "compaction_count": int(detail.get("compaction_count") or compactions.get("count") or 0),
        "attribution_confidence": str(
            detail.get("attribution_confidence") or telemetry.get("attribution_confidence") or "none"
        ),
        "detail_status": str(detail_status or telemetry.get("status") or "unavailable"),
        "five_pillars_assessment": assess_five_pillars(payload),
    }


def _manifest_usage(payload: dict[str, Any]) -> dict[str, Any]:
    telemetry = payload.get("rollout_telemetry") if isinstance(payload.get("rollout_telemetry"), dict) else {}
    actual = _normalized_usage(telemetry.get("usage"))
    input_tokens = actual["input_tokens"]
    cached = actual["cached_input_tokens"]
    return {
        "raw_ledger": None,
        "raw_ledger_status": "available_after_detail_load",
        "deduplicated_actual": actual,
        "uncached_input_tokens": max(0, input_tokens - cached),
        "cache_ratio": round(cached / input_tokens, 6) if input_tokens else 0.0,
        "reasoning_included_in_output": True,
    }


def _public_usage(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    raw_value = source.get("raw_ledger")
    actual_value = source.get("deduplicated_actual")
    raw = _normalized_usage(raw_value) if isinstance(raw_value, dict) else None
    actual = _normalized_usage(actual_value)
    input_tokens = actual["input_tokens"]
    cached = actual["cached_input_tokens"]
    return {
        "raw_ledger": raw,
        "raw_ledger_status": "available" if raw is not None else str(source.get("raw_ledger_status") or "unavailable"),
        "deduplicated_actual": actual,
        "uncached_input_tokens": max(0, input_tokens - cached),
        "cache_ratio": round(cached / input_tokens, 6) if input_tokens else 0.0,
        "reasoning_included_in_output": True,
    }


def _public_session(value: dict[str, Any]) -> dict[str, Any]:
    usage = _public_usage(value.get("usage"))
    events = value.get("events") if isinstance(value.get("events"), list) else []
    return {
        "session_id": str(value.get("session_id") or ""),
        "parent_session_id": str(value.get("parent_session_id") or ""),
        "role": str(value.get("role") or "session"),
        "depth": _integer(value.get("depth")),
        "status": str(value.get("status") or "unknown"),
        "started_at": str(value.get("started_at") or ""),
        "completed_at": str(value.get("completed_at") or ""),
        "provider": str(value.get("provider") or ""),
        "model": str(value.get("model") or ""),
        "reasoning_effort": str(value.get("reasoning_effort") or ""),
        "usage": usage,
        "turn_count": _integer(value.get("turn_count")),
        "tool_count": _integer(value.get("tool_count")),
        "event_count": len(events),
    }


def _public_event(value: dict[str, Any], *, index: int) -> dict[str, Any]:
    session_id = str(value.get("session_id") or "")
    timestamp = str(value.get("timestamp") or "")
    kind = str(value.get("kind") or value.get("category") or "event")[:80]
    summary = value.get("summary") if isinstance(value.get("summary"), dict) else {}
    label = str(summary.get("name") or summary.get("item_type") or kind)[:160]
    status = str(summary.get("status") or value.get("status") or "recorded")[:80]
    identifier = hashlib.sha256(
        f"{session_id}\0{timestamp}\0{kind}\0{index}".encode("utf-8", errors="replace")
    ).hexdigest()[:20]
    event = {
        "event_id": identifier,
        "timestamp": timestamp,
        "kind": kind,
        "category": str(value.get("category") or "event")[:80],
        "label": label,
        "status": status,
    }
    duration = value.get("duration_ms")
    if duration is not None:
        event["duration_ms"] = _number(duration)
    usage_delta = value.get("usage_delta")
    if isinstance(usage_delta, dict):
        event["usage_delta"] = _public_usage(usage_delta)
    preview: dict[str, Any] = {}
    content = summary.get("content") if isinstance(summary.get("content"), dict) else {}
    if content:
        preview["content"] = {
            "char_count": _integer(content.get("char_count")),
            "sha256": str(content.get("sha256") or "")[:64],
            "redacted": True,
        }
    call_id = str(summary.get("call_id") or "")
    if call_id:
        preview["call_id"] = call_id[:128]
    window_id = str(summary.get("window_id") or "")
    if window_id:
        preview["window_id"] = window_id[:128]
    if preview:
        event["safe_preview"] = preview
    return event


def _normalized_usage(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return {
        "input_tokens": _integer(source.get("input_tokens")),
        "cached_input_tokens": _integer(source.get("cached_input_tokens")),
        "output_tokens": _integer(source.get("output_tokens")),
        "reasoning_output_tokens": _integer(source.get("reasoning_output_tokens")),
        "total_tokens": _integer(source.get("total_tokens")),
    }


def _pillar_evidence(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    pillars = manifest.get("pillars") if isinstance(manifest.get("pillars"), dict) else {}
    labels = {
        "routing": "路由",
        "memory": "本地记忆",
        "managed": "托管开发",
        "acceptance": "真实验收",
        "dogfood": "狗粮开发",
    }
    evidence: list[dict[str, Any]] = []
    for kind, label in labels.items():
        item = pillars.get(kind) if isinstance(pillars.get(kind), dict) else {}
        assessment = assess_pillar(kind, item)
        evidence.append(
            {
                "kind": kind,
                "status": str(assessment.get("status") or "indeterminate"),
                "label": label,
                "detail": str(item.get("state") or "no_evidence"),
                "passed": bool(assessment.get("passed")),
                "available": bool(assessment.get("available")),
                "adequacy": str(assessment.get("adequacy") or "unknown"),
                "reason_codes": [str(code) for code in assessment.get("reason_codes") or []],
                "assessment": assessment,
                "timestamp": str(manifest.get("completed_at") or manifest.get("started_at") or ""),
            }
        )
    return evidence


def _trusted_sessions_root(value: Any) -> Path:
    requested = Path(str(value or "")).expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    codex_home = (Path(configured).expanduser() if configured else Path.home() / ".codex").resolve()
    expected = (codex_home / "sessions").resolve()
    if os.path.normcase(str(requested)) != os.path.normcase(str(expected)):
        raise ObservabilityRequestError("rollout baseline 指向非受信任 sessions 目录。", status=403)
    return expected


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _valid_identifier(value: str, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.fullmatch(str(value or "")))


def _validated_identifier(value: Any, pattern: re.Pattern[str], *, name: str) -> str:
    selected = str(value or "")
    if not _valid_identifier(selected, pattern):
        raise ObservabilityRequestError(f"{name} 格式无效。", status=400)
    return selected


def _bounded_integer(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    try:
        selected = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ObservabilityRequestError(f"{name} 必须是整数。", status=400) from exc
    if selected < minimum or selected > maximum:
        raise ObservabilityRequestError(f"{name} 必须在 {minimum} 到 {maximum} 之间。", status=400)
    return selected


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _number(value: Any) -> float | None:
    try:
        return round(max(0.0, float(value)), 3)
    except (TypeError, ValueError, OverflowError):
        return None
