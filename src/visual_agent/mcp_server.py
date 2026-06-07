from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .console import build_report_detail, build_workspace_dashboard, dashboard_to_markdown, find_report_json_path, report_detail_to_markdown
from .gui import write_gui_action_event
from .models import to_jsonable
from .preflight import run_preflight
from .reports import compact_run_report, list_run_summaries
from .security import scrub_secrets
from .validation import validate_workflow_file
from .verification_status import enrich_verification_payload, report_artifacts, write_verification_status
from .workflow import parse_workflow_file
from .workspace import Workspace, build_workspace_report_index, discover_workflows, find_workflow, load_workspace_inputs, run_workspace_workflow, workspace_report_access_payload


APP_NAME = "visual-agent"
APP_VERSION = "0.1.0"
RUN_PROFILE_ORDER = {"dry-run": 0, "supervised": 1, "semi-auto": 1, "approved": 2}
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
                    "run_profile": {"type": "string", "enum": ["dry-run", "supervised", "semi-auto", "approved"], "default": "dry-run"},
                    "verbose": {"type": "boolean", "default": False, "description": "Return verbose run summary instead of compact report."},
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
            name="diagnose_failure",
            description=(
                "Build an AI-readable evidence pack for the latest failed workflow run, including "
                "failed step, deterministic diagnosis, artifacts, and workflow YAML excerpt."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "run_id": {"type": "string", "description": "Optional run id. Defaults to latest failed run."},
                    "max_chars": {"type": "integer", "default": 12000},
                },
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="repair_workflow",
            description=(
                "Suggest a safe workflow/app repair from failure evidence. Defaults to deterministic "
                "advice; provider='anthropic' or 'openai' enables model-generated advice when configured."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "run_id": {"type": "string", "description": "Optional run id. Defaults to latest failed run."},
                    "provider": {"type": "string", "enum": ["none", "anthropic", "openai"], "default": "none"},
                    "model": {"type": "string"},
                    "max_chars": {"type": "integer", "default": 12000},
                    "apply": {
                        "type": "boolean",
                        "default": False,
                        "description": "Apply a high-confidence deterministic workflow patch and create a backup.",
                    },
                    "min_confidence": {"type": "number", "default": 0.75},
                    "verify": {
                        "type": "boolean",
                        "default": False,
                        "description": "Rerun the repaired workflow after apply. Defaults to dry-run.",
                    },
                    "verify_run_profile": {"type": "string", "enum": ["dry-run", "supervised", "semi-auto"], "default": "dry-run"},
                    "inputs_file": {"type": "string"},
                    "rollback_on_fail": {
                        "type": "boolean",
                        "default": False,
                        "description": "Restore the workflow backup when verification fails.",
                    },
                    "candidate_id": {
                        "type": "string",
                        "description": "Repair candidate id to apply. Default: deterministic workflow patch when available.",
                    },
                },
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="auto_repair_failure",
            description=(
                "Diagnose the latest failure, apply only a safe deterministic workflow repair, "
                "verify it, and rollback automatically if verification fails."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "run_id": {"type": "string", "description": "Optional run id. Defaults to latest failed run."},
                    "max_chars": {"type": "integer", "default": 12000},
                    "min_confidence": {"type": "number", "default": 0.75},
                    "verify_run_profile": {"type": "string", "enum": ["dry-run", "supervised", "semi-auto"], "default": "dry-run"},
                    "inputs_file": {"type": "string"},
                    "candidate_id": {"type": "string"},
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": "Preview the selected repair candidate without applying or verifying.",
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Apply even when repair health is high risk.",
                    },
                    "promote_regression": {
                        "type": "boolean",
                        "default": False,
                        "description": "After verified auto repair, export and promote the failed run as a regression test.",
                    },
                    "overwrite_regression": {
                        "type": "boolean",
                        "default": False,
                        "description": "Overwrite existing regression export/test when promoting.",
                    },
                    "run_regression": {
                        "type": "boolean",
                        "default": False,
                        "description": "Run workspace regression tests after promotion.",
                    },
                    "regression_timeout_seconds": {
                        "type": "number",
                        "default": 120,
                        "description": "Timeout for run_regression.",
                    },
                },
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="list_repair_history",
            description="List recent workflow repair attempts recorded in repair_history.jsonl.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                    "workflow": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="rollback_repair",
            description="Rollback a workflow from a recorded repair backup.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "history_id": {"type": "string"},
                    "workflow": {"type": "string"},
                },
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="get_repair_health",
            description="Summarize repair reliability, verification, and rollback risk from repair_history.jsonl.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                    "workflow": {"type": "string"},
                },
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="list_benchmarks",
            description="List public reference benchmark projects and scenarios for real-world Visual Agent testing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="build_benchmark_plan",
            description="Create an executable Visual Agent benchmark coverage plan from public reference benchmarks.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "category": {"type": "string"},
                    "benchmark_id": {"type": "string"},
                },
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="build_benchmark_draft",
            description="Generate a local workflow YAML draft for one benchmark scenario. Defaults to preview without saving.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "scenario_id": {"type": "string"},
                    "output": {"type": "string"},
                    "save": {"type": "boolean", "default": False},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["workspace_root", "scenario_id"],
            },
        ),
        Tool(
            name="run_browser_smoke",
            description="Open a URL in a real browser, check blank/error state, optionally click once, and return screenshots/diagnostics.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "url": {"type": "string"},
                    "headed": {"type": "boolean", "default": False},
                    "timeout_ms": {"type": "integer", "default": 10000},
                    "min_text_length": {"type": "integer", "default": 1},
                    "min_interactive": {"type": "integer", "default": 0},
                    "expect_text": {"type": "array", "items": {"type": "string"}},
                    "expect_url_contains": {"type": "array", "items": {"type": "string"}},
                    "fill": {"type": "array", "items": {"type": "string"}, "description": "Semantic input fills formatted as label=value."},
                    "fill_selector": {"type": "array", "items": {"type": "string"}, "description": "CSS input fills formatted as selector=value."},
                    "click_text": {"type": "string"},
                    "click_selector": {"type": "string"},
                    "require_change_after_click": {"type": "boolean", "default": False},
                    "wait_for_text_after": {"type": "array", "items": {"type": "string"}},
                    "wait_for_url_contains_after": {"type": "array", "items": {"type": "string"}},
                    "wait_timeout_seconds": {"type": "number", "default": 5},
                    "expect_text_after": {"type": "array", "items": {"type": "string"}},
                    "expect_url_contains_after": {"type": "array", "items": {"type": "string"}},
                    "save_workflow": {"type": "string", "description": "Optional workspace-relative workflow YAML path to save."},
                    "overwrite_workflow": {"type": "boolean", "default": False},
                },
                "required": ["workspace_root", "url"],
            },
        ),
        Tool(
            name="run_browser_smoke_suite",
            description="Run a JSON/YAML browser smoke suite and return a batch summary with case artifacts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "suite_file": {"type": "string"},
                    "headed": {"type": "boolean", "default": False},
                },
                "required": ["workspace_root", "suite_file"],
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
            name="save_task_context",
            description=(
                "Save the AI assistant's current task state before switching windows. "
                "The saved context is included in get_session_context/context-snapshot."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "task": {"type": "string", "description": "What you are currently trying to accomplish."},
                    "analyzed_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                    "root_cause": {"type": "string", "default": ""},
                    "plan": {"type": "string", "default": ""},
                    "tried": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
                "required": ["workspace_root", "task"],
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
                    "run_profile": {"type": "string", "enum": ["dry-run", "supervised", "semi-auto"], "default": "dry-run"},
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
            name="generate_workflow_from_context",
            description=(
                "Generate a verification workflow from code changes using static code context. "
                "Call this after writing or modifying UI code. Returns workflow path, detected UI semantics, "
                "and a quality score with gaps when assertions are weak."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "task_description": {"type": "string"},
                    "code_changes": {
                        "type": "array",
                        "description": "Optional. If omitted, Visual Agent reads git diff from repo_root.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_path": {"type": "string"},
                                "before": {"type": ["string", "null"]},
                                "after": {"type": "string"},
                                "change_type": {"type": "string", "enum": ["added", "modified", "deleted"]},
                            },
                            "required": ["file_path", "after", "change_type"],
                        },
                    },
                    "base_url": {"type": "string"},
                    "repo_root": {"type": "string", "default": "."},
                    "base": {"type": "string", "default": "HEAD"},
                    "include_untracked": {"type": "boolean", "default": True},
                    "framework_hint": {"type": "string"},
                    "model": {"type": "string", "default": "claude-haiku-4-5-20251001"},
                    "dry_run": {"type": "boolean", "default": False},
                },
                "required": ["workspace_root", "task_description", "base_url"],
            },
        ),
        Tool(
            name="verify_implementation",
            description=(
                "Generate a workflow from code changes, run it, and return pass/fail with compact diagnosis. "
                "This is the single-call verification loop for AI coding assistants."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "task_description": {"type": "string"},
                    "code_changes": {
                        "type": "array",
                        "description": "Optional. If omitted, Visual Agent reads git diff from repo_root.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_path": {"type": "string"},
                                "before": {"type": ["string", "null"]},
                                "after": {"type": "string"},
                                "change_type": {"type": "string", "enum": ["added", "modified", "deleted"]},
                            },
                            "required": ["file_path", "after", "change_type"],
                        },
                    },
                    "base_url": {"type": "string"},
                    "repo_root": {"type": "string", "default": "."},
                    "base": {"type": "string", "default": "HEAD"},
                    "include_untracked": {"type": "boolean", "default": True},
                    "framework_hint": {"type": "string"},
                    "model": {"type": "string", "default": "claude-haiku-4-5-20251001"},
                    "inputs": {"type": "object"},
                    "run_profile": {"type": "string", "enum": ["dry-run", "supervised", "semi-auto", "approved"], "default": "supervised"},
                    "min_quality_score": {
                        "type": "number",
                        "default": 0.6,
                        "description": "Minimum generated workflow quality required before running verification.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "default": 30,
                        "description": "Maximum seconds to wait for the generated workflow run before returning timeout.",
                    },
                    "run_negative": {
                        "type": "boolean",
                        "default": False,
                        "description": "Opt in to run the generated negative workflow draft after the success-path workflow passes.",
                    },
                },
                "required": ["workspace_root", "task_description", "base_url"],
            },
        ),
        Tool(
            name="generate_workflow",
            description=(
                "Generate a workflow YAML from a natural language description. "
                "Use this after adding UI features to create a reusable verification workflow."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "description": {
                        "type": "string",
                        "description": "Natural language description of what to verify.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": "Return YAML without saving it to disk.",
                    },
                    "model": {
                        "type": "string",
                        "default": "claude-haiku-4-5-20251001",
                    },
                },
                "required": ["workspace_root", "description"],
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
            "diagnose_failure": diagnose_failure_payload,
            "repair_workflow": repair_workflow_payload,
            "auto_repair_failure": auto_repair_failure_payload,
            "list_repair_history": list_repair_history_payload,
            "rollback_repair": rollback_repair_payload,
            "get_repair_health": get_repair_health_payload,
            "list_benchmarks": list_benchmarks_payload,
            "build_benchmark_plan": build_benchmark_plan_payload,
            "build_benchmark_draft": build_benchmark_draft_payload,
            "run_browser_smoke": run_browser_smoke_payload,
            "run_browser_smoke_suite": run_browser_smoke_suite_payload,
            "get_session_context": get_session_context_payload,
            "save_task_context": save_task_context_payload,
            "run_verification": run_verification_payload,
            "generate_workflow_from_context": generate_workflow_from_context_payload,
            "verify_implementation": verify_implementation_payload,
            "generate_workflow": generate_workflow_payload,
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
    if not bool(args.get("verbose", False)):
        return {
            **compact_run_report(result),
            "requested_run_profile": run_profile,
            "report_hint": f"Use get_run_report with run_id='{result.run_id}' for full details.",
        }
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


def diagnose_failure_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .repair import build_failure_evidence_pack

    workspace = require_workspace(args)
    return build_failure_evidence_pack(
        workspace.root,
        run_id=str(args.get("run_id") or "") or None,
        max_chars=int(args.get("max_chars") or 12000),
    )


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
        verify=bool(args.get("verify", False)),
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


def list_benchmarks_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .benchmarks import list_public_benchmarks

    workspace = require_workspace(args)
    return {"workspace": str(workspace.root), **list_public_benchmarks(category=str(args.get("category") or "") or None)}


def build_benchmark_plan_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .benchmarks import build_benchmark_plan

    workspace = require_workspace(args)
    return {
        "workspace": str(workspace.root),
        **build_benchmark_plan(
            category=str(args.get("category") or "") or None,
            benchmark_id=str(args.get("benchmark_id") or "") or None,
        ),
    }


def build_benchmark_draft_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .benchmarks import build_benchmark_workflow_draft

    workspace = require_workspace(args)
    output = str(args.get("output") or "") or None
    output_path = (workspace.root / output).resolve() if output else None
    return {
        "workspace": str(workspace.root),
        **build_benchmark_workflow_draft(
            scenario_id=require_str(args, "scenario_id"),
            workspace_root=workspace.root,
            output_path=output_path,
            dry_run=not bool(args.get("save", False)),
            overwrite=bool(args.get("overwrite", False)),
        ),
    }


def run_browser_smoke_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .browser_smoke import run_browser_smoke

    workspace = require_workspace(args)
    return {
        "workspace": str(workspace.root),
        **run_browser_smoke(
            url=require_str(args, "url"),
            output_dir=workspace.root / "browser-smoke-runs",
            headed=bool(args.get("headed", False)),
            timeout_ms=int(args.get("timeout_ms") or 10_000),
            min_text_length=int(args.get("min_text_length") or 1),
            min_interactive=int(args.get("min_interactive") or 0),
            expect_text=[str(item) for item in args.get("expect_text", []) if str(item)],
            expect_url_contains=[str(item) for item in args.get("expect_url_contains", []) if str(item)],
            fill=[str(item) for item in args.get("fill", []) if str(item)],
            fill_selector=[str(item) for item in args.get("fill_selector", []) if str(item)],
            click_text=str(args.get("click_text") or "") or None,
            click_selector=str(args.get("click_selector") or "") or None,
            require_change_after_click=bool(args.get("require_change_after_click", False)),
            wait_for_text_after=[str(item) for item in args.get("wait_for_text_after", []) if str(item)],
            wait_for_url_contains_after=[str(item) for item in args.get("wait_for_url_contains_after", []) if str(item)],
            wait_timeout_seconds=float(args.get("wait_timeout_seconds") or 5.0),
            expect_text_after=[str(item) for item in args.get("expect_text_after", []) if str(item)],
            expect_url_contains_after=[str(item) for item in args.get("expect_url_contains_after", []) if str(item)],
            save_workflow=(workspace.root / str(args.get("save_workflow"))).resolve() if str(args.get("save_workflow") or "").strip() else None,
            overwrite_workflow=bool(args.get("overwrite_workflow", False)),
        ),
    }


def run_browser_smoke_suite_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .browser_smoke_suite import run_browser_smoke_suite

    workspace = require_workspace(args)
    suite_file = (workspace.root / require_str(args, "suite_file")).resolve()
    return {
        "workspace": str(workspace.root),
        **run_browser_smoke_suite(
            suite_file,
            output_dir=workspace.root / "browser-smoke-suite-runs",
            headed=True if bool(args.get("headed", False)) else None,
        ),
    }


def get_session_context_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .session import load_agent_session, workspace_session_snapshot_text

    workspace = require_workspace(args)
    session = load_agent_session(workspace.root)
    if session is None:
        snapshot = workspace_session_snapshot_text(workspace.root)
        return {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "snapshot": snapshot,
            "token_estimate": len(snapshot) // 4,
            "within_budget": True,
        }
    snapshot = workspace_session_snapshot_text(workspace.root)
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "snapshot": snapshot,
        "token_estimate": len(snapshot) // 4,
        "within_budget": len(snapshot) <= 2000,
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


def run_verification_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .verify import run_verify, verify_to_markdown

    workspace = require_workspace(args)
    run_profile = str(args.get("run_profile") or "dry-run")
    if run_profile not in {"dry-run", "supervised", "semi-auto"}:
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


def generate_workflow_from_context_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .workflow_synthesis import generate_workflow_from_context

    workspace = require_workspace(args)
    ctx = generation_context_from_args(args, workspace)
    result = generate_workflow_from_context(
        ctx=ctx,
        dry_run=bool(args.get("dry_run", False)),
        model_id=str(args.get("model") or "claude-haiku-4-5-20251001"),
    )
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        **workflow_generation_response(result, include_yaml=bool(args.get("dry_run", False))),
    }


