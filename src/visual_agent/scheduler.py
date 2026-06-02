from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from time import sleep, time
from typing import Any, Callable, TypeVar
from uuid import uuid4

from .db import open_workspace_db
from .models import ActionStatus, to_jsonable
from .workspace import Workspace, run_workspace_workflow

try:
    import portalocker
except ImportError:  # pragma: no cover - dependency fallback for editable source trees
    portalocker = None


PENDING_STATUSES = {"pending"}
FINISHED_STATUSES = {"success", "failed", "canceled"}
SENSITIVE_INPUT_KEYS = ("password", "passwd", "pwd", "token", "secret", "api_key", "apikey", "key", "credential")
T = TypeVar("T")


@dataclass(frozen=True)
class QueueTask:
    task_id: str
    workflow: str
    status: str
    priority: int
    created_at: float
    updated_at: float
    attempts: int = 0
    max_retries: int = 0
    inputs: dict[str, Any] | None = None
    inputs_file: str | None = None
    run_profile: str = "dry-run"
    dry_run: bool = True
    metadata: dict[str, Any] | None = None
    last_run_id: str | None = None
    last_error: str | None = None


def queue_state_path(workspace: Workspace) -> Path:
    workspace.queue_dir.mkdir(parents=True, exist_ok=True)
    return workspace.queue_dir / "tasks.json"


def load_queue_state(workspace: Workspace) -> dict[str, Any]:
    path = queue_state_path(workspace)
    if not path.exists():
        return empty_queue_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty_queue_state()
    if not isinstance(payload, dict):
        return empty_queue_state()
    payload.setdefault("schema_version", 1)
    payload.setdefault("tasks", [])
    payload.setdefault("history", [])
    return payload


def write_queue_state(workspace: Workspace, state: dict[str, Any]) -> Path:
    path = queue_state_path(workspace)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _locked_update_queue_state(workspace: Workspace, updater_fn: Callable[[dict[str, Any]], T]) -> T:
    path = queue_state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        lock_file(handle)
        try:
            handle.seek(0)
            content = handle.read()
            try:
                state = json.loads(content) if content.strip() else empty_queue_state()
            except Exception:
                state = empty_queue_state()
            if not isinstance(state, dict):
                state = empty_queue_state()
            state.setdefault("schema_version", 1)
            state.setdefault("tasks", [])
            state.setdefault("history", [])
            result = updater_fn(state)
            state["generated_at"] = time()
            handle.seek(0)
            handle.truncate()
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            return result
        finally:
            unlock_file(handle)


def lock_file(handle: Any) -> None:
    if portalocker is not None:
        portalocker.lock(handle, portalocker.LOCK_EX)
        return
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def unlock_file(handle: Any) -> None:
    if portalocker is not None:
        portalocker.unlock(handle)
        return
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def empty_queue_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": time(),
        "tasks": [],
        "history": [],
    }


def submit_queue_task(
    workspace: Workspace,
    workflow: str,
    *,
    inputs: dict[str, Any] | None = None,
    inputs_file: str | None = None,
    priority: int = 0,
    max_retries: int = 0,
    run_profile: str = "dry-run",
    dry_run: bool = True,
    metadata: dict[str, Any] | None = None,
) -> QueueTask:
    if queue_backend(workspace) == "sqlite":
        return submit_queue_task_sqlite(
            workspace,
            workflow,
            inputs=inputs,
            inputs_file=inputs_file,
            priority=priority,
            max_retries=max_retries,
            run_profile=run_profile,
            dry_run=dry_run,
            metadata=metadata,
        )
    if inputs is not None and inputs_file is not None:
        raise ValueError("Use either inline inputs or an inputs file, not both.")
    now = time()
    task = QueueTask(
        task_id=f"task-{uuid4().hex[:12]}",
        workflow=workflow,
        status="pending",
        priority=priority,
        created_at=now,
        updated_at=now,
        max_retries=max(0, max_retries),
        inputs=redact_sensitive_inputs(inputs),
        inputs_file=inputs_file,
        run_profile=run_profile,
        dry_run=dry_run,
        metadata=metadata,
    )
    def update(state: dict[str, Any]) -> None:
        state["tasks"].append(queue_task_to_dict(task))

    _locked_update_queue_state(workspace, update)
    return task


