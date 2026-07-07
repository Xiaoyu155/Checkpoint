"""MCP server helpers — consolidated from 6 thin modules."""
from __future__ import annotations

import json
from typing import Any

from .run_profile import RUN_PROFILE_CHOICES, run_profile_privilege
from .workspace import Workspace


# ── policy ────────────────────────────────────────────────────────────────────

RUN_PROFILE_ORDER = {name: run_profile_privilege(name) for name in RUN_PROFILE_CHOICES}


def mcp_config(workspace: Workspace) -> dict[str, Any]:
    path = workspace.root / "workspace.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload.get("mcp") if isinstance(payload.get("mcp"), dict) else {}


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


# ── audit ─────────────────────────────────────────────────────────────────────

def workspace_for_audit(args: dict[str, Any]) -> Workspace | None:
    from .mcp_common import require_workspace
    try:
        return require_workspace(args)
    except Exception:  # noqa: BLE001
        return None


def audit_mcp_call(workspace: Workspace | None, tool_name: str, args: dict[str, Any], payload: dict[str, Any]) -> None:
    if workspace is None:
        return
    if mcp_config(workspace).get("audit_all_calls", True) is not True:
        return
    from .gui import write_gui_action_event
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


# ── benchmarks ────────────────────────────────────────────────────────────────

def list_benchmarks_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .benchmarks import list_public_benchmarks
    from .mcp_common import require_workspace
    workspace = require_workspace(args)
    return {"workspace": str(workspace.root), **list_public_benchmarks(category=str(args.get("category") or "") or None)}


def build_benchmark_plan_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .benchmarks import build_benchmark_plan
    from .mcp_common import require_workspace
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
    from .mcp_common import require_str, require_workspace
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


# ── browser smoke ─────────────────────────────────────────────────────────────

def run_browser_smoke_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .browser_smoke import run_browser_smoke
    from .mcp_common import require_str, require_workspace
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
            expect_text=[str(i) for i in args.get("expect_text", []) if str(i)],
            expect_url_contains=[str(i) for i in args.get("expect_url_contains", []) if str(i)],
            fill=[str(i) for i in args.get("fill", []) if str(i)],
            fill_selector=[str(i) for i in args.get("fill_selector", []) if str(i)],
            click_text=str(args.get("click_text") or "") or None,
            click_selector=str(args.get("click_selector") or "") or None,
            require_change_after_click=bool(args.get("require_change_after_click", False)),
            wait_for_text_after=[str(i) for i in args.get("wait_for_text_after", []) if str(i)],
            wait_for_url_contains_after=[str(i) for i in args.get("wait_for_url_contains_after", []) if str(i)],
            wait_timeout_seconds=float(args.get("wait_timeout_seconds") or 5.0),
            expect_text_after=[str(i) for i in args.get("expect_text_after", []) if str(i)],
            expect_url_contains_after=[str(i) for i in args.get("expect_url_contains_after", []) if str(i)],
            save_workflow=(workspace.root / str(args.get("save_workflow"))).resolve() if str(args.get("save_workflow") or "").strip() else None,
            overwrite_workflow=bool(args.get("overwrite_workflow", False)),
        ),
    }


def run_browser_smoke_suite_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .browser_smoke_suite import run_browser_smoke_suite
    from .mcp_common import require_str, require_workspace
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


# ── generation format ─────────────────────────────────────────────────────────

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
        "required_field_count": sum(1 for f in model.form_fields if f.required),
        "sensitive_field_count": sum(1 for f in model.form_fields if f.is_sensitive),
        "validation_rule_count": sum(len(f.validation_rules) for f in model.form_fields),
        "submit_action_count": len(model.submit_actions),
        "success_state_count": len(model.success_states),
        "error_state_count": len(model.error_states),
        "data_display_count": len(model.data_displays),
        "negative_input_case_count": len(generation.negative_input_cases),
        "fields": [f.name for f in model.form_fields[:8]],
        "success_states": [s.value for s in model.success_states[:5]],
        "data_displays": list(model.data_displays[:8]),
        "matched_data_displays": list(display_summary.matched[:8]),
        "unmatched_data_displays": list(display_summary.unmatched[:8]),
        "warnings": list(generation.warnings[:5]),
    }


# ── session ───────────────────────────────────────────────────────────────────

def get_session_context_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .mcp_common import require_workspace
    from .session import load_agent_session, workspace_session_snapshot_text
    workspace = require_workspace(args)
    session = load_agent_session(workspace.root)
    snapshot = workspace_session_snapshot_text(workspace.root)
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "snapshot": snapshot,
        "token_estimate": len(snapshot) // 4,
        "within_budget": True if session is None else len(snapshot) <= 2000,
    }


def get_visual_status_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .mcp_common import require_workspace
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
    from .mcp_common import require_str, require_workspace
    from .session import save_task_context, session_to_snapshot_text
    workspace = require_workspace(args)
    session = save_task_context(
        workspace.root,
        task=require_str(args, "task"),
        analyzed_files=[str(i) for i in args.get("analyzed_files", []) if str(i)],
        root_cause=str(args.get("root_cause") or ""),
        plan=str(args.get("plan") or ""),
        tried=[str(i) for i in args.get("tried", []) if str(i)],
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
