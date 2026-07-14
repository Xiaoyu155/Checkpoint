from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
DEFAULT_MAX_LINE_BYTES = 1_048_576
DEFAULT_MAX_EVENTS_PER_SESSION = 50_000
MAX_IDENTITY_SCAN_BYTES = 1_048_576

_TERMINAL_KINDS = {
    "task_complete": "completed",
    "turn_aborted": "interrupted",
    "shutdown_complete": "completed",
    "terminal": "completed",
}
_TEXT_KEYS = {
    "arguments",
    "content",
    "input",
    "message",
    "output",
    "prompt",
    "reasoning",
    "response",
    "text",
}


@dataclass(frozen=True)
class _TokenSample:
    timestamp: str
    offset: int
    usage: dict[str, int]


@dataclass(frozen=True)
class _SessionIdentity:
    path: Path
    size: int
    modified_ns: int
    baseline_size: int
    baseline_known: bool
    session_id: str
    parent_session_id: str
    cwd: str
    started_at: str


@dataclass
class _ParsedSession:
    path: Path
    relative_path: str
    size: int
    baseline_size: int
    baseline_known: bool = False
    launch_active: bool = False
    session_id: str = ""
    parent_session_id: str = ""
    depth: int = 0
    cwd: str = ""
    started_at: str = ""
    last_event_at: str = ""
    completed_at: str = ""
    provider: str = ""
    model: str = ""
    reasoning_effort: str = ""
    status: str = "active"
    token_samples: list[_TokenSample] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, int] = field(
        default_factory=lambda: {
            "bad_json_lines": 0,
            "truncated_lines": 0,
            "oversized_lines": 0,
            "event_limit_reached": 0,
        }
    )

    @property
    def changed(self) -> bool:
        return self.launch_active


