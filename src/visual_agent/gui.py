from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from typing import Any, Callable
from uuid import uuid4

from .auth_state import import_auth_state, inspect_storage_state
from .console import build_report_detail, build_workspace_dashboard, report_detail_to_markdown, risk_policy_check_to_markdown
from .external_samples import (
    build_external_sample_batch_rerun_plan,
    build_external_sample_run_plan,
    build_external_sample_run_summary,
    build_external_sample_rerun_plan,
    external_samples_readiness,
    export_external_sample_batch_report,
    load_external_sample_batch_report_index,
    submit_external_sample_batch,
    submit_external_sample_batch_reruns,
    submit_external_sample_reruns,
)
from .planner import check_planner_draft
from .planner_generate import (
    generate_planner_draft,
    planner_draft_result_to_markdown,
    preview_planner_draft_save,
    save_planner_draft_result,
    workflow_to_dict,
)
from .quality import (
    build_install_check_plan,
    build_release_check_plan,
    demo_workspace_check_to_markdown,
    install_check_plan_to_markdown,
    mcp_smoke_check_to_markdown,
    release_check_plan_to_markdown,
    run_demo_workspace_check,
    run_mcp_smoke_check,
)
from .recorder import record_browser_session, recorded_result_to_dict, recorded_result_to_markdown
from .scheduler import cancel_queue_task, retry_queue_task, run_next_queue_task
from .workspace import (
    Workspace,
    build_workspace_risk_policy_apply_plan,
    discover_workflows,
    find_workflow,
    load_workspace_gui_action_history_risk_config,
    load_workspace_inputs,
    run_workspace_workflow,
)
from .workflow import parse_workflow_file


GUI_ACTIONS = {
    "run_workflow",
    "queue_run_next",
    "cancel_queue_task",
    "retry_queue_task",
    "open_artifact",
    "inspect_auth_state",
    "import_auth_state",
    "delete_auth_state",
    "refresh_readiness",
    "plan_external_sample_run",
    "submit_external_sample_batch",
    "external_sample_summary",
    "external_sample_batch_report",
    "plan_external_sample_batch_reruns",
    "submit_external_sample_batch_reruns",
    "plan_external_sample_reruns",
    "submit_external_sample_reruns",
    "plan_risk_policy_patch",
    "apply_risk_policy_patch",
    "preview_planner_draft_save",
    "generate_planner_draft_preview",
    "save_generated_planner_draft",
    "record_browser_workflow",
    "read_input_template",
    "save_input_template",
    "install_check",
    "release_check",
    "demo_workspace_check",
    "mcp_smoke_check",
}

LONG_RUNNING_GUI_ACTIONS = {
    "run_workflow",
    "queue_run_next",
    "record_browser_workflow",
    "generate_planner_draft_preview",
    "save_generated_planner_draft",
    "external_sample_batch_report",
    "submit_external_sample_batch",
    "submit_external_sample_batch_reruns",
    "submit_external_sample_reruns",
    "demo_workspace_check",
    "mcp_smoke_check",
}


@dataclass
class GuiAsyncJob:
    job_id: str
    action: str
    status: str = "running"
    started_at: float = field(default_factory=time)
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    thread: threading.Thread | None = None

    @property
    def done(self) -> bool:
        return self.status in {"success", "error"}


def is_long_running_gui_action(action: str) -> bool:
    return action in LONG_RUNNING_GUI_ACTIONS


def run_gui_action_async(
    workspace: Workspace,
    plan: dict[str, Any],
    *,
    selected_run_id: str | None = None,
    selected_batch_report_id: str | None = None,
    on_done: Callable[[GuiAsyncJob], None] | None = None,
) -> GuiAsyncJob:
    job = GuiAsyncJob(job_id=f"gui-job-{uuid4().hex[:12]}", action=str(plan.get("action") or "unknown"))

    def target() -> None:
        try:
            job.result = safe_execute_gui_action(
                workspace,
                plan,
                selected_run_id=selected_run_id,
                selected_batch_report_id=selected_batch_report_id,
            )
            job.status = "success" if job.result.get("status") != "error" else "error"
            if job.status == "error":
                job.error = job.result.get("error") if isinstance(job.result.get("error"), dict) else None
        except Exception as exc:
            job.status = "error"
            job.error = {"type": exc.__class__.__name__, "message": str(exc)}
            job.result = {
                "action": job.action,
                "status": "error",
                "error": job.error,
                "message": f"{job.action} failed: {exc}",
            }
        finally:
            job.finished_at = time()
            if on_done is not None:
                on_done(job)

    thread = threading.Thread(target=target, name=f"visual-agent-{job.job_id}", daemon=True)
    job.thread = thread
    thread.start()
    return job

DEFAULT_GUI_ACTION_HISTORY_RISK_POLICY = {
    "history_limit": 100,
    "error_rate_threshold": 0.2,
    "failed_action_limit": 5,
}


def build_console_window_model(
    workspace: Workspace,
    *,
    selected_run_id: str | None = None,
    selected_batch_report_id: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    dashboard = build_workspace_dashboard(workspace, limit=limit)
    readiness = build_external_sample_readiness(workspace)
    external_summary = build_external_sample_run_summary(workspace)
    batch_report_index = load_external_sample_batch_report_index(workspace, rebuild=True)
    action_events = list_gui_action_events(workspace, limit=limit)
    action_history_index = build_gui_action_history_index(workspace)
    action_history_risk = build_gui_action_history_risk_summary(
        workspace,
        config=load_workspace_gui_action_history_risk_config(workspace),
    )
    action_event_options = gui_action_event_options(action_events)
    action_risk_event_options = gui_action_risk_event_options(action_history_risk, action_event_options)
    report_opts = list_report_options(dashboard)
    batch_options = batch_report_options(batch_report_index)
    active_run_id = selected_run_id or (report_opts[0]["run_id"] if report_opts else None)
    active_batch_report_id = selected_batch_report_id or (batch_options[0]["report_id"] if batch_options else None)
    selected_report = build_report_detail(workspace, active_run_id) if active_run_id else None
    selected_batch_report = next(
        (item for item in batch_options if item["report_id"] == active_batch_report_id),
        None,
    )
    selected_batch_markdown = batch_report_detail_markdown(workspace, selected_batch_report) if selected_batch_report else ""
    workflow_opts = workflow_options(workspace)
    queue_opts = queue_options(dashboard)
    return {
        "schema_version": 1,
        "title": f"Checkpoint Console - {workspace.root.name}",
        "workspace_root": str(workspace.root),
        "dashboard": dashboard,
        "external_sample_readiness": readiness,
        "external_sample_summary": external_summary,
        "external_sample_batch_report_index": batch_report_index,
        "gui_action_history_index": action_history_index,
        "gui_action_history_risk": action_history_risk,
        "gui_action_history_risk_markdown": gui_action_history_risk_to_markdown(action_history_risk),
        "risk_policy_check_markdown": risk_policy_check_to_markdown(dashboard.get("risk_policy_check", {})),
        "strict_policy_failed_markdown": strict_policy_failed_markdown(dashboard),
        "gui_action_events": action_events,
        "gui_action_event_options": action_event_options,
        "selected_gui_action_event": action_event_options[0] if action_event_options else None,
        "selected_gui_action_event_markdown": gui_action_event_markdown(action_event_options[0]) if action_event_options else "",
        "gui_action_risk_event_options": action_risk_event_options,
        "selected_gui_action_risk_event": action_risk_event_options[0] if action_risk_event_options else None,
        "readiness_options": readiness_options(readiness),
        "batch_report_options": batch_options,
        "readiness_markdown": readiness_to_markdown(readiness),
        "summary_cards": summary_cards(dashboard, readiness, action_history_risk),
        "workflow_options": workflow_opts,
        "queue_options": queue_opts,
        "action_buttons": action_buttons(dashboard),
        "report_options": report_opts,
        "primary_columns": console_primary_columns(workflow_opts, report_opts, queue_opts),
        "artifact_options": artifact_options(workspace, selected_report),
        "auth_state_options": auth_state_options(workspace),
        "input_template_options": input_template_options(workspace),
        "selected_run_id": active_run_id,
        "selected_report": selected_report,
        "selected_report_markdown": report_detail_to_markdown(selected_report) if selected_report else "",
        "selected_batch_report_id": active_batch_report_id,
        "selected_batch_report": selected_batch_report,
        "selected_batch_report_markdown": selected_batch_markdown,
    }


def console_primary_columns(
    workflow_opts: list[dict[str, Any]],
    report_opts: list[dict[str, Any]],
    queue_opts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "id": "workflows",
            "title": "Workflows",
            "option_count": len(workflow_opts),
            "options": workflow_opts,
            "primary_actions": ["run_workflow", "preview_planner_draft_save", "record_browser_workflow"],
            "empty_state": "No workflows installed.",
        },
        {
            "id": "runs",
            "title": "Runs",
            "option_count": len(report_opts),
            "options": report_opts,
            "primary_actions": ["open_artifact", "show_strict_policy_failures", "show_action_history"],
            "empty_state": "No run reports yet.",
        },
        {
            "id": "queue",
            "title": "Queue",
            "option_count": len(queue_opts),
            "options": queue_opts,
            "primary_actions": ["queue_run_next", "cancel_queue_task", "retry_queue_task"],
            "empty_state": "No queue tasks.",
        },
    ]


def refreshed_console_model(
    workspace: Workspace,
    *,
    selected_run_id: str | None = None,
    selected_batch_report_id: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    return build_console_window_model(
        workspace,
        selected_run_id=selected_run_id,
        selected_batch_report_id=selected_batch_report_id,
        limit=limit,
    )


def attach_refreshed_console_model(
    payload: dict[str, Any],
    workspace: Workspace,
    *,
    selected_run_id: str | None = None,
    selected_batch_report_id: str | None = None,
) -> dict[str, Any]:
    payload["refreshed_model"] = refreshed_console_model(
        workspace,
        selected_run_id=selected_run_id,
        selected_batch_report_id=selected_batch_report_id,
    )
    return payload


def safe_planner_draft_save_name(draft: dict[str, Any]) -> str:
    workflow = draft.get("workflow") if isinstance(draft.get("workflow"), dict) else {}
    raw = str(workflow.get("name") or "generated_planner_draft")
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in raw.strip()).strip("_")
    return f"planner_generated/{safe or 'generated_planner_draft'}"


def queue_result_run_id(result: dict[str, Any]) -> str | None:
    run_result = result.get("result") if isinstance(result.get("result"), dict) else {}
    run_id = run_result.get("run_id")
    return str(run_id) if run_id else None


def console_window_selection_state(model: dict[str, Any]) -> dict[str, Any]:
    report_options = model.get("report_options") if isinstance(model.get("report_options"), list) else []
    workflow_options = model.get("workflow_options") if isinstance(model.get("workflow_options"), list) else []
    queue_options_ = model.get("queue_options") if isinstance(model.get("queue_options"), list) else []
    artifact_options_ = model.get("artifact_options") if isinstance(model.get("artifact_options"), list) else []
    auth_options = model.get("auth_state_options") if isinstance(model.get("auth_state_options"), list) else []
    readiness_options_ = model.get("readiness_options") if isinstance(model.get("readiness_options"), list) else []
    batch_options = model.get("batch_report_options") if isinstance(model.get("batch_report_options"), list) else []
    action_event_options = model.get("gui_action_event_options") if isinstance(model.get("gui_action_event_options"), list) else []
    state = {
        "report": selection_group(report_options, "run_id", model.get("selected_run_id")),
        "workflow": selection_group(workflow_options),
        "queue": selection_group(queue_options_),
        "artifact": selection_group(artifact_options_),
        "auth_state": selection_group(auth_options),
        "readiness": selection_group(readiness_options_),
        "batch_report": selection_group(batch_options, "report_id", model.get("selected_batch_report_id")),
        "action_history": selection_group(action_event_options),
    }
    return state


def selection_group(options: list[dict[str, Any]], selected_key: str | None = None, selected_value: object | None = None) -> dict[str, Any]:
    by_label = {str(item.get("label") or ""): item for item in options if isinstance(item, dict) and item.get("label")}
    labels = list(by_label)
    selected_label = ""
    if selected_key and selected_value is not None:
        selected_label = next(
            (
                str(item.get("label") or "")
                for item in options
                if isinstance(item, dict) and str(item.get(selected_key) or "") == str(selected_value)
            ),
            "",
        )
    if not selected_label and labels:
        selected_label = labels[0]
    return {"by_label": by_label, "labels": labels, "selected_label": selected_label}


def console_model_detail_markdown(model: dict[str, Any]) -> str:
    return str(
        model.get("selected_error_detail_markdown")
        or (gui_error_detail_to_markdown(model["selected_error_detail"]) if isinstance(model.get("selected_error_detail"), dict) else "")
        or
        model.get("selected_report_markdown")
        or model.get("selected_batch_report_markdown")
        or model.get("selected_gui_action_event_markdown")
        or model.get("risk_policy_check_markdown")
        or model.get("gui_action_history_risk_markdown")
        or "No reports yet."
    )


