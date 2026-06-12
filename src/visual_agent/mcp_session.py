from __future__ import annotations

from typing import Any

from .mcp_common import require_str, require_workspace


def get_session_context_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .session import load_agent_session, workspace_session_snapshot_text

    workspace = require_workspace(args)
    session = load_agent_session(workspace.root)
    snapshot = workspace_session_snapshot_text(workspace.root)
    if session is None:
        return {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "snapshot": snapshot,
            "token_estimate": len(snapshot) // 4,
            "within_budget": True,
        }
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "snapshot": snapshot,
        "token_estimate": len(snapshot) // 4,
        "within_budget": len(snapshot) <= 2000,
    }


def get_visual_status_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .visual_status import read_status_file, visual_status_to_dict

    workspace = require_workspace(args)
    status = read_status_file(workspace.project_root)
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "project_root": str(workspace.project_root),
        "visual_status": visual_status_to_dict(status),
    }


def save_task_context_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .session import save_task_context, session_to_snapshot_text

    workspace = require_workspace(args)
    session = save_task_context(
        workspace.root,
        task=require_str(args, "task"),
        analyzed_files=[str(item) for item in args.get("analyzed_files", []) if str(item)],
        root_cause=str(args.get("root_cause") or ""),
        plan=str(args.get("plan") or ""),
        tried=[str(item) for item in args.get("tried", []) if str(item)],
    )
    snapshot = session_to_snapshot_text(session)
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "status": "saved",
        "task": session.ai_task_context.task if session.ai_task_context else "",
        "snapshot": snapshot,
        "token_estimate": len(snapshot) // 4,
        "within_budget": len(snapshot) <= 2000,
        "message": "Task context saved. Resume with context-snapshot --format markdown.",
    }
