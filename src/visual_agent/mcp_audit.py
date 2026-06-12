from __future__ import annotations

from typing import Any

from .gui import write_gui_action_event
from .mcp_common import require_workspace
from .mcp_policy import mcp_config
from .workspace import Workspace


def workspace_for_audit(args: dict[str, Any]) -> Workspace | None:
    try:
        return require_workspace(args)
    except Exception:
        return None


def audit_mcp_call(workspace: Workspace | None, tool_name: str, args: dict[str, Any], payload: dict[str, Any]) -> None:
    if workspace is None:
        return
    config = mcp_config(workspace)
    if config.get("audit_all_calls", True) is not True:
        return
    status = "error" if "error" in payload else str(payload.get("status") or "success")
    write_gui_action_event(
        workspace,
        {
            "action": f"mcp:{tool_name}",
            "workflow": args.get("workflow_name"),
            "run_profile": args.get("run_profile") or "dry-run",
            "inputs_file": args.get("inputs_file"),
            "phase": payload.get("phase") or "exit",
        },
        {
            "action": f"mcp:{tool_name}",
            "status": status,
            "message": str(payload.get("error") or payload.get("report_hint") or ""),
            "phase": payload.get("phase") or "exit",
            "result": {
                "run_id": payload.get("run_id"),
                "workflow": payload.get("workflow"),
                "artifact_count": payload.get("artifact_count"),
                "workflow_count": payload.get("workflow_count"),
                "workspace": payload.get("workspace"),
            },
        },
    )