def console_window_button_states(model: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    state = state or console_window_selection_state(model)
    selected_queue = selected_group_item(state, "queue")
    selected_readiness = selected_group_item(state, "readiness")
    selected_batch = selected_group_item(state, "batch_report")
    selected_artifact = selected_group_item(state, "artifact")
    selected_auth = selected_group_item(state, "auth_state")
    selected_workflow = selected_group_item(state, "workflow")
    selected_history = selected_group_item(state, "action_history")
    selected_risk_event = model.get("selected_gui_action_risk_event") if isinstance(model.get("selected_gui_action_risk_event"), dict) else None
    dashboard = model.get("dashboard") if isinstance(model.get("dashboard"), dict) else {}
    queue = dashboard.get("queue") if isinstance(dashboard.get("queue"), dict) else {}
    quality = dashboard.get("quality_gates") if isinstance(dashboard.get("quality_gates"), dict) else {}
    readiness = model.get("external_sample_readiness") if isinstance(model.get("external_sample_readiness"), dict) else {}
    summary = model.get("external_sample_summary") if isinstance(model.get("external_sample_summary"), dict) else {}
    pending_count = int(queue.get("pending") or 0)
    ready_samples = int(readiness.get("ready_samples") or 0)
    has_failed_external = any_external_sample_status(summary, "failed")
    has_ready_failed_external = any_external_sample_status(summary, "failed", ready=True)
    queue_status = str(selected_queue.get("status") or "") if selected_queue else ""
    states = {
        "run_workflow": button_state(bool(selected_workflow), "No workflow selected."),
        "queue_run_next": button_state(pending_count > 0, "No pending queue task."),
        "cancel_queue_task": button_state(bool(selected_queue) and queue_status == "pending", "Select a pending queue task."),
        "retry_queue_task": button_state(bool(selected_queue) and queue_status in {"failed", "canceled"}, "Select a failed or canceled queue task."),
        "open_artifact": button_state(bool(selected_artifact), "No artifact selected."),
        "inspect_auth_state": button_state(bool(selected_auth), "No auth state selected."),
        "delete_auth_state": button_state(bool(selected_auth), "No auth state selected."),
        "refresh_readiness": button_state(True),
        "plan_external_sample_run": button_state(bool(selected_readiness), "No external sample selected."),
        "submit_external_sample_batch": button_state(ready_samples > 0, "No ready external samples."),
        "external_sample_summary": button_state(True),
        "external_sample_batch_report": button_state(True),
        "open_batch_report": button_state(bool(selected_batch), "No batch report selected."),
        "plan_external_sample_batch_reruns": button_state(bool(selected_batch), "No batch report selected."),
        "submit_external_sample_batch_reruns": button_state(bool(selected_batch), "No batch report selected."),
        "plan_external_sample_reruns": button_state(has_failed_external, "No failed external sample reports."),
        "submit_external_sample_reruns": button_state(has_ready_failed_external, "No ready failed external samples."),
        "plan_risk_policy_patch": button_state(True),
        "apply_risk_policy_patch": button_state(True),
        "preview_planner_draft_save": button_state(bool(selected_workflow), "No workflow selected."),
        "generate_planner_draft_preview": button_state(True),
        "save_generated_planner_draft": button_state(True),
        "record_browser_workflow": button_state(True),
        "read_input_template": button_state(True),
        "save_input_template": button_state(True),
        "install_check": button_state(True),
        "release_check": button_state(True),
        "demo_workspace_check": button_state(True),
        "mcp_smoke_check": button_state(True),
        "show_strict_policy_failures": button_state(
            bool(quality.get("strict_policy_failed_reports")),
            "No strict policy failure reports.",
        ),
        "show_action_risk": button_state(bool(selected_risk_event), "No risky GUI action event selected."),
        "show_action_history": button_state(bool(selected_history), "No GUI action event selected."),
    }
    return states


def selected_group_item(state: dict[str, Any], group_name: str) -> dict[str, Any] | None:
    group = state.get(group_name) if isinstance(state.get(group_name), dict) else {}
    label = str(group.get("selected_label") or "")
    by_label = group.get("by_label") if isinstance(group.get("by_label"), dict) else {}
    item = by_label.get(label)
    return item if isinstance(item, dict) else None


def button_state(enabled: bool, disabled_reason: str = "") -> dict[str, Any]:
    return {"enabled": bool(enabled), "disabled_reason": "" if enabled else disabled_reason}


def any_external_sample_status(summary: dict[str, Any], status: str, *, ready: bool | None = None) -> bool:
    entries = summary.get("entries") if isinstance(summary.get("entries"), list) else []
    for entry in entries:
        if not isinstance(entry, dict) or str(entry.get("status") or "") != status:
            continue
        if ready is not None and bool(entry.get("ready")) is not ready:
            continue
        return True
    return False


def gui_action_feedback(result: dict[str, Any], preferred: str | None = None) -> str:
    if result.get("status") == "error":
        return gui_error_detail_to_markdown(build_gui_error_detail(result))
    result_preferred = result.get("preferred")
    if preferred is None and isinstance(result_preferred, str) and result_preferred.strip():
        preferred = result_preferred
    if isinstance(result.get("policy_plan"), dict):
        return risk_policy_plan_to_markdown(result["policy_plan"])
    if isinstance(result.get("planner_draft"), dict):
        return planner_draft_result_to_markdown(result["planner_draft"])
    if isinstance(result.get("recording"), dict):
        return recording_result_to_markdown(result["recording"])
    if preferred is not None:
        return preferred
    message = result.get("message")
    if message:
        return str(message)
    action = str(result.get("action") or "action")
    status = str(result.get("status") or "unknown")
    return f"{action}: {status}"


def build_gui_error_detail(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "unknown")
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    failure_report = payload.get("failure_report") if isinstance(payload.get("failure_report"), dict) else {}
    preflight = payload.get("preflight") if isinstance(payload.get("preflight"), dict) else {}
    quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
    failed_step = payload.get("failed_step") if isinstance(payload.get("failed_step"), dict) else {}
    detail = {
        "schema_version": 1,
        "source": str(payload.get("source") or ("gui_action" if action else "unknown")),
        "action": action,
        "status": str(payload.get("status") or "error"),
        "title": str(payload.get("title") or f"Action Failed: {action}"),
        "message": str(error.get("message") or payload.get("message") or "Unknown error"),
        "error_type": str(error.get("type") or payload.get("error_type") or "Error"),
        "recovery_hint": str(payload.get("recovery_hint") or ""),
        "failed_step": failed_step,
        "preflight": {
            "ok": preflight.get("ok"),
            "missing_required_capabilities": preflight.get("missing_required_capabilities") if isinstance(preflight.get("missing_required_capabilities"), list) else [],
            "unavailable_used_capabilities": preflight.get("unavailable_used_capabilities") if isinstance(preflight.get("unavailable_used_capabilities"), list) else [],
        } if preflight else {},
        "quality": {
            "risk_level": quality.get("risk_level"),
            "warning_count": quality.get("warning_count"),
            "strict_policy_gate": quality.get("strict_policy_gate") if isinstance(quality.get("strict_policy_gate"), dict) else {},
            "secret_scan": quality.get("secret_scan") if isinstance(quality.get("secret_scan"), dict) else {},
        } if quality else {},
        "artifacts": {
            "failure_json": failure_report.get("json_report"),
            "failure_markdown": failure_report.get("markdown_report"),
            "run_report": payload.get("run_report"),
            "quality_report": payload.get("quality_report"),
        },
    }
    return detail


def gui_error_detail_to_markdown(detail: dict[str, Any]) -> str:
    lines = [
        f"# {detail.get('title') or 'Error Detail'}",
        "",
        f"- Source: `{detail.get('source') or 'unknown'}`",
        f"- Action: `{detail.get('action') or 'unknown'}`",
        f"- Status: `{detail.get('status') or 'error'}`",
        f"- Error type: `{detail.get('error_type') or 'Error'}`",
        f"- Message: {detail.get('message') or 'Unknown error'}",
    ]
    if detail.get("recovery_hint"):
        lines.append(f"- Recovery: {detail.get('recovery_hint')}")
    failed_step = detail.get("failed_step") if isinstance(detail.get("failed_step"), dict) else {}
    if failed_step:
        lines.extend(["", "## Failed Step", ""])
        for key in ("id", "action", "status", "message"):
            if failed_step.get(key) is not None:
                lines.append(f"- {key}: `{failed_step.get(key)}`")
    preflight = detail.get("preflight") if isinstance(detail.get("preflight"), dict) else {}
    if preflight:
        lines.extend(["", "## Preflight", "", f"- OK: `{preflight.get('ok')}`"])
        missing = preflight.get("missing_required_capabilities") if isinstance(preflight.get("missing_required_capabilities"), list) else []
        unavailable = preflight.get("unavailable_used_capabilities") if isinstance(preflight.get("unavailable_used_capabilities"), list) else []
        if missing:
            lines.append(f"- Missing required: `{', '.join(str(item) for item in missing)}`")
        if unavailable:
            lines.append(f"- Unavailable used: `{', '.join(str(item) for item in unavailable)}`")
    quality = detail.get("quality") if isinstance(detail.get("quality"), dict) else {}
    if quality:
        strict = quality.get("strict_policy_gate") if isinstance(quality.get("strict_policy_gate"), dict) else {}
        secret_scan = quality.get("secret_scan") if isinstance(quality.get("secret_scan"), dict) else {}
        lines.extend(["", "## Quality", "", f"- Risk level: `{quality.get('risk_level') or 'unknown'}`", f"- Warnings: {quality.get('warning_count') or 0}"])
        if strict:
            lines.append(f"- Strict failed: `{bool(strict.get('failed'))}`")
        if secret_scan:
            lines.append(f"- Secret findings: {int(secret_scan.get('finding_count') or 0)}")
    artifacts = detail.get("artifacts") if isinstance(detail.get("artifacts"), dict) else {}
    artifact_items = [(key, value) for key, value in artifacts.items() if value]
    if artifact_items:
        lines.extend(["", "## Artifacts", ""])
        labels = {
            "failure_json": "Failure report JSON",
            "failure_markdown": "Failure report",
            "run_report": "Run report",
            "quality_report": "Quality report",
        }
        for key, value in artifact_items:
            lines.append(f"- {labels.get(str(key), str(key))}: `{value}`")
    lines.append("")
    return "\n".join(lines)


def recording_result_to_markdown(recording: dict[str, Any]) -> str:
    return recorded_result_to_markdown(recording)


def risk_policy_plan_to_markdown(plan: dict[str, Any]) -> str:
    before = plan.get("validation_before") if isinstance(plan.get("validation_before"), dict) else {}
    after = plan.get("validation_after") if isinstance(plan.get("validation_after"), dict) else {}
    changed_paths = plan.get("changed_paths") if isinstance(plan.get("changed_paths"), list) else []
    lines = [
        "# Risk Policy Patch",
        "",
        f"- Mode: `{plan.get('mode') or 'unknown'}`",
        f"- Applied: `{bool(plan.get('applied'))}`",
        f"- Changed: `{bool(plan.get('changed'))}`",
        f"- Manifest: `{plan.get('manifest_path') or ''}`",
        "",
        "## Validation",
        "",
        "| phase | status | errors | warnings |",
        "| --- | --- | --- | --- |",
        f"| before | `{before.get('status') or 'unknown'}` | {int(before.get('error_count') or 0)} | {int(before.get('warning_count') or 0)} |",
        f"| after | `{after.get('status') or 'unknown'}` | {int(after.get('error_count') or 0)} | {int(after.get('warning_count') or 0)} |",
        "",
        "## Changed Paths",
        "",
    ]
    if changed_paths:
        lines.extend(f"- `{path}`" for path in changed_paths)
    else:
        lines.append("none")
    lines.append("")
    return "\n".join(lines)


def strict_policy_failed_markdown(dashboard: dict[str, Any]) -> str:
    quality = dashboard.get("quality_gates") if isinstance(dashboard.get("quality_gates"), dict) else {}
    markdown = str(quality.get("strict_policy_failed_markdown") or "").strip()
    if markdown:
        return markdown + "\n"
    return "# Quality Gate Index\n\nNo strict policy failure reports.\n"


def safe_execute_gui_action(
    workspace: Workspace,
    plan: dict[str, Any],
    *,
    selected_run_id: str | None = None,
    selected_batch_report_id: str | None = None,
) -> dict[str, Any]:
    try:
        result = execute_gui_action(workspace, plan)
    except Exception as exc:
        action = str(plan.get("action") or "")
        result = {
            "action": action,
            "status": "error",
            "error": {
                "type": exc.__class__.__name__,
                "message": str(exc),
            },
            "failure_report": getattr(exc, "failure_report", None),
            "message": f"{action or 'GUI action'} failed: {exc}",
            "recovery_hint": gui_action_recovery_hint(action, exc),
            "refreshed_model": refreshed_console_model(
                workspace,
                selected_run_id=selected_run_id,
                selected_batch_report_id=selected_batch_report_id,
            ),
        }
        event = write_gui_action_event(workspace, plan, result)
        result["action_event"] = event
        refreshed = result.get("refreshed_model") if isinstance(result.get("refreshed_model"), dict) else None
        if refreshed is not None:
            refresh_gui_action_history_model_fields(refreshed, workspace)
        return result
    event = write_gui_action_event(workspace, plan, result)
    result["action_event"] = event
    refreshed = result.get("refreshed_model") if isinstance(result.get("refreshed_model"), dict) else None
    if refreshed is not None:
        refresh_gui_action_history_model_fields(refreshed, workspace)
    return result


def refresh_gui_action_history_model_fields(model: dict[str, Any], workspace: Workspace) -> None:
    events = list_gui_action_events(workspace)
    index = build_gui_action_history_index(workspace)
    risk = build_gui_action_history_risk_summary(
        workspace,
        config=load_workspace_gui_action_history_risk_config(workspace),
    )
    options = gui_action_event_options(events)
    risk_options = gui_action_risk_event_options(risk, options)
    model["gui_action_events"] = events
    model["gui_action_history_index"] = index
    model["gui_action_history_risk"] = risk
    model["gui_action_history_risk_markdown"] = gui_action_history_risk_to_markdown(risk)
    model["gui_action_event_options"] = options
    model["selected_gui_action_event"] = options[0] if options else None
    model["selected_gui_action_event_markdown"] = gui_action_event_markdown(options[0]) if options else ""
    model["gui_action_risk_event_options"] = risk_options
    model["selected_gui_action_risk_event"] = risk_options[0] if risk_options else None
    dashboard = model.get("dashboard") if isinstance(model.get("dashboard"), dict) else {}
    readiness = model.get("external_sample_readiness") if isinstance(model.get("external_sample_readiness"), dict) else None
    if dashboard:
        model["summary_cards"] = summary_cards(dashboard, readiness, risk)


def gui_action_audit_path(workspace: Workspace) -> Path:
    root = workspace.root / "gui"
    root.mkdir(parents=True, exist_ok=True)
    return root / "actions.jsonl"


def write_gui_action_event(workspace: Workspace, plan: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    event = {
        "schema_version": 1,
        "event_id": f"gui-action-{uuid4().hex[:12]}",
        "created_at": time(),
        "workspace_root": str(workspace.root),
        "action": str(result.get("action") or plan.get("action") or ""),
        "status": str(result.get("status") or "unknown"),
        "message": str(result.get("message") or ""),
        "plan": compact_gui_action_plan(plan),
        "result": compact_gui_action_result(result),
    }
    path = gui_action_audit_path(workspace)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def list_gui_action_events(
    workspace: Workspace,
    *,
    limit: int = 10,
    action: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    path = gui_action_audit_path(workspace)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if action is not None and event.get("action") != action:
            continue
        if status is not None and event.get("status") != status:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events[-limit:][::-1]


def gui_action_event_options(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options = []
    for event in events:
        if not isinstance(event, dict):
            continue
        option = {
            "event_id": str(event.get("event_id") or ""),
            "action": str(event.get("action") or "unknown"),
            "status": str(event.get("status") or "unknown"),
            "created_at": event.get("created_at"),
            "message": str(event.get("message") or ""),
            "event": event,
        }
        option["label"] = gui_action_event_label(option)
        options.append(option)
    return options


def gui_action_event_label(option: dict[str, Any]) -> str:
    event_id = str(option.get("event_id") or "")
    short_id = event_id[-12:] if len(event_id) > 12 else event_id
    return f"{str(option.get('status') or 'unknown').upper()}  {option.get('action') or 'unknown'}  {short_id}".strip()


def gui_action_risk_event_options(risk_summary: dict[str, Any], action_event_options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recent_errors = risk_summary.get("recent_errors") if isinstance(risk_summary.get("recent_errors"), list) else []
    risky_event_ids = [
        str(item.get("event_id") or "")
        for item in recent_errors
        if isinstance(item, dict) and item.get("event_id")
    ]
    if not risky_event_ids:
        return []
    by_event_id = {
        str(option.get("event_id") or ""): option
        for option in action_event_options
        if isinstance(option, dict) and option.get("event_id")
    }
    options = []
    for event_id in risky_event_ids:
        option = by_event_id.get(event_id)
        if not option:
            continue
        risk_option = dict(option)
        risk_option["risk_label"] = f"RISK  {risk_option.get('action') or 'unknown'}  {event_id[-12:]}"
        options.append(risk_option)
    return options


def gui_action_event_markdown(option: dict[str, Any] | None) -> str:
    if not option:
        return ""
    event = option.get("event") if isinstance(option.get("event"), dict) else option
    lines = [
        "# GUI Action Event",
        "",
        f"- Event ID: `{event.get('event_id') or 'unknown'}`",
        f"- Action: `{event.get('action') or 'unknown'}`",
        f"- Status: `{event.get('status') or 'unknown'}`",
        f"- Message: {event.get('message') or 'none'}",
        f"- Created at: {event.get('created_at') or 'unknown'}",
    ]
    result = event.get("result") if isinstance(event.get("result"), dict) else {}
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    policy_plan = result.get("policy_plan") if isinstance(result.get("policy_plan"), dict) else {}
    planner_draft = result.get("planner_draft") if isinstance(result.get("planner_draft"), dict) else {}
    if policy_plan:
        lines.extend(
            [
                "",
                "## Risk Policy Patch",
                "",
                f"- Applied: `{bool(policy_plan.get('applied'))}`",
                f"- Changed: `{bool(policy_plan.get('changed'))}`",
                f"- Mode: `{policy_plan.get('mode') or 'unknown'}`",
                f"- Changed paths: {', '.join(f'`{path}`' for path in policy_plan.get('changed_paths', [])) or 'none'}",
            ]
        )
        after = policy_plan.get("validation_after") if isinstance(policy_plan.get("validation_after"), dict) else {}
        lines.append(f"- Validation after: `{after.get('status') or 'unknown'}` ({after.get('error_count', 0)} errors, {after.get('warning_count', 0)} warnings)")
    if planner_draft:
        save = planner_draft.get("save") if isinstance(planner_draft.get("save"), dict) else {}
        check = planner_draft.get("check") if isinstance(planner_draft.get("check"), dict) else {}
        preflight = planner_draft.get("preflight") if isinstance(planner_draft.get("preflight"), dict) else {}
        lines.extend(
            [
                "",
                "## Planner Draft",
                "",
                f"- Draft status: `{planner_draft.get('status') or 'unknown'}`",
                f"- Save status: `{save.get('status') or 'unknown'}`",
                f"- Save path: `{save.get('path') or 'none'}`",
                f"- Reason: `{save.get('reason') or 'none'}`",
                f"- Overwrite: `{bool(save.get('overwrite'))}`",
                f"- Target existed: `{bool(save.get('target_exists'))}`",
                f"- Check valid: `{bool(check.get('valid'))}`",
            ]
        )
        if preflight:
            lines.append(f"- Preflight OK: `{bool(preflight.get('ok'))}`")
            lines.append(f"- Preflight warnings: {int(preflight.get('warning_count') or 0)}")
    if error:
        lines.extend(
            [
                "",
                "## Error",
                "",
                f"- Type: `{error.get('type') or 'Error'}`",
                f"- Message: {error.get('message') or 'unknown'}",
            ]
        )
        hint = result.get("recovery_hint")
        if hint:
            lines.append(f"- Recovery: {hint}")
    lines.extend(["", "## Plan", "", "```json", json.dumps(event.get("plan") or {}, ensure_ascii=False, indent=2), "```"])
    lines.extend(["", "## Result", "", "```json", json.dumps(result, ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines)


def build_gui_action_history_report(
    workspace: Workspace,
    *,
    limit: int = 20,
    action: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    events = list_gui_action_events(workspace, limit=limit, action=action, status=status)
    return {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "filters": {"action": action, "status": status, "limit": limit},
        "total_events": len(events),
        "success_events": sum(1 for event in events if event.get("status") == "success"),
        "error_events": sum(1 for event in events if event.get("status") == "error"),
        "events": events,
        "options": gui_action_event_options(events),
    }


def build_gui_action_history_index(workspace: Workspace, *, limit: int = 100, recent_error_limit: int = 5) -> dict[str, Any]:
    events = list_gui_action_events(workspace, limit=limit)
    status_counts: dict[str, int] = {}
    action_counts: dict[str, dict[str, Any]] = {}
    recent_errors: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        action = str(event.get("action") or "unknown")
        status = str(event.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        entry = action_counts.setdefault(
            action,
            {
                "action": action,
                "total": 0,
                "success": 0,
                "error": 0,
                "unknown": 0,
                "error_rate": 0.0,
            },
        )
        entry["total"] += 1
        if status in {"success", "error"}:
            entry[status] += 1
        else:
            entry["unknown"] += 1
        if status == "error" and len(recent_errors) < recent_error_limit:
            recent_errors.append(compact_gui_action_event_summary(event))
    for entry in action_counts.values():
        total = int(entry.get("total") or 0)
        errors = int(entry.get("error") or 0)
        entry["error_rate"] = errors / total if total else 0.0
    actions = sorted(
        action_counts.values(),
        key=lambda item: (-int(item.get("total") or 0), str(item.get("action") or "")),
    )
    failed_actions = sorted(
        [item for item in actions if int(item.get("error") or 0) > 0],
        key=lambda item: (-int(item.get("error") or 0), -int(item.get("total") or 0), str(item.get("action") or "")),
    )
    total_events = len(events)
    error_events = status_counts.get("error", 0)
    return {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "generated_at": time(),
        "filters": {"limit": limit, "recent_error_limit": recent_error_limit},
        "total_events": total_events,
        "success_events": status_counts.get("success", 0),
        "error_events": error_events,
        "error_rate": error_events / total_events if total_events else 0.0,
        "status_counts": status_counts,
        "actions": actions,
        "failed_actions": failed_actions,
        "recent_errors": recent_errors,
        "latest_event": compact_gui_action_event_summary(events[0]) if events else None,
    }


def build_gui_action_history_risk_summary(
    workspace: Workspace,
    *,
    limit: int | None = None,
    error_rate_threshold: float | None = None,
    failed_action_limit: int | None = None,
    config: dict[str, Any] | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    policy = gui_action_history_risk_policy(config, profile=profile)
    if limit is not None:
        policy["history_limit"] = max(1, int(limit))
    if error_rate_threshold is not None:
        policy["error_rate_threshold"] = max(0.0, min(1.0, float(error_rate_threshold)))
    if failed_action_limit is not None:
        policy["failed_action_limit"] = max(0, int(failed_action_limit))
    limit = int(policy["history_limit"])
    error_rate_threshold = float(policy["error_rate_threshold"])
    failed_action_limit = int(policy["failed_action_limit"])
    events = list_gui_action_events(workspace, limit=limit)
    index = build_gui_action_history_index(workspace, limit=limit, recent_error_limit=failed_action_limit)
    warnings: list[dict[str, Any]] = []
    total_events = int(index.get("total_events") or 0)
    error_rate = float(index.get("error_rate") or 0.0)
    if total_events > 0 and error_rate >= error_rate_threshold:
        warnings.append(
            {
                "level": "warning",
                "code": "gui_action_error_rate",
                "message": f"Recent GUI action error rate is {error_rate:.2%}.",
                "error_rate": error_rate,
                "threshold": error_rate_threshold,
            }
        )
    failed_actions = index.get("failed_actions") if isinstance(index.get("failed_actions"), list) else []
    for action in failed_actions[:failed_action_limit]:
        if not isinstance(action, dict):
            continue
        warnings.append(
            {
                "level": "warning",
                "code": "gui_action_failed_action",
                "message": f"GUI action has recent failures: {action.get('action') or 'unknown'}.",
                "action": action.get("action"),
                "error_events": int(action.get("error") or 0),
                "total_events": int(action.get("total") or 0),
                "error_rate": float(action.get("error_rate") or 0.0),
            }
        )
    recent_errors = index.get("recent_errors") if isinstance(index.get("recent_errors"), list) else []
    remediation_items = gui_action_history_remediation_items(recent_errors)
    trend = gui_action_history_risk_trend(events, window_size=max(1, failed_action_limit))
    return {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "source": "gui_action_history_index",
        "filters": {
            "limit": limit,
            "error_rate_threshold": error_rate_threshold,
            "failed_action_limit": failed_action_limit,
            "profile": profile,
        },
        "policy": policy,
        "risk_level": "warning" if warnings else "ok",
        "warning_count": len(warnings),
        "total_events": total_events,
        "success_events": int(index.get("success_events") or 0),
        "error_events": int(index.get("error_events") or 0),
        "error_rate": error_rate,
        "failed_actions": failed_actions[:failed_action_limit],
        "recent_errors": recent_errors,
        "remediation_items": remediation_items,
        "trend": trend,
        "warnings": warnings,
    }


def gui_action_history_remediation_items(recent_errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for error in recent_errors:
        if not isinstance(error, dict):
            continue
        action = str(error.get("action") or "unknown")
        error_type = str(error.get("error_type") or "Error")
        hint = str(error.get("recovery_hint") or "").strip()
        if not hint:
            continue
        key = (action, error_type, hint)
        item = grouped.setdefault(
            key,
            {
                "action": action,
                "error_type": error_type,
                "recovery_hint": hint,
                "count": 0,
                "event_ids": [],
                "latest_event_id": "",
                "latest_error_message": "",
            },
        )
        item["count"] += 1
        event_id = str(error.get("event_id") or "")
        if event_id:
            item["event_ids"].append(event_id)
            if not item["latest_event_id"]:
                item["latest_event_id"] = event_id
        if not item["latest_error_message"]:
            item["latest_error_message"] = str(error.get("error_message") or error.get("message") or "")
    return sorted(
        grouped.values(),
        key=lambda item: (-int(item.get("count") or 0), str(item.get("action") or ""), str(item.get("error_type") or "")),
    )


def gui_action_history_risk_trend(events: list[dict[str, Any]], *, window_size: int = 5) -> dict[str, Any]:
    window_size = max(1, int(window_size))
    newest = [event for event in events[:window_size] if isinstance(event, dict)]
    older = [event for event in events[window_size : window_size * 2] if isinstance(event, dict)]
    newest_summary = gui_action_history_window_summary(newest)
    older_summary = gui_action_history_window_summary(older)
    error_rate_delta = float(newest_summary["error_rate"]) - float(older_summary["error_rate"])
    remediation_count_delta = int(newest_summary["remediation_count"]) - int(older_summary["remediation_count"])
    direction = "stable"
    if error_rate_delta < 0 and remediation_count_delta <= 0:
        direction = "improving"
    elif error_rate_delta > 0 and remediation_count_delta >= 0:
        direction = "worsening"
    elif (error_rate_delta < 0 and remediation_count_delta > 0) or (error_rate_delta > 0 and remediation_count_delta < 0):
        direction = "mixed"
    if older_summary["total_events"] == 0:
        direction = "insufficient_history"
    return {
        "schema_version": 1,
        "window_size": window_size,
        "direction": direction,
        "error_rate_delta": error_rate_delta,
        "remediation_count_delta": remediation_count_delta,
        "newest": newest_summary,
        "older": older_summary,
    }


def gui_action_history_window_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    total_events = len(events)
    error_events = sum(1 for event in events if event.get("status") == "error")
    recent_errors = [compact_gui_action_event_summary(event) for event in events if event.get("status") == "error"]
    remediation_count = sum(int(item.get("count") or 0) for item in gui_action_history_remediation_items(recent_errors))
    return {
        "total_events": total_events,
        "error_events": error_events,
        "success_events": sum(1 for event in events if event.get("status") == "success"),
        "error_rate": error_events / total_events if total_events else 0.0,
        "remediation_count": remediation_count,
    }


def gui_action_history_risk_to_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# GUI Action Risk",
        "",
        f"- Risk level: `{summary.get('risk_level') or 'ok'}`",
        f"- Warnings: {summary.get('warning_count', 0)}",
        f"- Events: {summary.get('total_events', 0)}",
        f"- Errors: {summary.get('error_events', 0)}",
        f"- Error rate: {float(summary.get('error_rate') or 0.0):.2%}",
        "",
        "## Trend",
        "",
    ]
    trend = summary.get("trend") if isinstance(summary.get("trend"), dict) else {}
    if trend:
        newest = trend.get("newest") if isinstance(trend.get("newest"), dict) else {}
        older = trend.get("older") if isinstance(trend.get("older"), dict) else {}
        lines.extend(
            [
                f"- Direction: `{trend.get('direction') or 'unknown'}`",
                f"- Window size: {trend.get('window_size', 0)}",
                f"- Newest error rate: {float(newest.get('error_rate') or 0.0):.2%}",
                f"- Older error rate: {float(older.get('error_rate') or 0.0):.2%}",
                f"- Error rate delta: {float(trend.get('error_rate_delta') or 0.0):+.2%}",
                f"- Remediation count delta: {int(trend.get('remediation_count_delta') or 0):+d}",
                "",
            ]
        )
    else:
        lines.extend(["- No trend data.", ""])
    lines.extend(
        [
        "## Remediation Checklist",
        "",
        ]
    )
    remediation_items = summary.get("remediation_items") if isinstance(summary.get("remediation_items"), list) else []
    if remediation_items:
        for item in remediation_items:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- [{item.get('count', 0)}x] `{item.get('action') or 'unknown'}` "
                f"`{item.get('error_type') or 'Error'}`: {item.get('recovery_hint') or ''}"
            )
    else:
        lines.append("- No recovery actions needed.")
    lines.extend(
        [
            "",
            "## Warnings",
            "",
        ]
    )
    warnings = summary.get("warnings") if isinstance(summary.get("warnings"), list) else []
    if warnings:
        for warning in warnings:
            if not isinstance(warning, dict):
                continue
            lines.append(f"- `{warning.get('code') or 'warning'}`: {warning.get('message') or ''}")
    else:
        lines.append("- No GUI action risk warnings.")
    lines.extend(["", "## Failed Actions", "", "| action | errors | total | error_rate |", "| --- | ---: | ---: | ---: |"])
    failed_actions = summary.get("failed_actions") if isinstance(summary.get("failed_actions"), list) else []
    for action in failed_actions:
        if not isinstance(action, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_table_cell(action.get("action")),
                    markdown_table_cell(action.get("error")),
                    markdown_table_cell(action.get("total")),
                    f"{float(action.get('error_rate') or 0.0):.2%}",
                ]
            )
            + " |"
        )
    if not failed_actions:
        lines.append("| none | 0 | 0 | 0.00% |")
    lines.extend(["", "## Recent Errors", "", "| action | error | recovery |", "| --- | --- | --- |"])
    recent_errors = summary.get("recent_errors") if isinstance(summary.get("recent_errors"), list) else []
    for error in recent_errors:
        if not isinstance(error, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_table_cell(error.get("action")),
                    markdown_table_cell(error.get("error_message") or error.get("message")),
                    markdown_table_cell(error.get("recovery_hint")),
                ]
            )
            + " |"
        )
    if not recent_errors:
        lines.append("| none | none | none |")
    return "\n".join(lines)


def gui_action_history_risk_policy(config: dict[str, Any] | None = None, *, profile: str | None = None) -> dict[str, Any]:
    policy = dict(DEFAULT_GUI_ACTION_HISTORY_RISK_POLICY)
    if isinstance(config, dict):
        apply_gui_action_history_risk_policy(policy, config)
        profiles = config.get("profiles") if isinstance(config.get("profiles"), dict) else {}
        profile_config = profiles.get(profile) if profile else None
        if isinstance(profile_config, dict):
            apply_gui_action_history_risk_policy(policy, profile_config)
    return policy


def apply_gui_action_history_risk_policy(policy: dict[str, Any], config: dict[str, Any]) -> None:
    if "history_limit" in config:
        policy["history_limit"] = parse_int_policy_value(config["history_limit"], policy["history_limit"], minimum=1)
    if "limit" in config:
        policy["history_limit"] = parse_int_policy_value(config["limit"], policy["history_limit"], minimum=1)
    if "error_rate_threshold" in config:
        policy["error_rate_threshold"] = parse_float_policy_value(
            config["error_rate_threshold"],
            policy["error_rate_threshold"],
            minimum=0.0,
            maximum=1.0,
        )
    if "failed_action_limit" in config:
        policy["failed_action_limit"] = parse_int_policy_value(config["failed_action_limit"], policy["failed_action_limit"], minimum=0)


def parse_int_policy_value(value: Any, default: int, *, minimum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(minimum, parsed)


def parse_float_policy_value(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return max(minimum, min(maximum, parsed))


def compact_gui_action_event_summary(event: dict[str, Any]) -> dict[str, Any]:
    result = event.get("result") if isinstance(event.get("result"), dict) else {}
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    return {
        "event_id": str(event.get("event_id") or ""),
        "created_at": event.get("created_at"),
        "action": str(event.get("action") or "unknown"),
        "status": str(event.get("status") or "unknown"),
        "message": str(event.get("message") or ""),
        "error_type": str(error.get("type") or ""),
        "error_message": str(error.get("message") or ""),
        "recovery_hint": str(result.get("recovery_hint") or ""),
    }


def gui_action_history_index_to_markdown(index: dict[str, Any]) -> str:
    lines = [
        "# GUI Action History Index",
        "",
        f"- Workspace: `{index.get('workspace_root') or ''}`",
        f"- Events: {index.get('total_events', 0)}",
        f"- Success: {index.get('success_events', 0)}",
        f"- Errors: {index.get('error_events', 0)}",
        f"- Error rate: {float(index.get('error_rate') or 0.0):.2%}",
        "",
        "## Actions",
        "",
        "| action | total | success | error | error_rate |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    actions = index.get("actions") if isinstance(index.get("actions"), list) else []
    for action in actions:
        if not isinstance(action, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_table_cell(action.get("action")),
                    markdown_table_cell(action.get("total")),
                    markdown_table_cell(action.get("success")),
                    markdown_table_cell(action.get("error")),
                    f"{float(action.get('error_rate') or 0.0):.2%}",
                ]
            )
            + " |"
        )
    if not actions:
        lines.append("| none | 0 | 0 | 0 | 0.00% |")
    lines.extend(["", "## Recent Errors", "", "| action | event_id | error | recovery |", "| --- | --- | --- | --- |"])
    recent_errors = index.get("recent_errors") if isinstance(index.get("recent_errors"), list) else []
    for event in recent_errors:
        if not isinstance(event, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_table_cell(event.get("action")),
                    markdown_table_cell(event.get("event_id")),
                    markdown_table_cell(event.get("error_message") or event.get("message")),
                    markdown_table_cell(event.get("recovery_hint")),
                ]
            )
            + " |"
        )
    if not recent_errors:
        lines.append("| none | none | none | none |")
    return "\n".join(lines)


def gui_action_history_report_to_markdown(report: dict[str, Any]) -> str:
    filters = report.get("filters") if isinstance(report.get("filters"), dict) else {}
    lines = [
        "# GUI Action History",
        "",
        f"- Workspace: `{report.get('workspace_root') or ''}`",
        f"- Events: {report.get('total_events', 0)}",
        f"- Success: {report.get('success_events', 0)}",
        f"- Errors: {report.get('error_events', 0)}",
        f"- Filter action: `{filters.get('action') or 'all'}`",
        f"- Filter status: `{filters.get('status') or 'all'}`",
        "",
        "| status | action | event_id | message |",
        "| --- | --- | --- | --- |",
    ]
    events = report.get("events") if isinstance(report.get("events"), list) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_table_cell(event.get("status")),
                    markdown_table_cell(event.get("action")),
                    markdown_table_cell(event.get("event_id")),
                    markdown_table_cell(event.get("message")),
                ]
            )
            + " |"
        )
    if not events:
        lines.append("| none | none | none | no GUI action events |")
    return "\n".join(lines)


def markdown_table_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def compact_gui_action_plan(plan: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "action",
        "workflow",
        "task_id",
        "path",
        "auth_name",
        "sample_id",
        "batch_report_id",
        "run_profile",
        "dry_run",
        "requires_confirmation",
        "inputs_file",
        "overwrite",
        "save_as",
        "preferred",
        "model",
    }
    return {key: json_safe(value) for key, value in plan.items() if key in allowed and value is not None}


def compact_gui_action_result(result: dict[str, Any]) -> dict[str, Any]:
    skipped = {"refreshed_model", "action_event"}
    compact: dict[str, Any] = {}
    for key, value in result.items():
        if key in skipped:
            continue
        if key == "result" and isinstance(value, dict):
            compact[key] = compact_nested_result(value)
            continue
        if key == "policy_plan" and isinstance(value, dict):
            compact[key] = compact_policy_plan_result(value)
            continue
        if key == "planner_draft" and isinstance(value, dict):
            compact[key] = compact_planner_draft_result(value)
            continue
        if key == "input_template" and isinstance(value, dict):
            compact[key] = compact_input_template_result(value)
            continue
        compact[key] = json_safe(value)
    return compact


def compact_input_template_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": json_safe(value.get("path")),
        "keys": json_safe(value.get("keys") if isinstance(value.get("keys"), list) else []),
        "byte_count": int(value.get("byte_count") or len(str(value.get("text") or "").encode("utf-8"))),
    }


