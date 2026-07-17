from __future__ import annotations

import asyncio
import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from .console import build_report_detail, report_detail_to_markdown
from .mcp_helpers import (
    RUN_PROFILE_ORDER,
    audit_mcp_call,
    build_benchmark_draft_payload,
    build_benchmark_plan_payload,
    enforce_mcp_run_profile,
    get_session_context_payload,
    get_visual_status_payload,
    list_benchmarks_payload,
    mcp_config as mcp_config,
    quality_gate_payload,
    run_browser_smoke_payload,
    run_browser_smoke_suite_payload,
    save_task_context_payload,
    semantic_summary_payload,
    workspace_for_audit,
)
from .mcp_common import (  # noqa: F401
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
from .models import to_jsonable as to_jsonable
from .pacer_pillars import assess_five_pillars
from .managed_state import new_managed_run, transition_managed_run
from .preflight import run_preflight as run_preflight
from .reports import compact_run_report
from .run_profile import RUN_PROFILE_CHOICES as RUN_PROFILE_CHOICES
from .security import scrub_secrets
from .verification_status import enrich_verification_payload, report_artifacts, write_verification_status
from .workflow import parse_workflow_file as parse_workflow_file
from .workspace import Workspace, build_workspace_report_index, find_workflow as find_workflow, load_workspace_inputs, run_workspace_workflow
from .mcp_workspace_read import (
    apply_coverage_repair_payload,
    draft_coverage_repair_payload,
    get_run_report_payload,
    get_workspace_dashboard_payload,
    list_run_artifacts_payload,
    list_workflows_payload,
    plan_coverage_repair_payload,
    validate_workflow_payload,
)
from .mcp_repair import (
    auto_repair_failure_payload,
    get_repair_health_payload,
    list_repair_history_payload,
    repair_workflow_payload,
    rollback_repair_payload,
)
from .mcp_response import budget_mcp_payload, mcp_error_payload
from .mcp_pacer_contracts import (
    PACER_TYPED_TOOL_NAMES,
    pacer_tool_input_schema,
    pacer_tool_output_schema,
    validate_pacer_tool_input,
    validate_pacer_tool_output,
)


APP_NAME = "visual-agent"
APP_VERSION = "0.1.0"
MAX_PACER_COMPLETION_ATTEMPTS = 3
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import CallToolResult, TextContent, Tool
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
        outputSchema: dict[str, Any] | None = None
        annotations: dict[str, Any] | None = None

    @dataclass(frozen=True)
    class CallToolResult:  # type: ignore[no-redef]
        content: list[Any]
        structuredContent: dict[str, Any] | None = None
        isError: bool = False


server = Server(APP_NAME) if Server is not None else None
_PACER_VERIFICATION_SENTINEL = object()
_PACER_DOCUMENTATION_COMPILE_SENTINEL = object()
_PACER_PINNED_LAUNCH_SENTINEL = object()
_PACER_COMPLETION_AUDIT_SENTINEL = object()
_PACER_MCP_DISPATCH_SENTINEL = object()
_PACER_MANAGED_RUNTIME_SENTINEL = object()


def mcp_tools() -> list[Tool]:
    tools = [
        Tool(
            name="begin_pacer_task",
            description=(
                "Internal first-call handshake for a Pacer task. Call it before reading or modifying repository "
                "files. The MCP process captures the source baseline and registers a process-local receipt that "
                "complete_pacer_task requires. A normal MCP get_pacer_memory call performs the same handshake."
            ),
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "workspace_root": {"type": "string", "default": ".agent-workspace"},
                    "repo_root": {"type": "string", "default": "."},
                    "goal": {"type": "string", "minLength": 1},
                },
                "required": ["workspace_root", "repo_root", "goal"],
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "schema_version": {"type": "integer"},
                    "status": {"type": "string"},
                    "launch_id": {"type": "string"},
                    "task_contract": {"type": "object"},
                    "error": {"type": "string"},
                },
                "required": ["schema_version"],
                "additionalProperties": True,
            },
        ),
        Tool(
            name="get_pacer_memory",
            description=(
                "Load Pacer's local evidence-derived memory before a Codex coding or review task. "
                "Pass known_memory_receipt on later calls in the same launch to receive a small not-modified response."
            ),
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string", "default": ".agent-workspace"},
                    "repo_root": {"type": "string", "default": "."},
                    "goal": {"type": "string"},
                    "limit": {"type": "integer", "default": 8},
                    "detail": {"type": "string", "enum": ["compact", "full"], "default": "compact"},
                    "memory_budget_chars": {"type": "integer", "default": 6000},
                    "known_memory_receipt": {
                        "type": "string",
                        "description": "Receipt returned by the first full memory response in this Pacer launch.",
                    },
                    "memory_ids_used": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {"type": "string", "minLength": 1, "maxLength": 200},
                        "description": (
                            "Optional IDs actually used from a prior response. Requires a matching "
                            "known_memory_receipt; every ID must have been delivered as trusted memory."
                        ),
                    },
                },
                "required": ["workspace_root", "repo_root"],
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "schema_version": {"type": "integer"},
                    "status": {"type": "string"},
                    "effective_memory": {"type": "object"},
                    "memory_receipt": {"type": "string"},
                    "error": {"type": "string"},
                },
                "required": ["schema_version"],
                "additionalProperties": True,
            },
        ),
        Tool(
            name="record_pacer_outcome",
            description=(
                "Legacy failure/blocker recorder. Successful tasks must use complete_pacer_task so goal, source "
                "files, verification steps, runtime and task review are atomically bound."
            ),
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string", "default": ".agent-workspace"},
                    "repo_root": {"type": "string", "default": "."},
                    "goal": {"type": "string"},
                    "summary": {"type": "string"},
                    "verification": {
                        "type": "string",
                        "description": "Verification evidence as a string in the exact form run_id=<id>.",
                        "examples": ["run_id=20260714-120000-abcd1234"],
                    },
                    "verification_receipt": {
                        "type": "string",
                        "description": (
                            "Process-local receipt returned by run_pacer_verification; required whenever "
                            "verification references a run_id."
                        ),
                    },
                    "status": {"type": "string", "enum": ["failed", "blocked"]},
                },
                "required": ["workspace_root", "repo_root", "goal", "summary", "status"],
                "dependentRequired": {"verification": ["verification_receipt"]},
            },
        ),
        Tool(
            name="get_pacer_runtime_telemetry",
            description="Read prompt-free provider, model, token and compaction telemetry for the active Pacer launch.",
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string", "default": ".agent-workspace"},
                    "repo_root": {"type": "string", "default": "."},
                    "detail": {"type": "string", "enum": ["compact", "full"], "default": "full"},
                },
                "required": ["workspace_root", "repo_root"],
            },
        ),
        Tool(
            name="get_pacer_events",
            description="Return recent prompt-free Pacer lifecycle events for the current workspace.",
            annotations={
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string", "default": ".agent-workspace"},
                    "repo_root": {"type": "string", "default": "."},
                    "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                    "launch_id": {"type": "string"},
                },
                "required": ["workspace_root", "repo_root"],
            },
        ),
        Tool(
            name="run_pacer_commands",
            description=(
                "Run up to 20 long verification or setup commands locally in one MCP call. Full output is saved "
                "under .agent-workspace; Codex receives compact tails to prevent context growth. Pass "
                "steps=[{name, argv, cwd?, timeout_seconds?, env?}]. Use argv arrays, not command strings."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string", "default": ".agent-workspace"},
                    "repo_root": {"type": "string", "default": "."},
                    "steps": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "argv": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 1,
                                    "description": "Command argv as a string array; never pass a shell command string.",
                                    "examples": [["python", "-m", "pytest", "-q"]],
                                },
                                "cwd": {"type": "string", "default": "."},
                                "timeout_seconds": {"type": "number", "default": 600},
                                "env": {"type": "object", "additionalProperties": {"type": "string"}},
                            },
                            "required": ["name", "argv"],
                        },
                    },
                    "stop_on_failure": {"type": "boolean", "default": False},
                    "tail_chars": {"type": "integer", "default": 2000},
                },
                "required": ["steps"],
            },
        ),
        Tool(
            name="run_pacer_verification",
            description=(
                "Run a local, allowlisted verification batch without shell commands. Supports common test, "
                "compile, lint, analyze, build, and read-only Git checks; rejects setup and arbitrary execution."
            ),
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string", "default": ".agent-workspace"},
                    "repo_root": {"type": "string", "default": "."},
                    "steps": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "argv": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 1,
                                    "description": (
                                        "Verification argv as a string array, for example "
                                        "['python', '-m', 'pytest', '-q']; do not pass a command field or shell string."
                                    ),
                                    "examples": [["python", "-m", "pytest", "-q"]],
                                },
                                "cwd": {"type": "string", "default": "."},
                                "timeout_seconds": {"type": "number", "default": 600},
                            },
                            "required": ["name", "argv"],
                        },
                    },
                    "stop_on_failure": {"type": "boolean", "default": False},
                    "tail_chars": {"type": "integer", "default": 2000},
                },
                "required": ["workspace_root", "repo_root", "steps"],
            },
        ),
        Tool(
            name="complete_pacer_task",
            description=(
                "Atomically verify and finish one task in the current Pacer launch. Pass verification steps as argv "
                "arrays, for example steps=[{name: 'tests', argv: ['python', '-m', 'pytest', '-q']}]. The tool "
                "runs the allowlisted verification path, captures compact runtime telemetry, and automatically binds "
                "the verified run_id to the completed/failed outcome. Each claim only needs one immutable requirement "
                "ID, a concrete result, and named verification steps. Pacer derives file paths and created/modified/"
                "deleted states from the trusted launch baseline so model-authored file status is never trusted. "
                "It never accepts a run_pacer_commands batch."
            ),
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "workspace_root": {"type": "string", "default": ".agent-workspace"},
                    "repo_root": {"type": "string", "default": "."},
                    "goal": {"type": "string", "minLength": 1},
                    "summary": {"type": "string", "minLength": 1},
                    "completion_evidence": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "result_kind": {
                                "type": "string",
                                "enum": ["change", "configuration", "review", "research", "test"],
                                "description": "Legacy compatibility field. Pacer derives the canonical value.",
                            },
                            "claims": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 20,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "kind": {
                                            "type": "string",
                                            "enum": ["change", "configuration", "review", "research", "test"],
                                            "description": "Legacy compatibility field. Pacer derives the canonical value.",
                                        },
                                        "requirement_ids": {
                                            "type": "array",
                                            "minItems": 1,
                                            "maxItems": 20,
                                            "items": {"type": "string", "minLength": 1, "maxLength": 80},
                                            "description": (
                                                "IDs from the immutable task_contract returned by begin_pacer_task."
                                            ),
                                        },
                                        "requirement": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 500,
                                            "description": "Legacy compatibility field. Pacer loads immutable text by requirement ID.",
                                        },
                                        "result": {"type": "string", "minLength": 1, "maxLength": 1000},
                                        "files": {
                                            "type": "array",
                                            "maxItems": 200,
                                            "description": "Legacy compatibility field. Pacer ignores it and derives file facts.",
                                            "items": {
                                                "type": "object",
                                                "additionalProperties": False,
                                                "properties": {
                                                    "path": {"type": "string", "minLength": 1, "maxLength": 500},
                                                    "state": {
                                                        "type": "string",
                                                        "enum": ["created", "modified", "deleted"],
                                                    },
                                                },
                                                "required": ["path", "state"],
                                            },
                                        },
                                        "verification_steps": {
                                            "type": "array",
                                            "minItems": 1,
                                            "maxItems": 20,
                                            "items": {"type": "string", "minLength": 1, "maxLength": 120},
                                            "description": (
                                                "Names must exactly match steps[].name and must pass. Reuse the task's "
                                                "substantive test/build/analyze step for read-only or protected-path "
                                                "claims; Pacer derives file facts, so do not add Git inspection steps."
                                            ),
                                        },
                                    },
                                    "required": [
                                        "requirement_ids",
                                        "result",
                                        "verification_steps",
                                    ],
                                },
                            },
                            "unresolved_items": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string", "maxLength": 400},
                                "description": "Requested work that is not complete. Any item blocks completion.",
                            },
                            "known_risks": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string", "maxLength": 400},
                            },
                        },
                        "required": ["claims", "unresolved_items", "known_risks"],
                    },
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "name": {"type": "string"},
                                "argv": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 1,
                                    "description": (
                                        "Verification argv must be a string array; do not use a command field or "
                                        "a shell command string. The resulting verification run_id is bound "
                                        "automatically to the outcome."
                                    ),
                                    "examples": [["python", "-m", "pytest", "-q"]],
                                },
                                "cwd": {"type": "string", "default": "."},
                                "timeout_seconds": {"type": "number", "default": 600},
                            },
                            "required": ["name", "argv"],
                        },
                    },
                    "stop_on_failure": {"type": "boolean", "default": False},
                    "tail_chars": {"type": "integer", "minimum": 200, "maximum": 2000, "default": 1200},
                },
                "required": [
                    "workspace_root",
                    "repo_root",
                    "goal",
                    "summary",
                    "completion_evidence",
                    "steps",
                ],
            },
            outputSchema={
                "type": "object",
                "properties": {
                    "schema_version": {"type": "integer"},
                    "status": {"type": "string"},
                    "launch_id": {"type": "string"},
                    "task_review": {"type": "object"},
                    "five_pillars_active": {"type": "boolean"},
                    "five_pillars_assessment": {"type": "object"},
                    "error": {"type": "string"},
                },
                "required": ["schema_version"],
                "additionalProperties": True,
            },
        ),
        Tool(
            name="list_workflows",
            description="List available workspace workflows, quality/readiness, latest run status, and optional diff-aware recommendations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "include_slow": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include workflows tagged 'slow'. Default: skipped.",
                    },
                    "changed_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional changed file paths. When provided, returns recommended workflows for this diff.",
                    },
                    "repo_root": {
                        "type": "string",
                        "description": "Optional git repo root. If provided without changed_files, Checkpoint reads git diff.",
                    },
                    "base": {
                        "type": "string",
                        "default": "HEAD",
                        "description": "Git base ref used with repo_root. Default: HEAD.",
                    },
                },
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="plan_coverage_repair",
            description="Return a compact diff coverage repair plan with suggested affects and missing workflow drafts for coding agents.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "changed_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional changed file paths. Preferred for deterministic coverage planning.",
                    },
                    "repo_root": {
                        "type": "string",
                        "description": "Optional git repo root. If provided without changed_files, Checkpoint reads git diff.",
                    },
                    "base": {
                        "type": "string",
                        "default": "HEAD",
                        "description": "Git base ref used with repo_root. Default: HEAD.",
                    },
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
            name="draft_coverage_repair",
            description="Draft unified diffs for coverage repair suggestions without applying changes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "changed_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional changed file paths. Preferred for deterministic coverage repair drafts.",
                    },
                    "repo_root": {
                        "type": "string",
                        "description": "Optional git repo root. If provided without changed_files, Checkpoint reads git diff.",
                    },
                    "base": {
                        "type": "string",
                        "default": "HEAD",
                        "description": "Git base ref used with repo_root. Default: HEAD.",
                    },
                    "include_slow": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include workflows tagged 'slow'. Default: skipped.",
                    },
                    "max_items": {
                        "type": "integer",
                        "default": 5,
                        "description": "Maximum patch drafts to return. Default: 5.",
                    },
                },
                "required": ["workspace_root"],
            },
        ),
        Tool(
            name="apply_coverage_repair",
            description="Apply reviewed coverage repair suggestions. Defaults to dry-run unless apply=true.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "changed_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional changed file paths. Preferred for deterministic coverage repair.",
                    },
                    "repo_root": {
                        "type": "string",
                        "description": "Optional git repo root. If provided without changed_files, Checkpoint reads git diff.",
                    },
                    "base": {
                        "type": "string",
                        "default": "HEAD",
                        "description": "Git base ref used with repo_root. Default: HEAD.",
                    },
                    "include_slow": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include workflows tagged 'slow'. Default: skipped.",
                    },
                    "max_items": {
                        "type": "integer",
                        "default": 5,
                        "description": "Maximum repairs to apply. Default: 5.",
                    },
                    "apply": {
                        "type": "boolean",
                        "default": False,
                        "description": "Must be true to write files. Default false returns a dry-run draft.",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "default": False,
                        "description": "Allow overwriting drafted new workflow files if they already exist.",
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
    for index, tool in enumerate(tools):
        if tool.name not in PACER_TYPED_TOOL_NAMES:
            continue
        updates = {
            "inputSchema": pacer_tool_input_schema(tool.name),
            "outputSchema": pacer_tool_output_schema(tool.name),
        }
        if hasattr(tool, "model_copy"):
            tools[index] = tool.model_copy(update=updates)
        else:
            tools[index] = Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=updates["inputSchema"],
                outputSchema=updates["outputSchema"],
                annotations=tool.annotations,
            )
    return tools