def verify_implementation_payload(args: dict[str, Any]) -> dict[str, Any]:
    from time import monotonic

    from .workflow_synthesis import generate_workflow_from_context

    workspace = require_workspace(args)
    run_profile = str(args.get("run_profile") or "supervised")
    if run_profile not in RUN_PROFILE_ORDER:
        raise ValueError(f"Unsupported run_profile: {run_profile}")
    ctx = generation_context_from_args(args, workspace)
    started = monotonic()
    generation = generate_workflow_from_context(
        ctx=ctx,
        dry_run=False,
        model_id=str(args.get("model") or "claude-haiku-4-5-20251001"),
    )
    if not generation.workflow_path:
        raise RuntimeError("workflow generation did not produce a workflow path")
    min_quality_score = float(args.get("min_quality_score") if args.get("min_quality_score") is not None else 0.6)
    if generation.quality_score.total_score < min_quality_score:
        elapsed_ms = int((monotonic() - started) * 1000)
        payload = {
            "workspace": str(workspace.root),
            "result": "needs_workflow_improvement",
            "workflow_name": generation.workflow_name,
            "workflow_path": generation.workflow_path,
            "run_id": None,
            "run_profile": None,
            "requested_run_profile": run_profile,
            "quality_score": generation.quality_score.total_score,
            "quality": quality_gate_payload(generation.quality_score),
            "semantic_summary": semantic_summary_payload(generation),
            "generation_trace": list(generation.generation_trace[:10]),
            "min_quality_score": min_quality_score,
            "steps_passed": 0,
            "steps_total": 0,
            "duration_ms": elapsed_ms,
            "message": "Generated workflow quality is below the verification threshold; improve assertions before running implementation verification.",
        }
        payload = enrich_verification_payload(payload, workspace_root=workspace.root)
        _write_vscode_status(workspace.root, payload)
        return scrub_secrets(payload)
    effective_run_profile = enforce_mcp_run_profile(workspace, generation.workflow_name, run_profile)
    raw_inputs, inputs_source = generated_workflow_inputs(args, generation)
    timeout_seconds = float(args.get("timeout_seconds") if args.get("timeout_seconds") is not None else 30.0)
    if timeout_seconds <= 0:
        payload = verification_timeout_payload(
            workspace=workspace,
            generation=generation,
            run_profile=run_profile,
            effective_run_profile=effective_run_profile,
            inputs_source=inputs_source,
            timeout_seconds=timeout_seconds,
            started=started,
        )
        _write_vscode_status(workspace.root, payload)
        return scrub_secrets(payload)
    try:
        result = run_workspace_workflow_with_timeout(
            workspace,
            generation.workflow_name,
            inputs={str(key): value for key, value in raw_inputs.items()},
            dry_run=effective_run_profile == "dry-run",
            run_profile=effective_run_profile,
            timeout_seconds=timeout_seconds,
        )
    except TimeoutError:
        payload = verification_timeout_payload(
            workspace=workspace,
            generation=generation,
            run_profile=run_profile,
            effective_run_profile=effective_run_profile,
            inputs_source=inputs_source,
            timeout_seconds=timeout_seconds,
            started=started,
        )
        _write_vscode_status(workspace.root, payload)
        return scrub_secrets(payload)
    failed = next((step for step in result.steps if getattr(step.status, "value", str(step.status)) == "failed"), None)
    steps_passed = sum(1 for step in result.steps if getattr(step.status, "value", str(step.status)) in {"success", "dry_run"})
    elapsed_ms = int((monotonic() - started) * 1000)
    if failed is None:
        payload = {
            "workspace": str(workspace.root),
            "result": "pass",
            "workflow_name": generation.workflow_name,
            "workflow_path": generation.workflow_path,
            "run_id": result.run_id,
            "run_profile": result.run_profile,
            "requested_run_profile": run_profile,
            "quality_score": generation.quality_score.total_score,
            "quality": quality_gate_payload(generation.quality_score),
            "semantic_summary": semantic_summary_payload(generation),
            "generation_trace": list(generation.generation_trace[:10]),
            "inputs_path": generation.inputs_path,
            "inputs_source": inputs_source,
            "steps_passed": steps_passed,
            "steps_total": len(result.steps),
            "duration_ms": elapsed_ms,
            "message": "All steps passed. Implementation verified.",
        }
        if bool(args.get("run_negative", False)):
            payload["negative_verification"] = run_negative_workflow_verification(
                workspace,
                generation,
                run_profile=effective_run_profile,
                timeout_seconds=timeout_seconds,
            )
    else:
        payload = {
            "workspace": str(workspace.root),
            "result": "fail",
            "workflow_name": generation.workflow_name,
            "workflow_path": generation.workflow_path,
            "run_id": result.run_id,
            "run_profile": result.run_profile,
            "requested_run_profile": run_profile,
            "quality_score": generation.quality_score.total_score,
            "quality": quality_gate_payload(generation.quality_score),
            "semantic_summary": semantic_summary_payload(generation),
            "generation_trace": list(generation.generation_trace[:10]),
            "inputs_path": generation.inputs_path,
            "inputs_source": inputs_source,
            "failed_step": failed_step_payload(failed),
            "screenshot_path": failed_screenshot_path(failed),
            "steps_passed": steps_passed,
            "steps_total": len(result.steps),
            "duration_ms": elapsed_ms,
        }
    payload = enrich_verification_payload(payload, workspace_root=workspace.root)
    _write_vscode_status(workspace.root, payload)
    return scrub_secrets(payload)