def compact_policy_plan_result(value: dict[str, Any]) -> dict[str, Any]:
    before = value.get("validation_before") if isinstance(value.get("validation_before"), dict) else {}
    after = value.get("validation_after") if isinstance(value.get("validation_after"), dict) else {}
    return {
        "schema_version": value.get("schema_version"),
        "mode": value.get("mode"),
        "applied": bool(value.get("applied")),
        "changed": bool(value.get("changed")),
        "changed_paths": json_safe(value.get("changed_paths") if isinstance(value.get("changed_paths"), list) else []),
        "validation_before": {
            "status": before.get("status"),
            "error_count": int(before.get("error_count") or 0),
            "warning_count": int(before.get("warning_count") or 0),
        },
        "validation_after": {
            "status": after.get("status"),
            "error_count": int(after.get("error_count") or 0),
            "warning_count": int(after.get("warning_count") or 0),
        },
    }


def compact_planner_draft_result(value: dict[str, Any]) -> dict[str, Any]:
    save = value.get("save") if isinstance(value.get("save"), dict) else {}
    check = value.get("check") if isinstance(value.get("check"), dict) else {}
    issues = check.get("issues") if isinstance(check.get("issues"), list) else []
    workflow = value.get("workflow") if isinstance(value.get("workflow"), dict) else {}
    suggestions = value.get("recovery_suggestions") if isinstance(value.get("recovery_suggestions"), list) else []
    preflight = value.get("preflight") if isinstance(value.get("preflight"), dict) else {}
    return {
        "schema_version": value.get("schema_version"),
        "status": value.get("status"),
        "executed": bool(value.get("executed")),
        "provider": value.get("provider"),
        "selected_provider": value.get("selected_provider"),
        "model": value.get("model"),
        "parse_status": value.get("parse_status"),
        "workflow": {
            "name": workflow.get("name"),
            "step_count": len(workflow.get("steps") if isinstance(workflow.get("steps"), list) else []),
        },
        "check": {
            "valid": bool(check.get("valid")),
            "allowed_to_execute": bool(check.get("allowed_to_execute")),
            "dry_run_required": bool(check.get("dry_run_required")),
            "issue_count": len(issues),
        },
        "recovery_suggestions": json_safe(suggestions[:5]),
        "preflight": {
            key: json_safe(preflight.get(key))
            for key in (
                "ok",
                "workflow_name",
                "strict",
                "missing_required_count",
                "unavailable_used_count",
                "warning_count",
            )
            if key in preflight
        },
        "save": {
            key: json_safe(save.get(key))
            for key in (
                "requested",
                "status",
                "path",
                "reason",
                "target_exists",
                "overwrite",
            )
            if key in save
        },
    }


