from __future__ import annotations

import json
from typing import Any

from .console import build_report_detail, build_workspace_dashboard, dashboard_to_markdown, find_report_json_path, report_detail_to_markdown
from .mcp_common import (
    MCP_DETAIL_CONTENT_MAX_CHARS,
    MCP_DETAIL_RESPONSE_MAX_CHARS,
    budget_list_payload,
    budget_mcp_report_dict,
    budget_mcp_text,
    preflight_summary,
    require_str,
    require_workspace,
    safe_artifact,
    safe_workspace_child,
)
from .models import to_jsonable
from .preflight import run_preflight
from .reports import list_run_summaries
from .security import scrub_secrets
from .validation import validate_workflow_file
from .workflow import parse_workflow_file
from .workspace import discover_workflows, find_workflow, workspace_report_access_payload


def list_workflows_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    include_slow = bool(args.get("include_slow", False))
    latest_by_workflow = {}
    for summary in list_run_summaries(workspace.runs_dir, limit=50):
        latest_by_workflow.setdefault(summary.workflow_name, summary)
    workflows = []
    for ref in discover_workflows(workspace, include_slow=include_slow):
        latest = latest_by_workflow.get(ref.name)
        workflows.append(
            {
                "name": ref.name,
                "path": ref.relative_path,
                "tags": list(ref.tags),
                "visibility": ref.visibility,
                "author": ref.author,
                "description": ref.description,
                "license": ref.license,
                "last_run_status": latest.status if latest else None,
                "last_run_id": latest.run_id if latest else None,
            }
        )
    payload = {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "workflow_count": len(workflows),
        "workflows": workflows,
    }
    return budget_list_payload(payload, list_key="workflows", count_key="workflow_count")


def validate_workflow_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    workflow_name = require_str(args, "workflow_name")
    ref = find_workflow(workspace, workflow_name)
    workflow = parse_workflow_file(ref.path)
    validation = validate_workflow_file(ref.path)
    preflight = run_preflight(workflow)
    return {
        "schema_version": 1,
        "workflow": ref.name,
        "path": ref.relative_path,
        "valid": validation.valid,
        "validation": to_jsonable(validation),
        "preflight": preflight_summary(preflight),
    }


def get_run_report_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    run_id = require_str(args, "run_id")
    fmt = str(args.get("format") or "markdown")
    detail = build_report_detail(workspace, run_id)
    if not detail:
        raise FileNotFoundError(f"Run report not found: {run_id}")
    safe_detail = scrub_secrets(detail)
    if isinstance(safe_detail, dict) and safe_detail.get("status") == "upgrade_required":
        return safe_detail
    if fmt == "markdown":
        content, truncated = budget_mcp_text(report_detail_to_markdown(safe_detail), max_chars=MCP_DETAIL_CONTENT_MAX_CHARS)
        return {
            "schema_version": 1,
            "run_id": run_id,
            "format": "markdown",
            "content": content,
            "truncated": truncated,
            "within_budget": len(content) <= MCP_DETAIL_CONTENT_MAX_CHARS,
            "token_estimate": len(content) // 4,
            "report_hint": f"Use list_run_artifacts with run_id='{run_id}' to locate the full report file.",
        }
    if fmt != "json":
        raise ValueError(f"Unsupported report format: {fmt}")
    report, truncated = budget_mcp_report_dict(safe_detail)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "format": "json",
        "report": report,
        "truncated": truncated,
        "within_budget": len(json.dumps(report, ensure_ascii=False, default=str)) <= MCP_DETAIL_RESPONSE_MAX_CHARS,
        "report_hint": f"Use list_run_artifacts with run_id='{run_id}' to locate the full report file.",
    }


def list_run_artifacts_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    run_id = require_str(args, "run_id")
    try:
        report_path = find_report_json_path(workspace, run_id)
    except FileNotFoundError:
        report_path = None
    if report_path is not None:
        access = workspace_report_access_payload(workspace, report_path)
        if not access["allowed"]:
            return {
                "schema_version": 1,
                "status": "upgrade_required",
                "run_id": run_id,
                "history_access": scrub_secrets(access),
                "message": access.get("message"),
            }
    artifacts = []
    for suffix in (".json", ".md"):
        path = workspace.reports_dir / f"{run_id}{suffix}"
        if path.exists():
            artifacts.append(safe_artifact(workspace, path, "report"))
    run_dir = safe_workspace_child(workspace, workspace.runs_dir / run_id)
    if run_dir.exists():
        for path in sorted(run_dir.rglob("*")):
            if not path.is_file():
                continue
            kind = "screenshot" if path.suffix.lower() in {".png", ".jpg", ".jpeg"} else "artifact"
            try:
                artifacts.append(safe_artifact(workspace, path, kind))
            except ValueError:
                continue
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }
    return budget_list_payload(payload, list_key="artifacts", count_key="artifact_count")


def get_workspace_dashboard_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    fmt = str(args.get("format") or "markdown")
    limit = int(args.get("limit") or 5)
    dashboard = scrub_secrets(build_workspace_dashboard(workspace, limit=max(1, min(limit, 25))))
    if fmt == "markdown":
        content, truncated = budget_mcp_text(dashboard_to_markdown(dashboard), max_chars=MCP_DETAIL_CONTENT_MAX_CHARS)
        return {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "format": "markdown",
            "content": content,
            "truncated": truncated,
            "within_budget": len(content) <= MCP_DETAIL_CONTENT_MAX_CHARS,
        }
    if fmt != "json":
        raise ValueError(f"Unsupported dashboard format: {fmt}")
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "format": "json",
        "dashboard": dashboard,
    }