def build_launch_observability(
    *,
    sessions_root: str | Path,
    baseline_files: dict[str, int],
    repo_root: str | Path | None = None,
    started_at: str = "",
    completed_at: str = "",
    baseline_captured_at: str = "",
    launch_id: str = "",
    limit_sessions: int = 100,
) -> dict[str, Any]:
    """Build prompt-free observability for rollout files changed by one launch.

    ``baseline_files`` maps rollout paths to their byte sizes at launch start. A
    missing path is treated as a newly-created session. Paths are discovered
    below ``sessions_root``; entries in the mapping are never opened directly.
    """
    root = _resolve_sessions_root(sessions_root)
    limit = _bounded_limit(limit_sessions, default=100, maximum=5_000)
    captured_at = datetime.now(timezone.utc).isoformat()
    base: dict[str, Any] = {
        "schema_version": 1,
        "launch_id": str(launch_id or ""),
        "status": "no_sessions",
        "started_at": str(started_at or ""),
        "completed_at": str(completed_at or ""),
        "captured_at": captured_at,
        "sessions": [],
        "agents": [],
        "events": [],
        "usage": {
            "raw_ledger": _zero_usage(),
            "usage_samples": [],
            "deduplicated_actual": _zero_usage(),
            "uncached_input_tokens": 0,
            "cache_ratio": 0.0,
            "reasoning_included_in_output": True,
        },
        "compaction_count": 0,
        "attribution_confidence": "none",
        "warnings": [],
        "diagnostics": {
            "bad_json_lines": 0,
            "truncated_lines": 0,
            "oversized_lines": 0,
            "event_limit_reached": 0,
            "skipped_paths": 0,
            "warnings": [],
        },
    }
    if root is None:
        base["status"] = "unavailable"
        base["diagnostics"]["warnings"].append("sessions root is unavailable")
        return base

    baselines, rejected_baselines = _normalize_baselines(root, baseline_files)
    window_started_at = str(baseline_captured_at or started_at or "")
    parsed, skipped_paths = _scan_sessions(
        root,
        baselines,
        limit=limit,
        repo_root=repo_root,
        started_at=window_started_at,
        completed_at=completed_at,
    )
    for session in parsed:
        _apply_launch_window(session, started_at=window_started_at, completed_at=completed_at)
    base["diagnostics"]["skipped_paths"] = skipped_paths + rejected_baselines
    if not parsed:
        return base

    selected, candidate_roots = _select_launch_sessions(parsed, repo_root=repo_root)
    if candidate_roots > 1:
        base["status"] = "ambiguous"
        base["warnings"] = ["multiple changed root rollouts matched this launch"]
        base["diagnostics"]["warnings"] = list(base["warnings"])
        return base
    if not selected:
        base["status"] = "no_match" if repo_root is not None else "no_sessions"
        return base

    selected.sort(key=lambda item: (_timestamp_key(item.started_at), item.relative_path))
    by_id = {item.session_id: item for item in parsed if item.session_id}
    raw_total = _zero_usage()
    deduplicated_total = _zero_usage()
    ledger: list[dict[str, Any]] = []
    session_payloads: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []

    for session in selected:
        own_baseline = _usage_before_offset(session.token_samples, session.baseline_size)
        fork_baseline = _zero_usage()
        baseline_kind = "launch"
        if session.parent_session_id:
            parent = by_id.get(session.parent_session_id)
            if parent is not None:
                fork_baseline = _usage_at_timestamp(parent.token_samples, session.started_at)
            if any(fork_baseline.values()):
                baseline_kind = "fork"
        dedup_baseline = {
            field: max(own_baseline[field], fork_baseline[field]) for field in TOKEN_FIELDS
        }
        raw_previous = dict(own_baseline)
        dedup_previous = dict(dedup_baseline)
        session_raw = _zero_usage()
        session_deduplicated = _zero_usage()
        session_ledger: list[dict[str, Any]] = []
        launch_samples = [sample for sample in session.token_samples if sample.offset > session.baseline_size]

        for sample in launch_samples:
            raw_delta = _monotonic_delta(sample.usage, raw_previous)
            deduplicated_delta = _monotonic_delta(sample.usage, dedup_previous)
            _maximize_usage(raw_previous, sample.usage)
            _maximize_usage(dedup_previous, sample.usage)
            _add_usage(session_raw, raw_delta)
            _add_usage(session_deduplicated, deduplicated_delta)
            entry = {
                "session_id": session.session_id,
                "timestamp": sample.timestamp,
                "cumulative": dict(sample.usage),
                "raw_delta": raw_delta,
                "deduplicated_delta": deduplicated_delta,
                "baseline_kind": baseline_kind,
            }
            ledger.append(entry)
            session_ledger.append(entry)

        token_events = [event for event in session.events if event.get("kind") == "token_count"]
        for event, sample_entry in zip(token_events, session_ledger):
            event["usage_delta"] = _usage_delta_payload(
                sample_entry["raw_delta"],
                sample_entry["deduplicated_delta"],
            )

        _add_usage(raw_total, session_raw)
        _add_usage(deduplicated_total, session_deduplicated)
        timeline.extend(session.events)
        session_payload = _session_payload(
            session,
            own_baseline=own_baseline,
            fork_baseline=fork_baseline,
            raw_actual=session_raw,
            deduplicated_actual=session_deduplicated,
            token_samples=len(launch_samples),
            repo_root=repo_root,
        )
        session_payloads.append(session_payload)
        for key in ("bad_json_lines", "truncated_lines", "oversized_lines", "event_limit_reached"):
            base["diagnostics"][key] += session.diagnostics[key]

    ledger.sort(key=lambda item: (_timestamp_key(str(item.get("timestamp") or "")), str(item.get("session_id") or "")))
    timeline.sort(
        key=lambda item: (
            _timestamp_key(str(item.get("timestamp") or "")),
            str(item.get("session_id") or ""),
            int(item.get("sequence") or 0),
        )
    )
    for event in timeline:
        event.pop("sequence", None)

    base["status"] = "partial" if any(
        int(base["diagnostics"].get(key) or 0) > 0
        for key in ("bad_json_lines", "truncated_lines", "oversized_lines", "event_limit_reached", "skipped_paths")
    ) else "ok"
    uncached = max(0, deduplicated_total["input_tokens"] - deduplicated_total["cached_input_tokens"])
    base["sessions"] = session_payloads
    base["agents"] = [{key: value for key, value in item.items() if key != "events"} for item in session_payloads]
    base["events"] = timeline
    base["usage"] = {
        "raw_ledger": raw_total,
        "usage_samples": ledger,
        "deduplicated_actual": deduplicated_total,
        "uncached_input_tokens": uncached,
        "cache_ratio": round(deduplicated_total["cached_input_tokens"] / deduplicated_total["input_tokens"], 6)
        if deduplicated_total["input_tokens"]
        else 0.0,
        "reasoning_included_in_output": True,
    }
    base["compaction_count"] = sum(1 for event in timeline if event.get("kind") == "compacted")
    base["attribution_confidence"] = "high"
    base["warnings"] = list(base["diagnostics"]["warnings"])
    return base