def compact_nested_result(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version",
        "run_id",
        "task_id",
        "report_id",
        "submitted_count",
        "skipped_count",
        "candidate_count",
        "json_report",
        "markdown_report",
        "index",
    }
    compact = {key: json_safe(item) for key, item in value.items() if key in allowed}
    if "summary" in value and isinstance(value["summary"], dict):
        compact["summary"] = {
            key: json_safe(value["summary"].get(key))
            for key in ("total_samples", "ready_samples", "blocked_samples", "with_reports", "queued_samples")
            if key in value["summary"]
        }
    if "task" in value and isinstance(value["task"], dict):
        compact["task"] = {
            key: json_safe(value["task"].get(key))
            for key in ("task_id", "workflow", "status", "last_run_id", "last_error")
            if key in value["task"]
        }
    if "failure_report" in value and isinstance(value["failure_report"], dict):
        report = value["failure_report"]
        compact["failure_report"] = {
            key: json_safe(report.get(key))
            for key in ("report_id", "status", "json_report", "markdown_report", "recovery_hint")
            if key in report
        }
    return compact or json_safe(value)


def json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except TypeError:
        return str(value)


def gui_action_recovery_hint(action: str, exc: Exception) -> str:
    message = str(exc)
    if "outside allowed GUI roots" in message:
        return "Select an artifact or auth-state path from the workspace-controlled lists."
    if action == "record_browser_workflow":
        if isinstance(exc, FileExistsError) or "already exists" in message:
            return "Choose a different workflow name or enable overwrite in the recording dialog."
        if "Playwright is not installed" in message:
            return "Install the web extras with `pip install -e .[web]`, then retry recording."
        if "playwright install" in message.lower() or "Executable doesn't exist" in message:
            return "Install browser binaries with `python -m playwright install chromium`, then retry recording."
        return "Check the URL, browser availability, and workflow save name, then retry recording."
    if action in {"cancel_queue_task", "retry_queue_task", "queue_run_next"}:
        return "Refresh the queue state and select a task whose status allows this action."
    if action in {"plan_external_sample_batch_reruns", "submit_external_sample_batch_reruns"}:
        return "Select an existing batch report, then retry the batch action."
    if action in {"plan_external_sample_run", "submit_external_sample_batch", "submit_external_sample_reruns"}:
        return "Refresh readiness and resolve listed blockers before retrying."
    return "Refresh the console model, review the selected item, and retry the action."


