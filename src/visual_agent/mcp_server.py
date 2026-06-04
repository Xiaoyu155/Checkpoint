from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .console import build_report_detail, build_workspace_dashboard, dashboard_to_markdown, report_detail_to_markdown
from .gui import write_gui_action_event
from .models import to_jsonable
from .preflight import run_preflight
from .reports import list_run_summaries
from .security import scrub_secrets
from .validation import validate_workflow_file
from .workflow import parse_workflow_file
from .workspace import Workspace, build_workspace_report_index, discover_workflows, find_workflow, load_workspace_inputs, run_workspace_workflow


APP_NAME = "visual-agent"
APP_VERSION = "0.1.0"
RUN_PROFILE_ORDER = {"dry-run": 0, "supervised": 1, "approved": 2}
MCP_DETAIL_RESPONSE_MAX_CHARS = 8000
MCP_DETAIL_CONTENT_MAX_CHARS = 7000
MCP_RESPONSE_MAX_CHARS = 8000
MCP_STRUCTURED_LIST_MAX_CHARS = 6000

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    Server = None  # type: ignore[assignment]
    stdio_server = None  # type: ignore[assignment]

    @dataclass(frozen=True)
    class TextContent:  # type: ignore[no-redef]
        type: str
        text: str

    @dataclass(frozen=True)
    class Tool:  # type: ignore[no-redef]
        name: str
        description: str
        inputSchema: dict[str, Any]


server = Server(APP_NAME) if Server is not None else None


def mcp_tools() -> list[Tool]:
    return [
        Tool(
            name="list_workflows",
            description="List available workspace workflows and latest run status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "include_slow": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include workflows tagged 'slow'. Default: skipped.",
                    },
                },
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="validate_workflow",
            description="Validate a workflow and run preflight capability checks without executing it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "workflow_name": {"type": "string"},
                },
                "required": ["workspace_root", "workflow_name"],
            },
        ),
        Tool(
            name="run_workflow",
            description="Run a workflow. Defaults to dry-run. approved requires MCP workspace whitelist.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "workflow_name": {"type": "string"},
                    "inputs_file": {"type": "string"},
                    "run_profile": {"type": "string", "enum": ["dry-run", "supervised", "approved"], "default": "dry-run"},
                },
                "required": ["workspace_root", "workflow_name"],
            },
        ),
        Tool(
            name="get_run_report",
            description="Return a completed run report as markdown or redacted JSON.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "run_id": {"type": "string"},
                    "format": {"type": "string", "enum": ["markdown", "json"], "default": "markdown"},
                },
                "required": ["workspace_root", "run_id"],
            },
        ),
        Tool(
            name="list_run_artifacts",
            description="List reports, screenshots, downloads, and run artifacts for a completed run.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "run_id": {"type": "string"},
                },
                "required": ["workspace_root", "run_id"],
            },
        ),
        Tool(
            name="get_workspace_dashboard",
            description="Return a compact workspace health dashboard for coding agents.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "format": {"type": "string", "enum": ["markdown", "json"], "default": "markdown"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="get_latest_failure",
            description="Return the latest failed workflow report, including diagnosis when available.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "format": {"type": "string", "enum": ["markdown", "json"], "default": "markdown"},
                },
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="summarize_latest_failure",
            description=(
                "Get a token-efficient summary (<=400 tokens) of the latest workflow failure. "
                "Use this instead of reading full run reports to save tokens."
            ),
            inputSchema={
                "type": "object",
                "properties": {"workspace_root": {"type": "string"}},
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="get_session_context",
            description=(
                "Get a compact context snapshot (<=500 tokens) to resume work in a new chat. "
                "Returns pass/fail status, latest failure, and suggested next action."
            ),
            inputSchema={
                "type": "object",
                "properties": {"workspace_root": {"type": "string"}},
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="run_verification",
            description=(
                "Run verification-tagged workflows and return an AI-friendly report (<=800 tokens). "
                "Use after code changes to confirm workflows still pass."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}, "default": ["verification"]},
                    "workflow": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                        "description": "Optional workflow names or workspace-relative paths to run.",
                    },
                    "max_workflows": {"type": "integer", "default": 10},
                    "run_profile": {"type": "string", "enum": ["dry-run", "supervised"], "default": "dry-run"},
                    "include_slow": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include workflows tagged 'slow'. Default: skipped.",
                    },
                },
                "required": ["workspace_root"],
            },
        ),
    ]