def build_observability_snapshot(
    codex_home: str | Path | None,
    repo_root: str | Path | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Build a metadata-only snapshot of recent Codex rollout sessions."""
    if codex_home is None:
        configured = os.environ.get("CODEX_HOME")
        home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    else:
        home = Path(codex_home).expanduser()
    return build_launch_observability(
        sessions_root=home / "sessions",
        baseline_files={},
        repo_root=repo_root,
        limit_sessions=limit,
    )


def paginate_timeline(
    timeline: Sequence[dict[str, Any]],
    cursor: str | int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Paginate an already-sanitized timeline using a stable numeric cursor."""
    page_size = _bounded_limit(limit, default=100, maximum=1_000)
    try:
        start = max(0, int(cursor or 0))
    except (TypeError, ValueError, OverflowError):
        start = 0
    total = len(timeline)
    end = min(total, start + page_size)
    return {
        "items": list(timeline[start:end]),
        "cursor": str(start),
        "next_cursor": str(end) if end < total else None,
        "has_more": end < total,
        "total": total,
    }


def read_session_timeline(
    *,
    sessions_root: str | Path,
    session_path: str | Path,
    cursor: str | int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Safely read one rollout timeline, rejecting paths outside sessions root."""
    root = _resolve_sessions_root(sessions_root)
    if root is None:
        raise ValueError("sessions root is unavailable")
    requested = Path(session_path).expanduser()
    if not requested.is_absolute():
        requested = root / requested
    try:
        resolved = requested.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("session path must remain below sessions root") from exc
    if not resolved.is_file() or not resolved.name.startswith("rollout-") or resolved.suffix != ".jsonl":
        raise ValueError("session path is not a rollout JSONL file")
    parsed = _parse_session(resolved, root=root, baseline_size=0)
    return paginate_timeline(parsed.events, cursor=cursor, limit=limit)


def _scan_sessions(
    root: Path,
    baselines: dict[str, int],
    *,
    limit: int,
    repo_root: str | Path | None,
    started_at: str,
    completed_at: str,
) -> tuple[list[_ParsedSession], int]:
    identities: list[_SessionIdentity] = []
    skipped = 0
    try:
        iterator: Iterable[Path] = root.glob("**/rollout-*.jsonl")
        for path in iterator:
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
                if not resolved.is_file():
                    continue
                stat = resolved.stat()
                key = _path_key(resolved)
                identity = _read_session_identity(
                    resolved,
                    size=stat.st_size,
                    modified_ns=stat.st_mtime_ns,
                    baseline_size=baselines.get(key, 0),
                    baseline_known=key in baselines,
                )
                if identity is None:
                    skipped += 1
                    continue
                identities.append(identity)
            except (OSError, RuntimeError, ValueError):
                skipped += 1
    except OSError:
        return [], skipped + 1

    selected = _select_identity_tree(
        identities,
        repo_root=repo_root,
        started_at=started_at,
        completed_at=completed_at,
    )
    selected, over_limit = _limit_identity_trees(selected, limit=limit)
    skipped += over_limit
    selected.sort(key=lambda item: (item.modified_ns, str(item.path)), reverse=True)
    parsed: list[_ParsedSession] = []
    for identity in selected:
        try:
            parsed.append(
                _parse_session(
                    identity.path,
                    root=root,
                    baseline_size=identity.baseline_size,
                    baseline_known=identity.baseline_known,
                )
            )
        except OSError:
            skipped += 1
    return parsed, skipped


def _read_session_identity(
    path: Path,
    *,
    size: int,
    modified_ns: int,
    baseline_size: int,
    baseline_known: bool,
) -> _SessionIdentity | None:
    """Read only a bounded session_meta prefix before rollout attribution."""
    scanned = 0
    try:
        with path.open("rb") as handle:
            while scanned < MAX_IDENTITY_SCAN_BYTES:
                remaining = MAX_IDENTITY_SCAN_BYTES - scanned
                raw_line = handle.readline(min(DEFAULT_MAX_LINE_BYTES, remaining) + 1)
                if not raw_line:
                    return None
                scanned += len(raw_line)
                if len(raw_line) > DEFAULT_MAX_LINE_BYTES or scanned > MAX_IDENTITY_SCAN_BYTES:
                    return None
                if not raw_line.endswith(b"\n"):
                    return None
                try:
                    event = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
                    continue
                if not isinstance(event, dict) or str(event.get("type") or "") != "session_meta":
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                session_id = _safe_scalar(payload.get("session_id") or payload.get("id"), maximum=256)
                if not session_id:
                    return None
                return _SessionIdentity(
                    path=path,
                    size=max(0, int(size)),
                    modified_ns=max(0, int(modified_ns)),
                    baseline_size=min(max(0, int(size)), max(0, int(baseline_size or 0))),
                    baseline_known=bool(baseline_known),
                    session_id=session_id,
                    parent_session_id=_parent_from_payload(payload),
                    cwd=_safe_scalar(payload.get("cwd"), maximum=4_096),
                    started_at=_safe_scalar(event.get("timestamp") or payload.get("timestamp"), maximum=128),
                )
    except OSError:
        return None
    return None


def _select_identity_tree(
    identities: list[_SessionIdentity],
    *,
    repo_root: str | Path | None,
    started_at: str,
    completed_at: str,
) -> list[_SessionIdentity]:
    by_id = {item.session_id: item for item in identities if item.session_id}
    expected_repo = _normalized_path(repo_root) if repo_root is not None else ""
    selected_ids: set[str] = set()

    for identity in identities:
        if identity.size <= identity.baseline_size:
            continue
        if not identity.baseline_known and not _timestamp_in_window(
            identity.started_at,
            started_at=started_at,
            completed_at=completed_at,
            require_timestamp=bool(started_at or completed_at),
        ):
            continue
        lineage = _identity_lineage(identity, by_id)
        if not lineage:
            continue
        root_identity = lineage[-1]
        if root_identity.parent_session_id:
            continue
        if repo_root is not None and _normalized_path(root_identity.cwd) != expected_repo:
            continue
        selected_ids.update(item.session_id for item in lineage)

    return [item for item in identities if item.session_id in selected_ids]


def _identity_lineage(
    identity: _SessionIdentity,
    by_id: dict[str, _SessionIdentity],
) -> list[_SessionIdentity]:
    lineage = [identity]
    seen = {identity.session_id}
    current = identity
    while current.parent_session_id:
        if current.parent_session_id in seen:
            return []
        parent = by_id.get(current.parent_session_id)
        if parent is None:
            return []
        lineage.append(parent)
        seen.add(parent.session_id)
        current = parent
    return lineage


def _limit_identity_trees(
    identities: list[_SessionIdentity],
    *,
    limit: int,
) -> tuple[list[_SessionIdentity], int]:
    if not identities:
        return [], 0
    by_id = {item.session_id: item for item in identities}
    trees: dict[str, list[_SessionIdentity]] = {}
    for identity in identities:
        lineage = _identity_lineage(identity, by_id)
        if not lineage:
            continue
        trees.setdefault(lineage[-1].session_id, []).append(identity)
    ordered = sorted(
        trees.values(),
        key=lambda tree: max((item.modified_ns for item in tree), default=0),
        reverse=True,
    )
    selected: list[_SessionIdentity] = []
    over_limit = 0
    for tree in ordered:
        remaining = max(0, limit - len(selected))
        if not selected:
            selected.extend(tree)
            over_limit += max(0, len(tree) - remaining)
        elif len(tree) <= remaining:
            selected.extend(tree)
        else:
            over_limit += len(tree)
    return selected, over_limit


def _parse_session(
    path: Path,
    *,
    root: Path,
    baseline_size: int,
    baseline_known: bool = False,
) -> _ParsedSession:
    size = path.stat().st_size
    safe_baseline = min(size, max(0, int(baseline_size or 0)))
    session = _ParsedSession(
        path=path,
        relative_path=path.relative_to(root).as_posix(),
        size=size,
        baseline_size=safe_baseline,
        baseline_known=baseline_known,
    )
    offset = 0
    sequence = 0
    with path.open("rb") as handle:
        while True:
            start_offset = offset
            raw_line = handle.readline(DEFAULT_MAX_LINE_BYTES + 1)
            if not raw_line:
                break
            offset += len(raw_line)
            if len(raw_line) > DEFAULT_MAX_LINE_BYTES:
                session.diagnostics["oversized_lines"] += 1
                while raw_line and not raw_line.endswith(b"\n"):
                    raw_line = handle.readline(DEFAULT_MAX_LINE_BYTES + 1)
                    offset += len(raw_line)
                continue
            if not raw_line.endswith(b"\n"):
                session.diagnostics["truncated_lines"] += 1
                break
            try:
                event = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
                session.diagnostics["bad_json_lines"] += 1
                continue
            if not isinstance(event, dict):
                session.diagnostics["bad_json_lines"] += 1
                continue
            sequence += 1
            timestamp = _safe_scalar(event.get("timestamp"), maximum=128)
            session.last_event_at = timestamp or session.last_event_at
            event_type = _safe_scalar(event.get("type"), maximum=80)
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            after_baseline = start_offset >= safe_baseline

            if event_type == "session_meta":
                _apply_session_meta(session, payload, timestamp)
            elif event_type == "turn_context":
                session.model = _safe_scalar(payload.get("model"), maximum=160) or session.model
                session.reasoning_effort = _safe_scalar(payload.get("effort"), maximum=80) or session.reasoning_effort

            payload_type = _safe_scalar(payload.get("type"), maximum=80)
            if event_type == "event_msg" and payload_type == "token_count":
                usage = _token_usage(payload)
                session.token_samples.append(_TokenSample(timestamp=timestamp, offset=offset, usage=usage))

            terminal_kind = payload_type if event_type == "event_msg" else event_type
            if terminal_kind in _TERMINAL_KINDS:
                session.status = _TERMINAL_KINDS[terminal_kind]
                if after_baseline:
                    session.completed_at = timestamp

            if after_baseline:
                if len(session.events) >= DEFAULT_MAX_EVENTS_PER_SESSION:
                    session.diagnostics["event_limit_reached"] = 1
                    continue
                session.events.append(
                    _sanitize_event(
                        session_id=session.session_id,
                        event_type=event_type,
                        payload=payload,
                        timestamp=timestamp,
                        sequence=sequence,
                    )
                )
    if not session.started_at:
        session.started_at = session.last_event_at
    return session


def _apply_session_meta(session: _ParsedSession, payload: dict[str, Any], timestamp: str) -> None:
    session.session_id = _safe_scalar(payload.get("session_id") or payload.get("id"), maximum=256)
    session.cwd = _safe_scalar(payload.get("cwd"), maximum=4_096)
    session.started_at = timestamp or _safe_scalar(payload.get("timestamp"), maximum=128)
    session.provider = _safe_scalar(payload.get("model_provider"), maximum=160)
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    subagent = source.get("subagent") if isinstance(source.get("subagent"), dict) else {}
    spawn = subagent.get("thread_spawn") if isinstance(subagent.get("thread_spawn"), dict) else {}
    session.parent_session_id = _safe_scalar(spawn.get("parent_thread_id"), maximum=256)
    session.depth = _integer(spawn.get("depth"))


def _sanitize_event(
    *,
    session_id: str,
    event_type: str,
    payload: dict[str, Any],
    timestamp: str,
    sequence: int,
) -> dict[str, Any]:
    payload_type = _safe_scalar(payload.get("type"), maximum=80)
    kind = payload_type or event_type or "unknown"
    result: dict[str, Any] = {
        "session_id": session_id,
        "timestamp": timestamp,
        "category": event_type or "unknown",
        "kind": kind,
        "sequence": sequence,
    }
    if event_type == "session_meta":
        result["summary"] = {"role": "subagent" if _parent_from_payload(payload) else "root"}
    elif event_type == "turn_context":
        result["summary"] = {
            "model": _safe_scalar(payload.get("model"), maximum=160),
            "reasoning_effort": _safe_scalar(payload.get("effort"), maximum=80),
        }
    elif event_type == "event_msg" and payload_type == "token_count":
        result["summary"] = {"usage": _token_usage(payload)}
    elif event_type == "compacted" or kind == "compacted":
        result["summary"] = {"window_id": _safe_scalar(payload.get("window_id"), maximum=256)}
    elif event_type == "response_item" or kind in {
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "mcp_tool_call",
        "mcp_tool_call_end",
    }:
        result["summary"] = _response_item_summary(payload, kind)
    elif kind in _TERMINAL_KINDS:
        result["summary"] = {"status": _TERMINAL_KINDS[kind]}
    else:
        result["summary"] = _content_summary(payload)
    duration_ms = _duration_milliseconds(payload)
    if duration_ms is not None:
        result["duration_ms"] = duration_ms
    return result


def _response_item_summary(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    invocation = payload.get("invocation") if isinstance(payload.get("invocation"), dict) else {}
    server = _safe_scalar(invocation.get("server"), maximum=128)
    tool = _safe_scalar(invocation.get("tool"), maximum=256)
    summary: dict[str, Any] = {
        "item_type": kind,
        "name": _safe_scalar(payload.get("name") or payload.get("tool_name") or tool, maximum=256),
        "call_id": _safe_scalar(payload.get("call_id") or payload.get("id"), maximum=256),
    }
    if server:
        summary["server"] = server
    plugin_id = _safe_scalar(payload.get("plugin_id"), maximum=256)
    if plugin_id:
        summary["plugin_id"] = plugin_id
    status = _safe_scalar(payload.get("status"), maximum=80)
    if status:
        summary["status"] = status
    text_meta = _content_summary(payload)
    if text_meta:
        summary["content"] = text_meta
    return summary


def _duration_milliseconds(payload: dict[str, Any]) -> float | None:
    direct = payload.get("duration_ms")
    if direct is not None and not isinstance(direct, bool):
        try:
            return round(max(0.0, float(direct)), 3)
        except (TypeError, ValueError, OverflowError):
            pass
    duration = payload.get("duration") if isinstance(payload.get("duration"), dict) else {}
    try:
        seconds = max(0.0, float(duration.get("secs") or 0.0))
        nanos = max(0.0, float(duration.get("nanos") or 0.0))
    except (TypeError, ValueError, OverflowError):
        return None
    if not duration:
        return None
    return round(seconds * 1000.0 + nanos / 1_000_000.0, 3)


def _content_summary(value: Any) -> dict[str, Any]:
    fragments: list[str] = []

    def visit(item: Any, *, text_context: bool = False) -> None:
        if isinstance(item, str):
            if text_context:
                fragments.append(item)
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                visit(nested, text_context=text_context or str(key).lower() in _TEXT_KEYS)
            return
        if isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested, text_context=text_context)

    visit(value)
    if not fragments:
        return {}
    joined = "\n".join(fragments)
    return {
        "char_count": sum(len(fragment) for fragment in fragments),
        "sha256": hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest(),
        "redacted": True,
    }


def _apply_launch_window(
    session: _ParsedSession,
    *,
    started_at: str,
    completed_at: str,
) -> None:
    if not session.baseline_known and not _timestamp_in_window(
        session.started_at,
        started_at=started_at,
        completed_at=completed_at,
        require_timestamp=bool(started_at or completed_at),
    ):
        session.events = []
        session.token_samples = []
        session.launch_active = False
        return

    session.events = [
        event
        for event in session.events
        if _timestamp_in_window(
            str(event.get("timestamp") or ""),
            started_at=started_at,
            completed_at=completed_at,
        )
    ]
    session.token_samples = [
        sample
        for sample in session.token_samples
        if sample.offset <= session.baseline_size
        or _timestamp_in_window(
            sample.timestamp,
            started_at=started_at,
            completed_at=completed_at,
        )
    ]
    session.launch_active = bool(session.events)
    session.status = "active"
    session.completed_at = ""
    session.last_event_at = session.events[-1]["timestamp"] if session.events else session.started_at
    for event in session.events:
        kind = str(event.get("kind") or "")
        if kind in _TERMINAL_KINDS:
            session.status = _TERMINAL_KINDS[kind]
            session.completed_at = str(event.get("timestamp") or "")


def _session_payload(
    session: _ParsedSession,
    *,
    own_baseline: dict[str, int],
    fork_baseline: dict[str, int],
    raw_actual: dict[str, int],
    deduplicated_actual: dict[str, int],
    token_samples: int,
    repo_root: str | Path | None,
) -> dict[str, Any]:
    tool_kinds = {
        "function_call",
        "custom_tool_call",
        "mcp_tool_call",
        "mcp_tool_call_end",
    }
    context_turns = sum(1 for event in session.events if event.get("category") == "turn_context")
    turn_count = context_turns or sum(1 for event in session.events if event.get("kind") == "task_started")
    tool_count = sum(1 for event in session.events if event.get("kind") in tool_kinds)
    return {
        "session_id": session.session_id,
        "parent_session_id": session.parent_session_id,
        "role": "subagent" if session.parent_session_id else "root",
        "depth": session.depth,
        "started_at": session.started_at,
        "completed_at": session.completed_at,
        "last_event_at": session.last_event_at,
        "status": session.status,
        "provider": session.provider,
        "model": session.model,
        "reasoning_effort": session.reasoning_effort,
        "repo_match": repo_root is None or _normalized_path(session.cwd) == _normalized_path(repo_root),
        "changed": session.changed,
        "baseline_bytes": session.baseline_size,
        "observed_bytes": session.size,
        "usage": {
            "launch_baseline": own_baseline,
            "fork_baseline": fork_baseline,
            "raw_ledger": raw_actual,
            "deduplicated_actual": deduplicated_actual,
            "token_samples": token_samples,
        },
        "turn_count": turn_count,
        "tool_count": tool_count,
        "events": list(session.events),
        "diagnostics": dict(session.diagnostics),
    }


def _select_launch_sessions(
    sessions: list[_ParsedSession],
    *,
    repo_root: str | Path | None,
) -> tuple[list[_ParsedSession], int]:
    changed_ids = {item.session_id for item in sessions if item.changed and item.session_id}
    if not changed_ids:
        return [], 0
    by_id = {item.session_id: item for item in sessions if item.session_id}
    selected_ids = set(changed_ids)
    for thread_id in list(changed_ids):
        current = by_id.get(thread_id)
        seen: set[str] = set()
        while current is not None and current.parent_session_id and current.parent_session_id not in seen:
            seen.add(current.parent_session_id)
            selected_ids.add(current.parent_session_id)
            current = by_id.get(current.parent_session_id)

    if repo_root is not None:
        expected = _normalized_path(repo_root)
        allowed_roots = {
            item.session_id
            for item in sessions
            if item.session_id and not item.parent_session_id and _normalized_path(item.cwd) == expected
        }
        allowed = set(allowed_roots)
        changed = True
        while changed:
            changed = False
            for item in sessions:
                if item.session_id and item.parent_session_id in allowed and item.session_id not in allowed:
                    allowed.add(item.session_id)
                    changed = True
        selected_ids &= allowed
    selected = [item for item in sessions if item.session_id in selected_ids]
    selected_by_id = {item.session_id: item for item in selected}
    root_ids: set[str] = set()
    for thread_id in changed_ids & selected_ids:
        current = selected_by_id.get(thread_id)
        seen: set[str] = set()
        while current is not None and current.parent_session_id and current.parent_session_id not in seen:
            seen.add(current.parent_session_id)
            parent = selected_by_id.get(current.parent_session_id)
            if parent is None:
                break
            current = parent
        if current is not None and not current.parent_session_id:
            root_ids.add(current.session_id)
    return selected, len(root_ids)


def _normalize_baselines(root: Path, baseline_files: dict[str, int]) -> tuple[dict[str, int], int]:
    normalized: dict[str, int] = {}
    rejected = 0
    for raw_path, raw_size in (baseline_files or {}).items():
        try:
            path = Path(str(raw_path)).expanduser()
            if not path.is_absolute():
                path = root / path
            resolved = path.resolve(strict=False)
            resolved.relative_to(root)
            normalized[_path_key(resolved)] = max(0, int(raw_size or 0))
        except (OSError, RuntimeError, TypeError, ValueError, OverflowError):
            rejected += 1
    return normalized, rejected


def _resolve_sessions_root(value: str | Path) -> Path | None:
    try:
        root = Path(value).expanduser().resolve(strict=True)
        if not root.is_dir():
            return None
        return root
    except (OSError, RuntimeError, ValueError):
        return None


def _token_usage(payload: dict[str, Any]) -> dict[str, int]:
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    raw = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
    usage = {field: _integer(raw.get(field)) for field in TOKEN_FIELDS}
    if not usage["total_tokens"]:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def _usage_before_offset(samples: list[_TokenSample], offset: int) -> dict[str, int]:
    result = _zero_usage()
    for sample in samples:
        if sample.offset <= offset:
            _maximize_usage(result, sample.usage)
    return result


def _usage_at_timestamp(samples: list[_TokenSample], timestamp: str) -> dict[str, int]:
    result = _zero_usage()
    for sample in samples:
        if timestamp and sample.timestamp and _timestamp_key(sample.timestamp) > _timestamp_key(timestamp):
            continue
        _maximize_usage(result, sample.usage)
    return result


def _monotonic_delta(current: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    return {field: max(0, current[field] - previous[field]) for field in TOKEN_FIELDS}


def _usage_delta_payload(raw: dict[str, int], deduplicated: dict[str, int]) -> dict[str, Any]:
    uncached = max(0, deduplicated["input_tokens"] - deduplicated["cached_input_tokens"])
    return {
        "raw_ledger": dict(raw),
        "deduplicated_actual": dict(deduplicated),
        "uncached_input_tokens": uncached,
        "cache_ratio": round(deduplicated["cached_input_tokens"] / deduplicated["input_tokens"], 6)
        if deduplicated["input_tokens"]
        else 0.0,
        "reasoning_included_in_output": True,
    }


def _maximize_usage(target: dict[str, int], sample: dict[str, int]) -> None:
    for token_field in TOKEN_FIELDS:
        target[token_field] = max(target[token_field], sample[token_field])


def _add_usage(target: dict[str, int], delta: dict[str, int]) -> None:
    for token_field in TOKEN_FIELDS:
        target[token_field] += delta[token_field]


def _zero_usage() -> dict[str, int]:
    return {field: 0 for field in TOKEN_FIELDS}


def _parent_from_payload(payload: dict[str, Any]) -> str:
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    subagent = source.get("subagent") if isinstance(source.get("subagent"), dict) else {}
    spawn = subagent.get("thread_spawn") if isinstance(subagent.get("thread_spawn"), dict) else {}
    return _safe_scalar(spawn.get("parent_thread_id"), maximum=256)


def _safe_scalar(value: Any, *, maximum: int) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value)[:maximum]


def _integer(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _bounded_limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        return min(maximum, max(1, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _normalized_path(value: str | Path) -> str:
    try:
        return os.path.normcase(str(Path(value).expanduser().resolve(strict=False)))
    except (OSError, RuntimeError, ValueError):
        return os.path.normcase(str(value))


def _timestamp_in_window(
    value: str,
    *,
    started_at: str,
    completed_at: str,
    require_timestamp: bool = False,
) -> bool:
    timestamp = str(value or "")
    if not timestamp:
        return not require_timestamp
    key = _timestamp_key(timestamp)
    if started_at and key < _timestamp_key(started_at):
        return False
    if completed_at and key > _timestamp_key(completed_at):
        return False
    return True


def _timestamp_key(value: str) -> tuple[int, str]:
    text = str(value or "")
    if not text:
        return (1, "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (0, parsed.astimezone(timezone.utc).isoformat())
    except (ValueError, OverflowError):
        return (0, text)