def run_negative_workflow_verification(
    workspace: Workspace,
    generation: Any,
    *,
    run_profile: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    negative_path = str(getattr(generation, "negative_workflow_path", "") or "")
    reset_strategy = str(getattr(generation, "negative_workflow_reset_strategy", "") or "")
    negative_oracles = sanitize_negative_oracles(getattr(generation, "negative_oracles", ()) or ())
    if not negative_path:
        payload = {
            "requested": True,
            "status": "skipped",
            "reason": "no_negative_workflow",
            "workflow_path": None,
            "reset_strategy": reset_strategy,
            "oracles": negative_oracles,
        }
        payload["next_action"] = negative_verification_next_action(payload)
        return payload
    if not bool(getattr(generation, "negative_workflow_ready", False)):
        payload = {
            "requested": True,
            "status": "skipped",
            "reason": str(getattr(generation, "negative_workflow_reason", "") or "not_ready"),
            "workflow_name": f"{generation.workflow_name}_negative_draft",
            "workflow_path": negative_path,
            "reset_strategy": reset_strategy,
            "oracles": negative_oracles,
        }
        payload["next_action"] = negative_verification_next_action(payload)
        return payload
    workflow_name = f"{generation.workflow_name}_negative_draft"
    try:
        result = run_workspace_workflow_with_timeout(
            workspace,
            workflow_name,
            inputs={},
            dry_run=run_profile == "dry-run",
            run_profile=run_profile,
            timeout_seconds=timeout_seconds,
        )
    except TimeoutError:
        payload = {
            "requested": True,
            "status": "timeout",
            "workflow_name": workflow_name,
            "workflow_path": negative_path,
            "timeout_seconds": timeout_seconds,
            "reset_strategy": reset_strategy,
            "oracles": negative_oracles,
        }
        payload.update(report_artifacts(workspace.root, None))
        payload["next_action"] = negative_verification_next_action(payload)
        return payload
    failed = next((step for step in result.steps if getattr(step.status, "value", str(step.status)) == "failed"), None)
    steps_passed = sum(1 for step in result.steps if getattr(step.status, "value", str(step.status)) in {"success", "dry_run"})
    payload: dict[str, Any] = {
        "requested": True,
        "status": "pass" if failed is None else "fail",
        "workflow_name": workflow_name,
        "workflow_path": negative_path,
        "run_id": result.run_id,
        "run_profile": result.run_profile,
        "reset_strategy": reset_strategy,
        "oracles": negative_oracles,
        "steps_passed": steps_passed,
        "steps_total": len(result.steps),
    }
    if failed is not None:
        payload["failed_step"] = failed_step_payload(failed)
    payload.update(report_artifacts(workspace.root, result.run_id))
    payload["next_action"] = negative_verification_next_action(payload)
    return payload


def negative_verification_next_action(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "")
    reason = str(payload.get("reason") or "")
    if reason == "no_negative_oracle":
        return "Add or expose parsed validation error text before treating negative verification as executable."
    if reason == "no_negative_workflow":
        return "Add validation rules to generate a negative workflow draft."
    if status == "pass":
        return "Negative validation passed. Keep it opt-in until reset/oracle coverage is broader."
    if status == "fail":
        return "Inspect the negative verification report and decide whether the implementation or negative oracle needs adjustment."
    if status == "timeout":
        return "Increase timeout_seconds or narrow the negative workflow before rerunning negative verification."
    return "Review the generated negative workflow readiness reason before running negative verification."


def sanitize_negative_oracles(oracles: Any) -> list[dict[str, str]]:
    if not isinstance(oracles, (list, tuple)):
        return []
    sanitized: list[dict[str, str]] = []
    for item in oracles[:5]:
        if not isinstance(item, dict):
            continue
        sanitized.append(
            {
                "text": str(scrub_secrets(str(item.get("text") or ""))),
                "source": str(scrub_secrets(str(item.get("source") or ""))),
            }
        )
    return sanitized


def generated_workflow_inputs(args: dict[str, Any], generation: Any) -> tuple[dict[str, Any], str]:
    if isinstance(args.get("inputs"), dict):
        return {str(key): value for key, value in args["inputs"].items()}, "explicit"
    if generation.inputs_path:
        path = Path(str(generation.inputs_path))
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return {str(key): value for key, value in data.items()}, "generated_template"
    return {}, "empty"


def generation_context_from_args(args: dict[str, Any], workspace: Workspace) -> Any:
    from .context_ingestion import CodeChange, GenerationContext
    from .git_diff import collect_code_changes

    raw_changes = args.get("code_changes")
    changes: list[CodeChange] = []
    if raw_changes is None:
        repo_root = Path(str(args.get("repo_root") or "."))
        if not repo_root.is_absolute():
            repo_root = (workspace.project_root / repo_root).resolve()
        changes = list(
            collect_code_changes(
                base=str(args.get("base") or "HEAD"),
                cwd=repo_root,
                include_untracked=bool(args.get("include_untracked", True)),
            )
        )
    else:
        if not isinstance(raw_changes, list) or not raw_changes:
            raise ValueError("code_changes must be a non-empty array")
        for index, raw in enumerate(raw_changes):
            if not isinstance(raw, dict):
                raise ValueError(f"code_changes[{index}] must be an object")
            change_type = str(raw.get("change_type") or "")
            if change_type not in {"added", "modified", "deleted"}:
                raise ValueError(f"Unsupported change_type: {change_type}")
            changes.append(
                CodeChange(
                    file_path=str(raw.get("file_path") or ""),
                    before=str(raw["before"]) if raw.get("before") is not None else None,
                    after=str(raw.get("after") or ""),
                    change_type=change_type,  # type: ignore[arg-type]
                )
            )
    if not changes:
        raise ValueError("No code changes found. Provide code_changes or run inside a git repo with changes.")
    return GenerationContext(
        task_description=require_str(args, "task_description"),
        code_changes=tuple(changes),
        base_url=require_str(args, "base_url"),
        project_root=str(workspace.root),
        framework_hint=str(args.get("framework_hint") or "") or None,
    )


def workflow_generation_response(result: Any, *, include_yaml: bool = False) -> dict[str, Any]:
    quality = result.quality_score
    semantic_model = result.semantic_model
    payload = {
        "status": result.status,
        "workflow_name": result.workflow_name,
        "workflow_path": result.workflow_path,
        "inputs_path": result.inputs_path,
        "negative_workflow_path": result.negative_workflow_path,
        "negative_workflow_ready": result.negative_workflow_ready,
        "negative_workflow_reason": result.negative_workflow_reason,
        "negative_workflow_reset_strategy": result.negative_workflow_reset_strategy,
        "negative_oracles": list(result.negative_oracles),
        "generation_method": result.generation_method,
        "quality": {
            "score": quality.total_score,
            "covers_success_path": quality.covers_success_path,
            "covers_error_path": quality.covers_error_path,
            "business_assertions": quality.business_assertion_count,
            "data_display_assertions": quality.data_display_assertion_count,
            "forbidden_error_assertions": quality.forbidden_error_assertion_count,
            "text_from_input_references": quality.text_from_input_reference_count,
            "invalid_text_from_references": list(quality.invalid_text_from_references),
            "gaps": list(quality.gaps[:3]),
            "recommendation": quality.recommendation,
        },
        "framework_detected": semantic_model.framework,
        "confidence": semantic_model.confidence,
        "fields": [field.name for field in semantic_model.form_fields[:8]],
        "success_states": [state.value for state in semantic_model.success_states[:5]],
        "semantic_summary": semantic_summary_payload(result),
        "negative_input_cases": list(result.negative_input_cases[:8]),
        "negative_workflow_yaml": result.negative_workflow_yaml if include_yaml else None,
        "generation_trace": list(result.generation_trace[:10]),
        "warnings": list(result.warnings[:3]),
        "message": result.message,
    }
    if include_yaml:
        payload["yaml"] = result.workflow_yaml
    return scrub_secrets(payload)


def run_workspace_workflow_with_timeout(
    workspace: Workspace,
    workflow_name: str,
    *,
    inputs: dict[str, Any],
    dry_run: bool,
    run_profile: str,
    timeout_seconds: float,
) -> Any:
    import concurrent.futures

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        run_workspace_workflow,
        workspace,
        workflow_name,
        inputs=inputs,
        dry_run=dry_run,
        run_profile=run_profile,
        export_report=True,
    )
    try:
        result = future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        executor.shutdown(wait=False, cancel_futures=True)
        raise TimeoutError(f"workflow run exceeded timeout_seconds={timeout_seconds}") from exc
    executor.shutdown(wait=False)
    return result