def list_report_options(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    reports = dashboard.get("reports") if isinstance(dashboard.get("reports"), dict) else {}
    recent = reports.get("recent") if isinstance(reports.get("recent"), list) else []
    options = []
    for item in recent:
        if not isinstance(item, dict):
            continue
        run_id = item.get("run_id")
        if not run_id:
            continue
        options.append(
            {
                "run_id": str(run_id),
                "workflow_name": str(item.get("workflow_name") or ""),
                "status": str(item.get("status") or "unknown"),
                "failed_step": item.get("failed_step"),
                "label": report_option_label(item),
            }
        )
    return options


def summary_cards(
    dashboard: dict[str, Any],
    readiness: dict[str, Any] | None = None,
    action_history_risk: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    workspace = dashboard["workspace"]
    health = dashboard["health"]
    reports = dashboard["reports"]
    quality = dashboard["quality_gates"]
    queue = dashboard["queue"]
    cards = [
        {
            "id": "health",
            "label": "Health",
            "value": health["status"],
            "detail": ", ".join(health["issues"]) if health["issues"] else "none",
        },
        {
            "id": "workflows",
            "label": "Workflows",
            "value": f"{workspace['valid_workflows']}/{workspace['workflow_count']}",
            "detail": "valid",
        },
        {
            "id": "reports",
            "label": "Reports",
            "value": str(reports["total"]),
            "detail": f"{reports['failed']} failed",
        },
        {
            "id": "quality",
            "label": "Quality Gates",
            "value": str(quality["total"]),
            "detail": quality_summary_detail(quality),
        },
        {
            "id": "risk_policy",
            "label": "Risk Policy",
            "value": risk_policy_value(dashboard),
            "detail": risk_policy_detail(dashboard),
        },
        {
            "id": "auto_repair",
            "label": "Auto Repair",
            "value": auto_repair_policy_value(dashboard),
            "detail": auto_repair_policy_detail(dashboard),
        },
        {
            "id": "queue",
            "label": "Queue",
            "value": str(queue["total"]),
            "detail": f"{queue['pending']} pending, {queue['running']} running",
        },
    ]
    if readiness is not None:
        cards.append(
            {
                "id": "external_samples",
                "label": "External Samples",
                "value": f"{readiness.get('ready_samples', 0)}/{readiness.get('total_samples', 0)}",
                "detail": f"{readiness.get('blocked_samples', 0)} blocked",
            }
        )
    if action_history_risk is not None:
        cards.append(
            {
                "id": "gui_action_risk",
                "label": "GUI Action Risk",
                "value": str(action_history_risk.get("risk_level") or "ok"),
                "detail": (
                    f"{action_history_risk.get('warning_count', 0)} warnings, "
                    f"{float(action_history_risk.get('error_rate') or 0.0):.0%} error rate"
                ),
            }
        )
    return cards


def quality_summary_detail(quality: dict[str, Any]) -> str:
    risk_direction = str(quality.get("latest_risk_trend_direction") or "unknown")
    risk_warnings = int(quality.get("risk_warnings") or 0)
    strict_failed = int(quality.get("strict_policy_gate_failed") or 0)
    return f"{quality['failed']} failed, risk {risk_direction}, {risk_warnings} warnings, strict {strict_failed} failed"


def risk_policy_value(dashboard: dict[str, Any]) -> str:
    check = dashboard.get("risk_policy_check") if isinstance(dashboard.get("risk_policy_check"), dict) else {}
    return str(check.get("status") or "unknown")


def risk_policy_detail(dashboard: dict[str, Any]) -> str:
    check = dashboard.get("risk_policy_check") if isinstance(dashboard.get("risk_policy_check"), dict) else {}
    errors = int(check.get("error_count") or 0)
    warnings = int(check.get("warning_count") or 0)
    return f"{errors} errors, {warnings} warnings"


def auto_repair_policy_value(dashboard: dict[str, Any]) -> str:
    policy = dashboard.get("auto_repair_policy") if isinstance(dashboard.get("auto_repair_policy"), dict) else {}
    return str(policy.get("max_risk_level") or "medium")


def auto_repair_policy_detail(dashboard: dict[str, Any]) -> str:
    policy = dashboard.get("auto_repair_policy") if isinstance(dashboard.get("auto_repair_policy"), dict) else {}
    return (
        f"min {policy.get('min_confidence', 0.75)}, "
        f"force {'on' if policy.get('allow_force', True) else 'off'}, "
        f"{policy.get('source') or 'defaults'}"
    )


def workflow_options(workspace: Workspace) -> list[dict[str, Any]]:
    return [
        {
            "name": ref.name,
            "relative_path": ref.relative_path,
            "label": f"{ref.name}  ({ref.relative_path})",
        }
        for ref in discover_workflows(workspace)
    ]


def queue_options(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    queue = dashboard.get("queue") if isinstance(dashboard.get("queue"), dict) else {}
    recent = queue.get("recent") if isinstance(queue.get("recent"), list) else []
    options = []
    for item in recent:
        if not isinstance(item, dict) or not item.get("task_id"):
            continue
        options.append(
            {
                "task_id": str(item["task_id"]),
                "workflow": str(item.get("workflow") or ""),
                "status": str(item.get("status") or "unknown"),
                "label": queue_option_label(item),
            }
        )
    return options


def action_buttons(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    queue = dashboard["queue"]
    has_pending = int(queue.get("pending") or 0) > 0
    return [
        {
            "id": "run_workflow",
            "label": "Run Dry",
            "enabled": True,
            "requires_selection": "workflow",
            "risk_level": "medium",
            "dry_run_default": True,
        },
        {
            "id": "queue_run_next",
            "label": "Run Next",
            "enabled": has_pending,
            "requires_selection": None,
            "risk_level": "medium",
            "dry_run_default": True,
        },
        {
            "id": "cancel_queue_task",
            "label": "Cancel",
            "enabled": True,
            "requires_selection": "queue_task",
            "risk_level": "low",
            "dry_run_default": None,
        },
        {
            "id": "retry_queue_task",
            "label": "Retry",
            "enabled": True,
            "requires_selection": "queue_task",
            "risk_level": "low",
            "dry_run_default": None,
        },
        {
            "id": "open_artifact",
            "label": "Open Artifact",
            "enabled": True,
            "requires_selection": "artifact",
            "risk_level": "low",
            "dry_run_default": None,
        },
        {
            "id": "inspect_auth_state",
            "label": "Inspect Auth",
            "enabled": True,
            "requires_selection": "auth_state",
            "risk_level": "low",
            "dry_run_default": None,
        },
        {
            "id": "refresh_readiness",
            "label": "Readiness",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "low",
            "dry_run_default": None,
        },
        {
            "id": "plan_external_sample_run",
            "label": "Plan External",
            "enabled": True,
            "requires_selection": "external_sample",
            "risk_level": "medium",
            "dry_run_default": True,
        },
        {
            "id": "submit_external_sample_batch",
            "label": "Queue External",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "medium",
            "dry_run_default": True,
        },
        {
            "id": "external_sample_summary",
            "label": "External Summary",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "low",
            "dry_run_default": None,
        },
        {
            "id": "plan_risk_policy_patch",
            "label": "Plan Policy",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "low",
            "dry_run_default": True,
        },
        {
            "id": "apply_risk_policy_patch",
            "label": "Apply Policy",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "medium",
            "dry_run_default": False,
        },
        {
            "id": "preview_planner_draft_save",
            "label": "Preview Draft",
            "enabled": True,
            "requires_selection": "workflow",
            "risk_level": "low",
            "dry_run_default": True,
        },
        {
            "id": "generate_planner_draft_preview",
            "label": "Generate Draft",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "medium",
            "dry_run_default": True,
        },
        {
            "id": "save_generated_planner_draft",
            "label": "Save Draft",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "medium",
            "dry_run_default": False,
        },
        {
            "id": "record_browser_workflow",
            "label": "Record Browser",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "medium",
            "dry_run_default": True,
        },
        {
            "id": "delete_auth_state",
            "label": "Delete Auth",
            "enabled": True,
            "requires_selection": "auth_state",
            "risk_level": "medium",
            "dry_run_default": False,
        },
        {
            "id": "read_input_template",
            "label": "Open Inputs",
            "enabled": True,
            "requires_selection": "input_template",
            "risk_level": "low",
            "dry_run_default": None,
        },
        {
            "id": "save_input_template",
            "label": "Save Inputs",
            "enabled": True,
            "requires_selection": "input_template",
            "risk_level": "medium",
            "dry_run_default": False,
        },
        {
            "id": "install_check",
            "label": "Install Check",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "low",
            "dry_run_default": None,
        },
        {
            "id": "release_check",
            "label": "Release Check",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "low",
            "dry_run_default": None,
        },
        {
            "id": "demo_workspace_check",
            "label": "Demo Check",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "low",
            "dry_run_default": True,
        },
        {
            "id": "mcp_smoke_check",
            "label": "MCP Smoke",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "low",
            "dry_run_default": True,
        },
        {
            "id": "show_strict_policy_failures",
            "label": "Strict Failures",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "low",
            "dry_run_default": None,
        },
        {
            "id": "external_sample_batch_report",
            "label": "Batch Report",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "low",
            "dry_run_default": None,
        },
        {
            "id": "plan_external_sample_batch_reruns",
            "label": "Plan Batch Reruns",
            "enabled": True,
            "requires_selection": "batch_report",
            "risk_level": "medium",
            "dry_run_default": True,
        },
        {
            "id": "submit_external_sample_batch_reruns",
            "label": "Queue Batch Reruns",
            "enabled": True,
            "requires_selection": "batch_report",
            "risk_level": "medium",
            "dry_run_default": True,
        },
        {
            "id": "plan_external_sample_reruns",
            "label": "Plan Reruns",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "medium",
            "dry_run_default": True,
        },
        {
            "id": "submit_external_sample_reruns",
            "label": "Queue Reruns",
            "enabled": True,
            "requires_selection": None,
            "risk_level": "medium",
            "dry_run_default": True,
        },
    ]


def build_gui_action_plan(
    action: str,
    *,
    workflow: str | None = None,
    task_id: str | None = None,
    path: str | None = None,
    auth_name: str | None = None,
    source: str | None = None,
    sample_id: str | None = None,
    batch_report_id: str | None = None,
    allow_click: bool = False,
    inputs_file: str | None = None,
    input_text: str | None = None,
    overwrite: bool = False,
    save_as: str | None = None,
    instruction: str | None = None,
    preferred: str | None = None,
    model: str | None = None,
    url: str | None = None,
    assert_text: str | None = None,
    save_auth_state: str | None = None,
    preview_run: bool = False,
    queue_run: bool = False,
) -> dict[str, Any]:
    if action not in GUI_ACTIONS:
        raise ValueError(f"Unsupported GUI action: {action}")
    if action == "run_workflow" and not workflow:
        raise ValueError("run_workflow requires a workflow.")
    if action in {"cancel_queue_task", "retry_queue_task"} and not task_id:
        raise ValueError(f"{action} requires a queue task id.")
    if action in {"open_artifact", "inspect_auth_state", "delete_auth_state"} and not path:
        raise ValueError(f"{action} requires a path.")
    if action in {"read_input_template", "save_input_template"} and not path:
        raise ValueError(f"{action} requires a path.")
    if action == "save_input_template" and input_text is None:
        raise ValueError("save_input_template requires input_text.")
    if action == "import_auth_state" and (not source or not auth_name):
        raise ValueError("import_auth_state requires source and auth_name.")
    if action == "plan_external_sample_run" and not sample_id:
        raise ValueError("plan_external_sample_run requires sample_id.")
    if action in {"plan_external_sample_batch_reruns", "submit_external_sample_batch_reruns"} and not batch_report_id:
        raise ValueError(f"{action} requires batch_report_id.")
    if action == "preview_planner_draft_save" and not workflow:
        raise ValueError("preview_planner_draft_save requires a workflow.")
    if action == "generate_planner_draft_preview" and not instruction:
        raise ValueError("generate_planner_draft_preview requires an instruction.")
    if action == "save_generated_planner_draft" and (not instruction or not save_as):
        raise ValueError("save_generated_planner_draft requires instruction and save_as.")
    if action == "record_browser_workflow" and (not url or not save_as):
        raise ValueError("record_browser_workflow requires url and save_as.")
    run_profile = "approved" if allow_click else "dry-run"
    return {
        "schema_version": 1,
        "action": action,
        "workflow": workflow,
        "task_id": task_id,
        "path": path,
        "auth_name": auth_name,
        "source": source,
        "sample_id": sample_id,
        "batch_report_id": batch_report_id,
        "inputs_file": inputs_file,
        "input_text": input_text,
        "overwrite": overwrite,
        "save_as": save_as,
        "instruction": instruction,
        "preferred": preferred,
        "model": model,
        "url": url,
        "assert_text": assert_text,
        "save_auth_state": save_auth_state,
        "preview_run": preview_run,
        "queue_run": queue_run,
        "run_profile": run_profile,
        "dry_run": not allow_click,
        "requires_confirmation": allow_click,
        "origin": "workspace-gui",
    }


def execute_gui_action(workspace: Workspace, plan: dict[str, Any]) -> dict[str, Any]:
    action = str(plan.get("action") or "")
    if action not in GUI_ACTIONS:
        raise ValueError(f"Unsupported GUI action: {action}")
    if action == "run_workflow":
        workflow = str(plan.get("workflow") or "")
        result = run_workspace_workflow(
            workspace,
            workflow,
            dry_run=bool(plan.get("dry_run", True)),
            run_profile=str(plan.get("run_profile") or "dry-run"),
            inputs=load_workspace_inputs(workspace, None, str(plan["inputs_file"])) if plan.get("inputs_file") else None,
        )
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success",
                "run_id": result.run_id,
                "message": f"Workflow completed: {result.run_id}",
            },
            workspace,
            selected_run_id=result.run_id,
        )
    if action == "queue_run_next":
        result = run_next_queue_task(workspace)
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success" if not result["ran"] or result["task"]["status"] in {"success", "pending"} else "failed",
                "result": result,
                "message": result.get("message"),
            },
            workspace,
            selected_run_id=queue_result_run_id(result),
        )
    if action == "cancel_queue_task":
        task = cancel_queue_task(workspace, str(plan.get("task_id") or ""), reason="canceled from workspace-gui")
        return attach_refreshed_console_model(
            {"action": action, "status": "success", "task_id": task.task_id, "message": "Task canceled."},
            workspace,
        )
    if action == "retry_queue_task":
        task = retry_queue_task(workspace, str(plan.get("task_id") or ""))
        return attach_refreshed_console_model(
            {"action": action, "status": "success", "task_id": task.task_id, "message": "Task requeued."},
            workspace,
        )
    if action == "refresh_readiness":
        readiness = build_external_sample_readiness(workspace)
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success",
                "readiness": readiness,
                "message": f"External samples: {readiness['ready_samples']}/{readiness['total_samples']} ready.",
            },
            workspace,
        )
    if action == "install_check":
        plan = build_install_check_plan()
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success",
                "install_check": plan,
                "preferred": install_check_plan_to_markdown(plan),
                "message": "Install check plan generated.",
            },
            workspace,
        )
    if action == "release_check":
        plan = build_release_check_plan(workspace_root=workspace.root)
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success",
                "release_check": plan,
                "preferred": release_check_plan_to_markdown(plan),
                "message": "Release check plan generated.",
            },
            workspace,
        )
    if action == "demo_workspace_check":
        result = run_demo_workspace_check(root=workspace.root, overwrite=bool(plan.get("overwrite", False)))
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": result.get("status") or "unknown",
                "demo_workspace_check": result,
                "preferred": demo_workspace_check_to_markdown(result),
                "message": f"Demo workspace check {result.get('status')}.",
            },
            workspace,
            selected_run_id=str(result.get("run_id") or "") or None,
        )
    if action == "mcp_smoke_check":
        result = run_mcp_smoke_check(
            workspace_root=workspace.root,
            workflow=str(plan.get("workflow") or "local_html_form_workflow"),
            inputs_file=str(plan.get("inputs_file") or "demo_login.json"),
        )
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": result.get("status") or "unknown",
                "mcp_smoke": result,
                "preferred": mcp_smoke_check_to_markdown(result),
                "message": f"MCP smoke check {result.get('status')}.",
            },
            workspace,
            selected_run_id=str(result.get("run_id") or "") or None,
        )
    if action == "plan_risk_policy_patch":
        policy_plan = build_workspace_risk_policy_apply_plan(
            workspace,
            overwrite=bool(plan.get("overwrite", False)),
            apply=False,
        )
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success" if policy_plan["validation_after"]["error_count"] == 0 else "blocked",
                "policy_plan": policy_plan,
                "message": f"Risk policy patch changes: {len(policy_plan['changed_paths'])}.",
            },
            workspace,
        )
    if action == "apply_risk_policy_patch":
        policy_plan = build_workspace_risk_policy_apply_plan(
            workspace,
            overwrite=bool(plan.get("overwrite", False)),
            apply=True,
        )
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success" if policy_plan["validation_after"]["error_count"] == 0 else "blocked",
                "policy_plan": policy_plan,
                "message": f"Risk policy patch {'applied' if policy_plan['applied'] else 'unchanged'}: {len(policy_plan['changed_paths'])} path(s).",
            },
            workspace,
        )
    if action == "preview_planner_draft_save":
        ref = find_workflow(workspace, str(plan.get("workflow") or ""))
        workflow = parse_workflow_file(ref.path)
        check = check_planner_draft(workflow, workspace=workspace)
        draft = {
            "schema_version": 1,
            "status": "valid" if check.valid else "invalid",
            "executed": False,
            "workspace_root": str(workspace.root),
            "provider": "workspace-gui",
            "selected_provider": "workspace-gui",
            "model": "",
            "draft_text": ref.path.read_text(encoding="utf-8"),
            "parse_status": "success",
            "workflow": workflow_to_dict(workflow),
            "check": {
                "valid": check.valid,
                "allowed_to_execute": check.allowed_to_execute,
                "dry_run_required": check.dry_run_required,
                "issues": [issue.__dict__ for issue in check.issues],
            },
        }
        target = str(plan.get("save_as") or f"planner_preview/{ref.name}")
        preview = preview_planner_draft_save(workspace, draft, target)
        save = preview.get("save") if isinstance(preview.get("save"), dict) else {}
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success" if save.get("status") == "previewed" else "blocked",
                "planner_draft": preview,
                "message": f"Planner draft save preview: {save.get('path') or save.get('reason')}.",
            },
            workspace,
        )
    if action == "generate_planner_draft_preview":
        draft = generate_planner_draft(
            workspace,
            str(plan.get("instruction") or ""),
            source=str(plan.get("source") or "model_api_keys.txt"),
            preferred_provider=str(plan.get("preferred") or "openai"),
            model=str(plan.get("model") or "") or None,
            execute=True,
        )
        target = str(plan.get("save_as") or safe_planner_draft_save_name(draft))
        preview = preview_planner_draft_save(workspace, draft, target)
        save = preview.get("save") if isinstance(preview.get("save"), dict) else {}
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success" if save.get("status") == "previewed" else "blocked",
                "planner_draft": preview,
                "message": f"Generated planner draft preview: {save.get('path') or save.get('reason')}.",
            },
            workspace,
        )
    if action == "save_generated_planner_draft":
        draft = generate_planner_draft(
            workspace,
            str(plan.get("instruction") or ""),
            source=str(plan.get("source") or "model_api_keys.txt"),
            preferred_provider=str(plan.get("preferred") or "openai"),
            model=str(plan.get("model") or "") or None,
            execute=True,
        )
        saved = save_planner_draft_result(
            workspace,
            draft,
            str(plan.get("save_as") or safe_planner_draft_save_name(draft)),
            overwrite=bool(plan.get("overwrite", False)),
        )
        save = saved.get("save") if isinstance(saved.get("save"), dict) else {}
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success" if save.get("status") == "saved" else "blocked",
                "planner_draft": saved,
                "message": f"Generated planner draft save: {save.get('path') or save.get('reason')}.",
            },
            workspace,
        )
    if action == "record_browser_workflow":
        result = record_browser_session(
            workspace,
            url=str(plan.get("url") or ""),
            save_as=str(plan.get("save_as") or ""),
            assert_text=str(plan.get("assert_text") or "").strip() or None,
            save_auth_state=str(plan.get("save_auth_state") or "").strip() or None,
            check=True,
            preview_run=bool(plan.get("preview_run", False)),
            overwrite=bool(plan.get("overwrite", False)),
            queue_run=bool(plan.get("queue_run", False)),
        )
        record = recorded_result_to_dict(result)
        preflight = record.get("preflight") if isinstance(record.get("preflight"), dict) else None
        preview = record.get("preview") if isinstance(record.get("preview"), dict) else None
        blocked = (
            not result.validation.valid
            or (preflight is not None and not bool(preflight.get("ok")))
            or (preview is not None and not bool(preview.get("ok")))
        )
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "blocked" if blocked else "success",
                "recording": record,
                "message": f"Recorded workflow: {record.get('workflow_path') or plan.get('save_as')}.",
            },
            workspace,
        )
    if action == "read_input_template":
        resolved = resolve_input_template_path(workspace, str(plan.get("path") or ""))
        payload = json.loads(resolved.read_text(encoding="utf-8"))
        return {
            "action": action,
            "status": "success",
            "path": resolved.relative_to(workspace.root).as_posix(),
            "input_template": {
                "path": resolved.relative_to(workspace.root).as_posix(),
                "text": json.dumps(payload, ensure_ascii=False, indent=2),
                "keys": sorted(str(key) for key in payload) if isinstance(payload, dict) else [],
            },
            "message": f"Input template loaded: {resolved.name}.",
        }
    if action == "save_input_template":
        resolved = resolve_input_template_path(workspace, str(plan.get("path") or ""), create=True)
        payload = json.loads(str(plan.get("input_text") or ""))
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        keys = sorted(str(key) for key in payload) if isinstance(payload, dict) else []
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success",
                "path": resolved.relative_to(workspace.root).as_posix(),
                "input_template": {
                    "path": resolved.relative_to(workspace.root).as_posix(),
                    "keys": keys,
                    "byte_count": resolved.stat().st_size,
                },
                "message": f"Input template saved: {resolved.name}.",
            },
            workspace,
        )
    if action == "plan_external_sample_run":
        plan_result = build_external_sample_run_plan(
            str(plan.get("sample_id") or ""),
            workspace_root=workspace.root,
            run_profile=str(plan.get("run_profile") or "dry-run"),
        )
        return {
            "action": action,
            "status": "success" if plan_result["ready"] else "blocked",
            "plan": plan_result,
            "message": f"External sample {'ready' if plan_result['ready'] else 'blocked'}: {plan_result['sample_id']}",
        }
    if action == "submit_external_sample_batch":
        result = submit_external_sample_batch(
            workspace,
            run_profile=str(plan.get("run_profile") or "dry-run"),
        )
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success" if result["submitted_count"] > 0 else "blocked",
                "result": result,
                "message": f"Queued {result['submitted_count']} external sample(s), skipped {result['skipped_count']}.",
            },
            workspace,
        )
    if action == "external_sample_summary":
        summary = build_external_sample_run_summary(workspace)
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success",
                "summary": summary,
                "message": f"External samples: {summary['with_reports']} with reports, {summary['queued_samples']} queued.",
            },
            workspace,
        )
    if action == "external_sample_batch_report":
        result = export_external_sample_batch_report(workspace)
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success",
                "result": result,
                "message": f"External sample batch report exported: {result['report_id']}.",
            },
            workspace,
            selected_batch_report_id=str(result["report_id"]),
        )
    if action == "plan_external_sample_batch_reruns":
        plan_result = build_external_sample_batch_rerun_plan(
            workspace,
            str(plan.get("batch_report_id") or ""),
            run_profile=str(plan.get("run_profile") or "dry-run"),
        )
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success" if plan_result["candidate_count"] > 0 else "empty",
                "plan": plan_result,
                "message": f"Batch rerun candidates: {plan_result['candidate_count']}.",
            },
            workspace,
            selected_batch_report_id=str(plan_result["report_id"]),
        )
    if action == "submit_external_sample_batch_reruns":
        result = submit_external_sample_batch_reruns(
            workspace,
            str(plan.get("batch_report_id") or ""),
            run_profile=str(plan.get("run_profile") or "dry-run"),
        )
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success" if result["submitted_count"] > 0 else "empty",
                "result": result,
                "message": f"Queued {result['submitted_count']} batch rerun(s), skipped {result['skipped_count']}.",
            },
            workspace,
            selected_batch_report_id=str(result["report_id"]),
        )
    if action == "plan_external_sample_reruns":
        plan_result = build_external_sample_rerun_plan(
            workspace,
            run_profile=str(plan.get("run_profile") or "dry-run"),
        )
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success" if plan_result["candidate_count"] > 0 else "empty",
                "plan": plan_result,
                "message": f"Rerun candidates: {plan_result['candidate_count']}.",
            },
            workspace,
        )
    if action == "submit_external_sample_reruns":
        result = submit_external_sample_reruns(
            workspace,
            run_profile=str(plan.get("run_profile") or "dry-run"),
        )
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success" if result["submitted_count"] > 0 else "empty",
                "result": result,
                "message": f"Queued {result['submitted_count']} rerun(s), skipped {result['skipped_count']}.",
            },
            workspace,
        )
    if action == "open_artifact":
        resolved = resolve_gui_path(workspace, str(plan.get("path") or ""))
        return {
            "action": action,
            "status": "success",
            "path": str(resolved),
            "message": f"Artifact ready: {resolved}",
        }
    if action == "inspect_auth_state":
        resolved = resolve_gui_path(workspace, str(plan.get("path") or ""), allow_auth=True)
        metadata = inspect_storage_state(resolved)
        return {
            "action": action,
            "status": "success",
            "path": str(resolved),
            "metadata": metadata,
            "message": f"Auth state inspected: {metadata['filename']}",
        }
    if action == "delete_auth_state":
        resolved = resolve_gui_path(workspace, str(plan.get("path") or ""), allow_auth=True)
        if resolved.suffix.lower() != ".json" or resolved.name.endswith(".manifest.json"):
            raise ValueError("Only auth_state JSON files can be deleted from the GUI.")
        metadata = inspect_storage_state(resolved)
        manifest = resolved.with_suffix(resolved.suffix + ".manifest.json")
        resolved.unlink()
        manifest_deleted = False
        if manifest.exists():
            manifest.unlink()
            manifest_deleted = True
        return attach_refreshed_console_model(
            {
                "action": action,
                "status": "success",
                "path": str(resolved),
                "metadata": metadata,
                "manifest_deleted": manifest_deleted,
                "message": f"Auth state deleted: {resolved.name}",
            },
            workspace,
        )
    if action == "import_auth_state":
        result = import_auth_state(
            str(plan.get("source") or ""),
            name=str(plan.get("auth_name") or ""),
            workspace_root=workspace.root,
            overwrite=bool(plan.get("overwrite", False)),
        )
        return attach_refreshed_console_model(
            {"action": action, "status": "success", "result": result, "message": f"Auth state imported: {result['name']}"},
            workspace,
        )
    raise ValueError(f"Unsupported GUI action: {action}")


