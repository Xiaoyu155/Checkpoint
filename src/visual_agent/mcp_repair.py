from __future__ import annotations

from typing import Any

from .mcp_common import budget_list_payload, require_workspace


def repair_workflow_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .repair import suggest_workflow_repair

    workspace = require_workspace(args)
    return suggest_workflow_repair(
        workspace.root,
        run_id=str(args.get("run_id") or "") or None,
        provider=str(args.get("provider") or "none"),
        model=str(args.get("model") or "") or None,
        max_chars=int(args.get("max_chars") or 12000),
        apply=bool(args.get("apply", False)),
        min_confidence=float(args.get("min_confidence") or 0.75),
        rerun_verification=bool(args.get("verify", False)),
        verify_run_profile=str(args.get("verify_run_profile") or "dry-run"),
        inputs_file=str(args.get("inputs_file") or "") or None,
        rollback_on_fail=bool(args.get("rollback_on_fail", False)),
        candidate_id=str(args.get("candidate_id") or "") or None,
    )


def auto_repair_failure_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .repair import auto_repair_failure

    workspace = require_workspace(args)
    return auto_repair_failure(
        workspace.root,
        run_id=str(args.get("run_id") or "") or None,
        max_chars=int(args.get("max_chars") or 12000),
        min_confidence=float(args.get("min_confidence") or 0.75),
        verify_run_profile=str(args.get("verify_run_profile") or "dry-run"),
        inputs_file=str(args.get("inputs_file") or "") or None,
        candidate_id=str(args.get("candidate_id") or "") or None,
        dry_run=bool(args.get("dry_run", False)),
        force=bool(args.get("force", False)),
        promote_regression=bool(args.get("promote_regression", False)),
        overwrite_regression=bool(args.get("overwrite_regression", False)),
        run_regression=bool(args.get("run_regression", False)),
        regression_timeout_seconds=float(args.get("regression_timeout_seconds") or 120.0),
    )


def list_repair_history_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .repair_history import list_repair_history

    workspace = require_workspace(args)
    payload = list_repair_history(
        workspace.root,
        limit=int(args.get("limit") or 20),
        workflow=str(args.get("workflow") or "") or None,
        status=str(args.get("status") or "") or None,
    )
    return budget_list_payload(payload, list_key="entries", count_key="total_entries")


def rollback_repair_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .repair_history import rollback_repair_history_entry

    workspace = require_workspace(args)
    return rollback_repair_history_entry(
        workspace.root,
        history_id=str(args.get("history_id") or "") or None,
        workflow=str(args.get("workflow") or "") or None,
    )


def get_repair_health_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .repair_history import build_repair_health

    workspace = require_workspace(args)
    return build_repair_health(
        workspace.root,
        limit=int(args.get("limit") or 50),
        workflow=str(args.get("workflow") or "") or None,
    )