def verification_timeout_payload(
    *,
    workspace: Workspace,
    generation: Any,
    run_profile: str,
    effective_run_profile: str,
    inputs_source: str,
    timeout_seconds: float,
    started: float,
) -> dict[str, Any]:
    from time import monotonic

    payload = {
        "workspace": str(workspace.root),
        "result": "timeout",
        "workflow_name": generation.workflow_name,
        "workflow_path": generation.workflow_path,
        "run_id": None,
        "run_profile": effective_run_profile,
        "requested_run_profile": run_profile,
        "quality_score": generation.quality_score.total_score,
        "quality": quality_gate_payload(generation.quality_score),
        "semantic_summary": semantic_summary_payload(generation),
        "generation_trace": list(generation.generation_trace[:10]),
        "inputs_path": generation.inputs_path,
        "inputs_source": inputs_source,
        "timeout_seconds": timeout_seconds,
        "steps_passed": 0,
        "steps_total": 0,
        "duration_ms": int((monotonic() - started) * 1000),
        "message": f"Workflow run timed out after {timeout_seconds:g} seconds.",
    }
    return enrich_verification_payload(payload, workspace_root=workspace.root)


def quality_gate_payload(quality: Any) -> dict[str, Any]:
    return {
        "score": quality.total_score,
        "covers_success_path": quality.covers_success_path,
        "covers_error_path": quality.covers_error_path,
        "business_assertions": quality.business_assertion_count,
        "structural_assertions": quality.structural_assertion_count,
        "data_display_assertions": quality.data_display_assertion_count,
        "forbidden_error_assertions": quality.forbidden_error_assertion_count,
        "text_from_input_references": quality.text_from_input_reference_count,
        "invalid_text_from_references": list(quality.invalid_text_from_references),
        "gaps": list(quality.gaps[:3]),
        "recommendation": quality.recommendation,
    }