def build_external_sample_readiness(
    workspace: Workspace,
    *,
    sample_root: str | Path = "examples/external_samples",
) -> dict[str, Any]:
    readiness = external_samples_readiness(sample_root, workspace_root=workspace.root)
    entries = []
    missing_storage_state_files = 0
    for entry in readiness.get("entries", []) if isinstance(readiness.get("entries"), list) else []:
        if not isinstance(entry, dict):
            continue
        storage_paths = []
        existing_files = {
            str(item.get("path") or ""): item
            for item in entry.get("storage_state_files", [])
            if isinstance(item, dict)
        } if isinstance(entry.get("storage_state_files"), list) else {}
        for raw_path in entry.get("storage_state_paths", []) if isinstance(entry.get("storage_state_paths"), list) else []:
            allowed = True
            try:
                resolved = resolve_gui_path(workspace, str(raw_path), allow_auth=True)
                exists = resolved.exists()
            except ValueError:
                allowed = False
                resolved = Path(str(raw_path))
                exists = False
            if not exists:
                missing_storage_state_files += 1
            storage_paths.append(
                {
                    **existing_files.get(str(raw_path), {}),
                    "path": str(raw_path),
                    "resolved_path": str(resolved),
                    "exists": exists,
                    "allowed": allowed,
                }
            )
        entries.append({**entry, "storage_state_files": storage_paths})
    return {
        **readiness,
        "entries": entries,
        "missing_storage_state_files": missing_storage_state_files,
    }


def readiness_options(readiness: dict[str, Any]) -> list[dict[str, Any]]:
    options = []
    for entry in readiness.get("entries", []) if isinstance(readiness.get("entries"), list) else []:
        if not isinstance(entry, dict):
            continue
        options.append(
            {
                "sample_id": str(entry.get("sample_id") or "unknown"),
                "ready": bool(entry.get("ready")),
                "blockers": list(entry.get("blockers") or []),
                "label": readiness_option_label(entry),
            }
        )
    return options


def batch_report_options(index: dict[str, Any]) -> list[dict[str, Any]]:
    options = []
    for entry in index.get("entries", []) if isinstance(index.get("entries"), list) else []:
        if not isinstance(entry, dict):
            continue
        markdown = entry.get("markdown_report") or entry.get("json_report")
        if not markdown:
            continue
        options.append(
            {
                "report_id": str(entry.get("report_id") or ""),
                "status": str(entry.get("status") or "unknown"),
                "path": str(markdown),
                "json_report": str(entry.get("json_report") or ""),
                "markdown_report": str(entry.get("markdown_report") or ""),
                "label": batch_report_option_label(entry),
            }
        )
    return options


def batch_report_option_label(entry: dict[str, Any]) -> str:
    report_id = str(entry.get("report_id") or "")
    short_id = report_id[:32] if len(report_id) > 32 else report_id
    status = str(entry.get("status") or "unknown").upper()
    return f"{status}  samples={entry.get('total_samples', 0)} reports={entry.get('with_reports', 0)}  {short_id}".strip()


def batch_report_markdown(workspace: Workspace, option: dict[str, Any] | None) -> str:
    if not option:
        return ""
    path = str(option.get("markdown_report") or option.get("path") or "")
    if not path:
        return ""
    try:
        resolved = resolve_gui_path(workspace, path)
    except ValueError:
        return ""
    if not resolved.exists():
        return ""
    return resolved.read_text(encoding="utf-8")


def batch_report_detail_markdown(workspace: Workspace, option: dict[str, Any] | None) -> str:
    if not option:
        return ""
    body = batch_report_markdown(workspace, option)
    payload = batch_report_payload(workspace, option)
    summary = batch_report_status_summary_markdown(payload) if payload else ""
    if summary and body:
        return f"{summary}\n\n---\n\n{body}"
    return summary or body


def batch_report_payload(workspace: Workspace, option: dict[str, Any] | None) -> dict[str, Any] | None:
    if not option:
        return None
    path = str(option.get("json_report") or "")
    if not path:
        return None
    try:
        resolved = resolve_gui_path(workspace, path)
    except ValueError:
        return None
    if not resolved.exists():
        return None
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def batch_report_status_summary_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    entries = summary.get("entries") if isinstance(summary.get("entries"), list) else []
    failed = [entry for entry in entries if isinstance(entry, dict) and entry.get("status") == "failed"]
    blocked = [entry for entry in entries if isinstance(entry, dict) and not entry.get("ready")]
    ready_failed = [entry for entry in failed if entry.get("ready")]
    lines = [
        "# GUI Batch Status Summary",
        "",
        f"- Report ID: `{payload.get('report_id') or 'unknown'}`",
        f"- Samples: {summary.get('total_samples', 0)}",
        f"- Failed: {len(failed)}",
        f"- Blocked: {len(blocked)}",
        f"- Ready rerun candidates: {len(ready_failed)}",
    ]
    if ready_failed:
        lines.extend(["", "## Ready Rerun Candidates", ""])
        for entry in ready_failed:
            latest_report = entry.get("latest_report") if isinstance(entry.get("latest_report"), dict) else {}
            lines.append(
                f"- `{entry.get('sample_id')}`"
                f" run={latest_report.get('run_id') or 'none'}"
                f" failed_step={latest_report.get('failed_step') or 'unknown'}"
            )
    if blocked:
        lines.extend(["", "## Blocked Samples", ""])
        for entry in blocked:
            blockers = list(entry.get("blockers") or [])
            lines.append(f"- `{entry.get('sample_id')}`: {', '.join(blockers) or 'blocked'}")
    if not failed and not blocked:
        lines.extend(["", "## Review Notes", "", "- No failed or blocked samples; this batch is ready for normal review."])
    return "\n".join(lines)


def readiness_option_label(entry: dict[str, Any]) -> str:
    status = "READY" if entry.get("ready") else "BLOCKED"
    sample_id = str(entry.get("sample_id") or "unknown")
    blockers = entry.get("blockers") if isinstance(entry.get("blockers"), list) else []
    suffix = f"  blockers={len(blockers)}" if blockers else ""
    return f"{status}  {sample_id}{suffix}"


