from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re

from .console import build_report_detail
from .security import scrub_secrets
from .session import clamp_ai_text, load_agent_session
from .structured_failure import StructuredFailure, classify_root_cause, structured_failure_from_diagnosis, structured_failure_to_dict
from .workspace import Workspace


def build_failure_summary(workspace: Path, *, max_chars: int = 1600) -> dict[str, Any]:
    session = load_agent_session(workspace)
    if session is None or session.latest_failure is None:
        return {"status": "no_failure", "message": "No recent failures found."}

    failure = session.latest_failure
    workspace_obj = Workspace(workspace.resolve())
    report_detail = _latest_failure_report_detail(workspace_obj, failure.run_id)
    visual_provider = _infer_visual_provider(failure.action, report_detail)
    expected = clamp_ai_text(failure.expected, max_chars=max(80, max_chars // 8), suffix="...[truncated]")
    actual = clamp_ai_text(failure.actual, max_chars=max(80, max_chars // 8), suffix="...[truncated]")
    hint = clamp_ai_text(failure.hint, max_chars=max(80, max_chars // 8), suffix="...[truncated]")
    prompt = (
        f"The workflow '{failure.workflow}' fails at step '{failure.step_id}' ({failure.action}). "
        f"Expected: {expected}. Actual: {actual}. "
        f"Suggested fix: {hint}"
    )
    prompt = clamp_ai_text(prompt, max_chars=max(200, max_chars // 2), suffix="...[truncated]")
    structured_context = {
        "step_id": failure.step_id,
        "action": failure.action,
        "expected": failure.expected,
        "actual": failure.actual,
        "observation": {"visible_text": [failure.actual] if failure.actual else []},
        "recovery_suggestions": [failure.hint] if failure.hint else [],
    }
    root_cause, confidence = classify_root_cause(structured_context)
    structured_failure = structured_failure_to_dict(
        StructuredFailure(
            step_id=failure.step_id,
            action=failure.action,
            provider=visual_provider,
            expected=expected,
            actual_visible=[actual] if actual else [],
            page_url="",
            page_state="unknown",
            screenshot_path="",
            root_cause=root_cause,
            confidence=confidence,
            suggested_fix=hint,
            related_files=[f"workflows/{failure.workflow}.yaml"],
        )
    )
    if report_detail is not None:
        report_failure = report_detail.get("failure") if isinstance(report_detail.get("failure"), dict) else {}
        diagnosis = report_failure.get("diagnosis") if isinstance(report_failure.get("diagnosis"), dict) else {}
        if diagnosis:
            structured = structured_failure_from_diagnosis(
                diagnosis,
                project_root=workspace_obj.project_root,
                workflow_name=failure.workflow,
                provider=str(report_failure.get("provider") or visual_provider or ""),
            )
            structured_failure = structured_failure_to_dict(structured)
            structured_failure["actual_visible"] = [
                _redact_sensitive_failure_text(str(item)) for item in structured_failure.get("actual_visible", []) if str(item)
            ]
            structured_failure["suggested_fix"] = _redact_sensitive_failure_text(str(structured_failure.get("suggested_fix") or ""))
            structured_failure["provider"] = str(report_failure.get("provider") or structured_failure.get("provider") or visual_provider or "")
    payload = {
        "status": "found",
        "workflow": failure.workflow,
        "run_id": failure.run_id,
        "failed_step": {"id": failure.step_id, "action": failure.action},
        "expected": expected,
        "actual": actual,
        "hint": hint,
        "artifacts": failure.artifact_dir,
        "visual_provider": visual_provider or None,
        "structured_failure": structured_failure,
        "suggested_next_prompt": prompt,
        "token_estimate": len(prompt) // 4,
    }
    safe_payload = scrub_secrets(payload)
    while len(json.dumps(safe_payload, ensure_ascii=False, default=str)) > max_chars:
        current_prompt = str(safe_payload.get("suggested_next_prompt") or "")
        if len(current_prompt) > 120:
            safe_payload["suggested_next_prompt"] = clamp_ai_text(current_prompt, max_chars=max(120, len(current_prompt) - 120), suffix="...[truncated]")
            safe_payload["token_estimate"] = len(str(safe_payload["suggested_next_prompt"])) // 4
            continue
        for key in ("expected", "actual", "hint"):
            value = str(safe_payload.get(key) or "")
            if len(value) > 80:
                safe_payload[key] = clamp_ai_text(value, max_chars=80, suffix="...")
                break
        else:
            break
    return safe_payload


def _latest_failure_report_detail(workspace: Workspace, run_id: str) -> dict[str, Any] | None:
    if not run_id:
        return None
    try:
        detail = build_report_detail(workspace, run_id)
    except FileNotFoundError:
        return None
    if not isinstance(detail, dict) or detail.get("status") == "upgrade_required":
        return None
    return detail


def _infer_visual_provider(action: str, report_detail: dict[str, Any] | None) -> str:
    if report_detail is not None:
        failure = report_detail.get("failure") if isinstance(report_detail.get("failure"), dict) else {}
        provider = str(failure.get("provider") or "").strip()
        if provider:
            return provider
        diagnosis = failure.get("diagnosis") if isinstance(failure.get("diagnosis"), dict) else {}
        observation = diagnosis.get("observation") if isinstance(diagnosis.get("observation"), dict) else {}
        provider = str(observation.get("provider") or "").strip()
        if provider:
            return provider
    if action in {"click_visual", "assert_visual_text"}:
        return "omniparser"
    return ""


def _redact_sensitive_failure_text(text: str) -> str:
    value = str(text or "")
    if not value:
        return value
    if any(term in value.lower() for term in ("password", "cookie", "bearer", "token", "secret", "api_key", "apikey")):
        return "[REDACTED]"
    return re.sub(r"(?i)(password|cookie|bearer|token|secret|api[_-]?key|apikey)\s*[:=]\s*\S+", "[REDACTED]", value)