if server is not None:

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return mcp_tools()

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        return await call_tool(name, arguments)


async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> list[TextContent]:
    args = arguments or {}
    workspace = _workspace_for_audit(args)
    _audit_mcp_call(workspace, name, args, {"status": "started", "phase": "entry"})
    try:
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "list_workflows": list_workflows_payload,
            "validate_workflow": validate_workflow_payload,
            "run_workflow": run_workflow_payload,
            "get_run_report": get_run_report_payload,
            "list_run_artifacts": list_run_artifacts_payload,
            "get_workspace_dashboard": get_workspace_dashboard_payload,
            "get_latest_failure": get_latest_failure_payload,
            "summarize_latest_failure": summarize_latest_failure_payload,
            "get_session_context": get_session_context_payload,
            "run_verification": run_verification_payload,
        }
        if name not in handlers:
            raise ValueError(f"Unknown tool: {name}")
        payload = handlers[name](args)
        _audit_mcp_call(workspace, name, args, payload)
        return _ok_json(payload)
    except Exception as exc:
        payload = mcp_error_payload(f"{type(exc).__name__}: {exc}")
        _audit_mcp_call(workspace, name, args, payload)
        return _ok_json(payload)


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


def run_workflow_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    workflow_name = require_str(args, "workflow_name")
    run_profile = str(args.get("run_profile") or "dry-run")
    if run_profile not in RUN_PROFILE_ORDER:
        raise ValueError(f"Unsupported run_profile: {run_profile}")
    effective_run_profile = enforce_mcp_run_profile(workspace, workflow_name, run_profile)
    inputs_file = args.get("inputs_file")
    inputs = load_workspace_inputs(workspace, None, str(inputs_file)) if inputs_file else {}
    result = run_workspace_workflow(
        workspace,
        workflow_name,
        inputs=inputs,
        dry_run=effective_run_profile == "dry-run",
        run_profile=effective_run_profile,
        export_report=True,
    )
    failed_steps = [
        {"id": step.id, "action": step.action, "message": step.message}
        for step in result.steps
        if getattr(step.status, "value", str(step.status)) == "failed"
    ]
    return {
        "schema_version": 1,
        "run_id": result.run_id,
        "workflow": result.workflow_name,
        "run_profile": result.run_profile,
        "requested_run_profile": run_profile,
        "status": "failed" if failed_steps else "success",
        "step_count": len(result.steps),
        "failed_steps": failed_steps,
        "report_hint": f"Use get_run_report with run_id='{result.run_id}' for full details.",
    }


def get_run_report_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    run_id = require_str(args, "run_id")
    fmt = str(args.get("format") or "markdown")
    detail = build_report_detail(workspace, run_id)
    if not detail:
        raise FileNotFoundError(f"Run report not found: {run_id}")
    safe_detail = scrub_secrets(detail)
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


def get_latest_failure_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    fmt = str(args.get("format") or "markdown")
    index = build_workspace_report_index(workspace, failed_only=True)
    entries = index.get("entries") if isinstance(index.get("entries"), list) else []
    if not entries:
        return {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "status": "none",
            "message": "No failed workflow reports found.",
            "report": None,
        }
    latest = entries[0]
    run_id = str(latest.get("run_id") or "")
    detail = scrub_secrets(build_report_detail(workspace, run_id))
    if fmt == "markdown":
        content, truncated = budget_mcp_text(report_detail_to_markdown(detail), max_chars=MCP_DETAIL_CONTENT_MAX_CHARS)
        return {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "status": "found",
            "run_id": run_id,
            "format": "markdown",
            "content": content,
            "truncated": truncated,
            "within_budget": len(content) <= MCP_DETAIL_CONTENT_MAX_CHARS,
        }
    if fmt != "json":
        raise ValueError(f"Unsupported report format: {fmt}")
    report, truncated = budget_mcp_report_dict(detail)
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "status": "found",
        "run_id": run_id,
        "format": "json",
        "report": report,
        "truncated": truncated,
        "within_budget": len(json.dumps(report, ensure_ascii=False, default=str)) <= MCP_DETAIL_RESPONSE_MAX_CHARS,
    }


