"""Durable, task-scoped memory for Pacer-managed work.

The project-memory index is a derived and advisory view.  This module is the
small authoritative journal that must exist for every Pacer launch: the JSONL
log keeps the complete append-only record and the summary is a compact,
recoverable view for prompt injection and restart handling.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import portalocker

from .security import scrub_secrets


TASK_MEMORY_SCHEMA_VERSION = 1
TASK_MEMORY_MAX_RECENT_EVENTS = 64
TASK_MEMORY_COMPACT_EVERY = 128
TASK_MEMORY_LOCK_TIMEOUT_SECONDS = 5.0
_TASK_MEMORY_THREAD_LOCK = threading.RLock()
_MEMORY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class TaskMemoryError(RuntimeError):
    """Raised when the mandatory Pacer task journal cannot be persisted."""


def task_memory_id(*, launch_id: str = "", scope: str = "") -> str:
    raw = str(launch_id or scope or "pacer").strip()
    if _MEMORY_ID_PATTERN.fullmatch(raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    prefix = "launch" if launch_id else "scope"
    return f"{prefix}-{digest}"


def task_memory_paths(workspace_root: str | Path, memory_id: str) -> tuple[Path, Path]:
    workspace = Path(workspace_root).expanduser().resolve()
    normalized = task_memory_id(scope=memory_id)
    root = workspace / "pacer_native" / "task-memory"
    return root / f"{normalized}.jsonl", root / f"{normalized}.summary.json"


def initialize_task_memory(
    workspace_root: str | Path,
    *,
    memory_id: str,
    goal: str = "",
    repo_root: str | Path | None = None,
    launch_id: str = "",
    source: str = "pacer_launch",
) -> dict[str, Any]:
    """Create and durably mark a task journal before Pacer starts work."""

    log_path, summary_path = task_memory_paths(workspace_root, memory_id)
    workspace = Path(workspace_root).expanduser().resolve()
    normalized_id = task_memory_id(scope=memory_id)
    try:
        with _memory_lock(workspace):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if not summary_path.exists():
                log_path.touch(exist_ok=True)
                summary = _new_summary(
                    memory_id=normalized_id,
                    goal=goal,
                    repo_root=repo_root,
                    launch_id=launch_id,
                    source=source,
                    log_path=log_path,
                    summary_path=summary_path,
                )
                _atomic_write_json(summary_path, summary)
            elif not log_path.exists():
                # A summary without its journal is not a valid memory store.
                raise TaskMemoryError("task memory summary exists but its journal is missing")
            return _read_summary(summary_path)
    except TaskMemoryError:
        raise
    except (OSError, portalocker.exceptions.LockException, json.JSONDecodeError) as exc:
        raise TaskMemoryError(f"task memory initialization failed: {type(exc).__name__}: {exc}") from exc


def append_task_memory_event(
    workspace_root: str | Path,
    *,
    memory_id: str,
    event_type: str,
    data: dict[str, Any] | None = None,
    goal: str = "",
    repo_root: str | Path | None = None,
    launch_id: str = "",
) -> dict[str, Any]:
    """Append one durable event and update the compact recovery summary."""

    workspace = Path(workspace_root).expanduser().resolve()
    normalized_id = task_memory_id(scope=memory_id)
    log_path, summary_path = task_memory_paths(workspace, normalized_id)
    now = datetime.now(timezone.utc).isoformat()
    event = scrub_secrets(
        {
            "schema_version": TASK_MEMORY_SCHEMA_VERSION,
            "event_id": uuid.uuid4().hex,
            "timestamp": now,
            "memory_id": normalized_id,
            "launch_id": str(launch_id or ""),
            "type": str(event_type or "state_updated")[:120],
            "data": _compact_value(dict(data or {})),
        }
    )
    try:
        with _memory_lock(workspace):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            summary = (
                _read_summary(summary_path)
                if summary_path.exists()
                else _new_summary(
                    memory_id=normalized_id,
                    goal=goal,
                    repo_root=repo_root,
                    launch_id=launch_id,
                    source="pacer_task",
                    log_path=log_path,
                    summary_path=summary_path,
                )
            )
            with log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            recent = [item for item in summary.get("recent_events") or [] if isinstance(item, dict)]
            recent.append(
                {
                    "event_id": event["event_id"],
                    "timestamp": event["timestamp"],
                    "type": event["type"],
                    "data": event["data"],
                }
            )
            event_count = int(summary.get("event_count") or 0) + 1
            summary.update(
                {
                    "updated_at": now,
                    "event_count": event_count,
                    "last_event": {
                        "event_id": event["event_id"],
                        "timestamp": event["timestamp"],
                        "type": event["type"],
                        "data": event["data"],
                    },
                    "recent_events": recent[-TASK_MEMORY_MAX_RECENT_EVENTS:],
                    "compression": {
                        "mode": "summary_plus_append_only_log",
                        "full_log_retained": True,
                        "recent_event_limit": TASK_MEMORY_MAX_RECENT_EVENTS,
                        "last_compacted_event_count": event_count
                        if event_count % TASK_MEMORY_COMPACT_EVERY == 0
                        else int((summary.get("compression") or {}).get("last_compacted_event_count") or 0),
                    },
                }
            )
            _atomic_write_json(summary_path, summary)
            return {**event, "path": str(log_path), "summary_path": str(summary_path)}
    except (OSError, portalocker.exceptions.LockException, json.JSONDecodeError) as exc:
        raise TaskMemoryError(f"task memory append failed: {type(exc).__name__}: {exc}") from exc


def compact_task_memory(
    workspace_root: str | Path,
    *,
    memory_id: str,
    max_recent_events: int = TASK_MEMORY_MAX_RECENT_EVENTS,
) -> dict[str, Any]:
    """Rebuild the compact summary from the complete journal without deleting it."""

    workspace = Path(workspace_root).expanduser().resolve()
    log_path, summary_path = task_memory_paths(workspace, memory_id)
    try:
        with _memory_lock(workspace):
            summary = _read_summary(summary_path)
            events: list[dict[str, Any]] = []
            for line in log_path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    events.append(item)
            recent = events[-max(0, int(max_recent_events)) :]
            summary.update(
                {
                    "event_count": len(events),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "last_event": _compact_event(events[-1]) if events else {},
                    "recent_events": [_compact_event(item) for item in recent],
                    "compression": {
                        "mode": "summary_plus_append_only_log",
                        "full_log_retained": True,
                        "recent_event_limit": max(0, int(max_recent_events)),
                        "last_compacted_event_count": len(events),
                    },
                }
            )
            _atomic_write_json(summary_path, summary)
            return summary
    except (OSError, portalocker.exceptions.LockException, json.JSONDecodeError) as exc:
        raise TaskMemoryError(f"task memory compaction failed: {type(exc).__name__}: {exc}") from exc


def read_task_memory(workspace_root: str | Path, *, memory_id: str) -> dict[str, Any]:
    log_path, summary_path = task_memory_paths(workspace_root, memory_id)
    try:
        summary = _read_summary(summary_path)
        summary["health"] = {
            "status": "healthy" if log_path.is_file() else "broken",
            "log_exists": log_path.is_file(),
            "summary_exists": summary_path.is_file(),
            "event_count": int(summary.get("event_count") or 0),
            "log_path": str(log_path),
            "summary_path": str(summary_path),
        }
        return summary
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskMemoryError(f"task memory read failed: {type(exc).__name__}: {exc}") from exc


def _new_summary(
    *,
    memory_id: str,
    goal: str,
    repo_root: str | Path | None,
    launch_id: str,
    source: str,
    log_path: Path,
    summary_path: Path,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": TASK_MEMORY_SCHEMA_VERSION,
        "memory_id": memory_id,
        "launch_id": str(launch_id or ""),
        "goal": str(goal or "")[:2000],
        "repo_root": str(Path(repo_root).expanduser().resolve()) if repo_root else "",
        "source": str(source or "pacer_task"),
        "created_at": now,
        "updated_at": now,
        "event_count": 0,
        "last_event": {},
        "recent_events": [],
        "log_path": str(log_path),
        "summary_path": str(summary_path),
        "compression": {
            "mode": "summary_plus_append_only_log",
            "full_log_retained": True,
            "recent_event_limit": TASK_MEMORY_MAX_RECENT_EVENTS,
            "last_compacted_event_count": 0,
        },
    }


def _read_summary(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != TASK_MEMORY_SCHEMA_VERSION:
        raise TaskMemoryError("invalid task memory summary")
    return payload


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Windows can reject fsync on a read-only descriptor.  Reopen read/write
    # so the durability step is real on both Windows and POSIX.
    descriptor = os.open(str(temporary), os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _memory_lock(workspace: Path):
    lock_path = workspace / "pacer_native" / "task-memory" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return _CombinedMemoryLock(lock_path)


class _CombinedMemoryLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = None

    def __enter__(self):
        _TASK_MEMORY_THREAD_LOCK.acquire()
        try:
            self._lock = portalocker.Lock(str(self.path), mode="a+b", timeout=TASK_MEMORY_LOCK_TIMEOUT_SECONDS)
            self._lock.acquire()
            return self
        except Exception:
            _TASK_MEMORY_THREAD_LOCK.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._lock is not None:
                self._lock.release()
        finally:
            _TASK_MEMORY_THREAD_LOCK.release()


def _compact_event(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(item.get("event_id") or ""),
        "timestamp": str(item.get("timestamp") or ""),
        "type": str(item.get("type") or ""),
        "data": _compact_value(item.get("data") if isinstance(item.get("data"), dict) else {}),
    }


def _compact_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return "[truncated]"
    if isinstance(value, dict):
        return {str(key)[:80]: _compact_value(item, depth=depth + 1) for key, item in list(value.items())[:24]}
    if isinstance(value, (list, tuple)):
        return [_compact_value(item, depth=depth + 1) for item in list(value)[:12]]
    if isinstance(value, str):
        return value[:600]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:600]
