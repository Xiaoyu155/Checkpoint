from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import to_jsonable
from .security import scrub_secrets
from .workspace import Workspace


MCP_DETAIL_RESPONSE_MAX_CHARS = 8000
MCP_DETAIL_CONTENT_MAX_CHARS = 7000
MCP_RESPONSE_MAX_CHARS = 8000
MCP_STRUCTURED_LIST_MAX_CHARS = 6000


def preflight_summary(preflight: Any) -> dict[str, Any]:
    data = to_jsonable(preflight)
    missing = data.get("missing_required_capabilities") if isinstance(data.get("missing_required_capabilities"), list) else []
    unavailable = data.get("unavailable_used_capabilities") if isinstance(data.get("unavailable_used_capabilities"), list) else []
    warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else []
    return {
        "ok": bool(data.get("ok")),
        "workflow_name": data.get("workflow_name"),
        "strict": bool(data.get("strict")),
        "missing_required_count": len(missing),
        "unavailable_used_count": len(unavailable),
        "warning_count": len(warnings),
        "warnings": warnings,
    }


def budget_list_payload(payload: dict[str, Any], *, list_key: str, count_key: str) -> dict[str, Any]:
    safe_payload = scrub_secrets(payload)
    if len(json.dumps(safe_payload, ensure_ascii=False, default=str)) <= MCP_STRUCTURED_LIST_MAX_CHARS:
        return {
            **safe_payload,
            "truncated": False,
            "within_budget": True,
        }

    items = safe_payload.get(list_key) if isinstance(safe_payload.get(list_key), list) else []
    compact = {**safe_payload, list_key: []}
    omitted = len(items)
    for item in items:
        candidate_items = [*compact[list_key], item]
        candidate = {
            **compact,
            list_key: candidate_items,
            "truncated": omitted > 1,
            "omitted_count": max(0, len(items) - len(candidate_items)),
            "within_budget": True,
        }
        if len(json.dumps(candidate, ensure_ascii=False, default=str)) > MCP_STRUCTURED_LIST_MAX_CHARS:
            break
        compact = candidate
        omitted = len(items) - len(candidate_items)

    return {
        **compact,
        "truncated": omitted > 0,
        "omitted_count": omitted,
        count_key: safe_payload.get(count_key, len(items)),
        "response_hint": f"{list_key} was truncated to fit the MCP 2000-token response budget." if omitted > 0 else None,
        "within_budget": True,
    }


def budget_mcp_text(text: str, *, max_chars: int) -> tuple[str, bool]:
    safe_text = scrub_secrets(str(text))
    if len(safe_text) <= max_chars:
        return safe_text, False
    suffix = "\n...[truncated, use list_run_artifacts/get_run_report paths for full details]"
    budget = max(0, max_chars - len(suffix))
    return safe_text[:budget].rstrip() + suffix, True


def budget_mcp_report_dict(report: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    safe_report = scrub_secrets(report)
    encoded = json.dumps(safe_report, ensure_ascii=False, default=str)
    if len(encoded) <= MCP_DETAIL_RESPONSE_MAX_CHARS:
        return safe_report, False

    compact_steps = []
    for step in safe_report.get("steps", []) if isinstance(safe_report.get("steps"), list) else []:
        if not isinstance(step, dict):
            continue
        compact_steps.append(
            {
                "id": step.get("id"),
                "action": step.get("action"),
                "status": step.get("status"),
                "message": str(step.get("message") or "")[:160],
                "has_failure_diagnosis": step.get("has_failure_diagnosis"),
            }
        )
        if len(compact_steps) >= 10:
            break

    compact = {
        "schema_version": safe_report.get("schema_version", 1),
        "run_id": safe_report.get("run_id"),
        "workflow_name": safe_report.get("workflow_name"),
        "status": safe_report.get("status"),
        "run_profile": safe_report.get("run_profile"),
        "summary": safe_report.get("summary"),
        "paths": safe_report.get("paths"),
        "steps": compact_steps,
        "failure": safe_report.get("failure"),
        "truncated": True,
        "truncation_reason": "MCP report JSON exceeded the 2000-token response budget.",
    }
    compact_text = json.dumps(compact, ensure_ascii=False, default=str)
    if len(compact_text) <= MCP_DETAIL_RESPONSE_MAX_CHARS:
        return compact, True

    failure = compact.get("failure")
    if isinstance(failure, dict):
        compact["failure"] = {
            "failed_step": failure.get("failed_step"),
            "expected": str(failure.get("expected") or "")[:200],
            "actual": str(failure.get("actual") or "")[:200],
            "recovery_suggestions": [str(item)[:200] for item in (failure.get("recovery_suggestions") or [])[:2]]
            if isinstance(failure.get("recovery_suggestions"), list)
            else [],
        }
    return compact, True


def require_workspace(args: dict[str, Any]) -> Workspace:
    root = str(args.get("workspace_root") or os.environ.get("VISUAL_AGENT_WORKSPACE") or "").strip()
    if not root:
        raise ValueError("workspace_root is required")
    raw = Path(root)
    if any(part == ".." for part in raw.parts):
        raise ValueError("workspace_root must not contain '..'")
    path = raw.resolve()
    if not mcp_workspace_root_allowed(path):
        raise ValueError(f"workspace_root is outside allowed MCP roots: {path}")
    if not path.exists():
        raise FileNotFoundError(f"Workspace root not found: {path}")
    return Workspace(path)


def mcp_workspace_root_allowed(path: Path) -> bool:
    resolved = path.resolve()
    allowed_roots = [Path.cwd().resolve(), Path.home().resolve()]
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"{key} is required")
    return str(value)


def safe_workspace_child(workspace: Workspace, path: Path) -> Path:
    resolved = path.resolve()
    root = workspace.root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path escapes workspace: {path}") from exc
    return resolved


def safe_artifact(workspace: Workspace, path: Path, kind: str) -> dict[str, str]:
    resolved = safe_workspace_child(workspace, path)
    return {"type": kind, "path": str(resolved), "relative_path": resolved.relative_to(workspace.root.resolve()).as_posix()}
