from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .context_ingestion import UISemanticModel


ASSERTION_ACTIONS = frozenset({"wait_for", "assert_browser_ready", "assert_text", "assert_text_contract", "assert_no_error", "wait_for_text"})
STRUCTURAL_ACTIONS = frozenset({"assert_browser_ready"})


@dataclass(frozen=True)
class WorkflowQualityScore:
    total_score: float
    assertion_density: float
    business_assertion_count: int
    structural_assertion_count: int
    data_display_assertion_count: int
    forbidden_error_assertion_count: int
    text_from_input_reference_count: int
    invalid_text_from_references: tuple[str, ...]
    covers_success_path: bool
    covers_error_path: bool
    covers_data_display: bool
    gaps: tuple[str, ...]
    recommendation: str


def score_workflow_quality(workflow_yaml: str, model: UISemanticModel | None = None) -> WorkflowQualityScore:
    try:
        import yaml

        doc = yaml.safe_load(workflow_yaml)
    except Exception:
        return _zero_score("failed to parse workflow YAML")
    if not isinstance(doc, dict):
        return _zero_score("workflow YAML root is not an object")
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        return _zero_score("workflow has no steps")

    total_steps = len(steps)
    dict_steps = [step for step in steps if isinstance(step, dict)]
    assertion_steps = [step for step in dict_steps if step.get("action") in ASSERTION_ACTIONS]
    business_assertions = [
        step
        for step in assertion_steps
        if step.get("action") not in STRUCTURAL_ACTIONS
        and any(step.get(key) for key in ("text", "text_from", "url_contains", "url_contains_from", "required_all", "required_any", "forbidden_any"))
    ]
    structural_assertions = [step for step in assertion_steps if step.get("action") in STRUCTURAL_ACTIONS]
    data_display_assertion_count = sum(1 for step in dict_steps if _step_has_valid_text_from_input(step, model))
    forbidden_error_assertion_count = sum(1 for step in dict_steps if step.get("action") == "assert_text_contract" and step.get("forbidden_any"))
    text_from_references = _text_from_references(dict_steps)
    invalid_text_from_references = _invalid_text_from_references(text_from_references, model)
    text_from_input_reference_count = len(text_from_references) - len(invalid_text_from_references)
    assertion_density = len(assertion_steps) / total_steps
    covers_success = any(_step_covers_success(step) for step in dict_steps)
    error_coverage_strength = _error_coverage_strength(dict_steps)
    covers_error = error_coverage_strength > 0
    covers_data = any(_step_covers_data_display(step, model) for step in dict_steps)
    model_coverage = _model_success_coverage(dict_steps, model)

    score = (
        min(assertion_density / 0.3, 1.0) * 0.30
        + min(len(business_assertions) / max(total_steps * 0.2, 1), 1.0) * 0.30
        + (1.0 if covers_success else 0.0) * 0.20
        + error_coverage_strength * 0.10
        + model_coverage * 0.10
    )
    gaps: list[str] = []
    if not covers_success:
        gaps.append("no success state assertion")
    if not covers_error:
        gaps.append("no error path covered")
    if assertion_density < 0.2:
        gaps.append(f"low assertion density ({assertion_density:.0%})")
    if not business_assertions:
        gaps.append("no business assertions")
    if model and model.success_states and model_coverage < 0.5:
        gaps.append(f"only {model_coverage:.0%} of expected success states are verified")
    if invalid_text_from_references:
        gaps.append("text_from references missing input fields: " + ", ".join(invalid_text_from_references[:3]))

    return WorkflowQualityScore(
        total_score=round(min(score, 1.0), 3),
        assertion_density=round(assertion_density, 3),
        business_assertion_count=len(business_assertions),
        structural_assertion_count=len(structural_assertions),
        data_display_assertion_count=data_display_assertion_count,
        forbidden_error_assertion_count=forbidden_error_assertion_count,
        text_from_input_reference_count=text_from_input_reference_count,
        invalid_text_from_references=tuple(invalid_text_from_references),
        covers_success_path=covers_success,
        covers_error_path=covers_error,
        covers_data_display=covers_data,
        gaps=tuple(gaps),
        recommendation=_build_recommendation(gaps),
    )


