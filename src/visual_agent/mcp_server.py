from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .console import build_report_detail, report_detail_to_markdown
from .mcp_audit import audit_mcp_call, workspace_for_audit
from .mcp_common import (
    MCP_DETAIL_CONTENT_MAX_CHARS,
    MCP_DETAIL_RESPONSE_MAX_CHARS,
    MCP_STRUCTURED_LIST_MAX_CHARS,
    budget_list_payload,
    budget_mcp_report_dict,
    budget_mcp_text,
    mcp_workspace_root_allowed,
    preflight_summary,
    require_str,
    require_workspace,
    safe_artifact,
    safe_workspace_child,
)
from .models import to_jsonable
from .preflight import run_preflight
from .reports import compact_run_report
from .run_profile import RUN_PROFILE_CHOICES
from .security import scrub_secrets
from .verification_status import enrich_verification_payload, report_artifacts, write_verification_status
from .workflow import parse_workflow_file
from .workspace import Workspace, build_workspace_report_index, find_workflow, load_workspace_inputs, run_workspace_workflow
from .mcp_workspace_read import (
    get_run_report_payload,
    get_workspace_dashboard_payload,
    list_run_artifacts_payload,
    list_workflows_payload,
    validate_workflow_payload,
)
from .mcp_repair import (
    auto_repair_failure_payload,
    get_repair_health_payload,
    list_repair_history_payload,
    repair_workflow_payload,
    rollback_repair_payload,
)
from .mcp_benchmarks import build_benchmark_draft_payload, build_benchmark_plan_payload, list_benchmarks_payload
from .mcp_browser import run_browser_smoke_payload, run_browser_smoke_suite_payload
from .mcp_generation_format import quality_gate_payload, semantic_summary_payload
from .mcp_policy import RUN_PROFILE_ORDER, enforce_mcp_run_profile, mcp_config
from .mcp_response import budget_mcp_payload, mcp_error_payload
from .mcp_session import get_session_context_payload, get_visual_status_payload, save_task_context_payload


APP_NAME = "visual-agent"
APP_VERSION = "0.1.0"
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
            name="verify_workflow",
            description="Run one workflow as a verification check and return pass/fail with structured failure details.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "workflow_name": {"type": "string"},
                    "inputs_file": {"type": "string"},
                    "run_profile": {"type": "string", "enum": ["dry-run", "supervised", "semi-auto", "approved"], "default": "dry-run"},
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
            name="get_failure_details",
            description="Return the latest StructuredFailure JSON for coding agents to repair the current failure.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "run_id": {"type": "string", "description": "Optional run id. Defaults to latest failed run."},
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
            description="List public reference benchmark projects and scenarios for real-world Checkpoint testing.",
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
            description="Create an executable Checkpoint benchmark coverage plan from public reference benchmarks.",
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
            name="get_visual_status",
            description="Return the project .visual-agent-status.md as structured JSON for coding agents.",
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
                        "description": "Optional. If omitted, Checkpoint reads git diff from repo_root.",
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
                        "description": "Optional. If omitted, Checkpoint reads git diff from repo_root.",
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
                    "page_type": {
                        "type": "string",
                        "enum": ["auth", "form", "list", "detail", "ecommerce"],
                        "description": "Optional page type hint used to select stronger few-shot examples.",
                    },
                    "url": {
                        "type": "string",
                        "description": "Optional entry URL to use for the first observe_browser step.",
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
    workspace = workspace_for_audit(args)
    audit_mcp_call(workspace, name, args, {"status": "started", "phase": "entry"})
    try:
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "list_workflows": list_workflows_payload,
            "validate_workflow": validate_workflow_payload,
            "run_workflow": run_workflow_payload,
            "verify_workflow": verify_workflow_payload,
            "get_run_report": get_run_report_payload,
            "list_run_artifacts": list_run_artifacts_payload,
            "get_workspace_dashboard": get_workspace_dashboard_payload,
            "get_latest_failure": get_latest_failure_payload,
            "summarize_latest_failure": summarize_latest_failure_payload,
            "diagnose_failure": diagnose_failure_payload,
            "get_failure_details": get_failure_details_payload,
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
            "get_visual_status": get_visual_status_payload,
            "save_task_context": save_task_context_payload,
            "run_verification": run_verification_payload,
            "generate_workflow_from_context": generate_workflow_from_context_payload,
            "verify_implementation": verify_implementation_payload,
            "generate_workflow": generate_workflow_payload,
        }
        if name not in handlers:
            raise ValueError(f"Unknown tool: {name}")
        payload = handlers[name](args)
        audit_mcp_call(workspace, name, args, payload)
        return _ok_json(payload)
    except Exception as exc:
        payload = mcp_error_payload(f"{type(exc).__name__}: {exc}")
        audit_mcp_call(workspace, name, args, payload)
        return _ok_json(payload)


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


