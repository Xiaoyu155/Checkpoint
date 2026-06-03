from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ProviderKind
from .workflow import RUNTIME_VERSION, SUPPORTED_WORKFLOW_SCHEMA_VERSION, Workflow, parse_workflow_file, target_from_config


SUPPORTED_ACTIONS = {
    "observe_screen",
    "observe_browser",
    "observe_dom",
    "observe_uia",
    "observe_ocr",
    "observe_vision",
    "observe_fixture",
    "observe_html",
    "resolve",
    "click",
    "type",
    "paste",
    "assert_text",
    "assert_text_contract",
    "assert_response",
    "expect_download",
    "assert_file_exists",
    "save_storage_state",
    "wait_for",
}

REQUIRED_PARAMS = {
    "observe_dom": ("url",),
    "observe_browser": ("url",),
    "observe_fixture": ("path",),
    "observe_html": ("path",),
    "resolve": ("target",),
    "type": ("target",),
    "paste": ("target",),
    "expect_download": ("target",),
    "assert_text": ("text",),
    "wait_for": ("condition",),
}

ASSERTION_ACTIONS = {"assert_text", "assert_text_contract", "assert_response", "assert_file_exists"}
HIGH_RISK_ACTIONS = {"save_storage_state"}
MUTATING_ACTIONS = {"click", "type", "paste", "expect_download", "save_storage_state"}
SENSITIVE_NAME_HINTS = ("password", "passwd", "pwd", "token", "secret", "key", "cookie", "id_card", "ssn")


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    step_id: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    workflow_name: str
    issues: tuple[ValidationIssue, ...]


def validate_workflow_file(path: str | Path) -> ValidationResult:
    return validate_workflow(parse_workflow_file(path))


def validate_workflow_file_strict(path: str | Path, *, allow_high_risk: bool = False) -> ValidationResult:
    return validate_workflow(parse_workflow_file(path), strict=True, allow_high_risk=allow_high_risk)


def validate_workflow(
    workflow: Workflow,
    *,
    strict: bool = False,
    allow_high_risk: bool = False,
) -> ValidationResult:
    issues: list[ValidationIssue] = []
    seen_ids: set[str] = set()
    has_observation = False
    has_resolved_target = False
    has_assertion = False

    validate_workflow_schema(workflow, issues, strict=strict)

    for step in workflow.steps:
        if step.id in seen_ids:
            issues.append(ValidationIssue("error", step.id, "Duplicate step id."))
        seen_ids.add(step.id)

        if step.action not in SUPPORTED_ACTIONS:
            issues.append(ValidationIssue("error", step.id, f"Unsupported action: {step.action}"))
            continue

        for param in REQUIRED_PARAMS.get(step.action, ()):
            if step.action == "observe_browser" and param == "url" and step.params.get("reuse_page") is True:
                continue
            if step.action == "wait_for" and param == "condition" and step.params.get("conditions"):
                continue
            if missing_param(step.params, param):
                issues.append(ValidationIssue("error", step.id, f"Missing required parameter: {param}"))

        if step.action.startswith("observe_"):
            has_observation = True
        if step.action in ASSERTION_ACTIONS:
            has_assertion = True

        if step.action == "resolve":
            has_resolved_target = True
        if step.action == "wait_for" and wait_for_has_target_condition(step.params):
            has_resolved_target = True

        if step.action in {"click", "type", "paste", "expect_download"} and "target" not in step.params and not has_resolved_target:
            issues.append(
                ValidationIssue(
                    "error",
                    step.id,
                    "Action requires a target or a previous resolve step.",
                )
            )

        if step.action in {
            "resolve",
            "click",
            "type",
            "paste",
            "expect_download",
            "assert_text",
            "assert_text_contract",
            "assert_response",
            "save_storage_state",
            "wait_for",
        } and not has_observation:
            issues.append(
                ValidationIssue(
                    "warning",
                    step.id,
                    "Step may require a previous observation.",
                )
            )

        validate_target_like_params(step.id, step.params, issues)
        validate_value_params(step.id, step.action, step.params, issues)
        validate_text_contract(step.id, step.action, step.params, issues)
        validate_wait_for(step.id, step.action, step.params, issues)
        validate_retry_safety(step.id, step.action, step.params, issues)
        if strict:
            validate_strict_step(step.id, step.action, step.params, issues, allow_high_risk=allow_high_risk)

    if strict:
        validate_strict_workflow(has_observation, has_assertion, issues)

    has_errors = any(issue.level == "error" for issue in issues)
    return ValidationResult(valid=not has_errors, workflow_name=workflow.name, issues=tuple(issues))