def readiness_to_markdown(readiness: dict[str, Any]) -> str:
    lines = [
        "# External Sample Readiness",
        "",
        f"- Samples: {readiness.get('total_samples', 0)}",
        f"- Ready: {readiness.get('ready_samples', 0)}",
        f"- Blocked: {readiness.get('blocked_samples', 0)}",
        f"- Missing storage_state files: {readiness.get('missing_storage_state_files', 0)}",
        f"- Auth-ready samples: {readiness.get('auth_ready_samples', 0)}",
        f"- Auth-blocked samples: {readiness.get('auth_blocked_samples', 0)}",
    ]
    entries = readiness.get("entries") if isinstance(readiness.get("entries"), list) else []
    blocked_entries = [entry for entry in entries if isinstance(entry, dict) and not entry.get("ready")]
    ready_entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("ready")]
    lines.extend(["", "## Status Summary", ""])
    if ready_entries:
        lines.append("- Ready samples: " + ", ".join(f"`{entry.get('sample_id')}`" for entry in ready_entries))
    else:
        lines.append("- Ready samples: none")
    if blocked_entries:
        lines.append("- Blocked samples: " + ", ".join(f"`{entry.get('sample_id')}`" for entry in blocked_entries))
    else:
        lines.append("- Blocked samples: none")
    if blocked_entries:
        lines.extend(["", "## Blocked Remediation", ""])
        for entry in blocked_entries:
            blockers = list(entry.get("blockers") or [])
            lines.append(f"- `{entry.get('sample_id')}`: {readiness_remediation_hint(blockers)}")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        lines.extend(
            [
                "",
                f"## {entry.get('sample_id', 'unknown')}",
                "",
                f"- Status: {'ready' if entry.get('ready') else 'blocked'}",
                f"- Account environment: {entry.get('account_environment') or 'unknown'}",
                f"- Allowed domains: {', '.join(entry.get('allowed_domains') or []) or 'none'}",
                f"- Storage state policy: {entry.get('storage_state_policy') or 'unknown'}",
                f"- Download policy: {entry.get('download_policy') or 'unknown'}",
                f"- Requirements: {', '.join(entry.get('requirements') or []) or 'none'}",
                f"- Blockers: {', '.join(entry.get('blockers') or []) or 'none'}",
            ]
        )
        storage_files = entry.get("storage_state_files") if isinstance(entry.get("storage_state_files"), list) else []
        if storage_files:
            lines.append("- Storage state files:")
            for item in storage_files:
                lines.append(
                    f"  - {item['path']} ({'exists' if item['exists'] else 'missing'}, "
                    f"auth={item.get('status') or 'unknown'}, "
                    f"matched={', '.join(item.get('matched_allowed_domains') or []) or 'none'})"
                )
    return "\n".join(lines)


def readiness_remediation_hint(blockers: list[str]) -> str:
    if "missing_storage_state_file" in blockers:
        return "Import the required storage_state with auth-state-import, then refresh readiness."
    if "auth_state_not_ready" in blockers:
        return "Import a non-empty Playwright storage_state for the sample allowed domain, then refresh readiness."
    if blockers:
        return "Resolve blockers: " + ", ".join(blockers)
    return "Review the sample requirements before running."


