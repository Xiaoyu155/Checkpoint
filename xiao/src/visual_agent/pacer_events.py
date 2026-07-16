from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import time_ns
from typing import Any

import portalocker

from .security import scrub_secrets


_EVENT_SEQUENCE_LOCK_TIMEOUT_SECONDS = 5.0
_EVENT_SEQUENCE_THREAD_LOCK = threading.Lock()


def append_pacer_event(
    workspace_root: str | Path,
    event_type: str,
    *,
    launch_id: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace_root).expanduser().resolve()
    timestamp = datetime.now(timezone.utc)
    event = scrub_secrets(
        {
            "schema_version": 1,
            "event_id": uuid.uuid4().hex,
            "sequence": 0,
            "timestamp": timestamp.isoformat(),
            "type": str(event_type),
            "launch_id": str(launch_id or ""),
            "data": dict(data or {}),
        }
    )
    directory = workspace / "pacer_native" / "events" / timestamp.strftime("%Y%m%d")
    temporary: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        sequence = _next_event_sequence(workspace)
        event["sequence"] = sequence
        path = directory / (
            f"{timestamp.strftime('%H%M%S-%f')}-{sequence:020d}-{event['event_id']}.json"
        )
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        result = {**event, "path": str(path)}
        try:
            from .pacer_otel import export_pacer_event

            export_pacer_event(result)
        except Exception:
            # OTel is optional and runs only after the local evidence is durable.
            pass
        return result
    except (OSError, portalocker.exceptions.LockException) as exc:
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return {**event, "path": "", "write_error": type(exc).__name__}


def list_pacer_events(workspace_root: str | Path, *, limit: int = 100) -> list[dict[str, Any]]:
    root = Path(workspace_root).expanduser().resolve() / "pacer_native" / "events"
    try:
        paths = sorted(root.glob("*/*.json"), reverse=True)[: max(0, int(limit))]
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for path in reversed(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            events.append({**payload, "_event_path_name": path.name})
    events.sort(key=_event_sort_key)
    for event in events:
        event.pop("_event_path_name", None)
    return events


def _next_event_sequence(workspace: Path) -> int:
    lock_path = workspace / "pacer_native" / "events" / ".sequence.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _EVENT_SEQUENCE_THREAD_LOCK:
        with portalocker.Lock(
            str(lock_path),
            mode="a+b",
            timeout=_EVENT_SEQUENCE_LOCK_TIMEOUT_SECONDS,
            check_interval=0.01,
        ) as handle:
            handle.seek(0)
            try:
                previous = int(handle.read().decode("ascii").strip() or "0")
            except (UnicodeDecodeError, ValueError):
                previous = 0
            sequence = max(time_ns(), previous + 1)
            handle.seek(0)
            handle.truncate()
            handle.write(str(sequence).encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
            return sequence


def _event_sort_key(event: dict[str, Any]) -> tuple[int, str, str]:
    try:
        sequence = max(0, int(event.get("sequence") or 0))
    except (TypeError, ValueError):
        sequence = 0
    timestamp = str(event.get("timestamp") or "")
    if not sequence:
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            sequence = int(parsed.timestamp() * 1_000_000_000)
        except (OverflowError, ValueError):
            sequence = 0
    return sequence, timestamp, str(event.get("_event_path_name") or "")


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_process_exists(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        # A protected process still exists even when this user cannot open it.
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
    kernel32.CloseHandle(handle)
    return True