def summarize_latest_failure_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .failure_summary import build_failure_summary

    workspace = require_workspace(args)
    return {"schema_version": 1, "workspace": str(workspace.root), **build_failure_summary(workspace.root)}


def get_session_context_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .session import load_agent_session, session_to_snapshot_text

    workspace = require_workspace(args)
    session = load_agent_session(workspace.root)
    if session is None:
        snapshot = "No session data yet. Run a workflow first."
        return {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "snapshot": snapshot,
            "token_estimate": len(snapshot) // 4,
            "within_budget": True,
        }
    snapshot = session_to_snapshot_text(session)
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "snapshot": snapshot,
        "token_estimate": len(snapshot) // 4,
        "within_budget": len(snapshot) <= 2000,
    }


def run_verification_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .verify import run_verify, verify_to_markdown

    workspace = require_workspace(args)
    run_profile = str(args.get("run_profile") or "dry-run")
    if run_profile not in {"dry-run", "supervised"}:
        raise ValueError(f"Unsupported run_profile: {run_profile}")
    raw_tags = args.get("tags") or ["verification"]
    if not isinstance(raw_tags, list):
        raise ValueError("tags must be an array of strings")
    raw_workflows = args.get("workflow") or []
    if isinstance(raw_workflows, str):
        raw_workflows = [raw_workflows]
    if not isinstance(raw_workflows, list):
        raise ValueError("workflow must be a string or an array of strings")
    max_workflows = int(args.get("max_workflows") or 10)
    include_slow = bool(args.get("include_slow", False))
    report = run_verify(
        workspace,
        tags=tuple(str(item) for item in raw_tags),
        workflow_names=tuple(str(item) for item in raw_workflows),
        max_workflows=max_workflows,
        run_profile=run_profile,
        include_slow=include_slow,
    )
    content = verify_to_markdown(report)
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "format": "markdown",
        "content": content,
        "total": report.total,
        "passed": report.passed,
        "failed": report.failed,
        "token_estimate": len(content) // 4,
        "within_budget": len(content) <= 3200,
    }


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
    root = require_str(args, "workspace_root")
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


def _ok_json(payload: dict[str, Any]) -> list[TextContent]:
    safe_payload = budget_mcp_payload(scrub_secrets(payload))
    text = json.dumps(safe_payload, ensure_ascii=False, indent=2, default=str)
    return [TextContent(type="text", text=text)]


def budget_mcp_payload(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(text) <= MCP_RESPONSE_MAX_CHARS:
        return payload

    summary: dict[str, Any] = {}
    for key in (
        "schema_version",
        "workspace",
        "run_id",
        "workflow",
        "workflow_name",
        "status",
        "format",
        "error",
        "hint",
        "report_hint",
        "workflow_count",
        "artifact_count",
        "total",
        "passed",
        "failed",
    ):
        if key in payload:
            summary[key] = payload[key]
    summary.update(
        {
            "truncated": True,
            "within_budget": True,
            "truncation_reason": "MCP tool response exceeded the 2000-token response budget.",
            "available_keys": sorted(str(key) for key in payload.keys()),
        }
    )
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    if len(summary_text) <= MCP_RESPONSE_MAX_CHARS:
        return summary

    return {
        "schema_version": payload.get("schema_version", 1),
        "truncated": True,
        "within_budget": True,
        "truncation_reason": "MCP tool response exceeded the 2000-token response budget.",
    }


def mcp_error_payload(message: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "error": message,
        "hint": "Check workspace_root and workflow name. Use list_workflows to see available workflows.",
    }


def _workspace_for_audit(args: dict[str, Any]) -> Workspace | None:
    try:
        return require_workspace(args)
    except Exception:
        return None


def _audit_mcp_call(workspace: Workspace | None, tool_name: str, args: dict[str, Any], payload: dict[str, Any]) -> None:
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


def main() -> None:
    if server is None or stdio_server is None:
        raise RuntimeError("MCP support is not installed. Run: pip install -e .[mcp]")
    asyncio.run(_run())


async def _run() -> None:
    if server is None or stdio_server is None:
        raise RuntimeError("MCP support is not installed. Run: pip install -e .[mcp]")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()
