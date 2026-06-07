from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


STATUS_SCHEMA_VERSION = 1
VerificationResultName = Literal["pass", "fail", "needs_workflow_improvement", "timeout", "unknown"]


@dataclass(frozen=True)
class VerificationQuality:
    score: float | None = None
    covers_success_path: bool = False
    covers_error_path: bool = False
    business_assertions: int = 0
    structural_assertions: int = 0
    data_display_assertions: int = 0
    forbidden_error_assertions: int = 0
    text_from_input_references: int = 0
    invalid_text_from_references: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    recommendation: str = ""


@dataclass(frozen=True)
class VerificationFailedStep:
    id: str = ""
    action: str = ""
    expected: str = ""
    actual: str = ""
    fix_hint: str = ""


@dataclass(frozen=True)
class VerificationSemanticSummary:
    framework: str = ""
    confidence: float | None = None
    generation_method: str = ""
    field_count: int = 0
    required_field_count: int = 0
    sensitive_field_count: int = 0
    validation_rule_count: int = 0
    submit_action_count: int = 0
    success_state_count: int = 0
    error_state_count: int = 0
    data_display_count: int = 0
    negative_input_case_count: int = 0
    data_displays: tuple[str, ...] = ()
    matched_data_displays: tuple[str, ...] = ()
    unmatched_data_displays: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationNegativeOracle:
    text: str = ""
    source: str = ""


@dataclass(frozen=True)
class NegativeVerificationStatus:
    requested: bool = False
    status: str = ""
    reason: str = ""
    workflow_name: str = ""
    workflow_path: str | None = None
    run_id: str | None = None
    run_profile: str | None = None
    reset_strategy: str = ""
    oracles: tuple[VerificationNegativeOracle, ...] = ()
    report_path: str | None = None
    report_markdown_path: str | None = None
    report_hint: str | None = None
    next_action: str = ""
    steps_passed: int = 0
    steps_total: int = 0


@dataclass(frozen=True)
class VerificationStatus:
    schema_version: int
    result: VerificationResultName
    workflow_name: str
    workflow_path: str | None
    quality_score: float | None
    quality: VerificationQuality | None
    message: str
    next_action: str
    run_id: str | None = None
    run_profile: str | None = None
    requested_run_profile: str | None = None
    report_path: str | None = None
    report_markdown_path: str | None = None
    report_hint: str | None = None
    inputs_path: str | None = None
    inputs_source: str | None = None
    failed_step: VerificationFailedStep | None = None
    semantic_summary: VerificationSemanticSummary | None = None
    negative_verification: NegativeVerificationStatus | None = None
    generation_trace: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    steps_passed: int = 0
    steps_total: int = 0
    duration_ms: int = 0


def report_artifacts(workspace_root: Path, run_id: str | None) -> dict[str, str | None]:
    if not run_id:
        return {"report_path": None, "report_markdown_path": None, "report_hint": None}
    json_path = workspace_root / "reports" / f"{run_id}.json"
    markdown_path = workspace_root / "reports" / f"{run_id}.md"
    return {
        "report_path": str(json_path),
        "report_markdown_path": str(markdown_path),
        "report_hint": f"Use get_run_report with run_id='{run_id}' for full details.",
    }


def verification_next_action(payload: dict[str, Any]) -> str:
    result = str(payload.get("result") or "")
    if result == "pass":
        return "Implementation verified. Continue with code review or broader regression checks if needed."
    if result == "fail":
        failed = payload.get("failed_step") if isinstance(payload.get("failed_step"), dict) else {}
        fix_hint = str(failed.get("fix_hint") or "").strip()
        if fix_hint:
            return fix_hint
        return "Inspect the failed step and update the implementation or generated workflow, then rerun verify_implementation."
    if result == "needs_workflow_improvement":
        quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
        recommendation = str(quality.get("recommendation") or "").strip()
        return recommendation or "Improve generated workflow assertions before treating implementation verification as meaningful."
    if result == "timeout":
        return "Increase timeout_seconds, narrow the workflow, or run with dry-run first to diagnose slow steps."
    return "Review the verification result and rerun after making changes."


def enrich_verification_payload(payload: dict[str, Any], *, workspace_root: Path) -> dict[str, Any]:
    run_id = str(payload.get("run_id") or "") or None
    enriched = {
        "schema_version": STATUS_SCHEMA_VERSION,
        **payload,
        **report_artifacts(workspace_root, run_id),
    }
    enriched["next_action"] = verification_next_action(enriched)
    return enriched