def semantic_summary_payload(generation: Any) -> dict[str, Any]:
    from .context_ingestion import summarize_data_displays

    model = generation.semantic_model
    display_summary = summarize_data_displays(model)
    return {
        "framework": model.framework,
        "confidence": model.confidence,
        "generation_method": generation.generation_method,
        "field_count": len(model.form_fields),
        "required_field_count": sum(1 for field in model.form_fields if field.required),
        "sensitive_field_count": sum(1 for field in model.form_fields if field.is_sensitive),
        "validation_rule_count": sum(len(field.validation_rules) for field in model.form_fields),
        "submit_action_count": len(model.submit_actions),
        "success_state_count": len(model.success_states),
        "error_state_count": len(model.error_states),
        "data_display_count": len(model.data_displays),
        "negative_input_case_count": len(generation.negative_input_cases),
        "fields": [field.name for field in model.form_fields[:8]],
        "success_states": [state.value for state in model.success_states[:5]],
        "data_displays": list(model.data_displays[:8]),
        "matched_data_displays": list(display_summary.matched[:8]),
        "unmatched_data_displays": list(display_summary.unmatched[:8]),
        "warnings": list(generation.warnings[:5]),
    }


def failed_step_payload(step: Any) -> dict[str, Any]:
    diagnosis = step.metadata.get("failure_diagnosis") if isinstance(getattr(step, "metadata", None), dict) else None
    expected = ""
    actual = step.message
    if isinstance(diagnosis, dict):
        expected = str(diagnosis.get("expected") or "")
        actual = str(diagnosis.get("actual") or step.message or "")
    return {
        "id": step.id,
        "action": step.action,
        "expected": expected,
        "actual": str(actual)[:500],
        "fix_hint": build_fix_hint(step, expected=expected),
    }