def list_queue_tasks(workspace: Workspace, *, status: str | None = None) -> dict[str, Any]:
    if queue_backend(workspace) == "sqlite":
        return list_queue_tasks_sqlite(workspace, status=status)
    state = load_queue_state(workspace)
    tasks = [normalize_task(item) for item in state.get("tasks", []) if isinstance(item, dict)]
    if status is not None:
        tasks = [task for task in tasks if task.status == status]
    entries = [queue_task_to_dict(task) for task in sorted(tasks, key=queue_sort_key)]
    return {
        "schema_version": 1,
        "generated_at": time(),
        "workspace_root": str(workspace.root),
        "filters": {"status": status},
        "total_tasks": len(entries),
        "pending_tasks": sum(1 for item in entries if item["status"] == "pending"),
        "running_tasks": sum(1 for item in entries if item["status"] == "running"),
        "finished_tasks": sum(1 for item in entries if item["status"] in FINISHED_STATUSES),
        "entries": entries,
    }


def cancel_queue_task(workspace: Workspace, task_id: str, *, reason: str | None = None) -> QueueTask:
    if queue_backend(workspace) == "sqlite":
        return cancel_queue_task_sqlite(workspace, task_id, reason=reason)

    def update(state: dict[str, Any]) -> QueueTask:
        task = find_task(state, task_id)
        if task.status not in PENDING_STATUSES:
            raise RuntimeError(f"Only pending tasks can be canceled: {task_id}")
        updated = replace_task(state, task_id, status="canceled", last_error=reason or "canceled")
        append_history(state, updated, event="canceled", message=reason)
        return updated

    return _locked_update_queue_state(workspace, update)


def retry_queue_task(workspace: Workspace, task_id: str) -> QueueTask:
    if queue_backend(workspace) == "sqlite":
        return retry_queue_task_sqlite(workspace, task_id)

    def update(state: dict[str, Any]) -> QueueTask:
        task = find_task(state, task_id)
        if task.status not in {"failed", "canceled"}:
            raise RuntimeError(f"Only failed or canceled tasks can be retried: {task_id}")
        updated = replace_task(state, task_id, status="pending", last_error=None)
        append_history(state, updated, event="retried")
        return updated

    return _locked_update_queue_state(workspace, update)


def run_next_queue_task(workspace: Workspace) -> dict[str, Any]:
    if queue_backend(workspace) == "sqlite":
        return run_next_queue_task_sqlite(workspace)

    def mark_running(state: dict[str, Any]) -> QueueTask | None:
        pending = [
            normalize_task(item)
            for item in state.get("tasks", [])
            if isinstance(item, dict) and normalize_task(item).status == "pending"
        ]
        if not pending:
            return None
        selected = sorted(pending, key=queue_sort_key)[0]
        running = replace_task(state, selected.task_id, status="running", attempts=selected.attempts + 1, last_error=None)
        append_history(state, running, event="started")
        return running

    task = _locked_update_queue_state(workspace, mark_running)
    if task is None:
        return {
            "ran": False,
            "task": None,
            "result": None,
            "message": "No pending queue task.",
        }

    try:
        result = run_workspace_workflow(
            workspace,
            task.workflow,
            inputs=resolve_task_inputs(workspace, task),
            dry_run=task.dry_run,
            run_profile=task.run_profile,
        )
        annotate_queue_run(workspace, task, result)
    except Exception as exc:
        def fail_update(state: dict[str, Any]) -> QueueTask:
            current = find_task(state, task.task_id)
            should_retry = current.attempts <= current.max_retries
            status = "pending" if should_retry else "failed"
            updated = replace_task(state, task.task_id, status=status, last_error=str(exc))
            append_history(state, updated, event="retry_scheduled" if should_retry else "failed", message=str(exc))
            return updated

        updated = _locked_update_queue_state(workspace, fail_update)
        return {
            "ran": True,
            "task": queue_task_to_dict(updated),
            "result": None,
            "message": str(exc),
        }

    failed = any(step.status == ActionStatus.FAILED for step in result.steps)
    status = "failed" if failed else "success"
    error = next((step.message for step in result.steps if step.status == ActionStatus.FAILED), None)

    def finish_update(state: dict[str, Any]) -> QueueTask:
        current = find_task(state, task.task_id)
        should_retry = failed and current.attempts <= current.max_retries
        updated = replace_task(
            state,
            task.task_id,
            status="pending" if should_retry else status,
            last_run_id=result.run_id,
            last_error=error if failed else None,
        )
        append_history(
            state,
            updated,
            event="retry_scheduled" if should_retry else ("failed" if failed else "succeeded"),
            message=error,
            run_id=result.run_id,
        )
        return updated

    updated = _locked_update_queue_state(workspace, finish_update)
    return {
        "ran": True,
        "task": queue_task_to_dict(updated),
        "result": {
            "run_id": result.run_id,
            "status": status,
            "run_dir": str(result.run_dir),
        },
        "message": error if failed else "Task completed.",
    }