def verify_workflow_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .structured_failure import empty_structured_failure, structured_failure_from_diagnosis, structured_failure_to_dict

    workspace = require_workspace(args)
    workflow_name = require_str(args, "workflow_name")
    run_profile = str(args.get("run_profile") or "dry-run")
    if run_profile not in RUN_PROFILE_ORDER:
        raise ValueError(f"Unsupported run_profile: {run_profile}")
    try:
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
    except Exception as exc:
        suggestion = "Check workspace_root, workflow_name, inputs_file, and preflight errors, then rerun verify_workflow."
        return {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "workflow": workflow_name,
            "result": "error",
            "requested_run_profile": run_profile,
            "run_profile": None,
            "run_id": None,
            "steps_passed": 0,
            "steps_total": 0,
            "structured_failure": empty_structured_failure(message=str(exc), suggested_fix=suggestion),
            "message": str(exc),
            "suggestion": suggestion,
        }
    failed = next((step for step in result.steps if getattr(step.status, "value", str(step.status)) == "failed"), None)
    steps_passed = sum(1 for step in result.steps if getattr(step.status, "value", str(step.status)) in {"success", "dry_run"})
    structured_failure = None
    if failed is not None:
        metadata = dict(getattr(failed, "metadata", {}) or {})
        diagnosis = metadata.get("failure_diagnosis") if isinstance(metadata.get("failure_diagnosis"), dict) else {}
        if diagnosis:
            structured_failure = structured_failure_to_dict(
                structured_failure_from_diagnosis(
                    diagnosis,
                    project_root=workspace.project_root,
                    workflow_name=result.workflow_name,
                )
            )
        else:
            structured_failure = empty_structured_failure(
                message=str(getattr(failed, "message", "") or "Workflow step failed."),
                suggested_fix="Inspect the failed step message and run report, then rerun verify_workflow.",
            )
    payload = {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "workflow": result.workflow_name,
        "result": "pass" if failed is None else "fail",
        "requested_run_profile": run_profile,
        "run_profile": result.run_profile,
        "run_id": result.run_id,
        "steps_passed": steps_passed,
        "steps_total": len(result.steps),
        "structured_failure": structured_failure,
        "report_hint": f"Use get_run_report with run_id='{result.run_id}' for full details.",
    }
    return scrub_secrets(payload)


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


def get_failure_details_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .reports import load_run_report
    from .structured_failure import structured_failure_from_diagnosis, structured_failure_to_dict

    workspace = require_workspace(args)
    run_id = str(args.get("run_id") or "") or latest_failed_run_id(workspace)
    if not run_id:
        return {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "status": "none",
            "message": "No failed workflow reports found.",
        }
    report = load_run_report(workspace.runs_dir / run_id)
    failed = next((step for step in report.steps if step.status == "failed"), None)
    if not failed:
        return {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "status": "none",
            "run_id": run_id,
            "message": "Run has no failed step.",
        }
    diagnosis = failed.failure_diagnosis if isinstance(failed.failure_diagnosis, dict) else {}
    structured = diagnosis.get("structured_failure") if isinstance(diagnosis.get("structured_failure"), dict) else None
    if structured is None and diagnosis:
        structured = structured_failure_to_dict(
            structured_failure_from_diagnosis(
                diagnosis,
                project_root=workspace.project_root,
                workflow_name=report.workflow_name,
            )
        )
    return scrub_secrets(
        {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "status": "found",
            "run_id": run_id,
            "workflow": report.workflow_name,
            "failed_step": {"id": failed.id, "action": failed.action, "message": failed.message},
            "structured_failure": structured or {},
            "report_path": str(workspace.reports_dir / f"{run_id}.json"),
        }
    )


def latest_failed_run_id(workspace: Any) -> str:
    index = build_workspace_report_index(workspace, failed_only=True)
    entries = index.get("entries") if isinstance(index.get("entries"), list) else []
    if not entries:
        return ""
    return str(entries[0].get("run_id") or "")


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
    page_type = str(args.get("page_type") or "").strip()
    result = generate_workflow_yaml(
        description=description,
        workspace_root=workspace.root,
        model=str(args.get("model") or DEFAULT_MODEL),
        dry_run=bool(args.get("dry_run", False)),
        page_type=page_type or None,
        url=str(args.get("url") or "").strip() or None,
    )
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        **result,
    }


def _ok_json(payload: dict[str, Any]) -> list[TextContent]:
    safe_payload = budget_mcp_payload(scrub_secrets(payload))
    text = json.dumps(safe_payload, ensure_ascii=False, indent=2, default=str)
    return [TextContent(type="text", text=text)]


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

