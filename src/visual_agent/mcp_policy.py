from __future__ import annotations

import json
from typing import Any

from .run_profile import RUN_PROFILE_CHOICES, run_profile_privilege
from .workspace import Workspace


RUN_PROFILE_ORDER = {name: run_profile_privilege(name) for name in RUN_PROFILE_CHOICES}


def mcp_config(workspace: Workspace) -> dict[str, Any]:
    path = workspace.root / "workspace.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    config = payload.get("mcp") if isinstance(payload.get("mcp"), dict) else {}
    return config


def enforce_mcp_run_profile(workspace: Workspace, workflow_name: str, run_profile: str) -> str:
    config = mcp_config(workspace)
    max_profile = str(config.get("max_run_profile") or "supervised")
    if max_profile not in RUN_PROFILE_ORDER:
        max_profile = "supervised"
    if run_profile == "approved":
        approved = {str(item) for item in config.get("approved_workflows", []) if str(item)}
        if workflow_name not in approved:
            raise ValueError(f"run_profile='approved' rejected: '{workflow_name}' is not in workspace mcp.approved_workflows")
    if RUN_PROFILE_ORDER[run_profile] > RUN_PROFILE_ORDER[max_profile]:
        return max_profile
    return run_profile