if server is not None:

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return mcp_tools()

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        payload = await call_tool_payload(name, arguments)
        return _tool_result(payload, tool_name=name)


async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> list[TextContent]:
    return _ok_json(await call_tool_payload(name, arguments))


async def call_tool_payload(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    args = arguments or {}
    workspace = workspace_for_audit(args)
    audit_mcp_call(workspace, name, args, {"status": "started", "phase": "entry"})
    try:
        if name in PACER_TYPED_TOOL_NAMES:
            args = validate_pacer_tool_input(name, args)
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "begin_pacer_task": begin_pacer_task_payload,
            "get_pacer_memory": get_pacer_memory_payload,
            "record_pacer_outcome": record_pacer_outcome_payload,
            "get_pacer_runtime_telemetry": get_pacer_runtime_telemetry_payload,
            "get_pacer_events": get_pacer_events_payload,
            "run_pacer_commands": run_pacer_commands_payload,
            "run_pacer_verification": run_pacer_verification_payload,
            "complete_pacer_task": complete_pacer_task_payload,
            "list_workflows": list_workflows_payload,
            "plan_coverage_repair": plan_coverage_repair_payload,
            "draft_coverage_repair": draft_coverage_repair_payload,
            "apply_coverage_repair": apply_coverage_repair_payload,
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
        handler_args = args
        if name == "get_pacer_memory":
            handler_args = {
                **args,
                "_pacer_mcp_dispatch_sentinel": _PACER_MCP_DISPATCH_SENTINEL,
            }
        if name in {"run_workflow", "verify_workflow", "run_verification", "verify_implementation"}:
            payload = await asyncio.to_thread(handlers[name], handler_args)
        else:
            payload = handlers[name](handler_args)
        if name in PACER_TYPED_TOOL_NAMES:
            payload = validate_pacer_tool_output(name, payload)
        audit_mcp_call(workspace, name, args, payload)
        return payload
    except Exception as exc:
        payload = mcp_error_payload(f"{type(exc).__name__}: {exc}")
        if name in PACER_TYPED_TOOL_NAMES:
            payload = validate_pacer_tool_output(name, payload)
        audit_mcp_call(workspace, name, args, payload)
        return payload


def begin_pacer_task_payload(args: dict[str, Any]) -> dict[str, Any]:
    goal = str(args.get("goal") or "").strip()
    if not goal:
        raise ValueError("goal is required")
    workspace_root, repo_root, resolved_launch_id = _resolve_pacer_roots(args)
    active = _activate_pacer_project(
        workspace_root,
        repo_root,
        reason="task_begin",
        launch_id=resolved_launch_id,
    )
    launch_id = str(active.get("launch_id") or resolved_launch_id)
    if not launch_id:
        raise ValueError("begin_pacer_task requires an active Pacer launch")
    pinned_goal = " ".join(str(active.get("launch_goal") or "").split())
    submitted_goal = " ".join(goal.split())
    if pinned_goal and pinned_goal != submitted_goal:
        raise ValueError("begin_pacer_task goal does not match the immutable launch goal")
    from .pacer_launch_context import read_active_launch, update_active_launch

    active = update_active_launch(
        workspace_root,
        expected_launch_id=launch_id,
        launch_goal=goal,
        current_goal=goal[:2000],
        query_goal=goal[:2000],
    )
    active = read_active_launch(workspace_root, launch_id=launch_id) or active
    active, task_contract = _bind_pacer_task_contract(
        workspace_root=workspace_root,
        repo_root=repo_root,
        active=active,
        goal=goal,
    )
    baseline_trust = _ensure_trusted_task_source_baseline(
        workspace_root=workspace_root,
        repo_root=repo_root,
        active=active,
    )
    return {
        "schema_version": 1,
        "kind": "pacer_task_begin",
        "status": "started" if baseline_trust["status"] == "captured" else "already_started",
        "launch_id": launch_id,
        "goal_digest": _pacer_goal_digest(str(active.get("launch_goal") or goal)),
        "task_contract": task_contract,
        "source_baseline": baseline_trust,
    }


def _prelaunch_task_required() -> bool:
    from .pacer_launch_context import PRELAUNCH_TASK_REQUIRED_ENV

    return str(os.environ.get(PRELAUNCH_TASK_REQUIRED_ENV) or "").strip() == "1"


def _required_prelaunch_digest(env_name: str, label: str) -> str:
    digest = str(os.environ.get(env_name) or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"{label} digest is unavailable")
    return digest


def _bind_pacer_task_contract(
    *,
    workspace_root: Path,
    repo_root: Path,
    active: dict[str, Any],
    goal: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from .pacer_launch_context import (
        PRELAUNCH_TASK_CONTRACT_DIGEST_ENV,
        adopt_prelaunched_task_contract,
        register_trusted_task_contract,
        task_contract_digest,
        trusted_task_contract_errors,
        update_active_launch,
    )
    from .task_review import build_task_contract

    launch_id = str(active.get("launch_id") or "")
    pinned_goal = str(active.get("launch_goal") or goal).strip()
    task_contract = build_task_contract(pinned_goal, repo_root=repo_root)
    existing = active.get("task_contract") if isinstance(active.get("task_contract"), dict) else {}
    if existing and existing != task_contract:
        raise ValueError("active Pacer task contract does not match the immutable launch goal")
    if existing:
        if _prelaunch_task_required():
            receipt = adopt_prelaunched_task_contract(
                existing,
                goal=pinned_goal,
                workspace_root=workspace_root,
                launch_id=launch_id,
                repo_root=repo_root,
                prelaunch_digest=_required_prelaunch_digest(
                    PRELAUNCH_TASK_CONTRACT_DIGEST_ENV,
                    "prelaunch task contract",
                ),
                trusted_receipt=str(active.get("task_contract_receipt") or ""),
            )
            if not str(active.get("task_contract_receipt") or ""):
                active = update_active_launch(
                    workspace_root,
                    expected_launch_id=launch_id,
                    task_contract_receipt=receipt,
                    task_contract_trust_policy=2,
                )
        errors = trusted_task_contract_errors(
            existing,
            goal=pinned_goal,
            workspace_root=workspace_root,
            launch_id=launch_id,
            repo_root=repo_root,
            trusted_digest=str(active.get("task_contract_digest") or ""),
            trusted_receipt=str(active.get("task_contract_receipt") or ""),
        )
        if errors:
            raise ValueError("trusted task contract rejected: " + ", ".join(errors))
        return active, task_contract
    if _prelaunch_task_required():
        raise ValueError("prelaunch task contract is missing")
    receipt = register_trusted_task_contract(
        task_contract,
        goal=pinned_goal,
        workspace_root=workspace_root,
        launch_id=launch_id,
        repo_root=repo_root,
    )
    active = update_active_launch(
        workspace_root,
        expected_launch_id=launch_id,
        task_contract=task_contract,
        task_contract_digest=task_contract_digest(task_contract),
        task_contract_receipt=receipt,
        task_contract_trust_policy=1,
    )
    return active, task_contract


def _load_trusted_pacer_task_contract(
    *,
    workspace_root: Path,
    repo_root: Path,
    active: dict[str, Any],
) -> dict[str, Any]:
    from .pacer_launch_context import trusted_task_contract_errors

    contract = active.get("task_contract") if isinstance(active.get("task_contract"), dict) else {}
    launch_id = str(active.get("launch_id") or "")
    goal = str(active.get("launch_goal") or "")
    errors = trusted_task_contract_errors(
        contract,
        goal=goal,
        workspace_root=workspace_root,
        launch_id=launch_id,
        repo_root=repo_root,
        trusted_digest=str(active.get("task_contract_digest") or ""),
        trusted_receipt=str(active.get("task_contract_receipt") or ""),
    )
    if errors:
        raise ValueError("trusted task contract rejected: " + ", ".join(errors))
    return contract


def _ensure_trusted_task_source_baseline(
    *,
    workspace_root: Path,
    repo_root: Path,
    active: dict[str, Any],
) -> dict[str, Any]:
    from .pacer_launch_context import (
        PRELAUNCH_SOURCE_BASELINE_DIGEST_ENV,
        adopt_prelaunched_task_source_baseline,
        load_task_source_baseline,
        read_active_launch,
        register_trusted_task_source_baseline,
        save_task_source_baseline,
        task_source_baseline_digest,
        task_source_baseline_path,
        trusted_task_source_baseline_errors,
        update_active_launch,
    )

    launch_id = str(active.get("launch_id") or "")
    if not launch_id:
        raise ValueError("trusted task source baseline requires an active launch")
    path = task_source_baseline_path(workspace_root, launch_id)
    current = read_active_launch(workspace_root, launch_id=launch_id) or active
    if path.exists():
        payload = load_task_source_baseline(current, workspace_root=workspace_root)
        if not payload:
            raise ValueError("trusted task source baseline file is unreadable")
        if _prelaunch_task_required():
            receipt = adopt_prelaunched_task_source_baseline(
                payload,
                workspace_root=workspace_root,
                launch_id=launch_id,
                repo_root=repo_root,
                prelaunch_digest=_required_prelaunch_digest(
                    PRELAUNCH_SOURCE_BASELINE_DIGEST_ENV,
                    "prelaunch source baseline",
                ),
                trusted_receipt=str(current.get("source_baseline_receipt") or ""),
            )
            if not str(current.get("source_baseline_receipt") or ""):
                current = update_active_launch(
                    workspace_root,
                    expected_launch_id=launch_id,
                    source_baseline_receipt=receipt,
                    source_baseline_trust_policy=2,
                )
        errors = trusted_task_source_baseline_errors(
            payload,
            workspace_root=workspace_root,
            launch_id=launch_id,
            repo_root=repo_root,
            trusted_digest=str(current.get("source_baseline_digest") or ""),
            trusted_receipt=str(current.get("source_baseline_receipt") or ""),
        )
        if errors:
            raise ValueError("trusted task source baseline rejected: " + ", ".join(errors))
        return _compact_task_source_baseline_trust(
            payload,
            digest=task_source_baseline_digest(payload),
            receipt=str(current.get("source_baseline_receipt") or ""),
            status="verified",
        )
    if _prelaunch_task_required():
        raise ValueError("prelaunch task source baseline is missing")
    if any(
        str(current.get(key) or "").strip()
        for key in ("source_baseline_path", "source_baseline_digest", "source_baseline_receipt")
    ):
        raise ValueError("trusted task source baseline file is missing")

    from .task_review import capture_task_source_baseline

    captured = capture_task_source_baseline(repo_root)
    save_task_source_baseline(
        workspace_root=workspace_root,
        launch_id=launch_id,
        baseline=captured,
    )
    current = read_active_launch(workspace_root, launch_id=launch_id)
    persisted = load_task_source_baseline(current, workspace_root=workspace_root)
    if not persisted:
        raise ValueError("MCP-captured task source baseline was not persisted")
    digest = task_source_baseline_digest(persisted)
    receipt = register_trusted_task_source_baseline(
        persisted,
        workspace_root=workspace_root,
        launch_id=launch_id,
        repo_root=repo_root,
    )
    update_active_launch(
        workspace_root,
        expected_launch_id=launch_id,
        source_baseline_digest=digest,
        source_baseline_receipt=receipt,
        source_baseline_trust_policy=1,
    )
    from .pacer_events import append_pacer_event

    append_pacer_event(
        workspace_root,
        "task_source_baseline_captured",
        launch_id=launch_id,
        data={
            "digest": digest,
            "kind": str(persisted.get("kind") or ""),
            "complete": bool(persisted.get("complete")),
            "file_count": int(persisted.get("file_count") or 0),
        },
    )
    return _compact_task_source_baseline_trust(
        persisted,
        digest=digest,
        receipt=receipt,
        status="captured",
    )


def _compact_task_source_baseline_trust(
    payload: dict[str, Any],
    *,
    digest: str,
    receipt: str,
    status: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "policy_version": 1,
        "kind": str(payload.get("kind") or ""),
        "complete": bool(payload.get("complete")),
        "file_count": int(payload.get("file_count") or 0),
        "digest": str(digest),
        "receipt": str(receipt),
    }


def _load_trusted_task_source_baseline(
    *,
    workspace_root: Path,
    repo_root: Path,
    active: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    from .pacer_launch_context import (
        load_task_source_baseline,
        task_source_baseline_digest,
        trusted_task_source_baseline_errors,
    )

    launch_id = str(active.get("launch_id") or "")
    payload = load_task_source_baseline(active, workspace_root=workspace_root)
    if not payload:
        raise ValueError("trusted task source baseline is missing or unreadable")
    errors = trusted_task_source_baseline_errors(
        payload,
        workspace_root=workspace_root,
        launch_id=launch_id,
        repo_root=repo_root,
        trusted_digest=str(active.get("source_baseline_digest") or ""),
        trusted_receipt=str(active.get("source_baseline_receipt") or ""),
    )
    if errors:
        raise ValueError("trusted task source baseline rejected: " + ", ".join(errors))
    return payload, task_source_baseline_digest(payload)


def _normalized_memory_ids_used(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("memory_ids_used must be an array of memory IDs")
    if len(value) > 20:
        raise ValueError("memory_ids_used cannot contain more than 20 IDs")
    normalized: list[str] = []
    for raw in value:
        memory_id = str(raw).strip() if isinstance(raw, str) else ""
        if not memory_id or len(memory_id) > 200:
            raise ValueError("memory_ids_used entries must be non-empty strings up to 200 characters")
        if memory_id not in normalized:
            normalized.append(memory_id)
    return normalized


def get_pacer_memory_payload(args: dict[str, Any]) -> dict[str, Any]:
    started = monotonic()
    workspace_root, repo_root, resolved_launch_id = _resolve_pacer_roots(args)
    active = _activate_pacer_project(
        workspace_root,
        repo_root,
        reason="memory",
        launch_id=resolved_launch_id,
    )
    launch_id = str(active.get("launch_id") or resolved_launch_id)
    if active:
        from .pacer_launch_context import read_active_launch

        active = read_active_launch(workspace_root, launch_id=launch_id)
    query_goal = str(args.get("goal") or "").strip()
    if active and query_goal:
        from .pacer_launch_context import update_active_launch
        active = update_active_launch(
            workspace_root,
            expected_launch_id=launch_id,
            launch_goal=query_goal,
            current_goal=query_goal[:2000],
            query_goal=query_goal[:2000],
        )
    if (
        active
        and str(active.get("launch_goal") or query_goal).strip()
        and args.get("_pacer_mcp_dispatch_sentinel") is _PACER_MCP_DISPATCH_SENTINEL
    ):
        active, _ = _bind_pacer_task_contract(
            workspace_root=workspace_root,
            repo_root=repo_root,
            active=active,
            goal=str(active.get("launch_goal") or query_goal),
        )
        _ensure_trusted_task_source_baseline(
            workspace_root=workspace_root,
            repo_root=repo_root,
            active=active,
        )
    python_runtime = _managed_python_runtime(workspace_root, repo_root, launch_id=launch_id)
    memory_goal = str(active.get("launch_goal") or query_goal).strip()
    limit = max(1, min(20, int(args.get("limit") or 8)))
    detail = str(args.get("detail") or "compact")
    memory_budget = max(1000, min(20000, int(args.get("memory_budget_chars") or 6000)))
    known_receipt = str(args.get("known_memory_receipt") or "").strip()
    requested_used_ids = _normalized_memory_ids_used(args.get("memory_ids_used"))
    launch_id = str(active.get("launch_id") or launch_id)
    memory_goal_digest = _pacer_goal_digest(memory_goal)
    query_goal_digest = _pacer_goal_digest(query_goal)
    source_digest = _pacer_memory_source_digest(workspace_root, repo_root)
    view_digest = _pacer_memory_view_digest(
        detail=detail,
        limit=limit,
        memory_budget=memory_budget,
        memory_goal_digest=memory_goal_digest,
    )
    receipt = _pacer_memory_receipt(
        launch_id=launch_id,
        repo_root=repo_root,
        source_digest=source_digest,
        view_digest=view_digest,
    )
    cached = active.get("memory_cache") if isinstance(active.get("memory_cache"), dict) else {}
    can_reuse = bool(
        launch_id
        and known_receipt
        and known_receipt == receipt
        and str(cached.get("receipt") or "") == receipt
        and str(cached.get("source_digest") or "") == source_digest
        and str(cached.get("view_digest") or "") == view_digest
        and os.path.normcase(str(cached.get("repo_root") or "")) == os.path.normcase(str(repo_root))
    )
    if requested_used_ids and not can_reuse:
        raise ValueError("memory_ids_used requires a matching known_memory_receipt from this launch")
    if can_reuse:
        from .pacer_events import append_pacer_event
        from .pacer_launch_context import update_active_launch, update_pillar

        effective_memory = cached.get("effective_memory") if isinstance(cached.get("effective_memory"), dict) else {}
        lookup = cached.get("lookup") if isinstance(cached.get("lookup"), dict) else {}
        relevance = cached.get("relevance") if isinstance(cached.get("relevance"), dict) else {}
        cached_injection = cached.get("injection") if isinstance(cached.get("injection"), dict) else {}
        retrieved_ids = _normalized_memory_ids_used(relevance.get("retrieved_memory_ids"))
        injected_ids = _normalized_memory_ids_used(cached_injection.get("memory_ids"))
        if any(memory_id not in retrieved_ids for memory_id in injected_ids):
            raise ValueError("cached memory injection is not a subset of retrieved memory IDs")
        unknown_used_ids = [memory_id for memory_id in requested_used_ids if memory_id not in injected_ids]
        if unknown_used_ids:
            raise ValueError(
                "memory_ids_used contains IDs not delivered as trusted memory: " + ", ".join(unknown_used_ids)
            )
        prior_used_ids = _normalized_memory_ids_used(cached.get("used_memory_ids"))
        used_ids = list(dict.fromkeys([*prior_used_ids, *requested_used_ids]))
        injection = {
            **cached_injection,
            "status": "reused_prior_delivery" if injected_ids else "no_trusted_relevant_memory",
            "injected_hit": bool(injected_ids),
            "delivered_now": False,
            "memory_ids": injected_ids,
            "receipt": receipt,
        }
        effective_response = {
            **effective_memory,
            "hit": bool(injected_ids),
            "lookup_hit": bool(lookup.get("lookup_hit")),
            "relevant_hit": bool(relevance.get("relevant_hit")),
            "injected_hit": bool(injected_ids),
            "used_hit": bool(used_ids),
            "retrieved_memory_ids": retrieved_ids,
            "injected_memory_ids": injected_ids,
            "memory_ids_used": used_ids,
        }
        active = update_active_launch(
            workspace_root,
            expected_launch_id=launch_id,
            memory_cache={
                **cached,
                "injection": injection,
                "used_memory_ids": used_ids,
                "effective_memory": effective_response,
            },
        )
        active = update_pillar(
            workspace_root,
            "memory",
            {
                "active": True,
                "state": (
                    "reused_with_bound_use"
                    if used_ids
                    else "reused_with_injection"
                    if injected_ids
                    else "reused_without_relevant_memory"
                    if lookup.get("lookup_hit")
                    else "reused_empty"
                ),
                "retrieval_succeeded": True,
                "lookup_hit": bool(lookup.get("lookup_hit")),
                "relevant_hit": bool(relevance.get("relevant_hit")),
                "injected_hit": bool(injected_ids),
                "used_hit": bool(used_ids),
                "effective_hit": bool(injected_ids),
                "retrieved_memory_ids": retrieved_ids,
                "injected_memory_ids": injected_ids,
                "memory_ids_used": used_ids,
                "returned_entries": int(effective_response.get("total_returned") or 0),
                "duplicates_removed": int(cached.get("duplicates_removed") or 0),
                "budget_chars": cached.get("budget_chars"),
                "cache_status": "hit",
                "response_cache_status": "hit",
                "receipt": receipt,
            },
            launch_id=launch_id,
        )
        compact_memory_pillar = _compact_pacer_pillars(active.get("pillars")).get("memory", {})
        compact_assessment = (
            compact_memory_pillar.get("assessment")
            if isinstance(compact_memory_pillar.get("assessment"), dict)
            else {}
        )
        five_pillars_assessment = assess_five_pillars(active)
        response = {
            "schema_version": 2,
            "status": "memory_reused",
            "memory_status": "not_modified",
            "cache_status": "hit",
            "memory_reused": True,
            "response_cache": {"status": "hit", "reused": True},
            "memory_receipt": receipt,
            "launch_id": launch_id,
            "memory_goal_digest": memory_goal_digest,
            "query_goal_digest": query_goal_digest,
            "memory_use": {
                "used_hit": bool(used_ids),
                "memory_ids_used": used_ids,
            },
            "effective_memory": {
                "hit": bool(injected_ids),
                "lookup_hit": bool(lookup.get("lookup_hit")),
                "relevant_hit": bool(relevance.get("relevant_hit")),
                "injected_hit": bool(injected_ids),
                "used_hit": bool(used_ids),
                "retrieved_memory_ids": retrieved_ids,
                "injected_memory_ids": injected_ids,
                "memory_ids_used": used_ids,
            },
            "memory_assessment": {
                "status": str(compact_assessment.get("status") or "indeterminate"),
                "passed": bool(compact_assessment.get("passed")),
                "reason_codes": [
                    str(item) for item in (compact_assessment.get("reason_codes") or [])[:6]
                ],
            },
            "five_pillars_active": _all_pillars_active(active),
            "five_pillars_assessment": {
                "status": str(five_pillars_assessment.get("status") or "indeterminate"),
                "passed": bool(five_pillars_assessment.get("passed")),
            },
        }
        payload_chars = _pacer_payload_chars(response)
        append_pacer_event(
            workspace_root,
            "memory_reused",
            launch_id=launch_id,
            data={
                "payload_chars": payload_chars,
                "receipt": receipt,
                "cache_status": "hit",
                "lookup_hit": bool(lookup.get("lookup_hit")),
                "relevant_hit": bool(relevance.get("relevant_hit")),
                "injected_hit": bool(injected_ids),
                "used_hit": bool(used_ids),
                "memory_goal_digest": memory_goal_digest,
                "query_goal_digest": query_goal_digest,
                "duration_ms": round((monotonic() - started) * 1000, 3),
            },
        )
        return response

    from .project_memory import build_project_memory, score_memory_entry

    payload = build_project_memory(
        workspace_root=workspace_root,
        repo_root=repo_root,
        goal=memory_goal or None,
        limit=limit,
    )
    history_sources = [(workspace_root / "pacer_native", "canonical")]
    standard_workspace = (repo_root / ".agent-workspace").resolve()
    is_standard_workspace = os.path.normcase(str(workspace_root)) == os.path.normcase(str(standard_workspace))
    if is_standard_workspace:
        history_sources.append((repo_root / "pacer_native", "legacy"))
    native_history: list[dict[str, Any]] = []
    native_records: list[dict[str, Any]] = []
    seen_entries: set[str] = set()
    expected_repos = {os.path.normcase(str(repo_root))}
    if active:
        for key in ("launch_cwd", "project_root"):
            value = str(active.get(key) or "").strip()
            if value:
                expected_repos.add(os.path.normcase(str(Path(value).expanduser().resolve())))
    for native_root, source in history_sources:
        history_path = native_root / "history.jsonl"
        try:
            lines = history_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            entry_repo = os.path.normcase(str(Path(str(entry.get("repo_root") or ".")).expanduser().resolve()))
            fingerprint = json.dumps(entry, ensure_ascii=False, sort_keys=True)
            if entry_repo not in expected_repos:
                continue
            native_records.append({"entry": entry, "native_root": native_root, "source": source})
            if fingerprint not in seen_entries:
                native_history.append(entry)
                seen_entries.add(fingerprint)
    native_history.sort(key=_pacer_history_sort_key)
    total_native = len(native_history)
    unique_native_all = _dedupe_native_memory(native_history)
    scored_native: list[dict[str, Any]] = []
    for entry in unique_native_all:
        candidate = dict(entry)
        relevance_score = score_memory_entry(memory_goal, candidate)
        candidate["relevance_score"] = int(relevance_score.get("score") or 0)
        candidate["match_reasons"] = [str(item) for item in relevance_score.get("match_reasons") or []]
        candidate["relevance"] = relevance_score
        candidate["memory_id"] = _native_memory_id(candidate)
        scored_native.append(candidate)
    scored_native.sort(
        key=lambda item: (int(item.get("relevance_score") or 0), _pacer_history_sort_key(item)),
        reverse=True,
    )
    if memory_goal:
        relevant_native = [item for item in scored_native if (item.get("relevance") or {}).get("relevant") is True]
    else:
        relevant_native = list(scored_native)
    native_relevant_count = (
        len(relevant_native) if memory_goal else 0
    )
    unique_native = sorted(relevant_native[:limit], key=_pacer_history_sort_key)
    raw_formal = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    unique_formal = _dedupe_formal_memory([item for item in raw_formal if isinstance(item, dict)])
    formal_identities = {
        _memory_identity(str(item.get("objective") or ""))
        for item in unique_formal
        if _memory_identity(str(item.get("objective") or ""))
    }
    cross_source_removed = sum(
        1 for item in unique_native if _memory_identity(str(item.get("goal") or "")) in formal_identities
    )
    unique_native = [
        item for item in unique_native if _memory_identity(str(item.get("goal") or "")) not in formal_identities
    ]
    trusted_native_run_ids = _trusted_pacer_history_run_ids(
        native_records,
        visible_entries=unique_native,
        standard_workspace=is_standard_workspace,
    )
    for item in unique_native:
        run_id = str(item.get("batch_run_id") or "")
        trusted = bool(run_id and run_id in trusted_native_run_ids)
        item["trust"] = {
            "trusted": trusted,
            "basis": "verified_batch_summary" if trusted else "unverified_or_self_reported",
        }
    native_active = bool(trusted_native_run_ids)
    formal_lookup = payload.get("lookup") if isinstance(payload.get("lookup"), dict) else {}
    formal_relevance = payload.get("relevance") if isinstance(payload.get("relevance"), dict) else {}
    formal_relevant = formal_relevance.get("relevant_hit") is True
    formal_relevant_ids = [
        str(item.get("memory_id") or "")
        for item in unique_formal
        if formal_relevant and str(item.get("memory_id") or "")
    ]
    trusted_native_relevant_ids = [
        str(item.get("memory_id") or "")
        for item in unique_native
        if (item.get("relevance") or {}).get("relevant") is True
        and (item.get("trust") or {}).get("trusted") is True
        and str(item.get("memory_id") or "")
    ]
    trusted_relevant_ids = list(dict.fromkeys([*formal_relevant_ids, *trusted_native_relevant_ids]))
    lookup_hit = bool(formal_lookup.get("lookup_hit")) or total_native > 0
    relevant_candidate_hit = bool(formal_relevant_ids) or native_relevant_count > 0
    relevant_hit: bool | None = bool(trusted_relevant_ids) if memory_goal else None
    formal_ranking = formal_relevance.get("ranking") if isinstance(formal_relevance.get("ranking"), list) else []
    ranking_rows = [
        {**item, "source": "formal", "trusted": True}
        for item in formal_ranking
        if isinstance(item, dict)
    ]
    ranking_rows.extend(
        {
            "source": "native_history",
            "memory_id": str(item.get("memory_id") or ""),
            "score": int(item.get("relevance_score") or 0),
            "match_reasons": [str(reason) for reason in item.get("match_reasons") or []],
            "judgment": str((item.get("relevance") or {}).get("judgment") or "unjudged"),
            "relevant": (item.get("relevance") or {}).get("relevant"),
            "trusted": (item.get("trust") or {}).get("trusted") is True,
        }
        for item in unique_native
    )
    ranking_rows.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
    ranking = [{**item, "rank": index} for index, item in enumerate(ranking_rows[:limit], start=1)]
    lookup = {
        "status": "succeeded",
        "hit": lookup_hit,
        "lookup_hit": lookup_hit,
        "candidate_count": int(formal_lookup.get("candidate_count") or 0) + total_native,
        "formal_candidate_count": int(formal_lookup.get("candidate_count") or 0),
        "native_candidate_count": total_native,
    }
    relevance = {
        "status": "estimated" if memory_goal else "unjudged",
        "hit": relevant_hit,
        "relevant_hit": relevant_hit,
        "candidate_hit": relevant_candidate_hit if memory_goal else None,
        "threshold": formal_relevance.get("threshold"),
        "eligible_count": (
            int(formal_relevance.get("eligible_count") or 0) + native_relevant_count
            if memory_goal
            else None
        ),
        "trusted_eligible_count": len(trusted_relevant_ids) if memory_goal else None,
        "ranking": ranking,
    }
    if detail != "full":
        compact_formal = [_compact_formal_memory_entry(item) for item in unique_formal]
        compact_native = [_compact_native_memory_entry(item) for item in unique_native]
        formal_entries_list, native_history, memory_budget_usage = _budget_memory_sources(
            compact_formal,
            compact_native,
            budget_chars=memory_budget,
        )
        payload["entries"] = formal_entries_list
        payload["entry_count"] = len(formal_entries_list)
    else:
        payload["entries"] = unique_formal
        payload["entry_count"] = len(unique_formal)
        native_history = unique_native
        memory_budget_usage = {
            "limit_chars": None,
            "used_chars": len(json.dumps([*unique_formal, *unique_native], ensure_ascii=False)),
            "enforced": False,
        }
    formal_entries = len(payload["entries"])
    returned_formal_ids = {
        str(item.get("memory_id") or "")
        for item in payload["entries"]
        if isinstance(item, dict) and str(item.get("memory_id") or "")
    }
    returned_trusted_native_ids = {
        str(item.get("memory_id") or "")
        for item in native_history
        if isinstance(item, dict)
        and (item.get("trust") or {}).get("trusted") is True
        and str(item.get("memory_id") or "")
    }
    injected_ids = [
        memory_id
        for memory_id in trusted_relevant_ids
        if memory_id in returned_formal_ids or memory_id in returned_trusted_native_ids
    ]
    relevance["returned_count"] = formal_entries + len(native_history)
    relevance["trusted_returned_count"] = len(injected_ids)
    relevance["retrieved_memory_ids"] = trusted_relevant_ids
    injection = {
        "status": "delivered" if injected_ids else "no_trusted_relevant_memory",
        "injected_hit": bool(injected_ids),
        "delivered_now": True,
        "memory_ids": injected_ids,
        "formal_memory_ids": [memory_id for memory_id in injected_ids if memory_id in returned_formal_ids],
        "native_memory_ids": [memory_id for memory_id in injected_ids if memory_id in returned_trusted_native_ids],
        "receipt": receipt,
    }
    memory_use = {
        "status": "not_reported",
        "used_hit": False,
        "retrieved_memory_ids": trusted_relevant_ids,
        "injected_memory_ids": injected_ids,
        "memory_ids_used": [],
        "binding": "receipt_required",
        "evidence_level": "unobserved",
    }
    effective_memory = {
        "hit": bool(injected_ids),
        "lookup_hit": lookup_hit,
        "relevant_hit": relevant_hit,
        "injected_hit": bool(injected_ids),
        "used_hit": False,
        "retrieved_memory_ids": trusted_relevant_ids,
        "injected_memory_ids": injected_ids,
        "memory_ids_used": [],
        "total_returned": formal_entries + len(native_history),
        "trusted_returned_entries": len(injected_ids),
        "formal_entries": formal_entries,
        "native_history_entries": len(native_history),
        "trusted_native_history_entries": len(returned_trusted_native_ids),
        "sources": [
            source
            for source, count in (
                ("formal", len(injection["formal_memory_ids"])),
                ("native_history", len(injection["native_memory_ids"])),
            )
            if count > 0
        ],
        "returned_sources": [
            source
            for source, count in (("formal", formal_entries), ("native_history", len(native_history)))
            if count > 0
        ],
        "formal_raw_entries": len(raw_formal),
        "formal_unique_entries": len(unique_formal),
        "native_raw_entries": total_native,
        "native_unique_entries": len(unique_native_all),
        "native_relevant_entries": native_relevant_count,
        "native_untrusted_relevant_entries": max(0, native_relevant_count - len(trusted_native_relevant_ids)),
        "native_verified_evidence": native_active,
        "duplicates_removed": (len(raw_formal) - len(unique_formal)) + (total_native - len(unique_native_all)),
        "cross_source_duplicates_removed": cross_source_removed,
    }
    from .pacer_launch_context import latest_pending_recovery_capsule
    recovery = latest_pending_recovery_capsule(workspace_root, repo_root=repo_root)
    if recovery and active:
        from .pacer_launch_context import update_active_launch
        active = update_active_launch(
            workspace_root,
            expected_launch_id=launch_id,
            recovery_source_launch_id=str(recovery.get("source_launch_id") or ""),
        )
    from .pacer_events import append_pacer_event
    from .pacer_launch_context import update_active_launch, update_pillar
    cache_status = "invalidated" if known_receipt else "miss"
    active = update_pillar(
        workspace_root,
        "memory",
        {
            "active": True,
            "state": (
                "loaded_with_injection"
                if injected_ids
                else "loaded_with_relevant_memory"
                if relevant_hit
                else "loaded_without_relevant_memory"
                if lookup_hit
                else "loaded_empty"
            ),
            "retrieval_succeeded": True,
            "lookup_hit": lookup_hit,
            "relevant_hit": relevant_hit,
            "injected_hit": bool(injected_ids),
            "used_hit": False,
            "effective_hit": bool(effective_memory["hit"]),
            "retrieved_memory_ids": trusted_relevant_ids,
            "injected_memory_ids": injected_ids,
            "memory_ids_used": [],
            "returned_entries": int(effective_memory["total_returned"]),
            "duplicates_removed": int(effective_memory["duplicates_removed"]),
            "budget_chars": memory_budget_usage.get("limit_chars"),
            "cache_status": cache_status,
            "response_cache_status": cache_status,
            "receipt": receipt,
        },
        launch_id=launch_id,
    )
    if active:
        active = update_active_launch(
            workspace_root,
            expected_launch_id=launch_id,
            memory_cache={
                "receipt": receipt,
                "source_digest": source_digest,
                "view_digest": view_digest,
                "repo_root": str(repo_root),
                "lookup": lookup,
                "relevance": relevance,
                "injection": injection,
                "used_memory_ids": [],
                "effective_memory": effective_memory,
                "duplicates_removed": int(effective_memory["duplicates_removed"]),
                "budget_chars": memory_budget_usage.get("limit_chars"),
            },
        )
    response = {
        **payload,
        "status": "memory_loaded",
        "memory_status": "modified",
        "cache_status": cache_status,
        "memory_reused": False,
        "response_cache": {"status": cache_status, "reused": False},
        "memory_receipt": receipt,
        "memory_goal_digest": memory_goal_digest,
        "query_goal_digest": query_goal_digest,
        "lookup": lookup,
        "relevance": relevance,
        "memory_injection": injection,
        "memory_use": memory_use,
        "effective_memory": effective_memory,
        "memory_budget": memory_budget_usage,
        "recovery_capsule": recovery,
        "native_codex_history": native_history,
        "native_history_total": total_native,
        "native_history_returned": len(native_history),
        "native_history_omitted": max(0, total_native - len(native_history)),
        "five_pillars_active": _all_pillars_active(active),
        "five_pillars_assessment": assess_five_pillars(active),
        "pillars": active.get("pillars", {}),
        "launch_id": launch_id,
        "runtime": {"python": python_runtime},
    }
    if detail != "full":
        response = _compact_pacer_memory_payload(response)
    payload_chars = _pacer_payload_chars(response)
    append_pacer_event(
        workspace_root,
        "memory_loaded",
        launch_id=launch_id,
        data={
            "formal_entries": formal_entries,
            "native_history_entries": len(native_history),
            "recovery_capsule": bool(recovery),
            "payload_chars": payload_chars,
            "receipt": receipt,
            "cache_status": cache_status,
            "lookup_hit": lookup_hit,
            "relevant_hit": relevant_hit,
            "injected_hit": bool(injected_ids),
            "used_hit": False,
            "memory_goal_digest": memory_goal_digest,
            "query_goal_digest": query_goal_digest,
            "duration_ms": round((monotonic() - started) * 1000, 3),
        },
    )
    return response


def _compact_pacer_memory_payload(payload: dict[str, Any]) -> dict[str, Any]:
    effective = payload.get("effective_memory") if isinstance(payload.get("effective_memory"), dict) else {}
    budget = payload.get("memory_budget") if isinstance(payload.get("memory_budget"), dict) else {}
    lookup = payload.get("lookup") if isinstance(payload.get("lookup"), dict) else {}
    relevance = payload.get("relevance") if isinstance(payload.get("relevance"), dict) else {}
    injection = payload.get("memory_injection") if isinstance(payload.get("memory_injection"), dict) else {}
    memory_use = payload.get("memory_use") if isinstance(payload.get("memory_use"), dict) else {}
    response_cache = payload.get("response_cache") if isinstance(payload.get("response_cache"), dict) else {}
    response: dict[str, Any] = {
        "schema_version": payload.get("schema_version", 2),
        "response_detail": "compact",
        "status": str(payload.get("status") or ""),
        "memory_status": str(payload.get("memory_status") or ""),
        "cache_status": str(payload.get("cache_status") or ""),
        "memory_reused": bool(payload.get("memory_reused")),
        "response_cache": {
            "status": str(response_cache.get("status") or payload.get("cache_status") or ""),
            "reused": bool(response_cache.get("reused")),
        },
        "memory_receipt": str(payload.get("memory_receipt") or ""),
        "launch_id": str(payload.get("launch_id") or ""),
        "goal": str(payload.get("goal") or ""),
        "lookup": {
            "lookup_hit": bool(lookup.get("lookup_hit")),
            "candidate_count": int(lookup.get("candidate_count") or 0),
        },
        "relevance": {
            "relevant_hit": relevance.get("relevant_hit"),
            "candidate_hit": relevance.get("candidate_hit"),
            "retrieved_memory_ids": [str(item) for item in (relevance.get("retrieved_memory_ids") or [])[:20]],
        },
        "memory_injection": {
            "injected_hit": bool(injection.get("injected_hit")),
            "memory_ids": [str(item) for item in (injection.get("memory_ids") or [])[:20]],
        },
        "memory_use": {
            "used_hit": bool(memory_use.get("used_hit")),
            "memory_ids_used": [str(item) for item in (memory_use.get("memory_ids_used") or [])[:20]],
        },
        "effective_memory": {
            "hit": bool(effective.get("hit")),
            "lookup_hit": bool(effective.get("lookup_hit")),
            "relevant_hit": effective.get("relevant_hit"),
            "injected_hit": bool(effective.get("injected_hit")),
            "used_hit": bool(effective.get("used_hit")),
            "total_returned": int(effective.get("total_returned") or 0),
            "formal_entries": int(effective.get("formal_entries") or 0),
            "native_history_entries": int(effective.get("native_history_entries") or 0),
        },
        "entries": payload.get("entries") if isinstance(payload.get("entries"), list) else [],
        "native_codex_history": (
            payload.get("native_codex_history") if isinstance(payload.get("native_codex_history"), list) else []
        ),
        "native_history_total": int(payload.get("native_history_total") or 0),
        "memory_budget": {
            "limit_chars": budget.get("limit_chars"),
            "used_chars": int(budget.get("used_chars") or 0),
            "formal_omitted": int(budget.get("formal_omitted") or 0),
            "native_omitted": int(budget.get("native_omitted") or 0),
        },
        "five_pillars_active": bool(payload.get("five_pillars_active")),
        "five_pillars_assessment": _compact_five_pillars_assessment(
            payload.get("five_pillars_assessment")
        ),
        "pillars": _compact_pacer_pillars(payload.get("pillars")),
    }
    recovery = _compact_pacer_recovery_capsule(payload.get("recovery_capsule"))
    if recovery:
        response["recovery_capsule"] = recovery
    return response


def _compact_pacer_recovery_capsule(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {}
    current = value.get("current_context_usage") if isinstance(value.get("current_context_usage"), dict) else {}
    compactions = value.get("compactions") if isinstance(value.get("compactions"), dict) else {}
    return {
        "status": str(value.get("status") or ""),
        "source_launch_id": str(value.get("source_launch_id") or ""),
        "reason": str(value.get("reason") or ""),
        "goal": str(value.get("goal") or ""),
        "auto_compact_token_limit": int(value.get("auto_compact_token_limit") or 0),
        "current_context_input_tokens": int(current.get("input_tokens") or 0),
        "compactions_observed": int(compactions.get("count") or 0),
    }


def _compact_pacer_pillars(value: Any) -> dict[str, dict[str, Any]]:
    pillars = value if isinstance(value, dict) else {}
    response: dict[str, dict[str, Any]] = {}
    for name in ("routing", "memory", "managed", "acceptance", "dogfood"):
        pillar = pillars.get(name) if isinstance(pillars.get(name), dict) else {}
        summary: dict[str, Any] = {
            "active": bool(pillar.get("active")),
            "state": str(pillar.get("state") or "inactive"),
        }
        if name == "memory":
            for key in ("effective_hit", "lookup_hit", "relevant_hit", "injected_hit", "used_hit"):
                if key in pillar:
                    summary[key] = pillar[key]
            for key in ("retrieved_memory_ids", "injected_memory_ids", "memory_ids_used"):
                values = pillar.get(key) if isinstance(pillar.get(key), list) else []
                if values:
                    summary[key] = [str(item) for item in values[:20]]
        elif name == "routing":
            summary["mimo_used"] = bool(pillar.get("mimo_used"))
            for key in ("decision_id", "policy_match"):
                if key in pillar:
                    summary[key] = pillar[key]
        elif name == "managed":
            for key in ("transition_valid", "idempotency_key", "budget_status"):
                if key in pillar:
                    summary[key] = pillar[key]
        elif name == "acceptance":
            for key in (
                "evidence_integrity",
                "acceptance_adequacy",
                "product_verdict",
                "digest_verified",
                "standard_source",
            ):
                if key in pillar:
                    summary[key] = pillar[key]
        elif name == "dogfood":
            for key in (
                "dogfood_status",
                "pacer_on_pacer",
                "artifact_files_verified",
                "attestation_status",
                "evidence_digest",
            ):
                if key in pillar:
                    summary[key] = pillar[key]
        assessment = pillar.get("assessment") if isinstance(pillar.get("assessment"), dict) else {}
        if assessment:
            summary["assessment"] = {
                "status": str(assessment.get("status") or "indeterminate"),
                "passed": bool(assessment.get("passed")),
                "adequacy": str(assessment.get("adequacy") or "unknown"),
                "reason_codes": [str(item) for item in (assessment.get("reason_codes") or [])[:12]],
            }
        response[name] = summary
    return response


def _compact_five_pillars_assessment(value: Any) -> dict[str, Any]:
    assessment = value if isinstance(value, dict) else {}
    pillars = assessment.get("pillars") if isinstance(assessment.get("pillars"), dict) else {}
    return {
        "status": str(assessment.get("status") or "indeterminate"),
        "passed": bool(assessment.get("passed")),
        "counts": dict(assessment.get("counts"))
        if isinstance(assessment.get("counts"), dict)
        else {},
        "pillars": {
            name: {
                "status": str((pillars.get(name) or {}).get("status") or "indeterminate"),
                "passed": bool((pillars.get(name) or {}).get("passed")),
            }
            for name in ("routing", "memory", "managed", "acceptance", "dogfood")
        },
    }


def get_pacer_runtime_telemetry_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .codex_rollout_telemetry import aggregate_rollout_telemetry
    from .pacer_launch_context import load_rollout_baseline, update_active_launch, update_pillar

    detail = str(args.get("detail") or "full")
    workspace_root, repo_root, resolved_launch_id = _resolve_pacer_roots(args)
    active = _activate_pacer_project(
        workspace_root,
        repo_root,
        reason="runtime_telemetry",
        launch_id=resolved_launch_id,
    )
    launch_id = str(active.get("launch_id") or resolved_launch_id)
    baseline = load_rollout_baseline(active, workspace_root=workspace_root)
    if baseline is None:
        response = {
            "status": "unavailable",
            "launch_id": str(active.get("launch_id") or ""),
            "lifecycle_status": str(active.get("status") or ""),
            "liveness": active.get("liveness") if isinstance(active.get("liveness"), dict) else {},
            "five_pillars_active": False,
            "five_pillars_assessment": assess_five_pillars(active),
        }
        return _compact_pacer_runtime_telemetry_payload(response) if detail == "compact" else response
    rollout_ownership = (
        active.get("rollout_ownership")
        if isinstance(active.get("rollout_ownership"), dict)
        else {}
    )
    ownership_required = bool(rollout_ownership.get("required"))
    telemetry = aggregate_rollout_telemetry(
        baseline,
        repo_root=active.get("effective_repo_root") or active.get("launch_cwd") or repo_root,
        launch_id=launch_id if ownership_required else "",
    )
    usage = telemetry.get("usage") if isinstance(telemetry.get("usage"), dict) else {}
    current = telemetry.get("current_context_usage") if isinstance(telemetry.get("current_context_usage"), dict) else {}
    input_tokens = int(current.get("input_tokens") or 0)
    cached_tokens = min(input_tokens, int(current.get("cached_input_tokens") or 0))
    accumulated_input = int(usage.get("input_tokens") or 0)
    accumulated_cached = min(accumulated_input, int(usage.get("cached_input_tokens") or 0))
    compact_limit = int(active.get("auto_compact_token_limit") or 0)
    telemetry["context_control"] = {
        "auto_compact_token_limit": compact_limit,
        "scope": "total",
        "compactions_observed": int((telemetry.get("compactions") or {}).get("count") or 0),
        "usage_semantics": "cumulative_session_usage_not_current_context_size",
        "uncached_input_tokens": max(0, input_tokens - cached_tokens),
        "cached_input_ratio": round(cached_tokens / input_tokens, 4) if input_tokens else 0.0,
        "current_context_input_tokens": input_tokens,
        "current_context_total_tokens": int(current.get("total_tokens") or 0),
        "context_pressure_ratio": round(input_tokens / compact_limit, 4) if compact_limit else None,
        "accumulated_uncached_input_tokens": max(0, accumulated_input - accumulated_cached),
    }
    runtime = telemetry.get("runtime") if isinstance(telemetry.get("runtime"), dict) else {}
    provider = str(runtime.get("provider") or "")
    model = str(runtime.get("model") or "")
    uses_mimo = "mimo" in provider.lower() or "mimo" in model.lower()
    telemetry_ownership = telemetry.get("ownership") if isinstance(telemetry.get("ownership"), dict) else {}
    routing_active = (
        ownership_required
        and telemetry.get("status") == "captured"
        and str(telemetry.get("attribution_confidence") or "") == "high"
        and bool(telemetry_ownership.get("matched"))
        and bool(provider)
        and bool(model)
        and not uses_mimo
    )
    routing_decision = (
        active.get("routing_decision")
        if isinstance(active.get("routing_decision"), dict)
        else {}
    )
    selected = (
        routing_decision.get("selected")
        if isinstance(routing_decision.get("selected"), dict)
        else {}
    )
    request_evidence = (
        routing_decision.get("request_evidence")
        if isinstance(routing_decision.get("request_evidence"), dict)
        else {}
    )
    policy_match: bool | None = None
    if selected:
        policy_match = (
            str(selected.get("provider") or "").casefold() == provider.casefold()
            and str(selected.get("model") or "").casefold() == model.casefold()
        )
    update_pillar(
        workspace_root,
        "routing",
        {
            "active": routing_active,
            "state": "observed" if routing_active else "unavailable_or_disallowed",
            "runtime": runtime,
            "provider_inherited": True,
            "mimo_used": uses_mimo,
            "ownership_required": ownership_required,
            "ownership_matched": bool(telemetry_ownership.get("matched")),
            "attribution_confidence": str(telemetry.get("attribution_confidence") or "none"),
            "decision_id": str(routing_decision.get("decision_id") or ""),
            "policy_version": int(routing_decision.get("policy_version") or 0),
            "policy_match": policy_match,
            "request_evidence": request_evidence,
            "policy_verdict": (
                "matched" if policy_match is True else "mismatched" if policy_match is False else "passthrough"
            ),
            "continuity_scope": "current_launch_only",
        },
        launch_id=launch_id,
    )
    active = update_active_launch(
        workspace_root,
        expected_launch_id=launch_id,
        rollout_telemetry=telemetry,
    )
    response = {
        **telemetry,
        "launch_id": str(active.get("launch_id") or ""),
        "lifecycle_status": str(active.get("status") or ""),
        "liveness": active.get("liveness") if isinstance(active.get("liveness"), dict) else {},
        "pillars": active.get("pillars", {}),
        "five_pillars_active": _all_pillars_active(active),
        "five_pillars_assessment": assess_five_pillars(active),
    }
    return _compact_pacer_runtime_telemetry_payload(response) if detail == "compact" else response


def _compact_pacer_runtime_telemetry_payload(payload: dict[str, Any]) -> dict[str, Any]:
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    context = payload.get("context_control") if isinstance(payload.get("context_control"), dict) else {}
    agents = payload.get("agents") if isinstance(payload.get("agents"), dict) else {}
    liveness = payload.get("liveness") if isinstance(payload.get("liveness"), dict) else {}
    response: dict[str, Any] = {
        "schema_version": payload.get("schema_version", 1),
        "response_detail": "compact",
        "status": str(payload.get("status") or "unavailable"),
        "attribution_confidence": str(payload.get("attribution_confidence") or "none"),
        "launch_id": str(payload.get("launch_id") or ""),
        "lifecycle_status": str(payload.get("lifecycle_status") or ""),
        "runtime": {
            "provider": str(runtime.get("provider") or ""),
            "model": str(runtime.get("model") or ""),
            "reasoning_effort": str(runtime.get("reasoning_effort") or ""),
        },
        "usage": {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "cached_input_tokens": int(usage.get("cached_input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "reasoning_output_tokens": int(usage.get("reasoning_output_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
        "context_control": {
            "auto_compact_token_limit": int(context.get("auto_compact_token_limit") or 0),
            "compactions_observed": int(context.get("compactions_observed") or 0),
            "uncached_input_tokens": int(context.get("uncached_input_tokens") or 0),
            "current_context_input_tokens": int(context.get("current_context_input_tokens") or 0),
            "context_pressure_ratio": context.get("context_pressure_ratio"),
            "accumulated_uncached_input_tokens": int(context.get("accumulated_uncached_input_tokens") or 0),
        },
        "agents": {
            "total": int(agents.get("total") or 0),
            "completed": int(agents.get("completed") or 0),
            "interrupted": int(agents.get("interrupted") or 0),
            "active": int(agents.get("active") or 0),
        },
        "liveness": {
            "state": str(liveness.get("state") or ""),
            "monitoring": bool(liveness.get("monitoring")),
        },
        "pillars": _compact_pacer_pillars(payload.get("pillars")),
        "five_pillars_active": bool(payload.get("five_pillars_active")),
        "five_pillars_assessment": _compact_five_pillars_assessment(
            payload.get("five_pillars_assessment")
        ),
    }
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    if warnings:
        response["warnings"] = [str(item)[:240] for item in warnings[:2]]
    return response


def get_pacer_events_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .pacer_events import list_pacer_events
    from .pacer_launch_context import read_active_launch

    workspace_root, _, resolved_launch_id = _resolve_pacer_roots(args)
    limit = max(1, min(200, int(args.get("limit") or 50)))
    launch_id = str(args.get("launch_id") or resolved_launch_id).strip()
    events = list_pacer_events(workspace_root, limit=200 if launch_id else limit)
    if launch_id:
        events = [event for event in events if str(event.get("launch_id") or "") == launch_id][-limit:]
    active = read_active_launch(workspace_root, launch_id=launch_id) if launch_id else read_active_launch(workspace_root)
    return {
        "schema_version": 1,
        "workspace_root": str(workspace_root),
        "launch_id": launch_id,
        "event_count": len(events),
        "events": events,
        "lifecycle_status": str(active.get("status") or ""),
        "liveness": active.get("liveness") if isinstance(active.get("liveness"), dict) else {},
    }


def _compact_native_history(entries: list[dict[str, Any]], *, budget_chars: int) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    used = 0
    for entry in reversed(entries):
        compact = {
            "recorded_at": str(entry.get("recorded_at") or ""),
            "goal": str(entry.get("goal") or "")[:500],
            "summary": str(entry.get("summary") or "")[:1200],
            "verification": str(entry.get("verification") or "")[:1200],
            "status": str(entry.get("status") or ""),
            "evidence_level": str(entry.get("evidence_level") or "self_reported"),
            "batch_run_id": str(entry.get("batch_run_id") or ""),
        }
        size = len(json.dumps(compact, ensure_ascii=False))
        if kept and used + size > budget_chars:
            break
        kept.append(compact)
        used += size
    return list(reversed(kept))


def _pacer_goal_digest(goal: str) -> str:
    normalized = " ".join(str(goal or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _pacer_memory_view_digest(
    *,
    detail: str,
    limit: int,
    memory_budget: int,
    memory_goal_digest: str,
) -> str:
    value = json.dumps(
        {
            "detail": str(detail),
            "limit": int(limit),
            "memory_budget_chars": int(memory_budget),
            "memory_goal_digest": str(memory_goal_digest),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pacer_memory_receipt(
    *,
    launch_id: str,
    repo_root: Path,
    source_digest: str,
    view_digest: str,
) -> str:
    identity = "\0".join(
        (
            "pacer-memory-receipt-v1",
            str(launch_id or "standalone"),
            os.path.normcase(str(repo_root.resolve())),
            source_digest,
            view_digest,
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _pacer_memory_source_digest(workspace_root: Path, repo_root: Path) -> str:
    """Fingerprint only inputs that can change the Pacer memory response."""
    workspace = workspace_root.resolve()
    repo = repo_root.resolve()
    paths: set[Path] = {
        workspace / "pacer_native" / "history.jsonl",
        repo / "PACER.md",
        repo / "CHECKPOINT.md",
        repo / "AGENTS.md",
        repo / ".pacer" / "PACER.md",
        repo / ".pacer" / "memory.md",
    }
    standard_workspace = (repo / ".agent-workspace").resolve()
    native_roots = [workspace / "pacer_native"]
    if os.path.normcase(str(workspace)) == os.path.normcase(str(standard_workspace)):
        legacy_root = repo / "pacer_native"
        native_roots.append(legacy_root)
        paths.add(legacy_root / "history.jsonl")
    for native_root in native_roots:
        paths.update((native_root / "commands").glob("*/summary.json"))
    paths.update((workspace / "pacer_native" / "recovery").glob("*.json"))
    for directory, names in (
        (workspace / "missions", ("mission.json", "rounds.jsonl", "final_report.md")),
        (workspace / "chief_plans", ("plan.json", "workers.jsonl", "verification.json")),
    ):
        for name in names:
            paths.update(directory.glob(f"*/{name}"))
    paths.update((repo / ".pacer" / "rules").glob("*.md"))

    digest = hashlib.sha256()
    digest.update(b"pacer-memory-inputs-v1\0")
    digest.update(os.path.normcase(str(workspace)).encode("utf-8"))
    digest.update(b"\0")
    digest.update(os.path.normcase(str(repo)).encode("utf-8"))
    for path in sorted(paths, key=lambda item: os.path.normcase(str(item))):
        identity = os.path.normcase(str(path.resolve()))
        try:
            stat = path.stat()
            signature = f"{identity}\0{stat.st_mtime_ns}\0{stat.st_size}"
        except OSError:
            signature = f"{identity}\0missing"
        digest.update(b"\0")
        digest.update(signature.encode("utf-8"))
    return digest.hexdigest()


def _pacer_payload_chars(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _memory_identity(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", str(value).lower(), flags=re.UNICODE))


def _dedupe_formal_memory(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for entry in sorted(entries, key=lambda item: str(item.get("updated_at") or ""), reverse=True):
        objective = str(entry.get("objective") or "")
        match_index = next(
            (
                index
                for index, current in enumerate(selected)
                if _same_formal_memory_goal(objective, str(current.get("objective") or ""))
            ),
            None,
        )
        if match_index is None:
            selected.append(entry)
    return sorted(
        selected,
        key=lambda item: (int(item.get("relevance_score") or 0), str(item.get("updated_at") or "")),
        reverse=True,
    )


def _same_formal_memory_goal(left: str, right: str) -> bool:
    left_identity = _memory_identity(left)
    right_identity = _memory_identity(right)
    if not left_identity or not right_identity:
        return False
    if left_identity == right_identity:
        return True
    left_anchor = _memory_phase_anchor(left)
    right_anchor = _memory_phase_anchor(right)
    if not left_anchor or left_anchor != right_anchor:
        return False
    left_tokens = set(left_identity.split())
    right_tokens = set(right_identity.split())
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens)) >= 0.85


def _memory_phase_anchor(value: str) -> str:
    match = re.search(r"(?:\bphase\s*|阶段\s*)([0-9]+)", str(value), flags=re.IGNORECASE)
    return str(match.group(1)) if match else ""


def _dedupe_native_memory(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for entry in entries:
        identity = _memory_identity(str(entry.get("goal") or "")) or str(entry.get("batch_run_id") or "")
        current = selected.get(identity)
        if current is None or _pacer_history_sort_key(entry) >= _pacer_history_sort_key(current):
            selected[identity] = entry
    return sorted(selected.values(), key=_pacer_history_sort_key)


def _native_memory_id(entry: dict[str, Any]) -> str:
    run_id = str(entry.get("batch_run_id") or "")
    return f"pacer-native:{run_id}" if _valid_pacer_run_id(run_id) else ""


def _compact_formal_memory_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "formal",
        "memory_id": str(entry.get("memory_id") or ""),
        "objective": str(entry.get("objective") or "")[:700],
        "status": str(entry.get("status") or ""),
        "updated_at": str(entry.get("updated_at") or ""),
        "relevance_score": int(entry.get("relevance_score") or 0),
        "changed_files": [str(item)[:300] for item in (entry.get("changed_files") or [])[:12]],
        "verification": str(entry.get("verification") or "")[:700],
    }


def _compact_native_memory_entry(entry: dict[str, Any]) -> dict[str, Any]:
    result = {
        "source": "native_history",
        "memory_id": str(entry.get("memory_id") or ""),
        "recorded_at": str(entry.get("recorded_at") or ""),
        "goal": str(entry.get("goal") or "")[:500],
        "summary": str(entry.get("summary") or "")[:1000],
        "verification": str(entry.get("verification") or "")[:700],
        "status": str(entry.get("status") or ""),
        "evidence_level": str(entry.get("evidence_level") or "self_reported"),
        "batch_run_id": str(entry.get("batch_run_id") or ""),
        "relevance_score": int(entry.get("relevance_score") or 0),
        "match_reasons": [str(item) for item in (entry.get("match_reasons") or [])[:10]],
        "relevance": dict(entry.get("relevance") or {}) if isinstance(entry.get("relevance"), dict) else {},
        "trust": dict(entry.get("trust") or {}) if isinstance(entry.get("trust"), dict) else {},
    }
    task_review = entry.get("task_review") if isinstance(entry.get("task_review"), dict) else {}
    user_report = task_review.get("user_report") if isinstance(task_review.get("user_report"), dict) else {}
    if task_review:
        result["task_review"] = {
            "verdict": str(task_review.get("verdict") or ""),
            "trust": str(task_review.get("trust") or ""),
            "completed": [str(item)[:300] for item in (user_report.get("completed") or [])[:8]],
            "not_completed": [str(item)[:300] for item in (user_report.get("not_completed") or [])[:8]],
            "risks": [str(item)[:300] for item in (user_report.get("risks") or [])[:8]],
        }
    return result


def _budget_memory_sources(
    formal: list[dict[str, Any]],
    native: list[dict[str, Any]],
    *,
    budget_chars: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    kept_formal: list[dict[str, Any]] = []
    kept_native: list[dict[str, Any]] = []
    used = 0
    queues = [("native", list(reversed(native))), ("formal", list(formal))]
    while any(queue for _, queue in queues):
        progressed = False
        for source, queue in queues:
            if not queue:
                continue
            item = queue.pop(0)
            fitted = _fit_memory_item(item, budget_chars - used)
            if fitted is None:
                continue
            size = len(json.dumps(fitted, ensure_ascii=False))
            (kept_native if source == "native" else kept_formal).append(fitted)
            used += size
            progressed = True
        if not progressed:
            break
    kept_native.reverse()
    return kept_formal, kept_native, {
        "limit_chars": budget_chars,
        "used_chars": used,
        "enforced": True,
        "formal_omitted": max(0, len(formal) - len(kept_formal)),
        "native_omitted": max(0, len(native) - len(kept_native)),
    }


def _fit_memory_item(item: dict[str, Any], remaining: int) -> dict[str, Any] | None:
    if remaining <= 2:
        return None
    fitted = dict(item)
    while len(json.dumps(fitted, ensure_ascii=False)) > remaining:
        string_keys = [key for key, value in fitted.items() if isinstance(value, str) and len(value) > 40]
        list_keys = [key for key, value in fitted.items() if isinstance(value, list) and value]
        if string_keys:
            key = max(string_keys, key=lambda name: len(str(fitted[name])))
            value = str(fitted[key])
            fitted[key] = value[: max(40, len(value) // 2)]
            continue
        if list_keys:
            key = max(list_keys, key=lambda name: len(fitted[name]))
            fitted[key] = fitted[key][: max(0, len(fitted[key]) // 2)]
            continue
        return None
    return fitted


def _pacer_history_sort_key(entry: dict[str, Any]) -> tuple[float, str]:
    recorded_at = str(entry.get("recorded_at") or "")
    try:
        timestamp = datetime.fromisoformat(recorded_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        timestamp = 0.0
    return timestamp, recorded_at


def _verified_pacer_history_active(
    records: list[dict[str, Any]],
    *,
    visible_entries: list[dict[str, Any]],
    standard_workspace: bool,
) -> bool:
    return bool(
        _trusted_pacer_history_run_ids(
            records,
            visible_entries=visible_entries,
            standard_workspace=standard_workspace,
        )
    )


def _trusted_pacer_history_run_ids(
    records: list[dict[str, Any]],
    *,
    visible_entries: list[dict[str, Any]],
    standard_workspace: bool,
) -> set[str]:
    visible_run_ids = {
        str(entry.get("batch_run_id") or "")
        for entry in visible_entries
        if _valid_pacer_run_id(str(entry.get("batch_run_id") or ""))
    }
    trusted: set[str] = set()
    for run_id in visible_run_ids:
        group = [record for record in records if str(record["entry"].get("batch_run_id") or "") == run_id]
        canonical = [record for record in group if record["source"] == "canonical"]
        legacy = [record for record in group if record["source"] == "legacy"]
        if standard_workspace and canonical and legacy and _pacer_sources_conflict(canonical, legacy, run_id):
            continue
        selected_group = canonical or legacy
        if not selected_group:
            continue
        selected = max(selected_group, key=lambda record: _pacer_history_sort_key(record["entry"]))
        entry = selected["entry"]
        if str(entry.get("status") or "") != "completed" or str(entry.get("evidence_level") or "") != "verified_batch":
            continue
        summary = _read_pacer_batch_summary(Path(selected["native_root"]), run_id)
        task_review = entry.get("task_review") if isinstance(entry.get("task_review"), dict) else {}
        if summary is not None and _pacer_batch_passed(
            summary,
            expected_run_id=run_id,
            allow_compile_only=_task_review_allows_compile_only(task_review),
        ):
            trusted.add(run_id)
    return trusted


def _pacer_sources_conflict(
    canonical: list[dict[str, Any]],
    legacy: list[dict[str, Any]],
    run_id: str,
) -> bool:
    signatures = {
        (
            str(record["entry"].get("goal") or ""),
            str(record["entry"].get("summary") or ""),
            str(record["entry"].get("verification") or ""),
            str(record["entry"].get("status") or ""),
            str(record["entry"].get("evidence_level") or ""),
        )
        for record in [*canonical, *legacy]
    }
    if len(signatures) > 1:
        return True
    canonical_summary = _read_pacer_batch_summary(Path(canonical[-1]["native_root"]), run_id)
    legacy_summary = _read_pacer_batch_summary(Path(legacy[-1]["native_root"]), run_id)
    if canonical_summary is None or legacy_summary is None:
        return False
    return _pacer_batch_signature(canonical_summary) != _pacer_batch_signature(legacy_summary)


def _read_pacer_batch_summary(native_root: Path, run_id: str) -> dict[str, Any] | None:
    if not _valid_pacer_run_id(run_id):
        return None
    path = native_root / "commands" / run_id / "summary.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _pacer_batch_passed(
    payload: dict[str, Any],
    *,
    expected_launch_id: str | None = None,
    expected_run_id: str = "",
    allow_compile_only: bool = False,
) -> bool:
    from .pacer_verification import audit_pacer_verification_batch

    return audit_pacer_verification_batch(
        payload,
        expected_launch_id=expected_launch_id,
        expected_run_id=expected_run_id,
        allow_compile_only=allow_compile_only,
    ).valid


def _task_review_allows_compile_only(task_review: Any) -> bool:
    from .task_review import task_contract_allows_compile_only

    review = task_review if isinstance(task_review, dict) else {}
    return bool(review.get("valid")) and task_contract_allows_compile_only(review.get("task_contract"))


def _pacer_batch_signature(payload: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(payload.get("kind") or ""),
        str(payload.get("source_tool") or ""),
        _pacer_batch_integer(payload.get("policy_version")),
        str(payload.get("launch_id") or ""),
        str(payload.get("status") or ""),
        _pacer_batch_integer(payload.get("requested_steps")),
        _pacer_batch_integer(payload.get("executed_steps")),
        _pacer_batch_integer(payload.get("passed")),
        _pacer_batch_integer(payload.get("failed")),
        _pacer_batch_integer(payload.get("timed_out")),
        _pacer_batch_integer(payload.get("not_applicable")),
        tuple(str(value) for value in payload.get("step_classes") or []),
    )


def _pacer_batch_integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_pacer_run_id(value: str) -> bool:
    return re.fullmatch(r"[0-9]{8}-[0-9]{6}-[A-Za-z0-9_-]+", value) is not None


def record_pacer_outcome_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace_root, repo_root, resolved_launch_id = _resolve_pacer_roots(args)
    active = _activate_pacer_project(
        workspace_root,
        repo_root,
        reason="outcome",
        launch_id=resolved_launch_id,
    )
    launch_id = str(active.get("launch_id") or resolved_launch_id)
    goal = str(args.get("goal") or "").strip()
    summary = str(args.get("summary") or "").strip()
    verification = str(args.get("verification") or "").strip()
    status = str(args.get("status") or "blocked").strip().lower()
    if not goal:
        raise ValueError("goal is required")
    if not summary:
        raise ValueError("summary is required")
    if status not in {"completed", "failed", "blocked"}:
        raise ValueError("status must be completed, failed, or blocked")
    if status == "completed" and not verification:
        raise ValueError("completed outcomes require verification evidence")
    completion_audit = (
        dict(args.get("_pacer_completion_audit") or {})
        if args.get("_pacer_completion_audit_sentinel") is _PACER_COMPLETION_AUDIT_SENTINEL
        and isinstance(args.get("_pacer_completion_audit"), dict)
        else {}
    )
    trusted_completion = bool(completion_audit.get("valid")) and (
        args.get("_pacer_completion_audit_sentinel") is _PACER_COMPLETION_AUDIT_SENTINEL
    )
    if status == "completed" and not trusted_completion:
        raise ValueError(
            "completed outcomes must use complete_pacer_task with a valid process-local task review"
        )
    evidence_level, batch_run_id, verification_digest = _pacer_outcome_evidence(
        workspace_root,
        verification,
        outcome_status=status,
        expected_launch_id=str(active.get("launch_id") or ""),
        trusted_receipt=str(args.get("verification_receipt") or ""),
        allow_compile_only=_task_review_allows_compile_only(completion_audit),
    )
    directory = workspace_root / "pacer_native"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "history.jsonl"
    entry_payload: dict[str, Any] = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(repo_root),
        "goal": goal[:2000],
        "summary": summary[:6000],
        "verification": verification[:4000],
        "status": status,
        "evidence_level": evidence_level,
        "batch_run_id": batch_run_id,
        "verification_digest": verification_digest,
        "launch_id": str(active.get("launch_id") or ""),
    }
    if completion_audit:
        entry_payload["task_review"] = completion_audit
    entry = scrub_secrets(entry_payload)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    from .pacer_launch_context import update_pillar
    verified_acceptance = status == "completed" and evidence_level == "verified_batch"
    evidence_integrity = str(completion_audit.get("evidence_integrity") or "")
    acceptance_adequacy = str(completion_audit.get("acceptance_adequacy") or "unknown")
    product_verdict = str(completion_audit.get("product_verdict") or "indeterminate")
    acceptance_assessment = (
        completion_audit.get("acceptance_assessment")
        if isinstance(completion_audit.get("acceptance_assessment"), dict)
        else {}
    )
    digest_verified = acceptance_assessment.get("digest_verified") is True
    acceptance_active = bool(
        verified_acceptance
        and evidence_integrity == "verified"
        and acceptance_adequacy == "sufficient"
        and product_verdict == "pass"
        and digest_verified
    )
    active = update_pillar(
        workspace_root,
        "acceptance",
        {
            "active": acceptance_active,
            "state": "verified" if acceptance_active else "evidence_verified_result_indeterminate" if verified_acceptance else "not_verified",
            "run_id": batch_run_id,
            "outcome_status": status,
            "evidence_integrity": evidence_integrity,
            "acceptance_adequacy": acceptance_adequacy,
            "product_verdict": product_verdict,
            "digest_verified": digest_verified,
            "standard_source": str(acceptance_assessment.get("standard_source") or "unknown"),
            "standard_digest": str(acceptance_assessment.get("standard_digest") or ""),
        },
        launch_id=launch_id,
    )
    responsibility = active.get("source_responsibility") if isinstance(active.get("source_responsibility"), dict) else {}
    managed_runtime = (
        dict(args.get("_pacer_managed_runtime") or {})
        if args.get("_pacer_managed_runtime_sentinel") is _PACER_MANAGED_RUNTIME_SENTINEL
        and isinstance(args.get("_pacer_managed_runtime"), dict)
        else {}
    )
    managed_active = verified_acceptance and bool(active.get("launch_id"))
    managed_fields = {
        "active": managed_active,
        "state": "completed_in_place" if managed_active else "not_completed",
        "mode": "native_codex_in_place",
        "project_root": str(active.get("project_root") or ""),
        "outcome_recorded": True,
        "run_id": batch_run_id,
    }
    if managed_runtime:
        managed_fields.update(managed_runtime)
    active = update_pillar(
        workspace_root,
        "managed",
        managed_fields,
        launch_id=launch_id,
    )
    audit_valid = not completion_audit or bool(completion_audit.get("valid"))
    dogfood_assessment: dict[str, Any] = {
        "status": "indeterminate",
        "passed": False,
        "reason_codes": ["dogfood_evidence_missing"],
    }
    dogfood_path = repo_root / ".pacer" / "dogfood-evidence.json"
    if dogfood_path.is_file():
        from .dogfood_evidence import load_dogfood_evidence

        key_id = str(os.environ.get("PACER_DOGFOOD_ATTESTATION_KEY_ID") or "").strip()
        key = os.environ.get("PACER_DOGFOOD_ATTESTATION_KEY", "")
        attestation_keys = {key_id: key} if key_id and key else {}
        runs_root = repo_root.parent / ".runs"
        artifact_roots = (runs_root,) if runs_root.is_dir() and not runs_root.is_symlink() else ()
        try:
            dogfood_assessment = load_dogfood_evidence(
                repo_root,
                artifact_roots=artifact_roots,
                attestation_keys=attestation_keys,
            )
        except (OSError, TypeError, ValueError) as exc:
            dogfood_assessment = {
                "status": "failed",
                "passed": False,
                "reason_codes": ["dogfood_evidence_load_failed"],
                "error_type": type(exc).__name__,
            }
    dogfood_active = bool(
        dogfood_assessment.get("passed") is True and verified_acceptance and audit_valid
    )
    attestation = (
        dogfood_assessment.get("attestation")
        if isinstance(dogfood_assessment.get("attestation"), dict)
        else {}
    )
    dogfood_quality = (
        dogfood_assessment.get("quality")
        if isinstance(dogfood_assessment.get("quality"), dict)
        else {}
    )
    active = update_pillar(
        workspace_root,
        "dogfood",
        {
            "active": dogfood_active,
            "state": (
                "pacer_on_pacer_verified"
                if dogfood_active
                else "verified_source_discipline"
                if managed_active and bool(goal) and bool(summary) and audit_valid
                else "discipline_unverified"
            ),
            "source_mode": str(responsibility.get("mode") or ""),
            "project_existed_at_launch": bool(responsibility.get("project_existed_at_launch")),
            "alternate_directory_authorized": bool(responsibility.get("alternate_directory_authorized")),
            "project_root": str(active.get("project_root") or ""),
            "goal_recorded": bool(goal),
            "summary_recorded": bool(summary),
            "verified_batch": verified_acceptance,
            "task_review_valid": bool(completion_audit.get("valid")) if completion_audit else None,
            "pacer_on_pacer": bool(dogfood_assessment.get("pacer_on_pacer")),
            "self_change_attributed": bool(dogfood_assessment.get("self_change_attributed")),
            "installed_artifact_verified": bool(
                dogfood_assessment.get("installed_artifact_verified")
            ),
            "artifact_files_verified": bool(dogfood_assessment.get("artifact_files_verified")),
            "evidence_digest": str(dogfood_assessment.get("evidence_digest") or ""),
            "dogfood_status": str(dogfood_assessment.get("status") or "indeterminate"),
            "dogfood_reason_codes": [
                str(item) for item in (dogfood_assessment.get("reason_codes") or [])[:20]
            ],
            "attestation_status": str(attestation.get("status") or "missing"),
            "attestation_key_id": str(attestation.get("key_id") or ""),
            "quality_score": int(dogfood_quality.get("score") or 0),
            "quality_target_score": int(dogfood_quality.get("target_score") or 95),
            "quality_target_met": dogfood_quality.get("meets_target") is True,
            "quality_level": str(dogfood_quality.get("level") or "insufficient"),
        },
        launch_id=launch_id,
    )
    if acceptance_active and active.get("recovery_source_launch_id"):
        from .pacer_launch_context import resolve_recovery_capsule
        resolve_recovery_capsule(
            workspace_root,
            source_launch_id=str(active.get("recovery_source_launch_id") or ""),
            recovery_launch_id=str(active.get("launch_id") or ""),
        )
    from .pacer_events import append_pacer_event
    append_pacer_event(
        workspace_root,
        "outcome_recorded",
        launch_id=str(active.get("launch_id") or ""),
        data={
            "status": status,
            "evidence_level": evidence_level,
            "batch_run_id": batch_run_id,
            "task_review_verdict": str(completion_audit.get("verdict") or ""),
        },
    )
    return {
        "status": "recorded",
        "path": str(path),
        "outcome_status": status,
        "evidence_level": evidence_level,
        "batch_run_id": batch_run_id,
        "launch_id": launch_id,
        "pillars": active.get("pillars", {}),
        "five_pillars_active": _all_pillars_active(active),
        "five_pillars_assessment": assess_five_pillars(active),
    }


def _completion_managed_runtime(
    *,
    launch_id: str,
    run_id: str,
    attempt: int,
    max_attempts: int,
    verification_status: str,
) -> dict[str, Any]:
    """Represent completion verification as a managed, terminal operation.

    Completion already has a bounded attempt policy and a trusted verification
    batch. Persisting that operation through the canonical managed-state
    transition guard makes those controls auditable instead of leaving the
    Managed pillar with only a model-authored success claim.
    """
    idempotency_key = f"pacer-completion:{launch_id}:{run_id}"
    attempt_id = f"completion-attempt-{attempt}"
    state = new_managed_run(run_id=run_id, idempotency_key=idempotency_key)
    state = transition_managed_run(
        state,
        expected_revision=0,
        next_state="RUNNING",
        event="completion_started",
        attempt_id=attempt_id,
    )
    state = transition_managed_run(
        state,
        expected_revision=1,
        next_state="VERIFYING",
        event="verification_started",
    )
    if verification_status == "passed":
        state = transition_managed_run(
            state,
            expected_revision=2,
            next_state="SUCCEEDED",
            event="verification_passed",
        )
        budget_status = "within_budget" if attempt <= max_attempts else "budget_exhausted"
    else:
        state = transition_managed_run(
            state,
            expected_revision=2,
            next_state="FAILED",
            event="verification_failed",
            reason_code="verification_failed",
        )
        budget_status = "within_budget" if attempt <= max_attempts else "budget_exhausted"
    return {
        "transition_valid": True,
        "idempotency_key": idempotency_key,
        "budget_status": budget_status,
        "budget": {
            "max_attempts": int(max_attempts),
            "attempts_used": int(attempt),
        },
        "managed_state": state.to_dict(),
        "managed_revision": state.revision,
    }


def _pacer_outcome_evidence(
    workspace_root: Path,
    verification: str,
    *,
    outcome_status: str,
    expected_launch_id: str = "",
    trusted_receipt: str = "",
    allow_compile_only: bool = False,
) -> tuple[str, str, str]:
    match = re.search(r"(?:run_id\s*=\s*|batch\s+)([0-9]{8}-[0-9]{6}-[A-Za-z0-9_-]+)", verification)
    if not match:
        return "self_reported", "", ""
    run_id = match.group(1)
    summary_path = workspace_root / "pacer_native" / "commands" / run_id / "summary.json"
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError(f"verification references unknown Pacer command batch: {run_id}")
    batch_status = str(payload.get("status") or "")
    batch_launch_id = str(payload.get("launch_id") or "")
    if batch_launch_id != expected_launch_id:
        raise ValueError(
            f"verification batch launch mismatch: expected {expected_launch_id}, got {batch_launch_id or 'missing'}"
        )
    if int(payload.get("executed_steps") or 0) <= 0:
        raise ValueError(f"verification references an empty Pacer command batch: {run_id}")
    from .pacer_verification import (
        audit_pacer_verification_batch,
        pacer_verification_summary_digest,
        trusted_verification_receipt_errors,
    )

    if batch_status == "passed":
        audit = audit_pacer_verification_batch(
            payload,
            expected_launch_id=expected_launch_id,
            expected_run_id=run_id,
            allow_compile_only=allow_compile_only,
        )
        if not audit.valid:
            raise ValueError(
                "verification references a command batch that did not pass trusted verification: "
                + ", ".join(audit.errors)
            )
    trust_errors = trusted_verification_receipt_errors(
        payload,
        workspace_root=workspace_root,
        trusted_receipt=trusted_receipt,
    )
    if trust_errors:
        raise ValueError(
            "verification batch has no matching process-local trusted receipt: "
            + ", ".join(trust_errors)
        )
    summary_digest = pacer_verification_summary_digest(payload)
    if batch_status == "passed":
        from .pacer_verification import validate_pacer_verification_batch

        validation = validate_pacer_verification_batch(
            payload,
            workspace_root=workspace_root,
            trusted_receipt=trusted_receipt,
            expected_launch_id=expected_launch_id,
            expected_run_id=run_id,
            allow_compile_only=allow_compile_only,
        )
        if validation.valid:
            return "verified_batch", run_id, summary_digest
        raise ValueError(
            "verification references a command batch that did not pass trusted verification: "
            + ", ".join(validation.errors)
        )
    if batch_status == "failed" and outcome_status in {"failed", "blocked"}:
        return "verified_failed_batch", run_id, summary_digest
    raise ValueError(f"verification references a command batch that did not pass: {run_id}")


def run_pacer_commands_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .pacer_context import run_compact_command_batch
    from .pacer_verification import (
        PACER_COMMAND_BATCH_KIND,
        PACER_VERIFICATION_BATCH_KIND,
        PACER_VERIFICATION_POLICY_VERSION,
        PACER_VERIFICATION_SOURCE_TOOL,
    )

    raw_steps = args.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("steps must be a non-empty list")
    workspace_root, repo_root, resolved_launch_id = _resolve_pacer_roots(args)
    active = _activate_pacer_project(
        workspace_root,
        repo_root,
        reason="acceptance",
        launch_id=resolved_launch_id,
    )
    trusted_verification = args.get("_pacer_verification_sentinel") is _PACER_VERIFICATION_SENTINEL
    step_classes = (
        [str(value) for value in args.get("_pacer_verification_step_classes") or []]
        if trusted_verification
        else None
    )
    result = run_compact_command_batch(
        workspace_root=workspace_root,
        repo_root=repo_root,
        steps=[item for item in raw_steps if isinstance(item, dict)],
        stop_on_failure=bool(args.get("stop_on_failure", False)),
        tail_chars=int(args.get("tail_chars") or 2000),
        launch_id=str(active.get("launch_id") or ""),
        batch_kind=PACER_VERIFICATION_BATCH_KIND if trusted_verification else PACER_COMMAND_BATCH_KIND,
        source_tool=PACER_VERIFICATION_SOURCE_TOOL if trusted_verification else "",
        policy_version=PACER_VERIFICATION_POLICY_VERSION if trusted_verification else None,
        step_classes=step_classes,
    )
    if trusted_verification:
        from .pacer_verification import register_trusted_verification_batch

        persisted = _read_pacer_batch_summary(
            workspace_root / "pacer_native",
            str(result.get("run_id") or ""),
        )
        if persisted is None:
            raise ValueError("trusted verification summary was not persisted")
        result["verification_receipt"] = register_trusted_verification_batch(
            persisted,
            workspace_root=workspace_root,
        )
    from .pacer_events import append_pacer_event
    append_pacer_event(
        workspace_root,
        "verification_batch_finished",
        launch_id=str(active.get("launch_id") or ""),
        data={
            "run_id": str(result.get("run_id") or ""),
            "kind": str(result.get("kind") or ""),
            "status": str(result.get("status") or ""),
            "requested_steps": int(result.get("requested_steps") or 0),
            "executed_steps": int(result.get("executed_steps") or 0),
            "failed": int(result.get("failed") or 0),
            "elapsed_seconds": float(result.get("elapsed_seconds") or 0.0),
        },
    )
    return result


def run_pacer_verification_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .pacer_verification import (
        ACCEPTANCE_STEP_CLASSES,
        classify_verification_step,
        is_noop_verification_step,
    )

    raw_steps = args.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("steps must be a non-empty list")
    workspace_root, repo_root, resolved_launch_id = _resolve_pacer_roots(args)
    allow_compile_only = (
        args.get("_pacer_documentation_compile_sentinel")
        is _PACER_DOCUMENTATION_COMPILE_SENTINEL
    )
    python_runtime = _managed_python_runtime(
        workspace_root,
        repo_root,
        launch_id=resolved_launch_id,
    )
    resolved_steps: list[dict[str, Any]] = []
    step_classes: list[str] = []
    pytest_seen = False
    for item in raw_steps:
        argv = [str(value) for value in (item.get("argv") or [])] if isinstance(item, dict) else []
        if not _safe_verification_argv(argv):
            raise ValueError(f"verification command is not allowlisted: {argv[:4]}")
        if is_noop_verification_step(argv):
            raise ValueError(f"verification command is a non-executing inspection mode: {argv[:4]}")
        if isinstance(item, dict) and item.get("env"):
            raise ValueError("run_pacer_verification does not accept environment overrides")
        resolved_argv = list(argv)
        executable = Path(argv[0]).name.lower()
        bare_python = executable in {"python", "python.exe", "python3", "python3.exe"} and not Path(argv[0]).parent.name
        bare_pytest = executable in {"pytest", "pytest.exe"} and not Path(argv[0]).parent.name
        if bare_python or bare_pytest:
            managed_executable = str(python_runtime.get("executable") or "").strip()
            if not managed_executable:
                raise ValueError(
                    "managed Python runtime is unavailable; set PACER_PYTHON or create a project/Pacer venv"
                )
            resolved_argv = (
                [managed_executable, "-m", "pytest", *argv[1:]]
                if bare_pytest
                else [managed_executable, *argv[1:]]
            )
        pytest_policy = "native"
        pytest_cache_policy = "not_applicable"
        if _is_pytest_verification(resolved_argv):
            pytest_seen = True
            resolved_argv = _disable_pytest_plugin(resolved_argv, "cacheprovider")
            pytest_cache_policy = "disabled"
            if str(python_runtime.get("source") or "") == "known_root_venv":
                resolved_argv = _disable_pytest_plugin(resolved_argv, "visual_agent")
                pytest_policy = "pacer_plugin_disabled"
        step_class = classify_verification_step(resolved_argv)
        if step_class == "unknown":
            raise ValueError(f"verification command has no trusted step class: {argv[:4]}")
        resolved_item = {
            **item,
            "argv": resolved_argv,
            "pytest_plugin_policy": pytest_policy,
            "pytest_cache_policy": pytest_cache_policy,
        }
        resolved_steps.append(resolved_item)
        step_classes.append(step_class)
    acceptance_classes = ACCEPTANCE_STEP_CLASSES | ({"compile"} if allow_compile_only else set())
    if not any(step_class in acceptance_classes for step_class in step_classes):
        raise ValueError(
            "verification batch requires at least one substantive test, build, or analyze step; "
            "compile alone is insufficient"
        )
    result = run_pacer_commands_payload({
        **args,
        "steps": resolved_steps,
        "_pacer_verification_sentinel": _PACER_VERIFICATION_SENTINEL,
        "_pacer_verification_step_classes": step_classes,
    })
    return {
        **result,
        "runtime": {"python": python_runtime},
        "pytest_plugin_policy": "preserved_except_pacer_fallback_and_cacheprovider",
        "pytest_cache_policy": "disabled_for_pytest" if pytest_seen else "not_applicable",
    }


def complete_pacer_task_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .pacer_verification import (
        PACER_VERIFICATION_BATCH_KIND,
        PACER_VERIFICATION_POLICY_VERSION,
        PACER_VERIFICATION_SOURCE_TOOL,
    )
    from .task_review import (
        audit_task_completion,
        derive_task_completion_evidence,
        task_contract_allows_compile_only,
        task_review_error,
    )
    from .pacer_launch_context import (
        record_completion_rejection,
        register_completion_attempt,
    )

    goal = str(args.get("goal") or "").strip()
    summary = str(args.get("summary") or "").strip()
    if not goal:
        raise ValueError("goal is required")
    if not summary:
        raise ValueError("summary is required")
    workspace_root, repo_root, resolved_launch_id = _resolve_pacer_roots(args)
    active = _activate_pacer_project(
        workspace_root,
        repo_root,
        reason="completion",
        launch_id=resolved_launch_id,
    )
    launch_id = str(active.get("launch_id") or resolved_launch_id)
    if not launch_id:
        raise ValueError("complete_pacer_task requires an active Pacer launch")
    task_contract = _load_trusted_pacer_task_contract(
        workspace_root=workspace_root,
        repo_root=repo_root,
        active=active,
    )
    completion_policy = (
        task_contract.get("completion_policy")
        if isinstance(task_contract.get("completion_policy"), dict)
        else {}
    )
    max_attempts = max(
        1,
        min(
            MAX_PACER_COMPLETION_ATTEMPTS,
            int(completion_policy.get("max_attempts") or MAX_PACER_COMPLETION_ATTEMPTS),
        ),
    )
    completion_control = register_completion_attempt(
        workspace_root,
        launch_id=launch_id,
        max_attempts=max_attempts,
    )
    completion_attempt = int(completion_control.get("attempts") or 0)
    if completion_attempt > max_attempts:
        raise ValueError(
            _completion_attempts_exhausted_error(
                attempt=completion_attempt,
                max_attempts=max_attempts,
            )
        )
    allow_compile_only = task_contract_allows_compile_only(task_contract)
    source_baseline, source_baseline_digest = _load_trusted_task_source_baseline(
        workspace_root=workspace_root,
        repo_root=repo_root,
        active=active,
    )
    canonical_evidence = derive_task_completion_evidence(
        completion_evidence=args.get("completion_evidence"),
        repo_root=repo_root,
        task_contract=task_contract,
        source_baseline=source_baseline,
    )
    preflight_review = audit_task_completion(
        launch_goal=str(active.get("launch_goal") or ""),
        submitted_goal=goal,
        summary=summary,
        completion_evidence=canonical_evidence,
        requested_steps=args.get("steps"),
        repo_root=repo_root,
        task_contract=task_contract,
        source_baseline=source_baseline,
    )
    if not bool(preflight_review.get("valid")):
        retryable = completion_attempt < max_attempts
        record_completion_rejection(
            workspace_root,
            launch_id=launch_id,
            reason_codes=_task_review_reason_codes(preflight_review),
            retryable=retryable,
        )
        raise ValueError(
            task_review_error(
                preflight_review,
                retryable=retryable,
                attempt=completion_attempt,
                max_attempts=max_attempts,
            )
        )
    pinned_args = {
        "workspace_root": str(workspace_root),
        "repo_root": str(repo_root),
        "_pacer_pinned_launch_sentinel": _PACER_PINNED_LAUNCH_SENTINEL,
        "_pacer_pinned_launch_id": launch_id,
    }
    verification = run_pacer_verification_payload({
        **pinned_args,
        "steps": args.get("steps"),
        "stop_on_failure": bool(args.get("stop_on_failure", False)),
        "tail_chars": int(args.get("tail_chars") or 1200),
        "_pacer_documentation_compile_sentinel": (
            _PACER_DOCUMENTATION_COMPILE_SENTINEL if allow_compile_only else None
        ),
    })
    if (
        str(verification.get("kind") or "") != PACER_VERIFICATION_BATCH_KIND
        or str(verification.get("source_tool") or "") != PACER_VERIFICATION_SOURCE_TOOL
        or int(verification.get("policy_version") or 0) != PACER_VERIFICATION_POLICY_VERSION
    ):
        raise ValueError("complete_pacer_task rejects non-verification command batches")
    run_id = str(verification.get("run_id") or "")
    verification_launch_id = str(verification.get("launch_id") or "")
    if not _valid_pacer_run_id(run_id):
        raise ValueError("verification did not return a valid run_id")
    if verification_launch_id != launch_id:
        raise ValueError(
            f"verification launch mismatch: expected {launch_id}, got {verification_launch_id or 'missing'}"
        )
    verification_status = str(verification.get("status") or "")
    if verification_status not in {"passed", "failed"}:
        raise ValueError(f"verification returned unsupported status: {verification_status or 'missing'}")

    from .pacer_launch_context import read_active_launch

    active_after_verification = read_active_launch(workspace_root, launch_id=launch_id)
    task_contract = _load_trusted_pacer_task_contract(
        workspace_root=workspace_root,
        repo_root=repo_root,
        active=active_after_verification,
    )
    source_baseline, source_baseline_digest = _load_trusted_task_source_baseline(
        workspace_root=workspace_root,
        repo_root=repo_root,
        active=active_after_verification,
    )
    canonical_evidence = derive_task_completion_evidence(
        completion_evidence=args.get("completion_evidence"),
        repo_root=repo_root,
        task_contract=task_contract,
        source_baseline=source_baseline,
    )
    task_review = audit_task_completion(
        launch_goal=str(active_after_verification.get("launch_goal") or ""),
        submitted_goal=goal,
        summary=summary,
        completion_evidence=canonical_evidence,
        requested_steps=args.get("steps"),
        repo_root=repo_root,
        task_contract=task_contract,
        source_baseline=source_baseline,
        verification=verification,
    )
    if verification_status == "passed" and not bool(task_review.get("valid")):
        retryable = completion_attempt < max_attempts
        record_completion_rejection(
            workspace_root,
            launch_id=launch_id,
            reason_codes=_task_review_reason_codes(task_review),
            retryable=retryable,
        )
        raise ValueError(
            task_review_error(
                task_review,
                retryable=retryable,
                attempt=completion_attempt,
                max_attempts=max_attempts,
            )
        )
    task_review["source_baseline_trust"] = {
        "policy_version": 1,
        "digest": source_baseline_digest,
        "receipt_verified": True,
        "launch_id": launch_id,
    }

    runtime = get_pacer_runtime_telemetry_payload({**pinned_args, "detail": "compact"})
    runtime_launch_id = str(runtime.get("launch_id") or "")
    if runtime_launch_id != launch_id:
        raise ValueError(
            f"runtime telemetry launch mismatch: expected {launch_id}, got {runtime_launch_id or 'missing'}"
        )
    final_active = read_active_launch(workspace_root, launch_id=launch_id)
    _load_trusted_pacer_task_contract(
        workspace_root=workspace_root,
        repo_root=repo_root,
        active=final_active,
    )
    _, final_source_baseline_digest = _load_trusted_task_source_baseline(
        workspace_root=workspace_root,
        repo_root=repo_root,
        active=final_active,
    )
    if final_source_baseline_digest != source_baseline_digest:
        raise ValueError("trusted task source baseline changed during completion")
    outcome_status = "completed" if verification_status == "passed" else "failed"
    managed_runtime = _completion_managed_runtime(
        launch_id=launch_id,
        run_id=run_id,
        attempt=completion_attempt,
        max_attempts=max_attempts,
        verification_status=verification_status,
    )
    outcome = record_pacer_outcome_payload({
        **pinned_args,
        "goal": goal,
        "summary": summary,
        "verification": f"run_id={run_id}; status={verification_status}",
        "verification_receipt": str(verification.get("verification_receipt") or ""),
        "status": outcome_status,
        "_pacer_completion_audit_sentinel": _PACER_COMPLETION_AUDIT_SENTINEL,
        "_pacer_completion_audit": task_review,
        "_pacer_managed_runtime_sentinel": _PACER_MANAGED_RUNTIME_SENTINEL,
        "_pacer_managed_runtime": managed_runtime,
    })
    if str(outcome.get("launch_id") or "") != launch_id or str(outcome.get("batch_run_id") or "") != run_id:
        raise ValueError("recorded outcome did not preserve the verification launch/run binding")

    runtime_summary = dict(runtime)
    runtime_summary.pop("pillars", None)
    runtime_summary.pop("five_pillars_active", None)
    runtime_summary.pop("five_pillars_assessment", None)
    compact_outcome = {
        "status": str(outcome.get("status") or ""),
        "outcome_status": str(outcome.get("outcome_status") or outcome_status),
        "evidence_level": str(outcome.get("evidence_level") or ""),
        "batch_run_id": str(outcome.get("batch_run_id") or ""),
        "launch_id": str(outcome.get("launch_id") or ""),
    }
    pillars = _compact_pacer_pillars(outcome.get("pillars"))
    return {
        "schema_version": 1,
        "kind": "pacer_task_completion",
        "status": outcome_status,
        "launch_id": launch_id,
        "run_id": run_id,
        "verification": _compact_completed_verification(verification),
        "task_review": task_review,
        "runtime": runtime_summary,
        "outcome": compact_outcome,
        "pillars": pillars,
        "five_pillars_active": bool(outcome.get("five_pillars_active")),
        "five_pillars_assessment": outcome.get("five_pillars_assessment")
        if isinstance(outcome.get("five_pillars_assessment"), dict)
        else assess_five_pillars(outcome.get("pillars")),
    }


def _task_review_reason_codes(review: dict[str, Any]) -> list[str]:
    errors = review.get("errors") if isinstance(review.get("errors"), list) else []
    return [
        str(item.get("code") or "completion_rejected")
        for item in errors
        if isinstance(item, dict)
    ]


def _completion_attempts_exhausted_error(*, attempt: int, max_attempts: int) -> str:
    payload = {
        "schema_version": 1,
        "kind": "pacer_completion_correction",
        "retryable": False,
        "errors": [
            {
                "code": "completion_attempts_exhausted",
                "message": "Pacer completion correction attempts are exhausted.",
                "correction": "Stop retrying this launch and start a new task after fixing the evidence contract.",
            }
        ],
        "completion_control": {
            "attempt": max(1, int(attempt)),
            "max_attempts": max(1, int(max_attempts)),
        },
    }
    return "completion audit rejected: " + json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _compact_completed_verification(payload: dict[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status") or "")
    raw_records = payload.get("records") if isinstance(payload.get("records"), list) else []
    step_classes = payload.get("step_classes") if isinstance(payload.get("step_classes"), list) else []
    records: list[dict[str, Any]] = []
    for index, raw_record in enumerate(raw_records):
        record = raw_record if isinstance(raw_record, dict) else {}
        compact: dict[str, Any] = {
            "name": str(record.get("name") or f"step-{index + 1}"),
            "status": str(record.get("status") or ""),
            "step_class": str(step_classes[index]) if index < len(step_classes) else "unknown",
            "exit_code": record.get("exit_code"),
            "elapsed_seconds": float(record.get("elapsed_seconds") or 0.0),
        }
        reason = str(record.get("reason") or "").strip()
        if reason:
            compact["reason"] = reason[:240]
        if status != "passed" and compact["status"] != "passed":
            decisive_tail = _pacer_decisive_tail(record)
            if decisive_tail:
                compact["decisive_tail"] = decisive_tail
            compact["logs"] = {
                "stdout": str(record.get("stdout_log") or ""),
                "stderr": str(record.get("stderr_log") or ""),
            }
        records.append(compact)
    return {
        "kind": str(payload.get("kind") or ""),
        "status": status,
        "run_id": str(payload.get("run_id") or ""),
        "requested_steps": int(payload.get("requested_steps") or 0),
        "executed_steps": int(payload.get("executed_steps") or 0),
        "passed": int(payload.get("passed") or 0),
        "failed": int(payload.get("failed") or 0),
        "timed_out": int(payload.get("timed_out") or 0),
        "not_applicable": int(payload.get("not_applicable") or 0),
        "elapsed_seconds": float(payload.get("elapsed_seconds") or 0.0),
        "records": records,
        "run_dir": str(payload.get("run_dir") or ""),
    }


def _pacer_decisive_tail(record: dict[str, Any], *, limit: int = 600) -> str:
    stderr = str(record.get("stderr_tail") or "").strip()
    stdout = str(record.get("stdout_tail") or "").strip()
    combined = "\n".join(value for value in (stdout, stderr) if value)
    return combined[-max(120, min(600, int(limit))) :]


def _managed_python_runtime(
    workspace_root: Path,
    repo_root: Path,
    *,
    launch_id: str = "",
) -> dict[str, Any]:
    from .pacer_launch_context import read_active_launch, resolve_python_runtime, update_active_launch

    canonical_repo = repo_root.expanduser().resolve()
    canonical_identity = os.path.normcase(str(canonical_repo))
    active = (
        read_active_launch(workspace_root, launch_id=launch_id)
        if launch_id
        else read_active_launch(workspace_root)
    )
    lifecycle_status = str(active.get("status") or "")
    if lifecycle_status and lifecycle_status != "running":
        raise ValueError(
            f"Pacer launch {active.get('launch_id') or 'unknown'} is already {lifecycle_status}; "
            "terminal launch evidence is immutable"
        )
    runtime = active.get("runtime") if isinstance(active.get("runtime"), dict) else {}
    python_runtime = runtime.get("python") if isinstance(runtime.get("python"), dict) else {}
    source = str(python_runtime.get("source") or "")
    raw_bound_root = str(python_runtime.get("bound_repo_root") or "").strip()
    try:
        bound_identity = os.path.normcase(str(Path(raw_bound_root).expanduser().resolve())) if raw_bound_root else ""
    except OSError:
        bound_identity = ""

    if source == "environment":
        fixed_runtime = {**python_runtime, "bound_repo_root": str(canonical_repo)}
        if active and fixed_runtime != python_runtime:
            update_active_launch(
                workspace_root,
                expected_launch_id=str(active.get("launch_id") or ""),
                runtime={**runtime, "python": fixed_runtime},
            )
        return fixed_runtime
    if bound_identity == canonical_identity:
        return dict(python_runtime)

    known_roots: list[Path] = []
    launch_cwd = Path(str(active.get("launch_cwd") or ""))
    if launch_cwd.is_dir():
        known_roots.append(launch_cwd)
    resolution_environment = dict(os.environ)
    if python_runtime:
        # Older launchers injected their fallback binding into PACER_PYTHON.
        # Only a runtime already attributed to the user's environment is fixed.
        resolution_environment.pop("PACER_PYTHON", None)
    python_runtime = resolve_python_runtime(
        canonical_repo,
        known_roots=known_roots,
        environment=resolution_environment,
    )
    if active:
        update_active_launch(
            workspace_root,
            expected_launch_id=str(active.get("launch_id") or ""),
            runtime={**runtime, "python": python_runtime},
        )
    return python_runtime


def _is_pytest_verification(argv: list[str]) -> bool:
    if not argv:
        return False
    executable = Path(argv[0]).name.lower()
    arguments = [value.lower() for value in argv[1:]]
    return executable in {"pytest", "pytest.exe"} or (
        executable in {"python", "python.exe", "python3", "python3.exe"}
        and arguments[:2] == ["-m", "pytest"]
    )


def _disable_pytest_plugin(argv: list[str], plugin_name: str) -> list[str]:
    """Disable one pytest entry point while preserving the remaining project plugins."""
    if not _is_pytest_verification(argv):
        return list(argv)
    marker = f"no:{plugin_name}"
    if any(value == marker for index, value in enumerate(argv) if index > 0 and argv[index - 1] == "-p"):
        return list(argv)
    executable = Path(argv[0]).name.lower()
    insert_at = 3 if executable in {"python", "python.exe", "python3", "python3.exe"} else 1
    return [*argv[:insert_at], "-p", marker, *argv[insert_at:]]


def _safe_verification_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    executable = Path(argv[0]).name.lower()
    args = [value.lower() for value in argv[1:]]
    if _has_mutating_verification_option(executable, args):
        return False
    if executable in {"python", "python.exe", "python3", "python3.exe"}:
        if len(args) >= 2 and args[0] == "-m" and args[1] in {"pytest", "compileall", "mypy"}:
            return True
        if len(args) >= 3 and args[:3] == ["-m", "ruff", "check"]:
            return True
        if args == [
            "-m",
            "visual_agent.cli",
            "codex-check",
            "--workspace-root",
            ".agent-workspace",
            "--repo-root",
            ".",
        ]:
            return True
        return _safe_unittest_discover_argv(argv[1:])
    if executable in {"pytest", "pytest.exe", "mypy", "mypy.exe"}:
        return True
    if executable in {"ruff", "ruff.exe"}:
        return args[:1] == ["check"]
    if executable in {"git", "git.exe"}:
        return bool(args) and args[0] in {"diff", "status", "rev-parse"}
    if executable in {"npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd"}:
        return bool(args) and (args[0] == "test" or (len(args) >= 2 and args[0] == "run" and args[1] in {"test", "check", "lint", "build"}))
    if executable in {"dart", "dart.exe", "flutter", "flutter.bat"}:
        return bool(args) and args[0] in {"test", "analyze"}
    if executable in {"go", "go.exe", "cargo", "cargo.exe"}:
        return bool(args) and args[0] == "test"
    return False


def _has_mutating_verification_option(executable: str, args: list[str]) -> bool:
    joined = [str(value).lower() for value in args]
    module = joined[1] if len(joined) >= 2 and joined[0] == "-m" else ""
    tool = module or executable.removesuffix(".exe").removesuffix(".cmd")
    tool_args = joined[2:] if module else joined

    if tool == "ruff":
        mutating = {"--fix", "--fix-only", "--unsafe-fixes", "--output-file"}
        return any(
            value in mutating
            or value.startswith("--fix=")
            or value.startswith("--fix-only=")
            or value.startswith("--unsafe-fixes=")
            or value.startswith("--output-file=")
            for value in tool_args
        )
    if tool == "git":
        return any(
            value in {"--output", "--ext-diff", "--textconv"} or value.startswith("--output=")
            for value in tool_args
        )
    if tool in {"pytest", "py.test"}:
        mutating = {
            "--cache-clear",
            "--lf",
            "--last-failed",
            "--ff",
            "--failed-first",
            "--nf",
            "--new-first",
            "--sw",
            "--stepwise",
            "--snapshot-update",
            "--update-snapshot",
            "--update-snapshots",
            "--approve-snapshots",
        }
        if any(value in mutating for value in tool_args):
            return True
        if any(
            ("snapshot" in value and any(action in value for action in ("update", "approve", "record")))
            for value in tool_args
        ):
            return True
        for index, value in enumerate(tool_args[:-1]):
            if value == "-p" and tool_args[index + 1] in {"cacheprovider", "pytest_cacheprovider"}:
                return True
    return False


def _safe_unittest_discover_argv(args: list[str]) -> bool:
    """Allow one deterministic unittest discovery form without Python escape hatches."""
    if len(args) != 6:
        return False
    normalized = [value.lower() for value in args]
    if normalized[:4] != ["-m", "unittest", "discover", "-s"] or normalized[5] != "-v":
        return False
    start_dir = args[4]
    if not start_dir or start_dir != start_dir.strip() or "\x00" in start_dir:
        return False
    portable = start_dir.replace("\\", "/")
    if portable.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", portable):
        return False
    parts = portable.split("/")
    return all(part not in {"", ".", ".."} and not part.startswith("-") for part in parts)


def _resolve_pacer_roots(args: dict[str, Any]) -> tuple[Path, Path, str]:
    repo_root = Path(str(args.get("repo_root") or ".")).expanduser().resolve()
    raw_workspace = str(args.get("workspace_root") or ".agent-workspace").strip()
    workspace_path = Path(raw_workspace).expanduser()
    standard_relative = workspace_path in {Path("."), Path(".agent-workspace")}
    workspace_root = (
        workspace_path.resolve() if workspace_path.is_absolute() else (repo_root / workspace_path).resolve()
    )
    same_as_repo = os.path.normcase(str(workspace_root)) == os.path.normcase(str(repo_root))
    if standard_relative or same_as_repo:
        workspace_root = (repo_root / ".agent-workspace").resolve()
    from .pacer_launch_context import find_active_launch
    pinned_launch_id = (
        str(args.get("_pacer_pinned_launch_id") or "").strip()
        if args.get("_pacer_pinned_launch_sentinel") is _PACER_PINNED_LAUNCH_SENTINEL
        else ""
    )
    preferred_launch_id = pinned_launch_id or str(os.environ.get("PACER_LAUNCH_ID") or "").strip()
    active_workspace, active_launch = find_active_launch(
        repo_root=repo_root,
        suggested_workspace=workspace_root,
        preferred_launch_id=preferred_launch_id,
    )
    launch_id = str(active_launch.get("launch_id") or "")
    return (active_workspace or workspace_root), repo_root, launch_id


def _activate_pacer_project(
    workspace_root: Path,
    repo_root: Path,
    *,
    reason: str,
    launch_id: str = "",
) -> dict[str, Any]:
    from .pacer_launch_context import bind_active_project, read_active_launch

    active = read_active_launch(workspace_root, launch_id=launch_id) if launch_id else read_active_launch(workspace_root)
    if not active:
        return {}
    lifecycle_status = str(active.get("status") or "")
    if lifecycle_status and lifecycle_status != "running":
        raise ValueError(
            f"Pacer launch {active.get('launch_id') or 'unknown'} is already {lifecycle_status}; "
            "terminal launch evidence is immutable"
        )
    active = bind_active_project(
        workspace_root=workspace_root,
        repo_root=repo_root,
        reason=reason,
        launch_id=launch_id,
    )
    return active


def _all_pillars_active(active: dict[str, Any]) -> bool:
    return bool(assess_five_pillars(active)["passed"])


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
        "inspection_only": report.inspection_only,
        "failed": report.failed,
        "verdict": report.verdict,
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
    safe_payload = _safe_mcp_payload(payload)
    text = json.dumps(safe_payload, ensure_ascii=False, indent=2, default=str)
    return [TextContent(type="text", text=text)]


def _tool_result(payload: dict[str, Any], *, tool_name: str = "") -> CallToolResult:
    safe_payload = _safe_mcp_payload(payload)
    if tool_name in PACER_TYPED_TOOL_NAMES:
        try:
            safe_payload = validate_pacer_tool_output(tool_name, safe_payload)
        except Exception:
            safe_payload = validate_pacer_tool_output(
                tool_name,
                mcp_error_payload("MCP structured output validation failed"),
            )
    text = json.dumps(safe_payload, ensure_ascii=False, indent=2, default=str)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=safe_payload,
        isError=bool(safe_payload.get("error")),
    )


def _safe_mcp_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe = budget_mcp_payload(scrub_secrets(payload))
    return safe if isinstance(safe, dict) else {"schema_version": 1, "error": "invalid MCP payload"}


def main() -> None:
    _apply_startup_args()
    if server is None or stdio_server is None:
        raise RuntimeError("MCP support is not installed. Reinstall with: python -m pip install -e .")
    asyncio.run(_run())


def _apply_startup_args(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--workspace-root", default=None)
    namespace, _ = parser.parse_known_args(argv)
    workspace_root = str(namespace.workspace_root or "").strip()
    if workspace_root:
        os.environ["VISUAL_AGENT_WORKSPACE"] = workspace_root


async def _run() -> None:
    if server is None or stdio_server is None:
        raise RuntimeError("MCP support is not installed. Reinstall with: python -m pip install -e .")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()