def validate_target_like_params(step_id: str, params: dict[str, Any], issues: list[ValidationIssue]) -> None:
    target = params.get("target")
    if target is None:
        return
    try:
        parsed = target_from_config(target)
    except Exception as exc:
        issues.append(ValidationIssue("error", step_id, f"Invalid target: {exc}"))
        return

    if not any(
        [
            parsed.text,
            parsed.label,
            parsed.role,
            parsed.selector,
            parsed.test_id,
            parsed.contains_text,
            parsed.text_regex,
            parsed.row_text,
            parsed.row_contains_text,
            parsed.row_text_regex,
            parsed.column_header,
            parsed.column_contains_text,
            parsed.column_text_regex,
        ]
    ):
        issues.append(
            ValidationIssue(
                "warning",
                step_id,
                "Target has no text, label, role, selector, test_id, contains_text, text_regex, row condition, or column condition.",
            )
        )

    for provider in parsed.preferred:
        if not isinstance(provider, ProviderKind):
            issues.append(ValidationIssue("error", step_id, f"Invalid preferred provider: {provider}"))


def validate_value_params(step_id: str, action: str, params: dict[str, Any], issues: list[ValidationIssue]) -> None:
    if action not in {"type", "paste"}:
        return
    has_value = "value" in params
    has_value_from = "value_from" in params
    if has_value and has_value_from:
        issues.append(ValidationIssue("error", step_id, "Use either value or value_from, not both."))
    if not has_value and not has_value_from:
        issues.append(ValidationIssue("error", step_id, "Missing required parameter: value or value_from"))
    if has_value_from and not str(params["value_from"]).startswith("input."):
        issues.append(ValidationIssue("error", step_id, "value_from must start with input."))


def validate_text_contract(step_id: str, action: str, params: dict[str, Any], issues: list[ValidationIssue]) -> None:
    if action != "assert_text_contract":
        return
    if not any(
        key in params
        for key in (
            "text",
            "text_from",
            "required_all",
            "required_all_from",
            "required_any",
            "required_any_from",
            "forbidden_any",
            "forbidden_any_from",
            "forbidden_text",
            "forbidden_text_from",
        )
    ):
        issues.append(
            ValidationIssue(
                "error",
                step_id,
                "assert_text_contract requires text, required_all, required_any, or forbidden_any.",
            )
        )


def missing_param(params: dict[str, Any], param: str) -> bool:
    if param in params and params[param] not in (None, ""):
        return False
    from_key = f"{param}_from"
    return from_key not in params or params[from_key] in (None, "")


def validate_wait_for(step_id: str, action: str, params: dict[str, Any], issues: list[ValidationIssue]) -> None:
    if action != "wait_for":
        return
    conditions = params.get("conditions")
    if conditions is not None:
        if not isinstance(conditions, list) or not conditions:
            issues.append(ValidationIssue("error", step_id, "wait_for conditions must be a non-empty list."))
            return
        for index, condition_params in enumerate(conditions):
            if not isinstance(condition_params, dict):
                issues.append(ValidationIssue("error", step_id, f"wait_for conditions[{index}] must be an object."))
                continue
            validate_wait_for_condition(step_id, condition_params, issues, suffix=f" conditions[{index}]")
        match = params.get("match") or params.get("mode")
        if match is not None and str(match) not in {"all", "any"}:
            issues.append(ValidationIssue("error", step_id, f"Unsupported wait_for match mode: {match}"))
        return
    validate_wait_for_condition(step_id, params, issues)


def validate_wait_for_condition(step_id: str, params: dict[str, Any], issues: list[ValidationIssue], *, suffix: str = "") -> None:
    condition = params.get("condition") or params.get("type")
    label = f"wait_for{suffix} condition"
    if condition == "text" and missing_param(params, "text"):
        issues.append(ValidationIssue("error", step_id, f"{label} text requires text."))
    elif condition == "target" and "target" not in params:
        issues.append(ValidationIssue("error", step_id, f"{label} target requires target."))
    elif condition == "selector" and missing_param(params, "selector"):
        issues.append(ValidationIssue("error", step_id, f"{label} selector requires selector."))
    elif condition == "url" and not any(key in params for key in ("url", "url_contains", "url_regex")):
        issues.append(ValidationIssue("error", step_id, f"{label} url requires url, url_contains, or url_regex."))
    elif condition == "response" and not any(key in params for key in ("url_contains", "method", "status", "status_min", "status_max", "ok")):
        issues.append(ValidationIssue("error", step_id, f"{label} response requires response match fields."))
    elif condition not in {"text", "target", "selector", "url", "response", None}:
        issues.append(ValidationIssue("error", step_id, f"Unsupported wait_for condition: {condition}"))