def normalize_verification_status(payload: dict[str, Any]) -> VerificationStatus:
    result = str(payload.get("result") or "unknown")
    if result not in {"pass", "fail", "needs_workflow_improvement", "timeout"}:
        result = "unknown"
    quality_payload = payload.get("quality") if isinstance(payload.get("quality"), dict) else None
    failed_payload = payload.get("failed_step") if isinstance(payload.get("failed_step"), dict) else None
    semantic_payload = payload.get("semantic_summary") if isinstance(payload.get("semantic_summary"), dict) else None
    negative_payload = payload.get("negative_verification") if isinstance(payload.get("negative_verification"), dict) else None
    generation_trace = payload.get("generation_trace") if isinstance(payload.get("generation_trace"), list) else []
    return VerificationStatus(
        schema_version=int(payload.get("schema_version") or STATUS_SCHEMA_VERSION),
        result=result,  # type: ignore[arg-type]
        workflow_name=str(payload.get("workflow_name") or ""),
        workflow_path=_optional_str(payload.get("workflow_path")),
        quality_score=_optional_float(payload.get("quality_score")),
        quality=normalize_quality(quality_payload) if quality_payload else None,
        message=str(payload.get("message") or ""),
        next_action=str(payload.get("next_action") or verification_next_action(payload)),
        run_id=_optional_str(payload.get("run_id")),
        run_profile=_optional_str(payload.get("run_profile")),
        requested_run_profile=_optional_str(payload.get("requested_run_profile")),
        report_path=_optional_str(payload.get("report_path")),
        report_markdown_path=_optional_str(payload.get("report_markdown_path")),
        report_hint=_optional_str(payload.get("report_hint")),
        inputs_path=_optional_str(payload.get("inputs_path")),
        inputs_source=_optional_str(payload.get("inputs_source")),
        failed_step=normalize_failed_step(failed_payload) if failed_payload else None,
        semantic_summary=normalize_semantic_summary(semantic_payload) if semantic_payload else None,
        negative_verification=normalize_negative_verification(negative_payload) if negative_payload else None,
        generation_trace=tuple(str(item) for item in generation_trace if str(item)),
        timeout_seconds=_optional_float(payload.get("timeout_seconds")),
        steps_passed=int(payload.get("steps_passed") or 0),
        steps_total=int(payload.get("steps_total") or 0),
        duration_ms=int(payload.get("duration_ms") or 0),
    )


def normalize_quality(payload: dict[str, Any]) -> VerificationQuality:
    gaps = payload.get("gaps") if isinstance(payload.get("gaps"), list) else []
    invalid_text_from = payload.get("invalid_text_from_references") if isinstance(payload.get("invalid_text_from_references"), list) else []
    return VerificationQuality(
        score=_optional_float(payload.get("score")),
        covers_success_path=bool(payload.get("covers_success_path", False)),
        covers_error_path=bool(payload.get("covers_error_path", False)),
        business_assertions=int(payload.get("business_assertions") or 0),
        structural_assertions=int(payload.get("structural_assertions") or 0),
        data_display_assertions=int(payload.get("data_display_assertions") or 0),
        forbidden_error_assertions=int(payload.get("forbidden_error_assertions") or 0),
        text_from_input_references=int(payload.get("text_from_input_references") or 0),
        invalid_text_from_references=tuple(str(item) for item in invalid_text_from if str(item)),
        gaps=tuple(str(item) for item in gaps if str(item)),
        recommendation=str(payload.get("recommendation") or ""),
    )


def normalize_failed_step(payload: dict[str, Any]) -> VerificationFailedStep:
    return VerificationFailedStep(
        id=str(payload.get("id") or ""),
        action=str(payload.get("action") or ""),
        expected=str(payload.get("expected") or ""),
        actual=str(payload.get("actual") or ""),
        fix_hint=str(payload.get("fix_hint") or ""),
    )


