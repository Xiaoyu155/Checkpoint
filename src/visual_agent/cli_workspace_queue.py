from __future__ import annotations

import json
from typing import Any

from .models import to_jsonable
from .scheduler import (
    cancel_queue_task,
    list_queue_tasks,
    migrate_queue_to_sqlite,
    rollback_queue_from_sqlite,
    retry_queue_task,
    run_next_queue_task,
    run_queue_worker,
    submit_queue_task,
)
from .workspace import open_workspace


WORKSPACE_QUEUE_COMMANDS = {
    "workspace-queue-submit",
    "workspace-queue-list",
    "workspace-queue-cancel",
    "workspace-queue-retry",
    "workspace-queue-run-next",
    "workspace-queue-worker",
    "workspace-queue-migrate-sqlite",
    "workspace-queue-rollback-json",
}


def handle_workspace_queue_command(args: Any) -> int:
    if args.command == "workspace-queue-submit":
        task = submit_queue_task(
            open_workspace(args.root),
            args.workflow,
            inputs=load_inline_inputs(args.inputs) if args.inputs else None,
            inputs_file=args.inputs_file,
            priority=args.priority,
            max_retries=args.max_retries,
            run_profile="approved" if args.allow_click else args.run_profile,
            dry_run=args.run_profile == "dry-run" and not args.allow_click,
        )
        print_json(task)
        return 0
    if args.command == "workspace-queue-list":
        print_json(list_queue_tasks(open_workspace(args.root), status=args.status))
        return 0
    if args.command == "workspace-queue-cancel":
        task = cancel_queue_task(open_workspace(args.root), args.task_id, reason=args.reason)
        print_json(task)
        return 0
    if args.command == "workspace-queue-retry":
        task = retry_queue_task(open_workspace(args.root), args.task_id)
        print_json(task)
        return 0
    if args.command == "workspace-queue-run-next":
        result = run_next_queue_task(open_workspace(args.root))
        print_json(result)
        return 0 if not result["ran"] or result["task"]["status"] in {"success", "pending"} else 1
    if args.command == "workspace-queue-worker":
        result = run_queue_worker(
            open_workspace(args.root),
            poll_seconds=args.poll_seconds,
            max_tasks=args.max_tasks,
            max_seconds=args.max_seconds,
            stop_file=args.stop_file,
            once=args.once,
        )
        print_json(result)
        failed_runs = [
            run for run in result["runs"] if run.get("task") and run["task"].get("status") not in {"success", "pending"}
        ]
        return 1 if failed_runs else 0
    if args.command == "workspace-queue-migrate-sqlite":
        result = migrate_queue_to_sqlite(
            open_workspace(args.root),
            set_backend=not args.no_set_backend,
            backup_json=not args.no_backup,
        )
        print_json(result)
        return 0
    if args.command == "workspace-queue-rollback-json":
        result = rollback_queue_from_sqlite(
            open_workspace(args.root),
            set_backend=not args.no_set_backend,
            backup_json=not args.no_backup,
        )
        print_json(result)
        return 0
    raise ValueError(f"Unsupported workspace queue command: {args.command}")


def load_inline_inputs(raw_inputs: str) -> dict[str, Any]:
    return json.loads(raw_inputs)


def print_json(payload: Any) -> None:
    print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