def build_fix_hint(step: Any, *, expected: str = "") -> str:
    if step.action == "assert_text" and expected:
        return f"页面未找到期望文本：{expected}。检查提交后是否渲染成功提示；如果实际文案不同，请更新 assert_text 或运行 repair-workflow。"
    if step.action in {"wait_for", "wait_for_text"} and expected:
        return f"等待期望文本或 URL 超时：{expected}。确认页面会进入该状态，或增加 timeout_ms 后重试。"
    if step.action == "assert_browser_ready":
        return "页面未达到可验证状态。确认 base_url 可访问、dev server 正在运行，并检查首屏是否为空或仍在加载。"
    if step.action in {"click", "paste", "type"}:
        return "The target could not be acted on. Check labels, accessible names, button text, or DOM visibility in the implementation."
    return "Inspect the failed step message and the run report artifacts, then update the implementation or generated workflow semantics."


def failed_screenshot_path(step: Any) -> str | None:
    observation = getattr(step, "observation", None)
    if observation is not None and getattr(observation, "screenshot_path", None):
        return str(observation.screenshot_path)
    diagnosis = step.metadata.get("failure_diagnosis") if isinstance(getattr(step, "metadata", None), dict) else None
    if isinstance(diagnosis, dict):
        artifacts = diagnosis.get("artifacts") if isinstance(diagnosis.get("artifacts"), dict) else {}
        if artifacts.get("screenshot"):
            return str(artifacts["screenshot"])
    return None