def queue_worker_stop_path(workspace: Workspace) -> Path:
    workspace.queue_dir.mkdir(parents=True, exist_ok=True)
    return workspace.queue_dir / "worker.stop"


def run_queue_worker(
    workspace: Workspace,
    *,
    poll_seconds: float = 1.0,
    max_tasks: int | None = None,
    max_seconds: float | None = None,
    stop_file: str | Path | None = None,
    once: bool = False,
) -> dict[str, Any]:
    started_at = time()
    stop_path = Path(stop_file) if stop_file is not None else queue_worker_stop_path(workspace)
    runs: list[dict[str, Any]] = []
    tasks_run = 0
    idle_polls = 0
    status = "stopped"
    poll_delay = max(0.0, poll_seconds)

    while True:
        if stop_path.exists():
            status = "stopped_by_file"
            break
        if max_tasks is not None and tasks_run >= max(0, max_tasks):
            status = "max_tasks_reached"
            break
        if max_seconds is not None and time() - started_at >= max(0.0, max_seconds):
            status = "max_seconds_reached"
            break

        result = run_next_queue_task(workspace)
        if result.get("ran"):
            runs.append(result)
            tasks_run += 1
            idle_polls = 0
            if once:
                status = "once_completed"
                break
            continue

        idle_polls += 1
        if once:
            status = "idle"
            break
        sleep(poll_delay)

    finished_at = time()
    return {
        "status": status,
        "workspace_root": str(workspace.root),
        "backend": queue_backend(workspace),
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_seconds": finished_at - started_at,
        "poll_seconds": poll_delay,
        "max_tasks": max_tasks,
        "max_seconds": max_seconds,
        "stop_file": str(stop_path),
        "once": once,
        "tasks_run": tasks_run,
        "idle_polls": idle_polls,
        "runs": runs,
    }


def resolve_task_inputs(workspace: Workspace, task: QueueTask) -> dict[str, Any]:
    if task.inputs is not None:
        return task.inputs
    if task.inputs_file:
        path = Path(task.inputs_file)
        if not path.is_absolute():
            path = workspace.root / path
            if not path.exists():
                path = workspace.inputs_dir / task.inputs_file
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def queue_sort_key(task: QueueTask) -> tuple[int, float]:
    return (-task.priority, task.created_at)


def find_task(state: dict[str, Any], task_id: str) -> QueueTask:
    for item in state.get("tasks", []):
        if isinstance(item, dict) and item.get("task_id") == task_id:
            return normalize_task(item)
    raise FileNotFoundError(f"Queue task not found: {task_id}")


def replace_task(state: dict[str, Any], task_id: str, **changes: Any) -> QueueTask:
    tasks = state.get("tasks", [])
    for index, item in enumerate(tasks):
        if isinstance(item, dict) and item.get("task_id") == task_id:
            payload = queue_task_to_dict(normalize_task(item))
            payload.update(changes)
            payload["updated_at"] = time()
            tasks[index] = payload
            state["generated_at"] = payload["updated_at"]
            return normalize_task(payload)
    raise FileNotFoundError(f"Queue task not found: {task_id}")


def append_history(
    state: dict[str, Any],
    task: QueueTask,
    *,
    event: str,
    message: str | None = None,
    run_id: str | None = None,
) -> None:
    state.setdefault("history", []).append(
        {
            "task_id": task.task_id,
            "workflow": task.workflow,
            "event": event,
            "status": task.status,
            "attempts": task.attempts,
            "message": message,
            "run_id": run_id,
            "created_at": time(),
        }
    )


