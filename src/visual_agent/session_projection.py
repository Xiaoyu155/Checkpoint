"""Append-only model-visible events and pure session snapshot projection."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Iterable
from uuid import uuid4

try:
    import portalocker
except ImportError:  # pragma: no cover - the in-process fallback keeps local use available.
    portalocker = None  # type: ignore[assignment]


_FALLBACK_LOCK = threading.RLock()
COMPACTION_MAX_EVENTS = 512
COMPACTION_RETAIN_EVENTS = 64
COMPACTION_MAX_BYTES = 1024 * 1024


class SessionProjection:
    """Persist committed session events and fold them into a stateless snapshot.

    Full model-visible prompts are stored as request events so a later snapshot can
    reproduce exactly what Pacer supplied without reading chat history.
    """

    def __init__(self, workspace_root: str | Path = ".") -> None:
        root = Path(workspace_root).expanduser().resolve()
        self.events_path = root / ".pacer" / "session_events.jsonl"
        self.snapshot_path = root / ".pacer" / "session_events.snapshot.json"
        self.lock_path = self.events_path.with_suffix(".lock")
        self._append_count: int | None = None
        self._known_size: tuple[int, int, int] | None = None

    def append_event(self, event_type: str, **payload: Any) -> dict[str, Any]:
        event = {
            "event_id": uuid4().hex,
            "event_type": str(event_type),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        if portalocker is not None:
            with portalocker.Lock(str(self.lock_path), mode="a", timeout=5.0):
                self._sync_append_count_unlocked()
                self._append_unlocked(serialized)
                self._append_count += 1
                self._known_size = self._file_signature()
                self._compact_if_needed_unlocked()
        else:
            with _FALLBACK_LOCK:
                self._sync_append_count_unlocked()
                self._append_unlocked(serialized)
                self._append_count += 1
                self._known_size = self._file_signature()
                self._compact_if_needed_unlocked()
        return event

    def _file_signature(self) -> tuple[int, int, int]:
        try:
            info = self.events_path.stat()
            return info.st_size, info.st_mtime_ns, info.st_ino
        except FileNotFoundError:
            return 0, 0, 0

    def _sync_append_count_unlocked(self) -> None:
        signature = self._file_signature()
        if self._append_count is None or self._known_size != signature:
            base = self._base_snapshot_unlocked()
            self._append_count = len(self._events_after_watermark(
                self.read_events(), str(base.get("watermark_event_id") or "")))
            self._known_size = signature

    def append_model_request(
        self,
        *,
        backend: str,
        system_prompt: str,
        user_prompt: str,
        phase: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.append_event(
            "model_request",
            backend=str(backend),
            phase=str(phase),
            system_prompt=str(system_prompt or ""),
            user_prompt=str(user_prompt or ""),
            metadata=dict(metadata or {}),
        )

    def _append_unlocked(self, serialized: str) -> None:
        with self.events_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _base_snapshot_unlocked(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "schema_version": 1,
            "event_count": 0,
            "model_request_count": 0,
            "latest_user_input": "",
            "recent_focus_files": [],
            "latest_task_contract": None,
            "invariants": [],
            "latest_projected_prompt": None,
            "latest_model_request": None,
            "watermark_event_id": "",
        }
        if not self.snapshot_path.is_file():
            return defaults
        try:
            saved = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read session projection snapshot: {exc}") from exc
        if not isinstance(saved, dict) or saved.get("schema_version") != 1:
            raise RuntimeError("session projection snapshot has an unsupported schema")
        defaults.update(saved)
        return defaults

    @staticmethod
    def _events_after_watermark(events: list[dict[str, Any]], watermark: str) -> list[dict[str, Any]]:
        if not watermark:
            return events
        for index, event in enumerate(events):
            if event.get("event_id") == watermark:
                return events[index + 1:]
        return events

    def _compact_if_needed_unlocked(self) -> None:
        try:
            file_size = self.events_path.stat().st_size
        except OSError:
            file_size = 0
        if (self._append_count or 0) < COMPACTION_MAX_EVENTS and file_size < COMPACTION_MAX_BYTES:
            return
        base = self._base_snapshot_unlocked()
        events = self._events_after_watermark(self.read_events(), str(base.get("watermark_event_id") or ""))
        retained_size = 0
        keep = 0
        for event in reversed(events):
            event_size = len((json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            if keep >= COMPACTION_RETAIN_EVENTS or retained_size + event_size > COMPACTION_MAX_BYTES // 2:
                break
            retained_size += event_size
            keep += 1
        split = len(events) - keep
        if not split and events:
            split = 1
        if not split:
            self._append_count = 0
            return
        compacted = self.fold_events(events[:split], base=base)
        compacted["watermark_event_id"] = str(events[split - 1].get("event_id") or "")
        self._atomic_write(self.snapshot_path, json.dumps(compacted, ensure_ascii=False, separators=(",", ":")) + "\n")
        retained = "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in events[split:])
        self._atomic_write(self.events_path, retained)
        self._append_count = len(events) - split
        self._known_size = self._file_signature()

    def read_events(self) -> list[dict[str, Any]]:
        if not self.events_path.is_file():
            return []
        events: list[dict[str, Any]] = []
        try:
            with self.events_path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, UnicodeError):
                        continue  # Ignore an uncommitted/truncated final line.
                    if isinstance(item, dict) and isinstance(item.get("payload"), dict):
                        events.append(item)
        except OSError:
            return []
        return events

    @staticmethod
    def fold_events(events: Iterable[dict[str, Any]], *, base: dict[str, Any] | None = None) -> dict[str, Any]:
        """Purely fold committed events into the latest reconstructable session view."""
        snapshot: dict[str, Any] = {
            "schema_version": 1,
            "event_count": 0,
            "model_request_count": 0,
            "latest_user_input": "",
            "recent_focus_files": [],
            "latest_task_contract": None,
            "invariants": [],
            "latest_projected_prompt": None,
            "latest_model_request": None,
            "watermark_event_id": "",
        }
        if base:
            snapshot.update(base)
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                continue
            event_type = str(event.get("event_type") or "")
            payload = event["payload"]
            snapshot["event_count"] += 1
            snapshot["watermark_event_id"] = str(event.get("event_id") or snapshot.get("watermark_event_id") or "")
            if event_type == "user_input":
                snapshot["latest_user_input"] = str(payload.get("content") or "")
            elif event_type == "recent_focus_files":
                value = payload.get("paths")
                snapshot["recent_focus_files"] = [str(path) for path in value] if isinstance(value, list) else []
            elif event_type == "task_contract":
                snapshot["latest_task_contract"] = dict(payload)
            elif event_type == "invariants":
                value = payload.get("items")
                snapshot["invariants"] = list(value) if isinstance(value, list) else []
            elif event_type == "prompt_projection":
                snapshot["latest_projected_prompt"] = {
                    "event_id": event.get("event_id"),
                    "occurred_at": event.get("occurred_at"),
                    "prompt": str(payload.get("prompt") or ""),
                    "task_instruction": str(payload.get("task_instruction") or ""),
                    "target_files": list(payload.get("target_files") or []),
                }
            elif event_type == "model_request":
                snapshot["model_request_count"] += 1
                snapshot["latest_model_request"] = {
                    "event_id": event.get("event_id"),
                    "occurred_at": event.get("occurred_at"),
                    "backend": str(payload.get("backend") or ""),
                    "phase": str(payload.get("phase") or ""),
                    "system_prompt": str(payload.get("system_prompt") or ""),
                    "user_prompt": str(payload.get("user_prompt") or ""),
                    "metadata": dict(payload.get("metadata") or {}),
                }
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        """Return a folded session view independent of the retained journal length."""
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        if portalocker is not None:
            with portalocker.Lock(str(self.lock_path), mode="a", timeout=5.0):
                return self._snapshot_unlocked()
        with _FALLBACK_LOCK:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict[str, Any]:
        base = self._base_snapshot_unlocked()
        events = self._events_after_watermark(self.read_events(), str(base.get("watermark_event_id") or ""))
        return self.fold_events(events, base=base)