def _write_vscode_status(workspace_root: Path, result: dict[str, Any]) -> None:
    write_verification_status(workspace_root, scrub_secrets(result))


def generate_workflow_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .workflow_generator import DEFAULT_MODEL, generate_workflow_yaml

    workspace = require_workspace(args)
    description = require_str(args, "description")
    result = generate_workflow_yaml(
        description=description,
        workspace_root=workspace.root,
        model=str(args.get("model") or DEFAULT_MODEL),
        dry_run=bool(args.get("dry_run", False)),
    )
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        **result,
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
        "source",
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
    repair = payload.get("repair") if isinstance(payload.get("repair"), dict) else None
    if repair:
        candidates = repair.get("candidates") if isinstance(repair.get("candidates"), list) else []
        summary["repair"] = {
            "classification": repair.get("classification"),
            "confidence": repair.get("confidence"),
            "recommended_fix": repair.get("recommended_fix"),
            "apply_supported": repair.get("apply_supported"),
            "selected_candidate_id": repair.get("selected_candidate_id"),
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "id": item.get("id"),
                    "kind": item.get("kind"),
                    "status": item.get("status"),
                    "classification": item.get("classification"),
                    "confidence": item.get("confidence"),
                    "apply_supported": item.get("apply_supported"),
                    "recommended_fix": item.get("recommended_fix"),
                    "reason": item.get("reason"),
                }
                for item in candidates[:5]
                if isinstance(item, dict)
            ],
        }
    plan = payload.get("workflow_repair_plan") if isinstance(payload.get("workflow_repair_plan"), dict) else None
    if plan:
        summary["workflow_repair_plan"] = {
            "status": plan.get("status"),
            "applied": plan.get("applied"),
            "apply_requested": plan.get("apply_requested"),
            "verify_requested": plan.get("verify_requested"),
            "rollback_on_fail": plan.get("rollback_on_fail"),
            "verification": plan.get("verification"),
            "rollback": plan.get("rollback"),
        }
    history = payload.get("history") if isinstance(payload.get("history"), dict) else None
    if history:
        summary["history"] = history
    auto_repair = payload.get("auto_repair") if isinstance(payload.get("auto_repair"), dict) else None
    if auto_repair:
        summary["auto_repair"] = auto_repair
    repair_result = payload.get("repair_result") if isinstance(payload.get("repair_result"), dict) else None
    if repair_result:
        repair = repair_result.get("repair") if isinstance(repair_result.get("repair"), dict) else {}
        plan = repair_result.get("workflow_repair_plan") if isinstance(repair_result.get("workflow_repair_plan"), dict) else {}
        summary["repair_result"] = {
            "status": repair_result.get("status"),
            "source": repair_result.get("source"),
            "workflow": repair_result.get("workflow"),
            "run_id": repair_result.get("run_id"),
            "repair": {
                "classification": repair.get("classification"),
                "confidence": repair.get("confidence"),
                "selected_candidate_id": repair.get("selected_candidate_id"),
                "candidate_count": len(repair.get("candidates")) if isinstance(repair.get("candidates"), list) else 0,
                "apply_supported": repair.get("apply_supported"),
            },
            "workflow_repair_plan": {
                "status": plan.get("status"),
                "applied": plan.get("applied"),
                "verify_requested": plan.get("verify_requested"),
                "rollback_on_fail": plan.get("rollback_on_fail"),
                "verification": plan.get("verification"),
                "rollback": plan.get("rollback"),
            },
        }
    repair_health = payload.get("repair_health") if isinstance(payload.get("repair_health"), dict) else None
    if repair_health:
        summary["repair_health"] = {
            "risk_level": repair_health.get("risk_level"),
            "reliability_score": repair_health.get("reliability_score"),
            "analyzed_entries": repair_health.get("analyzed_entries"),
            "applied_count": repair_health.get("applied_count"),
            "verified_count": repair_health.get("verified_count"),
            "rollback_count": repair_health.get("rollback_count"),
            "recommendation": repair_health.get("recommendation"),
        }
    regression = payload.get("regression") if isinstance(payload.get("regression"), dict) else None
    if regression:
        summary["regression"] = {
            "status": regression.get("status"),
            "run_id": regression.get("run_id"),
            "test_path": regression.get("test_path"),
            "fixture_path": regression.get("fixture_path"),
            "test_run": regression.get("test_run"),
            "reason": regression.get("reason"),
        }
    preflight_health = payload.get("preflight_repair_health") if isinstance(payload.get("preflight_repair_health"), dict) else None
    if preflight_health:
        summary["preflight_repair_health"] = {
            "risk_level": preflight_health.get("risk_level"),
            "reliability_score": preflight_health.get("reliability_score"),
            "analyzed_entries": preflight_health.get("analyzed_entries"),
            "rollback_count": preflight_health.get("rollback_count"),
            "failed_verification_count": preflight_health.get("failed_verification_count"),
            "recommendation": preflight_health.get("recommendation"),
        }
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