def normalize_task(payload: dict[str, Any]) -> QueueTask:
    return QueueTask(
        task_id=str(payload["task_id"]),
        workflow=str(payload["workflow"]),
        status=str(payload.get("status") or "pending"),
        priority=int(payload.get("priority") or 0),
        created_at=float(payload.get("created_at") or 0.0),
        updated_at=float(payload.get("updated_at") or payload.get("created_at") or 0.0),
        attempts=int(payload.get("attempts") or 0),
        max_retries=int(payload.get("max_retries") or 0),
        inputs=payload.get("inputs") if isinstance(payload.get("inputs"), dict) else None,
        inputs_file=str(payload.get("inputs_file")) if payload.get("inputs_file") else None,
        run_profile=str(payload.get("run_profile") or "dry-run"),
        dry_run=bool(payload.get("dry_run", True)),
        metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        last_run_id=str(payload.get("last_run_id")) if payload.get("last_run_id") else None,
        last_error=str(payload.get("last_error")) if payload.get("last_error") else None,
    )


def queue_task_to_dict(task: QueueTask) -> dict[str, Any]:
    return to_jsonable(task)


def queue_backend(workspace: Workspace) -> str:
    manifest_path = workspace.root / "workspace.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return "json"
    if not isinstance(payload, dict):
        return "json"
    queue_config = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
    backend = str(payload.get("queue_backend") or queue_config.get("backend") or "").lower()
    return "sqlite" if backend == "sqlite" else "json"


