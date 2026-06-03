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
                "properties": {"workspace_root": {"type": "string"}},
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
    latest_by_workflow = {}
    for summary in list_run_summaries(workspace.runs_dir, limit=50):
        latest_by_workflow.setdefault(summary.workflow_name, summary)
    workflows = []
    for ref in discover_workflows(workspace):
        latest = latest_by_workflow.get(ref.name)
        workflows.append(
            {
                "name": ref.name,
                "path": ref.relative_path,
                "last_run_status": latest.status if latest else None,
                "last_run_id": latest.run_id if latest else None,
            }
        )
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "workflow_count": len(workflows),
        "workflows": workflows,
    }


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
        return {"schema_version": 1, "run_id": run_id, "format": "markdown", "content": report_detail_to_markdown(safe_detail)}
    if fmt != "json":
        raise ValueError(f"Unsupported report format: {fmt}")
    return {"schema_version": 1, "run_id": run_id, "format": "json", "report": safe_detail}


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
    return {
        "schema_version": 1,
        "run_id": run_id,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }


def get_workspace_dashboard_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    fmt = str(args.get("format") or "markdown")
    limit = int(args.get("limit") or 5)
    dashboard = scrub_secrets(build_workspace_dashboard(workspace, limit=max(1, min(limit, 25))))
    if fmt == "markdown":
        return {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "format": "markdown",
            "content": dashboard_to_markdown(dashboard),
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
        return {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "status": "found",
            "run_id": run_id,
            "format": "markdown",
            "content": report_detail_to_markdown(detail),
        }
    if fmt != "json":
        raise ValueError(f"Unsupported report format: {fmt}")
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "status": "found",
        "run_id": run_id,
        "format": "json",
        "report": detail,
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
    return [TextContent(type="text", text=json.dumps(scrub_secrets(payload), ensure_ascii=False, indent=2, default=str))]


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