def _step_covers_success(step: dict[str, Any]) -> bool:
    if step.get("action") not in {"wait_for", "assert_text"}:
        return False
    return any(step.get(key) for key in ("text", "text_from", "url_contains", "url_contains_from"))


def _step_covers_data_display(step: dict[str, Any], model: UISemanticModel | None) -> bool:
    if step.get("action") not in {"wait_for", "assert_text", "assert_text_contract"}:
        return False
    if step.get("text_from"):
        if model is None or not model.form_fields:
            return True
        reference = str(step.get("text_from") or "")
        field_name = reference.removeprefix("input.") if reference.startswith("input.") else reference
        return any(field.name == field_name and not field.is_sensitive for field in model.form_fields)
    if model is None or not model.data_displays:
        return bool(step.get("text"))
    display_names = {display.lower().split(".")[-1] for display in model.data_displays}
    text = str(step).lower()
    return any(name and name in text for name in display_names)


def _step_has_valid_text_from_input(step: dict[str, Any], model: UISemanticModel | None) -> bool:
    if step.get("action") not in {"wait_for", "assert_text", "assert_text_contract"}:
        return False
    reference = step.get("text_from")
    if not isinstance(reference, str) or not reference.startswith("input."):
        return False
    if model is None or not model.form_fields:
        return True
    return reference in {f"input.{field.name}" for field in model.form_fields if not field.is_sensitive}


def _step_covers_error(step: dict[str, Any]) -> bool:
    if step.get("action") == "assert_no_error":
        return True
    if step.get("action") == "assert_text_contract" and step.get("forbidden_any"):
        return True
    text = str(step).lower()
    return any(keyword in text for keyword in ("error", "failed", "invalid", "required", "错误", "失败", "无效", "必填"))


def _error_coverage_strength(steps: list[dict[str, Any]]) -> float:
    if any(step.get("action") == "assert_text_contract" and step.get("forbidden_any") for step in steps):
        return 1.0
    if any(_step_has_explicit_error_text(step) for step in steps):
        return 1.0
    if any(step.get("action") == "assert_no_error" for step in steps):
        return 0.5
    return 0.0


def _step_has_explicit_error_text(step: dict[str, Any]) -> bool:
    if step.get("action") not in {"wait_for", "assert_text", "wait_for_text"}:
        return False
    return any(keyword in str(step).lower() for keyword in ("error", "failed", "invalid", "required", "错误", "失败", "无效", "必填"))


def _text_from_references(steps: list[dict[str, Any]]) -> list[str]:
    references: list[str] = []
    for step in steps:
        value = step.get("text_from")
        if isinstance(value, str) and value:
            references.append(value)
    return references


def _invalid_text_from_references(references: list[str], model: UISemanticModel | None) -> list[str]:
    if model is None or not model.form_fields:
        return []
    allowed = {f"input.{field.name}" for field in model.form_fields if not field.is_sensitive}
    return [reference for reference in references if reference not in allowed]


def _model_success_coverage(steps: list[dict[str, Any]], model: UISemanticModel | None) -> float:
    if model is None or not model.success_states:
        return 1.0
    covered = 0
    for state in model.success_states:
        if any(state.value and state.value in str(step) for step in steps):
            covered += 1
    return covered / len(model.success_states)


def _build_recommendation(gaps: list[str]) -> str:
    if not gaps:
        return "Workflow quality is good."
    if "success state" in gaps[0]:
        return "Add a wait_for or assert_text step after submit that verifies the expected result."
    if "error path" in gaps[0]:
        return "Add an assertion for a visible error message or invalid-input state."
    if "assertion density" in gaps[0]:
        return "Add assertions after the main UI actions."
    return f"Address: {gaps[0]}"


def _zero_score(reason: str) -> WorkflowQualityScore:
    return WorkflowQualityScore(
        total_score=0.0,
        assertion_density=0.0,
        business_assertion_count=0,
        structural_assertion_count=0,
        data_display_assertion_count=0,
        forbidden_error_assertion_count=0,
        text_from_input_reference_count=0,
        invalid_text_from_references=(),
        covers_success_path=False,
        covers_error_path=False,
        covers_data_display=False,
        gaps=(reason,),
        recommendation=reason,
    )