def submit_queue_task_sqlite(
    workspace: Workspace,
    workflow: str,
    *,
    inputs: dict[str, Any] | None = None,
    inputs_file: str | None = None,
    priority: int = 0,
    max_retries: int = 0,
    run_profile: str = "dry-run",
    dry_run: bool = True,
    metadata: dict[str, Any] | None = None,
) -> QueueTask:
    if inputs is not None and inputs_file is not None:
        raise ValueError("Use either inline inputs or an inputs file, not both.")
    now = time()
    task = QueueTask(
        task_id=f"task-{uuid4().hex[:12]}",
        workflow=workflow,
        status="pending",
        priority=priority,
        created_at=now,
        updated_at=now,
        max_retries=max(0, max_retries),
        inputs=redact_sensitive_inputs(inputs),
        inputs_file=inputs_file,
        run_profile=run_profile,
        dry_run=dry_run,
        metadata=metadata,
    )
    with open_workspace_db(workspace.root) as conn:
        conn.execute(
            """
            INSERT INTO queue_tasks (
                task_id, workflow, status, priority, run_profile, dry_run, inputs_json, inputs_file,
                metadata_json, created_at, updated_at, attempts, max_retries, last_run_id, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            sqlite_task_values(task),
        )
        conn.commit()
    return task


def list_queue_tasks_sqlite(workspace: Workspace, *, status: str | None = None) -> dict[str, Any]:
    with open_workspace_db(workspace.root) as conn:
        if status is None:
            rows = conn.execute("SELECT * FROM queue_tasks").fetchall()
        else:
            rows = conn.execute("SELECT * FROM queue_tasks WHERE status = ?", (status,)).fetchall()
    tasks = [queue_task_from_sqlite_row(row) for row in rows]
    entries = [queue_task_to_dict(task) for task in sorted(tasks, key=queue_sort_key)]
    return {
        "schema_version": 1,
        "generated_at": time(),
        "workspace_root": str(workspace.root),
        "filters": {"status": status},
        "backend": "sqlite",
        "total_tasks": len(entries),
        "pending_tasks": sum(1 for item in entries if item["status"] == "pending"),
        "running_tasks": sum(1 for item in entries if item["status"] == "running"),
        "finished_tasks": sum(1 for item in entries if item["status"] in FINISHED_STATUSES),
        "entries": entries,
    }


def cancel_queue_task_sqlite(workspace: Workspace, task_id: str, *, reason: str | None = None) -> QueueTask:
    with open_workspace_db(workspace.root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        task = find_task_sqlite(conn, task_id)
        if task.status not in PENDING_STATUSES:
            conn.rollback()
            raise RuntimeError(f"Only pending tasks can be canceled: {task_id}")
        updated = update_task_sqlite(conn, task_id, status="canceled", last_error=reason or "canceled")
        append_history_sqlite(conn, updated, event="canceled", message=reason)
        conn.commit()
        return updated


def retry_queue_task_sqlite(workspace: Workspace, task_id: str) -> QueueTask:
    with open_workspace_db(workspace.root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        task = find_task_sqlite(conn, task_id)
        if task.status not in {"failed", "canceled"}:
            conn.rollback()
            raise RuntimeError(f"Only failed or canceled tasks can be retried: {task_id}")
        updated = update_task_sqlite(conn, task_id, status="pending", last_error=None)
        append_history_sqlite(conn, updated, event="retried")
        conn.commit()
        return updated


def run_next_queue_task_sqlite(workspace: Workspace) -> dict[str, Any]:
    with open_workspace_db(workspace.root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM queue_tasks WHERE status = 'pending' ORDER BY priority DESC, created_at ASC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.commit()
            return {"ran": False, "task": None, "result": None, "message": "No pending queue task."}
        selected = queue_task_from_sqlite_row(row)
        task = update_task_sqlite(conn, selected.task_id, status="running", attempts=selected.attempts + 1, last_error=None)
        append_history_sqlite(conn, task, event="started")
        conn.commit()

    try:
        result = run_workspace_workflow(
            workspace,
            task.workflow,
            inputs=resolve_task_inputs(workspace, task),
            dry_run=task.dry_run,
            run_profile=task.run_profile,
        )
        annotate_queue_run(workspace, task, result)
    except Exception as exc:
        with open_workspace_db(workspace.root) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = find_task_sqlite(conn, task.task_id)
            should_retry = current.attempts <= current.max_retries
            updated = update_task_sqlite(
                conn,
                task.task_id,
                status="pending" if should_retry else "failed",
                last_error=str(exc),
            )
            append_history_sqlite(conn, updated, event="retry_scheduled" if should_retry else "failed", message=str(exc))
            conn.commit()
        return {"ran": True, "task": queue_task_to_dict(updated), "result": None, "message": str(exc)}

    failed = any(step.status == ActionStatus.FAILED for step in result.steps)
    status = "failed" if failed else "success"
    error = next((step.message for step in result.steps if step.status == ActionStatus.FAILED), None)
    with open_workspace_db(workspace.root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = find_task_sqlite(conn, task.task_id)
        should_retry = failed and current.attempts <= current.max_retries
        updated = update_task_sqlite(
            conn,
            task.task_id,
            status="pending" if should_retry else status,
            last_run_id=result.run_id,
            last_error=error if failed else None,
        )
        append_history_sqlite(
            conn,
            updated,
            event="retry_scheduled" if should_retry else ("failed" if failed else "succeeded"),
            message=error,
            run_id=result.run_id,
        )
        conn.commit()
    return {
        "ran": True,
        "task": queue_task_to_dict(updated),
        "result": {"run_id": result.run_id, "status": status, "run_dir": str(result.run_dir)},
        "message": error if failed else "Task completed.",
    }


def sqlite_task_values(task: QueueTask) -> tuple[Any, ...]:
    return (
        task.task_id,
        task.workflow,
        task.status,
        task.priority,
        task.run_profile,
        1 if task.dry_run else 0,
        json.dumps(task.inputs, ensure_ascii=False) if task.inputs is not None else None,
        task.inputs_file,
        json.dumps(task.metadata, ensure_ascii=False) if task.metadata is not None else None,
        task.created_at,
        task.updated_at,
        task.attempts,
        task.max_retries,
        task.last_run_id,
        task.last_error,
    )


def queue_task_from_sqlite_row(row: sqlite3.Row) -> QueueTask:
    return QueueTask(
        task_id=str(row["task_id"]),
        workflow=str(row["workflow"]),
        status=str(row["status"]),
        priority=int(row["priority"] or 0),
        created_at=float(row["created_at"] or 0.0),
        updated_at=float(row["updated_at"] or row["created_at"] or 0.0),
        attempts=int(row["attempts"] or 0),
        max_retries=int(row["max_retries"] or 0),
        inputs=json.loads(row["inputs_json"]) if row["inputs_json"] else None,
        inputs_file=str(row["inputs_file"]) if row["inputs_file"] else None,
        run_profile=str(row["run_profile"] or "dry-run"),
        dry_run=bool(row["dry_run"]),
        metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else None,
        last_run_id=str(row["last_run_id"]) if row["last_run_id"] else None,
        last_error=str(row["last_error"]) if row["last_error"] else None,
    )


def find_task_sqlite(conn: sqlite3.Connection, task_id: str) -> QueueTask:
    row = conn.execute("SELECT * FROM queue_tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        raise FileNotFoundError(f"Queue task not found: {task_id}")
    return queue_task_from_sqlite_row(row)


def update_task_sqlite(conn: sqlite3.Connection, task_id: str, **changes: Any) -> QueueTask:
    current = queue_task_to_dict(find_task_sqlite(conn, task_id))
    current.update(changes)
    current["updated_at"] = time()
    task = normalize_task(current)
    conn.execute(
        """
        UPDATE queue_tasks
        SET workflow = ?, status = ?, priority = ?, run_profile = ?, dry_run = ?, inputs_json = ?,
            inputs_file = ?, metadata_json = ?, created_at = ?, updated_at = ?, attempts = ?,
            max_retries = ?, last_run_id = ?, last_error = ?
        WHERE task_id = ?
        """,
        (
            task.workflow,
            task.status,
            task.priority,
            task.run_profile,
            1 if task.dry_run else 0,
            json.dumps(task.inputs, ensure_ascii=False) if task.inputs is not None else None,
            task.inputs_file,
            json.dumps(task.metadata, ensure_ascii=False) if task.metadata is not None else None,
            task.created_at,
            task.updated_at,
            task.attempts,
            task.max_retries,
            task.last_run_id,
            task.last_error,
            task.task_id,
        ),
    )
    return task


def append_history_sqlite(
    conn: sqlite3.Connection,
    task: QueueTask,
    *,
    event: str,
    message: str | None = None,
    run_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO queue_history (
            task_id, workflow, status, event, attempts, run_profile, completed_at, last_run_id, last_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.task_id,
            task.workflow,
            task.status,
            event,
            task.attempts,
            task.run_profile,
            time(),
            run_id,
            message,
        ),
    )


def migrate_queue_to_sqlite(workspace: Workspace, *, set_backend: bool = True, backup_json: bool = True) -> dict[str, Any]:
    state = load_queue_state(workspace)
    tasks = [normalize_task(item) for item in state.get("tasks", []) if isinstance(item, dict)]
    history = [item for item in state.get("history", []) if isinstance(item, dict)]
    backup_path = None
    json_path = queue_state_path(workspace)
    if backup_json and json_path.exists():
        backup_path = json_path.with_suffix(f".tasks-json-backup-{int(time())}.json")
        shutil.copy2(json_path, backup_path)

    with open_workspace_db(workspace.root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for task in tasks:
            upsert_queue_task_sqlite(conn, task)
        conn.execute("DELETE FROM queue_history")
        for item in history:
            append_history_payload_sqlite(conn, item)
        conn.commit()

    if set_backend:
        set_queue_backend(workspace, "sqlite")

    return {
        "status": "migrated",
        "from_backend": "json",
        "to_backend": "sqlite",
        "workspace_root": str(workspace.root),
        "json_queue_path": str(json_path),
        "sqlite_db_path": str(workspace.root / "agent.db"),
        "backup_path": str(backup_path) if backup_path else None,
        "task_count": len(tasks),
        "history_count": len(history),
        "backend_updated": bool(set_backend),
    }


def rollback_queue_from_sqlite(workspace: Workspace, *, set_backend: bool = True, backup_json: bool = True) -> dict[str, Any]:
    json_path = queue_state_path(workspace)
    backup_path = None
    if backup_json and json_path.exists():
        backup_path = json_path.with_suffix(f".rollback-backup-{int(time())}.json")
        shutil.copy2(json_path, backup_path)

    with open_workspace_db(workspace.root) as conn:
        rows = conn.execute("SELECT * FROM queue_tasks").fetchall()
        history_rows = conn.execute("SELECT * FROM queue_history ORDER BY id ASC").fetchall()
    tasks = [queue_task_to_dict(queue_task_from_sqlite_row(row)) for row in rows]
    history = [queue_history_from_sqlite_row(row) for row in history_rows]
    state = {
        "schema_version": 1,
        "generated_at": time(),
        "tasks": tasks,
        "history": history,
        "source_backend": "sqlite",
    }
    write_queue_state(workspace, state)

    if set_backend:
        set_queue_backend(workspace, "json")

    return {
        "status": "rolled_back",
        "from_backend": "sqlite",
        "to_backend": "json",
        "workspace_root": str(workspace.root),
        "json_queue_path": str(json_path),
        "sqlite_db_path": str(workspace.root / "agent.db"),
        "backup_path": str(backup_path) if backup_path else None,
        "task_count": len(tasks),
        "history_count": len(history),
        "backend_updated": bool(set_backend),
    }


def upsert_queue_task_sqlite(conn: sqlite3.Connection, task: QueueTask) -> None:
    conn.execute(
        """
        INSERT INTO queue_tasks (
            task_id, workflow, status, priority, run_profile, dry_run, inputs_json, inputs_file,
            metadata_json, created_at, updated_at, attempts, max_retries, last_run_id, last_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            workflow = excluded.workflow,
            status = excluded.status,
            priority = excluded.priority,
            run_profile = excluded.run_profile,
            dry_run = excluded.dry_run,
            inputs_json = excluded.inputs_json,
            inputs_file = excluded.inputs_file,
            metadata_json = excluded.metadata_json,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at,
            attempts = excluded.attempts,
            max_retries = excluded.max_retries,
            last_run_id = excluded.last_run_id,
            last_error = excluded.last_error
        """,
        sqlite_task_values(task),
    )


def append_history_payload_sqlite(conn: sqlite3.Connection, item: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO queue_history (
            task_id, workflow, status, event, attempts, run_profile, completed_at, last_run_id, last_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(item.get("task_id") or ""),
            str(item.get("workflow") or ""),
            str(item.get("status") or ""),
            str(item.get("event") or "migrated"),
            int(item.get("attempts") or 0),
            str(item.get("run_profile") or "dry-run"),
            float(item.get("created_at") or item.get("completed_at") or time()),
            str(item.get("run_id") or item.get("last_run_id")) if item.get("run_id") or item.get("last_run_id") else None,
            str(item.get("message") or item.get("last_error")) if item.get("message") or item.get("last_error") else None,
        ),
    )


def queue_history_from_sqlite_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "task_id": str(row["task_id"]),
        "workflow": str(row["workflow"]),
        "event": str(row["event"]),
        "status": str(row["status"]),
        "attempts": int(row["attempts"] or 0),
        "message": row["last_error"],
        "run_id": row["last_run_id"],
        "created_at": float(row["completed_at"] or 0.0),
        "run_profile": row["run_profile"],
    }


def set_queue_backend(workspace: Workspace, backend: str) -> None:
    manifest_path = workspace.root / "workspace.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        payload = {"name": workspace.root.name, "version": 1}
    if not isinstance(payload, dict):
        payload = {"name": workspace.root.name, "version": 1}
    payload["queue_backend"] = backend
    queue_config = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
    queue_config["backend"] = backend
    payload["queue"] = queue_config
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def redact_sensitive_inputs(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {str(key): redact_sensitive_input_value(str(key), item) for key, item in value.items()}


def redact_sensitive_input_value(key: str, value: Any) -> Any:
    if isinstance(value, dict):
        return {str(child_key): redact_sensitive_input_value(str(child_key), child_value) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [redact_sensitive_input_value(key, item) for item in value]
    if is_sensitive_input_key(key):
        text = str(value)
        return {
            "redacted": True,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    return value


def is_sensitive_input_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(marker in normalized for marker in SENSITIVE_INPUT_KEYS)


def annotate_queue_run(workspace: Workspace, task: QueueTask, result: Any) -> None:
    metadata = task.metadata if isinstance(task.metadata, dict) else {}
    external_sample = metadata.get("external_sample") if isinstance(metadata.get("external_sample"), dict) else None
    if not external_sample:
        return
    try:
        from .external_samples import annotate_external_sample_report, external_sample_run_status

        annotate_external_sample_report(
            workspace,
            result.run_id,
            plan=external_sample,
            run_status=external_sample_run_status(result),
        )
    except Exception:
        return