def normalize_semantic_summary(payload: dict[str, Any]) -> VerificationSemanticSummary:
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    data_displays = payload.get("data_displays") if isinstance(payload.get("data_displays"), list) else []
    matched_data_displays = payload.get("matched_data_displays") if isinstance(payload.get("matched_data_displays"), list) else []
    unmatched_data_displays = payload.get("unmatched_data_displays") if isinstance(payload.get("unmatched_data_displays"), list) else []
    return VerificationSemanticSummary(
        framework=str(payload.get("framework") or ""),
        confidence=_optional_float(payload.get("confidence")),
        generation_method=str(payload.get("generation_method") or ""),
        field_count=int(payload.get("field_count") or 0),
        required_field_count=int(payload.get("required_field_count") or 0),
        sensitive_field_count=int(payload.get("sensitive_field_count") or 0),
        validation_rule_count=int(payload.get("validation_rule_count") or 0),
        submit_action_count=int(payload.get("submit_action_count") or 0),
        success_state_count=int(payload.get("success_state_count") or 0),
        error_state_count=int(payload.get("error_state_count") or 0),
        data_display_count=int(payload.get("data_display_count") or 0),
        negative_input_case_count=int(payload.get("negative_input_case_count") or 0),
        data_displays=tuple(str(item) for item in data_displays if str(item)),
        matched_data_displays=tuple(str(item) for item in matched_data_displays if str(item)),
        unmatched_data_displays=tuple(str(item) for item in unmatched_data_displays if str(item)),
        warnings=tuple(str(item) for item in warnings if str(item)),
    )


def normalize_negative_verification(payload: dict[str, Any]) -> NegativeVerificationStatus:
    return NegativeVerificationStatus(
        requested=bool(payload.get("requested", False)),
        status=str(payload.get("status") or ""),
        reason=str(payload.get("reason") or ""),
        workflow_name=str(payload.get("workflow_name") or ""),
        workflow_path=_optional_str(payload.get("workflow_path")),
        run_id=_optional_str(payload.get("run_id")),
        run_profile=_optional_str(payload.get("run_profile")),
        reset_strategy=str(payload.get("reset_strategy") or ""),
        oracles=normalize_negative_oracles(payload.get("oracles")),
        report_path=_optional_str(payload.get("report_path")),
        report_markdown_path=_optional_str(payload.get("report_markdown_path")),
        report_hint=_optional_str(payload.get("report_hint")),
        next_action=str(payload.get("next_action") or ""),
        steps_passed=int(payload.get("steps_passed") or 0),
        steps_total=int(payload.get("steps_total") or 0),
    )


def normalize_negative_oracles(value: Any) -> tuple[VerificationNegativeOracle, ...]:
    if not isinstance(value, list):
        return ()
    oracles: list[VerificationNegativeOracle] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        source = str(item.get("source") or "")
        if text or source:
            oracles.append(VerificationNegativeOracle(text=text, source=source))
    return tuple(oracles)


def status_file_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "updated_at": payload.get("updated_at"),
        "result": payload.get("result"),
        "workflow_name": payload.get("workflow_name"),
        "workflow_path": payload.get("workflow_path"),
        "quality_score": payload.get("quality_score"),
        "quality": payload.get("quality"),
        "semantic_summary": payload.get("semantic_summary"),
        "generation_trace": payload.get("generation_trace"),
        "failed_step": payload.get("failed_step"),
        "message": payload.get("message"),
        "next_action": payload.get("next_action"),
        "run_id": payload.get("run_id"),
        "run_profile": payload.get("run_profile"),
        "report_path": payload.get("report_path"),
        "report_markdown_path": payload.get("report_markdown_path"),
        "report_hint": payload.get("report_hint"),
        "inputs_path": payload.get("inputs_path"),
        "inputs_source": payload.get("inputs_source"),
        "negative_verification": compact_negative_verification(payload.get("negative_verification")),
        "timeout_seconds": payload.get("timeout_seconds"),
    }