def wait_for_has_target_condition(params: dict[str, Any]) -> bool:
    if params.get("condition") == "target":
        return True
    conditions = params.get("conditions")
    if not isinstance(conditions, list):
        return False
    return any(isinstance(condition, dict) and (condition.get("condition") or condition.get("type")) == "target" for condition in conditions)


def validate_retry_safety(step_id: str, action: str, params: dict[str, Any], issues: list[ValidationIssue]) -> None:
    if "retry" not in params and "retry_delay_seconds" not in params:
        return
    if retry_count(params) <= 0:
        return
    if action.startswith("observe_") or action == "wait_for" or action in {"assert_text", "assert_text_contract", "assert_response", "assert_file_exists"}:
        return
    issues.append(
        ValidationIssue(
            "warning",
            step_id,
            "Automatic retry is disabled for mutating or unsafe actions; retry only applies to observe/wait/assert steps.",
        )
    )


def retry_count(params: dict[str, Any]) -> int:
    raw = params.get("retry", 0)
    if isinstance(raw, dict):
        return int(raw.get("count", 0) or 0)
    return int(raw or 0)


def validate_strict_workflow(has_observation: bool, has_assertion: bool, issues: list[ValidationIssue]) -> None:
    if not has_observation:
        issues.append(ValidationIssue("error", "workflow", "Strict mode requires at least one observation step."))
    if not has_assertion:
        issues.append(ValidationIssue("error", "workflow", "Strict mode requires at least one verification assertion step."))


def validate_strict_step(
    step_id: str,
    action: str,
    params: dict[str, Any],
    issues: list[ValidationIssue],
    *,
    allow_high_risk: bool,
) -> None:
    if action in HIGH_RISK_ACTIONS and not allow_high_risk and params.get("require_confirm") is not True:
        issues.append(
            ValidationIssue(
                "error",
                step_id,
                "Strict mode blocks high-risk action unless require_confirm: true or --allow-high-risk is used.",
            )
        )

    if action in MUTATING_ACTIONS and params.get("dry_run") is False:
        issues.append(
            ValidationIssue(
                "warning",
                step_id,
                "Strict mode recommends leaving step dry_run unset and controlling real execution with CLI approval.",
            )
        )

    if action in {"type", "paste"}:
        value_from = str(params.get("value_from") or "")
        value = str(params.get("value") or "")
        if is_sensitive_value_ref(value_from or value) and params.get("sensitive") is not True:
            issues.append(
                ValidationIssue(
                    "error",
                    step_id,
                    "Strict mode requires sensitive: true for password/token/secret-like input fields.",
                )
            )


def is_sensitive_value_ref(value: str) -> bool:
    normalized = value.lower()
    return any(hint in normalized for hint in SENSITIVE_NAME_HINTS)


def validate_workflow_schema(workflow: Workflow, issues: list[ValidationIssue], *, strict: bool) -> None:
    if workflow.schema_version is None:
        issues.append(
            ValidationIssue(
                "error" if strict else "warning",
                "workflow",
                "Workflow schema_version is missing; add schema_version: 1 for forward compatibility.",
            )
        )
    elif workflow.schema_version != SUPPORTED_WORKFLOW_SCHEMA_VERSION:
        issues.append(
            ValidationIssue(
                "error",
                "workflow",
                f"Unsupported workflow schema_version: {workflow.schema_version}. Supported: {SUPPORTED_WORKFLOW_SCHEMA_VERSION}.",
            )
        )

    if workflow.min_runtime_version and compare_versions(workflow.min_runtime_version, RUNTIME_VERSION) > 0:
        issues.append(
            ValidationIssue(
                "error",
                "workflow",
                f"Workflow requires runtime >= {workflow.min_runtime_version}, current runtime is {RUNTIME_VERSION}.",
            )
        )


def compare_versions(left: str, right: str) -> int:
    left_parts = version_parts(left)
    right_parts = version_parts(right)
    max_len = max(len(left_parts), len(right_parts))
    left_parts.extend([0] * (max_len - len(left_parts)))
    right_parts.extend([0] * (max_len - len(right_parts)))
    if left_parts > right_parts:
        return 1
    if left_parts < right_parts:
        return -1
    return 0


def version_parts(value: str) -> list[int]:
    parts = []
    for item in str(value).split("."):
        digits = "".join(char for char in item if char.isdigit())
        parts.append(int(digits or 0))
    return parts
