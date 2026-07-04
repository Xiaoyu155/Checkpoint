"""Mission queue for DevPacer autonomous runs.

This queue is deliberately small. The unit of work is an existing mission, and
workers resume that mission through ``run_chief_mission(..., execute=True)`` so
there is only one execution engine to reason about.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable, TypeVar
from uuid import uuid4

from .chief_run import run_chief_mission
from .missions import load_mission
from .models import to_jsonable
from .scheduler import lock_file, unlock_file


MISSION_QUEUE_DIRNAME = "mission_queue"
RUNNABLE_MISSION_STATUSES = {"created", "preview"}
ACTIVE_QUEUE_STATUSES = {"pending", "running"}
FINISHED_QUEUE_STATUSES = {"success", "failed", "canceled"}
T = TypeVar("T")

MissionRunner = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class MissionQueueItem:
    queue_id: str
    mission_id: str
    status: str
    priority: int
    created_at: str
    updated_at: str
    attempts: int = 0
    run_profile: str = "dry-run"
    include_slow: bool = False
    max_workflows: int = 10
    timeout_seconds: float = 1800.0
    allow_dirty: bool = False
    allow_coverage_gap: bool = False
    agent: str | None = None
    test_command: str | None = None
    allow_test_edits: bool = False
    merge_policy: str = "manual"
    last_result_status: str | None = None
    last_stop_reason: str | None = None
    last_error: str | None = None
    final_report_path: str | None = None


def mission_queue_dir(workspace_root: str | Path) -> Path:
    return Path(workspace_root).expanduser().resolve() / MISSION_QUEUE_DIRNAME


def mission_queue_state_path(workspace_root: str | Path) -> Path:
    directory = mission_queue_dir(workspace_root)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "queue.json"


def empty_mission_queue_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "items": [],
        "history": [],
    }


def load_mission_queue_state(workspace_root: str | Path) -> dict[str, Any]:
    path = mission_queue_state_path(workspace_root)
    if not path.exists():
        return empty_mission_queue_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_mission_queue_state()
    if not isinstance(payload, dict):
        return empty_mission_queue_state()
    payload.setdefault("schema_version", 1)
    payload.setdefault("generated_at", _now_iso())
    payload.setdefault("items", [])
    payload.setdefault("history", [])
    return payload


def write_mission_queue_state(workspace_root: str | Path, state: dict[str, Any]) -> Path:
    path = mission_queue_state_path(workspace_root)
    path.write_text(json.dumps(to_jsonable(state), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def submit_mission_queue_item(
    *,
    workspace_root: str | Path,
    mission_id: str,
    priority: int = 0,
    run_profile: str = "dry-run",
    include_slow: bool = False,
    max_workflows: int = 10,
    timeout_seconds: float = 1800.0,
    allow_dirty: bool = False,
    allow_coverage_gap: bool = False,
    agent: str | None = None,
    test_command: str | None = None,
    allow_test_edits: bool = False,
    merge_policy: str = "manual",
    force: bool = False,
) -> MissionQueueItem:
    workspace_path = Path(workspace_root).expanduser().resolve()
    mission = load_mission(workspace_path, mission_id)
    if mission is None:
        raise FileNotFoundError(f"No saved mission found: {mission_id}")
    mission_status = str(mission.get("status") or "")
    if not force and mission_status not in RUNNABLE_MISSION_STATUSES:
        raise RuntimeError(
            f"Mission {mission_id} is not runnable from status '{mission_status}'. "
            "Queue only created/preview missions, or pass --force after reviewing the mission."
        )
    now = _now_iso()
    normalized_merge_policy = normalize_merge_policy(merge_policy)
    item = MissionQueueItem(
        queue_id=f"mq-{uuid4().hex[:12]}",
        mission_id=str(mission_id),
        status="pending",
        priority=int(priority),
        created_at=now,
        updated_at=now,
        run_profile=str(run_profile),
        include_slow=bool(include_slow),
        max_workflows=max(1, int(max_workflows)),
        timeout_seconds=max(1.0, float(timeout_seconds)),
        allow_dirty=bool(allow_dirty),
        allow_coverage_gap=bool(allow_coverage_gap),
        agent=str(agent).strip() if str(agent or "").strip() else None,
        test_command=str(test_command).strip() if str(test_command or "").strip() else None,
        allow_test_edits=bool(allow_test_edits),
        merge_policy=normalized_merge_policy,
    )

    def update(state: dict[str, Any]) -> MissionQueueItem:
        for payload in state.get("items", []):
            if not isinstance(payload, dict):
                continue
            existing = normalize_mission_queue_item(payload)
            if existing.mission_id == item.mission_id and existing.status in ACTIVE_QUEUE_STATUSES:
                raise RuntimeError(f"Mission {mission_id} is already queued or running as {existing.queue_id}.")
        state["items"].append(mission_queue_item_to_dict(item))
        append_mission_queue_history(state, item, event="submitted")
        return item

    return _locked_update_mission_queue_state(workspace_path, update)


def list_mission_queue_items(workspace_root: str | Path, *, status: str | None = None) -> dict[str, Any]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    state = load_mission_queue_state(workspace_path)
    items = [normalize_mission_queue_item(item) for item in state.get("items", []) if isinstance(item, dict)]
    if status:
        items = [item for item in items if item.status == status]
    entries = [mission_queue_item_to_dict(item) for item in sorted(items, key=mission_queue_sort_key)]
    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "workspace_root": str(workspace_path),
        "filters": {"status": status},
        "total_items": len(entries),
        "pending_items": sum(1 for item in entries if item["status"] == "pending"),
        "running_items": sum(1 for item in entries if item["status"] == "running"),
        "finished_items": sum(1 for item in entries if item["status"] in FINISHED_QUEUE_STATUSES),
        "entries": entries,
    }


def load_mission_queue_item(workspace_root: str | Path, queue_id: str) -> MissionQueueItem | None:
    state = load_mission_queue_state(workspace_root)
    for payload in state.get("items", []):
        if isinstance(payload, dict) and str(payload.get("queue_id") or "") == queue_id:
            return normalize_mission_queue_item(payload)
    return None


def claim_next_mission_queue_item(workspace_root: str | Path) -> MissionQueueItem | None:
    workspace_path = Path(workspace_root).expanduser().resolve()

    def update(state: dict[str, Any]) -> MissionQueueItem | None:
        pending = [
            normalize_mission_queue_item(item)
            for item in state.get("items", [])
            if isinstance(item, dict) and str(item.get("status") or "") == "pending"
        ]
        if not pending:
            return None
        selected = sorted(pending, key=mission_queue_sort_key)[0]
        updated = replace_mission_queue_item(
            state,
            selected.queue_id,
            status="running",
            attempts=selected.attempts + 1,
            last_error=None,
            last_result_status=None,
            last_stop_reason=None,
        )
        append_mission_queue_history(state, updated, event="claimed")
        return updated

    return _locked_update_mission_queue_state(workspace_path, update)


def finish_mission_queue_item(
    workspace_root: str | Path,
    queue_id: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> MissionQueueItem:
    payload = result if isinstance(result, dict) else {}
    result_status = str(payload.get("status") or "")
    stop_reason = str(payload.get("stop_reason") or "")
    success = bool(result_status == "verified")
    final_report_path = str(payload.get("final_report_path") or "") or None

    def update(state: dict[str, Any]) -> MissionQueueItem:
        status = "success" if success else "failed"
        updated = replace_mission_queue_item(
            state,
            queue_id,
            status=status,
            last_result_status=result_status or None,
            last_stop_reason=stop_reason or None,
            last_error=error,
            final_report_path=final_report_path,
        )
        append_mission_queue_history(
            state,
            updated,
            event="succeeded" if success else "failed",
            message=error or stop_reason or result_status,
        )
        return updated

    return _locked_update_mission_queue_state(workspace_root, update)


def run_next_mission_queue_item(
    *,
    workspace_root: str | Path,
    mission_runner: MissionRunner | None = None,
) -> dict[str, Any]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    item = claim_next_mission_queue_item(workspace_path)
    if item is None:
        return {
            "ran": False,
            "queue_item": None,
            "result": None,
            "message": "No pending mission queue item.",
        }
    runner = mission_runner or run_chief_mission
    try:
        result = runner(
            workspace_root=workspace_path,
            resume_mission_id=item.mission_id,
            agents=((item.agent,) if item.agent else ()),
            execute=True,
            dry_run=False,
            run_profile=item.run_profile,
            include_slow=item.include_slow,
            max_workflows=item.max_workflows,
            timeout_seconds=item.timeout_seconds,
            allow_dirty=item.allow_dirty,
            allow_coverage_gap=item.allow_coverage_gap,
            test_command=item.test_command,
            allow_test_edits=item.allow_test_edits,
            merge=item.merge_policy == "auto",
        )
    except Exception as exc:
        updated = finish_mission_queue_item(workspace_path, item.queue_id, error=f"{type(exc).__name__}: {exc}")
        return {
            "ran": True,
            "queue_item": mission_queue_item_to_dict(updated),
            "result": None,
            "message": updated.last_error,
        }

    updated = finish_mission_queue_item(
        workspace_path,
        item.queue_id,
        result=result,
        error=None if str(result.get("status") or "") == "verified" else str(result.get("message") or result.get("stop_reason") or ""),
    )
    return {
        "ran": True,
        "queue_item": mission_queue_item_to_dict(updated),
        "result": result,
        "message": "Mission verified." if updated.status == "success" else "Mission stopped before verification.",
    }


def run_mission_queue_worker(
    *,
    workspace_root: str | Path,
    run_once: bool = True,
    watch: bool = False,
    poll_seconds: float = 5.0,
    max_items: int | None = None,
    max_seconds: float | None = None,
    mission_runner: MissionRunner | None = None,
) -> dict[str, Any]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    started = monotonic()
    processed = 0
    idle_polls = 0
    runs: list[dict[str, Any]] = []
    status = "stopped"
    poll_delay = max(0.1, float(poll_seconds))
    runner = mission_runner or run_chief_mission

    while True:
        if max_items is not None and processed >= max(0, int(max_items)):
            status = "max_items_reached"
            break
        if max_seconds is not None and monotonic() - started >= max(0.0, float(max_seconds)):
            status = "max_seconds_reached"
            break

        result = run_next_mission_queue_item(workspace_root=workspace_path, mission_runner=runner)
        if result.get("ran"):
            processed += 1
            idle_polls = 0
            compact = _compact_queue_run(result)
            # Keep sequential programs moving: sync the finished mission's
            # program task and queue the next ready task. Advancement must
            # never break the worker itself.
            try:
                from .program_scheduler import advance_program_for_mission

                item = result.get("queue_item") if isinstance(result.get("queue_item"), dict) else {}
                advanced = advance_program_for_mission(
                    workspace_root=workspace_path,
                    mission_id=str(item.get("mission_id") or ""),
                )
                if advanced:
                    compact["program_advance"] = {
                        "program_id": advanced.get("program_id"),
                        "synced": advanced.get("synced"),
                        "queued": [q.get("task_id") for q in (advanced.get("queued_items") or [])],
                    }
            except Exception:  # noqa: BLE001
                pass
            runs.append(compact)
            if run_once and not watch:
                status = "run_once_completed"
                break
            continue

        idle_polls += 1
        if run_once and not watch:
            status = "idle"
            break
        sleep(poll_delay)

    return {
        "schema_version": 1,
        "product": "DevPacer",
        "verification_engine": "Checkpoint",
        "status": status,
        "workspace_root": str(workspace_path),
        "processed_items": processed,
        "idle_polls": idle_polls,
        "poll_seconds": poll_delay,
        "max_items": max_items,
        "max_seconds": max_seconds,
        "elapsed_seconds": round(monotonic() - started, 6),
        "runs": runs,
    }


def mission_queue_to_markdown(payload: dict[str, Any]) -> str:
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    if not entries:
        return "No queued DevPacer missions."
    lines = ["## DevPacer Mission Queue", ""]
    for item in entries:
        result = str(item.get("last_result_status") or "")
        stop = str(item.get("last_stop_reason") or "")
        suffix = ""
        if result or stop:
            suffix = f" -> {result or 'unknown'}"
            if stop:
                suffix += f" / {stop}"
        lines.append(
            f"- `{item.get('queue_id')}` [{item.get('status')}] mission `{item.get('mission_id')}`"
            f" priority={item.get('priority')} attempts={item.get('attempts')}"
            f" agent={item.get('agent') or 'mission-default'} merge={item.get('merge_policy') or 'manual'}{suffix}"
        )
    return "\n".join(lines)


def mission_queue_submit_to_markdown(item: MissionQueueItem) -> str:
    return "\n".join(
        [
            "## DevPacer Mission Queued",
            "",
            f"- Queue id: `{item.queue_id}`",
            f"- Mission id: `{item.mission_id}`",
            f"- Status: `{item.status}`",
            f"- Priority: `{item.priority}`",
            f"- Run profile: `{item.run_profile}`",
            f"- Agent: `{item.agent or 'mission-default'}`",
            f"- Test command: `{item.test_command or ''}`",
            f"- Merge policy: `{item.merge_policy}`",
        ]
    )


def mission_queue_worker_to_markdown(payload: dict[str, Any]) -> str:
    lines = ["## DevPacer Mission Queue Worker", ""]
    lines.append(f"Status: `{payload.get('status')}`")
    lines.append(f"Processed items: `{payload.get('processed_items')}`")
    runs = payload.get("runs") if isinstance(payload.get("runs"), list) else []
    if runs:
        lines.extend(["", "### Runs", ""])
        for run in runs:
            item = run.get("queue_item") if isinstance(run.get("queue_item"), dict) else {}
            result = run.get("result") if isinstance(run.get("result"), dict) else {}
            lines.append(
                f"- `{item.get('queue_id')}` mission `{item.get('mission_id')}` "
                f"queue={item.get('status')} result={result.get('status')} / {result.get('stop_reason')}"
            )
    return "\n".join(lines).rstrip()


def mission_queue_item_to_dict(item: MissionQueueItem) -> dict[str, Any]:
    return to_jsonable(item)


def normalize_mission_queue_item(payload: dict[str, Any]) -> MissionQueueItem:
    created_at = str(payload.get("created_at") or _now_iso())
    return MissionQueueItem(
        queue_id=str(payload.get("queue_id") or ""),
        mission_id=str(payload.get("mission_id") or ""),
        status=str(payload.get("status") or "pending"),
        priority=int(payload.get("priority") or 0),
        created_at=created_at,
        updated_at=str(payload.get("updated_at") or created_at),
        attempts=int(payload.get("attempts") or 0),
        run_profile=str(payload.get("run_profile") or "dry-run"),
        include_slow=bool(payload.get("include_slow", False)),
        max_workflows=max(1, int(payload.get("max_workflows") or 10)),
        timeout_seconds=max(1.0, float(payload.get("timeout_seconds") or 1800.0)),
        allow_dirty=bool(payload.get("allow_dirty", False)),
        allow_coverage_gap=bool(payload.get("allow_coverage_gap", False)),
        agent=str(payload.get("agent")).strip() if payload.get("agent") else None,
        test_command=str(payload.get("test_command")).strip() if payload.get("test_command") else None,
        allow_test_edits=bool(payload.get("allow_test_edits", False)),
        merge_policy=normalize_merge_policy(str(payload.get("merge_policy") or "manual")),
        last_result_status=str(payload.get("last_result_status")) if payload.get("last_result_status") else None,
        last_stop_reason=str(payload.get("last_stop_reason")) if payload.get("last_stop_reason") else None,
        last_error=str(payload.get("last_error")) if payload.get("last_error") else None,
        final_report_path=str(payload.get("final_report_path")) if payload.get("final_report_path") else None,
    )


def replace_mission_queue_item(state: dict[str, Any], queue_id: str, **changes: Any) -> MissionQueueItem:
    items = state.get("items", [])
    for index, item in enumerate(items):
        if isinstance(item, dict) and str(item.get("queue_id") or "") == queue_id:
            payload = mission_queue_item_to_dict(normalize_mission_queue_item(item))
            payload.update(changes)
            payload["updated_at"] = _now_iso()
            updated = normalize_mission_queue_item(payload)
            items[index] = mission_queue_item_to_dict(updated)
            state["generated_at"] = payload["updated_at"]
            return updated
    raise FileNotFoundError(f"Mission queue item not found: {queue_id}")


def append_mission_queue_history(
    state: dict[str, Any],
    item: MissionQueueItem,
    *,
    event: str,
    message: str | None = None,
) -> None:
    state.setdefault("history", []).append(
        {
            "queue_id": item.queue_id,
            "mission_id": item.mission_id,
            "event": event,
            "status": item.status,
            "attempts": item.attempts,
            "message": message,
            "created_at": _now_iso(),
        }
    )


def mission_queue_sort_key(item: MissionQueueItem) -> tuple[int, str]:
    return (-item.priority, item.created_at)


def normalize_merge_policy(value: str | None) -> str:
    policy = str(value or "manual").strip().lower()
    if policy in {"auto", "automatic", "merge"}:
        return "auto"
    if policy in {"never", "no", "none"}:
        return "never"
    return "manual"


def payload_to_json(payload: dict[str, Any] | MissionQueueItem) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2)


def _locked_update_mission_queue_state(workspace_root: str | Path, updater_fn: Callable[[dict[str, Any]], T]) -> T:
    path = mission_queue_state_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        lock_file(handle)
        try:
            handle.seek(0)
            content = handle.read()
            try:
                state = json.loads(content) if content.strip() else empty_mission_queue_state()
            except json.JSONDecodeError:
                state = empty_mission_queue_state()
            if not isinstance(state, dict):
                state = empty_mission_queue_state()
            state.setdefault("schema_version", 1)
            state.setdefault("items", [])
            state.setdefault("history", [])
            result = updater_fn(state)
            state["generated_at"] = _now_iso()
            handle.seek(0)
            handle.truncate()
            json.dump(to_jsonable(state), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            return result
        finally:
            unlock_file(handle)


def _compact_queue_run(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    queue_item = payload.get("queue_item") if isinstance(payload.get("queue_item"), dict) else {}
    return {
        "ran": bool(payload.get("ran")),
        "message": str(payload.get("message") or ""),
        "queue_item": queue_item,
        "result": {
            "status": result.get("status"),
            "stop_reason": result.get("stop_reason"),
            "message": result.get("message"),
            "final_report_path": result.get("final_report_path"),
        },
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