def compact_negative_verification(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    keys = (
        "requested",
        "status",
        "reason",
        "workflow_name",
        "workflow_path",
        "run_id",
        "run_profile",
        "reset_strategy",
        "oracles",
        "report_path",
        "report_markdown_path",
        "report_hint",
        "next_action",
        "steps_passed",
        "steps_total",
    )
    return {key: value.get(key) for key in keys if key in value}


def write_verification_status(workspace_root: Path, payload: dict[str, Any]) -> Path:
    from time import time

    status = {**payload, "updated_at": time()}
    status_path = workspace_root / ".vscode-agent-status.json"
    status_path.write_text(json.dumps(status_file_payload(status), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return status_path


def read_verification_status(workspace_root: Path) -> VerificationStatus | None:
    status_path = workspace_root / ".vscode-agent-status.json"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return normalize_verification_status(payload)


def verification_status_to_markdown(status: VerificationStatus) -> str:
    lines = [
        f"Result: {status.result or 'unknown'}",
        f"Workflow: {status.workflow_name}",
        f"Quality: {status.quality_score:.2f}" if status.quality_score is not None else "",
        f"Run: {status.run_id}" if status.run_id else "",
        f"Timeout: {status.timeout_seconds:g}s" if status.timeout_seconds is not None else "",
        f"Report: {status.report_path}" if status.report_path else "",
        f"Report Markdown: {status.report_markdown_path}" if status.report_markdown_path else "",
        f"Report Hint: {status.report_hint}" if status.report_hint else "",
        f"Inputs: {status.inputs_path}" if status.inputs_path else "",
        f"Inputs Source: {status.inputs_source}" if status.inputs_source else "",
        f"Message: {status.message}" if status.message else "",
    ]
    lines = [line for line in lines if line]
    if status.negative_verification:
        negative = status.negative_verification
        lines.extend(
            [
                "",
                "Negative Verification:",
                f"- status: {negative.status}",
            ]
        )
        if negative.reason:
            lines.append(f"- reason: {negative.reason}")
        if negative.workflow_name:
            lines.append(f"- workflow: {negative.workflow_name}")
        if negative.reset_strategy:
            lines.append(f"- reset strategy: {negative.reset_strategy}")
        lines.append(f"- oracle count: {len(negative.oracles)}")
        if negative.steps_passed or negative.steps_total:
            lines.append(f"- steps: {negative.steps_passed}/{negative.steps_total}")
        if negative.run_id:
            lines.append(f"- run: {negative.run_id}")
        if negative.report_path:
            lines.append(f"- report: {negative.report_path}")
        if negative.report_markdown_path:
            lines.append(f"- report markdown: {negative.report_markdown_path}")
        if negative.report_hint:
            lines.append(f"- report hint: {negative.report_hint}")
        for oracle in negative.oracles:
            source = f" ({oracle.source})" if oracle.source else ""
            lines.append(f"- oracle: {oracle.text}{source}")
        if negative.next_action:
            lines.append(f"- next action: {negative.next_action}")
    if status.semantic_summary:
        semantic = status.semantic_summary
        lines.extend(
            [
                "",
                "Semantics:",
                f"- framework: {semantic.framework}",
            ]
        )
        if semantic.confidence is not None:
            lines.append(f"- confidence: {semantic.confidence:.2f}")
        if semantic.generation_method:
            lines.append(f"- generation method: {semantic.generation_method}")
        lines.extend(
            [
                f"- fields: {semantic.field_count}",
                f"- required fields: {semantic.required_field_count}",
                f"- validation rules: {semantic.validation_rule_count}",
                f"- success states: {semantic.success_state_count}",
                f"- data displays: {semantic.data_display_count}",
                f"- negative input cases: {semantic.negative_input_case_count}",
            ]
        )
        lines.extend(f"- display: {display}" for display in semantic.data_displays)
        lines.extend(f"- matched display: {display}" for display in semantic.matched_data_displays)
        lines.extend(f"- unmatched display: {display}" for display in semantic.unmatched_data_displays)
        lines.extend(f"- warning: {warning}" for warning in semantic.warnings)
    if status.generation_trace:
        lines.extend(["", "Generation Trace:"])
        lines.extend(f"- {item}" for item in status.generation_trace)
    if status.quality and (status.quality.gaps or status.quality.recommendation):
        lines.extend(["", "Quality:"])
        lines.extend(f"- gap: {gap}" for gap in status.quality.gaps)
        lines.append(f"- data display assertions: {status.quality.data_display_assertions}")
        lines.append(f"- forbidden error assertions: {status.quality.forbidden_error_assertions}")
        lines.append(f"- text_from input references: {status.quality.text_from_input_references}")
        lines.extend(f"- invalid text_from: {reference}" for reference in status.quality.invalid_text_from_references)
        if status.quality.recommendation:
            lines.append(f"- recommendation: {status.quality.recommendation}")
    if status.failed_step:
        failed = status.failed_step
        lines.extend(["", "Failed step:", f"- id: {failed.id}", f"- action: {failed.action}"])
        if failed.expected:
            lines.append(f"- expected: {failed.expected}")
        if failed.actual:
            lines.append(f"- actual: {failed.actual}")
        if failed.fix_hint:
            lines.append(f"- fix hint: {failed.fix_hint}")
    if status.next_action:
        lines.extend(["", f"Next Action: {status.next_action}"])
    return "\n".join(lines)


def _optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
