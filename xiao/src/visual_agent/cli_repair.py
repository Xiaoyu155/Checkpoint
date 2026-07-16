from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import to_jsonable


REPAIR_COMMANDS = {
    "diagnose-latest-failure",
    "repair-workflow",
    "auto-repair",
    "repair-history",
    "repair-health",
    "repair-rollback",
}


def handle_repair_command(args: Any) -> int:
    if args.command == "diagnose-latest-failure":
        return handle_diagnose_latest_failure(args)
    if args.command == "repair-workflow":
        return handle_repair_workflow(args)
    if args.command == "auto-repair":
        return handle_auto_repair(args)
    if args.command == "repair-history":
        return handle_repair_history(args)
    if args.command == "repair-health":
        return handle_repair_health(args)
    if args.command == "repair-rollback":
        return handle_repair_rollback(args)
    raise ValueError(f"Unsupported repair command: {args.command}")


def handle_diagnose_latest_failure(args: Any) -> int:
    from .repair import build_failure_evidence_pack, repair_to_markdown

    payload = build_failure_evidence_pack(
        Path(args.workspace_root).resolve(),
        run_id=args.run_id,
        max_chars=args.max_chars,
    )
    print_payload(payload, args.format, markdown=repair_to_markdown)
    return 0 if payload.get("status") in {"found", "no_failure"} else 1


def handle_repair_workflow(args: Any) -> int:
    from .repair import repair_to_markdown, suggest_workflow_repair

    payload = suggest_workflow_repair(
        Path(args.workspace_root).resolve(),
        run_id=args.run_id,
        provider=args.provider,
        model=args.model,
        max_chars=args.max_chars,
        apply=args.apply,
        min_confidence=args.min_confidence,
        rerun_verification=args.verify,
        verify_run_profile=args.verify_run_profile,
        inputs_file=args.inputs_file,
        rollback_on_fail=args.rollback_on_fail,
        candidate_id=args.candidate_id,
    )
    print_payload(payload, args.format, markdown=repair_to_markdown)
    return 0 if payload.get("status") in {"suggested", "needs_model", "no_failure", "applied", "verified", "rolled_back"} else 1


def handle_auto_repair(args: Any) -> int:
    from .repair import auto_repair_failure, auto_repair_to_markdown

    payload = auto_repair_failure(
        Path(args.workspace_root).resolve(),
        run_id=args.run_id,
        max_chars=args.max_chars,
        min_confidence=args.min_confidence,
        verify_run_profile=args.verify_run_profile,
        inputs_file=args.inputs_file,
        candidate_id=args.candidate_id,
        dry_run=args.dry_run,
        force=args.force,
        promote_regression=args.promote_regression,
        overwrite_regression=args.overwrite_regression,
        run_regression=args.run_regression,
        regression_timeout_seconds=args.regression_timeout_seconds,
    )
    print_payload(payload, args.format, markdown=auto_repair_to_markdown)
    return 0 if payload.get("status") in {"suggested", "verified", "rolled_back", "no_failure"} else 1


def handle_repair_history(args: Any) -> int:
    from .repair_history import list_repair_history, repair_history_to_markdown

    payload = list_repair_history(
        Path(args.workspace_root).resolve(),
        limit=args.limit,
        workflow=args.workflow,
        status=args.status,
    )
    print_payload(payload, args.format, markdown=repair_history_to_markdown)
    return 0


def handle_repair_health(args: Any) -> int:
    from .repair_history import build_repair_health, repair_health_to_markdown

    payload = build_repair_health(
        Path(args.workspace_root).resolve(),
        limit=args.limit,
        workflow=args.workflow,
    )
    print_payload(payload, args.format, markdown=repair_health_to_markdown)
    return 0


def handle_repair_rollback(args: Any) -> int:
    from .repair_history import repair_rollback_to_markdown, rollback_repair_history_entry

    payload = rollback_repair_history_entry(
        Path(args.workspace_root).resolve(),
        history_id=args.history_id,
        workflow=args.workflow,
    )
    print_payload(payload, args.format, markdown=repair_rollback_to_markdown)
    return 0 if payload.get("status") == "manual_rolled_back" else 1


def print_payload(payload: dict[str, Any], fmt: str, *, markdown: Any) -> None:
    if fmt == "markdown":
        print(markdown(payload))
    else:
        print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