def artifact_options(workspace: Workspace, report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not report:
        return []
    options: list[dict[str, Any]] = []
    paths = report.get("paths") if isinstance(report.get("paths"), dict) else {}
    for key in ("json_report", "markdown_report"):
        value = paths.get(key)
        if value:
            options.append(artifact_option(workspace, str(value), key))
    artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
    for key, value in artifacts.items():
        if isinstance(value, list):
            for index, item in enumerate(value):
                options.append(artifact_option(workspace, str(item), f"{key}[{index}]"))
        elif value:
            options.append(artifact_option(workspace, str(value), key))
    for item in report.get("downloads", []) if isinstance(report.get("downloads"), list) else []:
        if isinstance(item, dict) and item.get("path"):
            options.append(artifact_option(workspace, str(item["path"]), f"download:{item.get('filename') or 'file'}"))
    seen = set()
    unique = []
    for option in options:
        if option["path"] in seen:
            continue
        seen.add(option["path"])
        unique.append(option)
    return unique


def artifact_option(workspace: Workspace, path: str, label: str) -> dict[str, Any]:
    resolved = resolve_gui_path(workspace, path)
    return {
        "label": f"{label}  {path}",
        "path": path,
        "resolved_path": str(resolved),
        "exists": resolved.exists(),
    }


def auth_state_options(workspace: Workspace) -> list[dict[str, Any]]:
    auth_dir = workspace.root / ".agent-auth"
    if not auth_dir.exists():
        auth_dir = Path(".agent-auth")
    if not auth_dir.exists():
        return []
    options = []
    for path in sorted(auth_dir.glob("*.json")):
        if path.name.endswith(".manifest.json"):
            continue
        try:
            metadata = inspect_storage_state(path)
        except Exception:
            metadata = {"valid": False, "filename": path.name}
        status = auth_state_gui_status(metadata)
        options.append(
            {
                "label": f"{status.upper()}  {path.name}  cookies={metadata.get('cookie_count', '?')}",
                "path": str(path),
                "status": status,
                "warning": auth_state_gui_warning(metadata),
                "metadata": metadata,
            }
        )
    return options


def auth_state_gui_status(metadata: dict[str, Any]) -> str:
    if metadata.get("valid") is False:
        return "invalid"
    if not metadata.get("has_session_material"):
        return "empty"
    cookie_count = int(metadata.get("cookie_count") or 0)
    expired_count = int(metadata.get("expired_cookie_count") or 0)
    origin_count = int(metadata.get("origin_count") or 0)
    if cookie_count > 0 and expired_count >= cookie_count and origin_count == 0:
        return "expired"
    return "ready"


def auth_state_gui_warning(metadata: dict[str, Any]) -> str:
    status = auth_state_gui_status(metadata)
    if status == "expired":
        return "All persistent cookies are expired; re-import or refresh this auth_state."
    if status == "empty":
        return "No cookies or localStorage origins found."
    if status == "invalid":
        return "Storage state JSON could not be inspected."
    return ""


def input_template_options(workspace: Workspace) -> list[dict[str, Any]]:
    if not workspace.inputs_dir.exists():
        return []
    options = []
    for path in sorted(workspace.inputs_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            keys = sorted(str(key) for key in payload) if isinstance(payload, dict) else []
            valid = True
        except Exception:
            keys = []
            valid = False
        relative = path.relative_to(workspace.root).as_posix()
        options.append(
            {
                "label": f"{path.name}  keys={len(keys)}",
                "path": relative,
                "keys": keys,
                "valid": valid,
            }
        )
    return options


def resolve_input_template_path(workspace: Workspace, path: str, *, create: bool = False) -> Path:
    raw = Path(str(path or "").strip())
    if raw.is_absolute():
        raise ValueError("Input template path must be relative.")
    if raw.suffix.lower() != ".json":
        raise ValueError("Input template must use .json extension.")
    root = workspace.inputs_dir.resolve()
    if raw.parts and raw.parts[0] == "inputs":
        raw = Path(*raw.parts[1:])
    resolved = (root / raw).resolve()
    if not is_relative_to(resolved, root):
        raise ValueError(f"Input template path must stay under workspace inputs/: {path}")
    if not create and not resolved.exists():
        raise FileNotFoundError(f"Input template not found: {path}")
    return resolved


def resolve_gui_path(workspace: Workspace, path: str, *, allow_auth: bool = False) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace.root / candidate
        if not candidate.exists() and allow_auth:
            candidate = Path(path)
    resolved = candidate.resolve()
    allowed_roots = [workspace.root.resolve()]
    if allow_auth:
        allowed_roots.append((Path(".agent-auth")).resolve())
    if not any(is_relative_to(resolved, root) for root in allowed_roots):
        raise ValueError(f"Path is outside allowed GUI roots: {path}")
    return resolved


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def report_option_label(report: dict[str, Any]) -> str:
    run_id = str(report.get("run_id") or "")
    workflow = str(report.get("workflow_name") or "")
    status = str(report.get("status") or "unknown")
    short_id = run_id[:19] if len(run_id) > 19 else run_id
    return f"{status.upper()}  {workflow}  {short_id}".strip()


def queue_option_label(task: dict[str, Any]) -> str:
    return f"{str(task.get('status') or 'unknown').upper()}  {task.get('workflow') or ''}  {task.get('task_id') or ''}".strip()


def open_workspace_window(
    workspace: Workspace,
    *,
    selected_run_id: str | None = None,
    limit: int = 10,
) -> int:
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk

    model = build_console_window_model(workspace, selected_run_id=selected_run_id, limit=limit)
    root = tk.Tk()
    root.title(model["title"])
    root.geometry("1120x720")
    root.minsize(900, 560)

    main = ttk.Frame(root, padding=10)
    main.pack(fill=tk.BOTH, expand=True)
    main.columnconfigure(1, weight=1)
    main.rowconfigure(2, weight=1)

    ttk.Label(main, text=model["title"], font=("Segoe UI", 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(main, text=model["workspace_root"]).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))

    cards = ttk.Frame(main)
    cards.grid(row=2, column=0, sticky="nsew", padx=(0, 10))
    cards.columnconfigure(0, weight=1)
    card_widgets: dict[str, tuple[Any, Any]] = {}
    for row, card in enumerate(model["summary_cards"]):
        frame = ttk.LabelFrame(cards, text=card["label"], padding=8)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        value_label = ttk.Label(frame, text=card["value"], font=("Segoe UI", 12, "bold"))
        detail_label = ttk.Label(frame, text=card["detail"])
        value_label.pack(anchor="w")
        detail_label.pack(anchor="w")
        card_widgets[str(card["id"])] = (value_label, detail_label)

    detail = ttk.Frame(main)
    detail.grid(row=2, column=1, sticky="nsew")
    detail.columnconfigure(0, weight=1)
    detail.rowconfigure(2, weight=1)

    state = console_window_selection_state(model)

    report_var = tk.StringVar(value=state["report"]["selected_label"])
    option_by_label = state["report"]["by_label"]
    labels = state["report"]["labels"]

    selector = ttk.Combobox(detail, textvariable=report_var, values=labels, state="readonly")
    selector.grid(row=0, column=0, sticky="ew", pady=(0, 8))

    controls = ttk.Frame(detail)
    controls.grid(row=1, column=0, sticky="ew", pady=(0, 8))
    controls.columnconfigure(0, weight=1)
    controls.columnconfigure(1, weight=1)

    workflow_by_label = state["workflow"]["by_label"]
    workflow_labels = state["workflow"]["labels"]
    workflow_var = tk.StringVar(value=state["workflow"]["selected_label"])
    workflow_selector = ttk.Combobox(controls, textvariable=workflow_var, values=workflow_labels, state="readonly")
    workflow_selector.grid(row=0, column=0, sticky="ew", padx=(0, 6))

    queue_by_label = state["queue"]["by_label"]
    queue_labels = state["queue"]["labels"]
    queue_var = tk.StringVar(value=state["queue"]["selected_label"])
    queue_selector = ttk.Combobox(controls, textvariable=queue_var, values=queue_labels, state="readonly")
    queue_selector.grid(row=0, column=1, sticky="ew", padx=(0, 6))

    artifact_by_label = state["artifact"]["by_label"]
    artifact_labels = state["artifact"]["labels"]
    artifact_var = tk.StringVar(value=state["artifact"]["selected_label"])
    artifact_selector = ttk.Combobox(controls, textvariable=artifact_var, values=artifact_labels, state="readonly")
    artifact_selector.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=(6, 0))

    auth_by_label = state["auth_state"]["by_label"]
    auth_labels = state["auth_state"]["labels"]
    auth_var = tk.StringVar(value=state["auth_state"]["selected_label"])
    auth_selector = ttk.Combobox(controls, textvariable=auth_var, values=auth_labels, state="readonly")
    auth_selector.grid(row=1, column=1, sticky="ew", padx=(0, 6), pady=(6, 0))

    readiness_by_label = state["readiness"]["by_label"]
    readiness_labels = state["readiness"]["labels"]
    readiness_var = tk.StringVar(value=state["readiness"]["selected_label"])
    readiness_selector = ttk.Combobox(controls, textvariable=readiness_var, values=readiness_labels, state="readonly")
    readiness_selector.grid(row=2, column=0, columnspan=2, sticky="ew", padx=(0, 6), pady=(6, 0))

    batch_report_by_label = state["batch_report"]["by_label"]
    batch_report_labels = state["batch_report"]["labels"]
    batch_report_var = tk.StringVar(value=state["batch_report"]["selected_label"])
    batch_report_selector = ttk.Combobox(controls, textvariable=batch_report_var, values=batch_report_labels, state="readonly")
    batch_report_selector.grid(row=3, column=0, columnspan=2, sticky="ew", padx=(0, 6), pady=(6, 0))

    action_history_by_label = state["action_history"]["by_label"]
    action_history_labels = state["action_history"]["labels"]
    action_history_var = tk.StringVar(value=state["action_history"]["selected_label"])
    action_history_selector = ttk.Combobox(controls, textvariable=action_history_var, values=action_history_labels, state="readonly")
    action_history_selector.grid(row=4, column=0, columnspan=2, sticky="ew", padx=(0, 6), pady=(6, 0))

    buttons = ttk.Frame(controls)
    buttons.grid(row=0, column=2, rowspan=5, sticky="e")

    text = tk.Text(detail, wrap="word", font=("Consolas", 10), padx=8, pady=8)
    text.grid(row=2, column=0, sticky="nsew")
    text.insert("1.0", console_model_detail_markdown(model))
    text.configure(state="disabled")

    def refresh_detail(_event: object | None = None) -> None:
        selected = option_by_label.get(report_var.get())
        run_id = selected.get("run_id") if selected else None
        markdown = "No report selected."
        if run_id:
            markdown = report_detail_to_markdown(build_report_detail(workspace, str(run_id)))
        text.configure(state="normal")
        text.delete("1.0", tk.END)
        text.insert("1.0", markdown)
        text.configure(state="disabled")
        update_button_states()

    selector.bind("<<ComboboxSelected>>", refresh_detail)

    def refresh_batch_detail(_event: object | None = None) -> None:
        selected = batch_report_by_label.get(batch_report_var.get())
        markdown = batch_report_detail_markdown(workspace, selected) or "No batch report selected."
        text.configure(state="normal")
        text.delete("1.0", tk.END)
        text.insert("1.0", markdown)
        text.configure(state="disabled")
        update_button_states()

    batch_report_selector.bind("<<ComboboxSelected>>", refresh_batch_detail)

    def refresh_action_history_detail(_event: object | None = None) -> None:
        selected = action_history_by_label.get(action_history_var.get())
        markdown = gui_action_event_markdown(selected) or "No GUI action event selected."
        text.configure(state="normal")
        text.delete("1.0", tk.END)
        text.insert("1.0", markdown)
        text.configure(state="disabled")
        update_button_states()

    action_history_selector.bind("<<ComboboxSelected>>", refresh_action_history_detail)

    def write_message(message: str) -> None:
        text.configure(state="normal")
        text.delete("1.0", tk.END)
        text.insert("1.0", message)
        text.configure(state="disabled")

    def apply_selector(selector_widget: Any, variable: Any, group: dict[str, Any]) -> None:
        selector_widget.configure(values=group["labels"])
        variable.set(group["selected_label"])

    def current_ui_state() -> dict[str, Any]:
        current = console_window_selection_state(model)
        current["report"]["selected_label"] = report_var.get()
        current["workflow"]["selected_label"] = workflow_var.get()
        current["queue"]["selected_label"] = queue_var.get()
        current["artifact"]["selected_label"] = artifact_var.get()
        current["auth_state"]["selected_label"] = auth_var.get()
        current["readiness"]["selected_label"] = readiness_var.get()
        current["batch_report"]["selected_label"] = batch_report_var.get()
        current["action_history"]["selected_label"] = action_history_var.get()
        return current

    def update_button_states() -> None:
        for action_id, state_ in console_window_button_states(model, current_ui_state()).items():
            widget = button_widgets.get(action_id)
            if widget is None:
                continue
            widget.configure(state="normal" if state_["enabled"] else "disabled")

    for selector_widget in (
        workflow_selector,
        queue_selector,
        artifact_selector,
        auth_selector,
        readiness_selector,
    ):
        selector_widget.bind("<<ComboboxSelected>>", lambda _event: update_button_states())

    def apply_console_model(next_model: dict[str, Any], message: str | None = None) -> None:
        nonlocal model, state
        nonlocal option_by_label, labels
        nonlocal workflow_by_label, workflow_labels
        nonlocal queue_by_label, queue_labels
        nonlocal artifact_by_label, artifact_labels
        nonlocal auth_by_label, auth_labels
        nonlocal readiness_by_label, readiness_labels
        nonlocal batch_report_by_label, batch_report_labels
        nonlocal action_history_by_label, action_history_labels

        model = next_model
        state = console_window_selection_state(model)
        for card in model.get("summary_cards", []) if isinstance(model.get("summary_cards"), list) else []:
            if not isinstance(card, dict):
                continue
            widgets = card_widgets.get(str(card.get("id") or ""))
            if widgets:
                widgets[0].configure(text=str(card.get("value") or ""))
                widgets[1].configure(text=str(card.get("detail") or ""))

        option_by_label = state["report"]["by_label"]
        labels = state["report"]["labels"]
        workflow_by_label = state["workflow"]["by_label"]
        workflow_labels = state["workflow"]["labels"]
        queue_by_label = state["queue"]["by_label"]
        queue_labels = state["queue"]["labels"]
        artifact_by_label = state["artifact"]["by_label"]
        artifact_labels = state["artifact"]["labels"]
        auth_by_label = state["auth_state"]["by_label"]
        auth_labels = state["auth_state"]["labels"]
        readiness_by_label = state["readiness"]["by_label"]
        readiness_labels = state["readiness"]["labels"]
        batch_report_by_label = state["batch_report"]["by_label"]
        batch_report_labels = state["batch_report"]["labels"]
        action_history_by_label = state["action_history"]["by_label"]
        action_history_labels = state["action_history"]["labels"]

        apply_selector(selector, report_var, state["report"])
        apply_selector(workflow_selector, workflow_var, state["workflow"])
        apply_selector(queue_selector, queue_var, state["queue"])
        apply_selector(artifact_selector, artifact_var, state["artifact"])
        apply_selector(auth_selector, auth_var, state["auth_state"])
        apply_selector(readiness_selector, readiness_var, state["readiness"])
        apply_selector(batch_report_selector, batch_report_var, state["batch_report"])
        apply_selector(action_history_selector, action_history_var, state["action_history"])
        write_message(message if message is not None else console_model_detail_markdown(model))
        update_button_states()

    def apply_action_result(result: dict[str, Any], message: str | None = None) -> None:
        refreshed = result.get("refreshed_model") if isinstance(result.get("refreshed_model"), dict) else None
        if refreshed:
            apply_console_model(refreshed, message=gui_action_feedback(result, message))
            return
        write_message(gui_action_feedback(result, message))

    def execute_or_schedule_action(
        plan: dict[str, Any],
        *,
        message: str | None = None,
        selected_batch_report_id: str | None = None,
    ) -> None:
        action = str(plan.get("action") or "")
        if not is_long_running_gui_action(action):
            apply_action_result(safe_execute_gui_action(workspace, plan, selected_batch_report_id=selected_batch_report_id), message=message)
            return

        def done(job: GuiAsyncJob) -> None:
            root.after(0, lambda: apply_action_result(job.result or {"action": action, "status": "error", "error": job.error}, message=message))

        job = run_gui_action_async(
            workspace,
            plan,
            selected_batch_report_id=selected_batch_report_id,
            on_done=done,
        )
        write_message(f"{action} started in background: `{job.job_id}`")

    def run_selected_workflow() -> None:
        selected = workflow_by_label.get(workflow_var.get())
        if not selected:
            write_message("No workflow selected.")
            return
        plan = build_gui_action_plan("run_workflow", workflow=str(selected["name"]))
        execute_or_schedule_action(plan)

    def run_next_task() -> None:
        execute_or_schedule_action(build_gui_action_plan("queue_run_next"))

    def update_selected_task(action: str) -> None:
        selected = queue_by_label.get(queue_var.get())
        if not selected:
            write_message("No queue task selected.")
            return
        result = safe_execute_gui_action(workspace, build_gui_action_plan(action, task_id=str(selected["task_id"])))
        apply_action_result(result)

    def open_selected_artifact() -> None:
        selected = artifact_by_label.get(artifact_var.get())
        if not selected:
            write_message("No artifact selected.")
            return
        result = safe_execute_gui_action(workspace, build_gui_action_plan("open_artifact", path=str(selected["path"])))
        write_message(gui_action_feedback(result))

    def inspect_selected_auth_state() -> None:
        selected = auth_by_label.get(auth_var.get())
        if not selected:
            write_message("No auth state selected.")
            return
        result = safe_execute_gui_action(workspace, build_gui_action_plan("inspect_auth_state", path=str(selected["path"])))
        write_message(gui_action_feedback(result, jsonish(result.get("metadata") or result)))

    def refresh_readiness() -> None:
        result = safe_execute_gui_action(workspace, build_gui_action_plan("refresh_readiness"))
        apply_action_result(result, message=readiness_to_markdown(result["readiness"]) if "readiness" in result else None)

    def plan_selected_external_sample() -> None:
        selected = readiness_by_label.get(readiness_var.get())
        if not selected:
            write_message("No external sample selected.")
            return
        result = safe_execute_gui_action(
            workspace,
            build_gui_action_plan("plan_external_sample_run", sample_id=str(selected["sample_id"])),
        )
        write_message(gui_action_feedback(result, jsonish(result.get("plan") or result)))

    def queue_ready_external_samples() -> None:
        execute_or_schedule_action(build_gui_action_plan("submit_external_sample_batch"))

    def show_external_sample_summary() -> None:
        result = safe_execute_gui_action(workspace, build_gui_action_plan("external_sample_summary"))
        apply_action_result(result, message=jsonish(result.get("summary") or result))

    def plan_risk_policy_patch() -> None:
        result = safe_execute_gui_action(workspace, build_gui_action_plan("plan_risk_policy_patch"))
        apply_action_result(result)

    def apply_risk_policy_patch() -> None:
        result = safe_execute_gui_action(workspace, build_gui_action_plan("apply_risk_policy_patch"))
        apply_action_result(result)

    def preview_selected_planner_draft() -> None:
        selected = workflow_by_label.get(workflow_var.get())
        if not selected:
            write_message("No workflow selected.")
            return
        result = safe_execute_gui_action(
            workspace,
            build_gui_action_plan(
                "preview_planner_draft_save",
                workflow=str(selected["name"]),
                save_as=f"planner_preview/{selected['name']}",
            ),
        )
        apply_action_result(result)

    def generate_planner_draft_preview() -> None:
        execute_or_schedule_action(
            build_gui_action_plan(
                "generate_planner_draft_preview",
                instruction="基于当前 workspace 生成一个只读 dry-run workflow 草案，至少包含一次 observe 和一次 assert，不使用凭据或高风险动作。",
                source="model_api_keys.txt",
                preferred="openai",
            )
        )

    def save_generated_planner_draft() -> None:
        instruction = simpledialog.askstring(
            "Save Planner Draft",
            "Automation instruction:",
            initialvalue="基于当前 workspace 生成一个只读 dry-run workflow 草案，至少包含一次 observe 和一次 assert，不使用凭据或高风险动作。",
            parent=root,
        )
        if not instruction:
            write_message("Planner draft save canceled: no instruction.")
            return
        save_as = simpledialog.askstring(
            "Save Planner Draft",
            "Workflow save name:",
            initialvalue="planner_generated/gui_generated_workflow",
            parent=root,
        )
        if not save_as:
            write_message("Planner draft save canceled: no workflow name.")
            return
        overwrite = messagebox.askyesno("Save Planner Draft", "Overwrite the workflow if it already exists?", parent=root)
        confirmed = messagebox.askyesno("Save Planner Draft", "Generate, validate, and save this draft now?", parent=root)
        if not confirmed:
            write_message("Planner draft save canceled.")
            return
        execute_or_schedule_action(
            build_gui_action_plan(
                "save_generated_planner_draft",
                instruction=instruction,
                save_as=save_as,
                source="model_api_keys.txt",
                preferred="openai",
                overwrite=overwrite,
            ),
        )

    def record_browser_workflow() -> None:
        url = simpledialog.askstring("Record Browser", "Start URL:", parent=root)
        if not url:
            write_message("Browser recording canceled: no URL.")
            return
        save_as = simpledialog.askstring("Record Browser", "Workflow save name:", initialvalue="recorded/browser_workflow", parent=root)
        if not save_as:
            write_message("Browser recording canceled: no workflow name.")
            return
        assert_text = simpledialog.askstring("Record Browser", "Success text to assert (optional):", parent=root)
        save_auth_state = simpledialog.askstring("Record Browser", "Auth state name to save (optional):", parent=root)
        overwrite = messagebox.askyesno("Record Browser", "Overwrite the workflow if it already exists?", parent=root)
        queue_run = messagebox.askyesno("Record Browser", "Queue the recorded workflow as a dry-run task?", parent=root)
        execute_or_schedule_action(
            build_gui_action_plan(
                "record_browser_workflow",
                url=url,
                save_as=save_as,
                assert_text=assert_text or None,
                save_auth_state=save_auth_state or None,
                preview_run=True,
                overwrite=overwrite,
                queue_run=queue_run,
            ),
        )

    def show_strict_policy_failures() -> None:
        write_message(strict_policy_failed_markdown(model.get("dashboard", {})))

    def export_external_sample_report() -> None:
        execute_or_schedule_action(build_gui_action_plan("external_sample_batch_report"))

    def open_selected_batch_report() -> None:
        selected = batch_report_by_label.get(batch_report_var.get())
        if not selected:
            write_message("No batch report selected.")
            return
        result = safe_execute_gui_action(workspace, build_gui_action_plan("open_artifact", path=str(selected["path"])))
        write_message(gui_action_feedback(result))

    def plan_selected_batch_reruns() -> None:
        selected = batch_report_by_label.get(batch_report_var.get())
        if not selected:
            write_message("No batch report selected.")
            return
        result = safe_execute_gui_action(
            workspace,
            build_gui_action_plan("plan_external_sample_batch_reruns", batch_report_id=str(selected["report_id"])),
            selected_batch_report_id=str(selected["report_id"]),
        )
        apply_action_result(result, message=jsonish(result.get("plan") or result))

    def queue_selected_batch_reruns() -> None:
        selected = batch_report_by_label.get(batch_report_var.get())
        if not selected:
            write_message("No batch report selected.")
            return
        execute_or_schedule_action(
            build_gui_action_plan("submit_external_sample_batch_reruns", batch_report_id=str(selected["report_id"])),
            selected_batch_report_id=str(selected["report_id"]),
        )

    def plan_external_sample_reruns() -> None:
        result = safe_execute_gui_action(workspace, build_gui_action_plan("plan_external_sample_reruns"))
        apply_action_result(result, message=jsonish(result.get("plan") or result))

    def queue_external_sample_reruns() -> None:
        execute_or_schedule_action(build_gui_action_plan("submit_external_sample_reruns"))

    def show_selected_action_history() -> None:
        selected = action_history_by_label.get(action_history_var.get())
        if not selected:
            write_message("No GUI action event selected.")
            return
        write_message(gui_action_event_markdown(selected))

    def show_selected_action_risk() -> None:
        selected = model.get("selected_gui_action_risk_event") if isinstance(model.get("selected_gui_action_risk_event"), dict) else None
        if not selected:
            write_message("No risky GUI action event selected.")
            return
        label = str(selected.get("label") or "")
        if label in action_history_by_label:
            action_history_var.set(label)
        write_message(gui_action_event_markdown(selected))

    button_widgets: dict[str, Any] = {}

    def add_button(action_id: str, label: str, command: Any, *, last: bool = False) -> None:
        button = ttk.Button(buttons, text=label, command=command)
        button.pack(side=tk.LEFT, padx=(0, 0 if last else 4))
        button_widgets[action_id] = button

    add_button("run_workflow", "Run Dry", run_selected_workflow)
    add_button("queue_run_next", "Run Next", run_next_task)
    add_button("cancel_queue_task", "Cancel", lambda: update_selected_task("cancel_queue_task"))
    add_button("retry_queue_task", "Retry", lambda: update_selected_task("retry_queue_task"))
    add_button("open_artifact", "Artifact", open_selected_artifact)
    add_button("inspect_auth_state", "Auth", inspect_selected_auth_state)
    add_button("refresh_readiness", "Readiness", refresh_readiness)
    add_button("plan_external_sample_run", "Plan External", plan_selected_external_sample)
    add_button("submit_external_sample_batch", "Queue External", queue_ready_external_samples)
    add_button("external_sample_summary", "External Summary", show_external_sample_summary)
    add_button("plan_risk_policy_patch", "Plan Policy", plan_risk_policy_patch)
    add_button("apply_risk_policy_patch", "Apply Policy", apply_risk_policy_patch)
    add_button("preview_planner_draft_save", "Preview Draft", preview_selected_planner_draft)
    add_button("generate_planner_draft_preview", "Generate Draft", generate_planner_draft_preview)
    add_button("save_generated_planner_draft", "Save Draft", save_generated_planner_draft)
    add_button("record_browser_workflow", "Record", record_browser_workflow)
    add_button("show_strict_policy_failures", "Strict Failures", show_strict_policy_failures)
    add_button("external_sample_batch_report", "Batch Report", export_external_sample_report)
    add_button("open_batch_report", "Open Batch", open_selected_batch_report)
    add_button("plan_external_sample_batch_reruns", "Plan Batch", plan_selected_batch_reruns)
    add_button("submit_external_sample_batch_reruns", "Queue Batch", queue_selected_batch_reruns)
    add_button("plan_external_sample_reruns", "Plan Reruns", plan_external_sample_reruns)
    add_button("submit_external_sample_reruns", "Queue Reruns", queue_external_sample_reruns)
    add_button("show_action_risk", "Risk", show_selected_action_risk)
    add_button("show_action_history", "History", show_selected_action_history, last=True)
    update_button_states()
    root.mainloop()
    return 0


def jsonish(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2)

