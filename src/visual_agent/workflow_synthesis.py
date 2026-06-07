from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .context_ingestion import (
    GenerationContext,
    SubmitAction,
    UISemanticModel,
    data_display_matches_field_name,
    ingest_context,
    summarize_data_displays,
)
from .security import scrub_secrets
from .workflow import parse_workflow_file, workflow_from_dict
from .workflow_quality import WorkflowQualityScore, score_workflow_quality


LLM_SYSTEM_PROMPT = """You generate Visual Agent workflow YAML for local UI verification.

Return only valid YAML, without markdown fences or explanation.

Required schema:
schema_version: 1
min_runtime_version: "0.1.0"
name: snake_case_name
version: 1
description: "Human readable workflow description"
tags: [verification, fast]
visibility: private
author: ""
license: ""
steps:
  - id: observe_initial
    action: observe_browser
    url: "http://localhost:3000"
  - id: assert_browser_ready
    action: assert_browser_ready
    min_text_length: 1
    min_interactive: 1

Rules:
1. Start browser workflows with observe_browser.
2. Add assert_browser_ready after the first observation.
3. Use semantic targets over coordinates.
4. Use value_from: input.<field> for user input values. Never inline credentials or secrets.
5. Mark password/token/secret fields with sensitive: true.
6. End with meaningful wait_for or assert_text assertions based on actual code behavior.
"""


@dataclass(frozen=True)
class WorkflowGenerationResult:
    status: str
    workflow_name: str
    workflow_path: str | None
    workflow_yaml: str
    quality_score: WorkflowQualityScore
    semantic_model: UISemanticModel
    message: str
    warnings: tuple[str, ...] = ()
    generation_method: str = "static"
    inputs_path: str | None = None
    negative_input_cases: tuple[dict[str, Any], ...] = ()
    negative_workflow_path: str | None = None
    negative_workflow_yaml: str | None = None
    negative_workflow_ready: bool = False
    negative_workflow_reason: str = ""
    negative_workflow_reset_strategy: str = ""
    negative_oracles: tuple[dict[str, str], ...] = ()
    generation_trace: tuple[str, ...] = ()


def generate_workflow_from_context(
    *,
    ctx: GenerationContext,
    output_path: Path | None = None,
    dry_run: bool = False,
    model_id: str = "claude-haiku-4-5-20251001",
) -> WorkflowGenerationResult:
    model = ingest_context(ctx)
    workflow_name = _task_to_workflow_name(ctx.task_description)
    description = f"Auto-generated verification for: {ctx.task_description[:120]}"
    warnings = list(model.parse_warnings)
    generation_method = "static"
    if model.confidence >= 0.5:
        yaml_text = synthesize_workflow(model, workflow_name, description)
    else:
        try:
            yaml_text = synthesize_workflow_with_llm(model, ctx, workflow_name, description, model_id=model_id)
            generation_method = "llm"
        except Exception as exc:
            warnings.append(f"llm fallback unavailable: {type(exc).__name__}: {exc}")
            yaml_text = synthesize_workflow(model, workflow_name, description)
            generation_method = "static_fallback"
    quality = score_workflow_quality(yaml_text, model)
    negative_input_cases = tuple(build_negative_input_cases(model))
    negative_workflow_yaml = (
        synthesize_negative_workflow(model, workflow_name, description, negative_input_cases) if negative_input_cases else None
    )
    negative_workflow_ready, negative_workflow_reason = negative_workflow_readiness(negative_input_cases)
    negative_workflow_reset_strategy = "fresh_observe_per_case" if negative_input_cases else ""
    negative_oracles = tuple(build_negative_oracles(model))
    generation_trace = tuple(build_generation_trace(model, generation_method=generation_method, negative_input_case_count=len(negative_input_cases)))

    if dry_run:
        return WorkflowGenerationResult(
            status="success",
            workflow_name=workflow_name,
            workflow_path=None,
            workflow_yaml=yaml_text,
            quality_score=quality,
            semantic_model=model,
            message=f"Generated via {generation_method} (quality: {quality.total_score:.2f}).",
            warnings=tuple(warnings),
            generation_method=generation_method,
            inputs_path=None,
            negative_input_cases=negative_input_cases,
            negative_workflow_path=None,
            negative_workflow_yaml=negative_workflow_yaml,
            negative_workflow_ready=negative_workflow_ready,
            negative_workflow_reason=negative_workflow_reason,
            negative_workflow_reset_strategy=negative_workflow_reset_strategy,
            negative_oracles=negative_oracles,
            generation_trace=generation_trace,
        )

    saved_path = _save_workflow(yaml_text, workflow_name, Path(ctx.project_root), output_path)
    parse_workflow_file(saved_path)
    inputs_path = _save_inputs_example(model, saved_path)
    negative_workflow_path = _save_negative_workflow(negative_workflow_yaml, saved_path) if negative_workflow_yaml else None
    return WorkflowGenerationResult(
        status="success",
        workflow_name=workflow_name,
        workflow_path=str(saved_path),
        workflow_yaml=yaml_text,
        quality_score=quality,
        semantic_model=model,
        message=f"Saved to {saved_path} via {generation_method} (quality: {quality.total_score:.2f}).",
        warnings=tuple(warnings),
        generation_method=generation_method,
        inputs_path=str(inputs_path) if inputs_path else None,
        negative_input_cases=negative_input_cases,
        negative_workflow_path=str(negative_workflow_path) if negative_workflow_path else None,
        negative_workflow_yaml=negative_workflow_yaml,
        negative_workflow_ready=negative_workflow_ready,
        negative_workflow_reason=negative_workflow_reason,
        negative_workflow_reset_strategy=negative_workflow_reset_strategy,
        negative_oracles=negative_oracles,
        generation_trace=generation_trace,
    )


def synthesize_workflow(model: UISemanticModel, workflow_name: str, description: str) -> str:
    steps: list[dict[str, Any]] = [
        {
            "id": "observe_initial",
            "action": "observe_browser",
            "url": model.entry_url,
        },
        {
            "id": "assert_browser_ready",
            "action": "assert_browser_ready",
            "min_text_length": 1,
            "min_interactive": 1,
        },
    ]
    if model.page_title:
        steps.append({"id": "assert_page_title_text", "action": "assert_text", "text": model.page_title})

    for field in model.form_fields:
        step: dict[str, Any] = {
            "id": f"fill_{_slug(field.name)}",
            "action": "paste",
            "target": {"label": field.label, "role": "input"},
            "value_from": f"input.{field.name}",
        }
        if field.is_sensitive:
            step["sensitive"] = True
        steps.append(step)

    submit_actions = _submit_actions_to_click(model)
    if submit_actions:
        for index, submit in enumerate(submit_actions, start=1):
            steps.append(
                {
                    "id": "click_submit" if index == 1 else f"click_confirm_{index}",
                    "action": "click",
                    "target": _submit_target(submit),
                    "wait_after_seconds": 0.5,
                    "browser_post_action_observe": True,
                }
            )
        for index, state in enumerate(model.success_states, start=1):
            if state.kind == "url_redirect":
                steps.append(
                    {
                        "id": f"wait_success_url_{index}",
                        "action": "wait_for",
                        "condition": "url",
                        "url_contains": state.value,
                        "timeout_seconds": 5,
                    }
                )
            else:
                steps.append(
                    {
                        "id": f"wait_success_text_{index}",
                        "action": "wait_for",
                        "condition": "text",
                        "text": state.value,
                        "timeout_seconds": 5,
                    }
                )
        steps.append({"id": "observe_after_submit", "action": "observe_browser", "reuse_page": True, "screenshot_label": "after-submit"})
        for index, state in enumerate(model.success_states, start=1):
            if state.kind == "text":
                steps.append({"id": f"assert_success_text_{index}", "action": "assert_text", "text": state.value})
        for field in _displayed_input_fields(model):
            steps.append(
                {
                    "id": f"assert_displayed_{_slug(field.name)}",
                    "action": "assert_text",
                    "text_from": f"input.{field.name}",
                }
            )
        if model.error_states:
            steps.append(
                {
                    "id": "assert_known_errors_absent",
                    "action": "assert_text_contract",
                    "forbidden_any": [state.text for state in model.error_states[:5]],
                }
            )

    if not model.success_states:
        steps.append({"id": "assert_no_error", "action": "assert_no_error"})
    workflow = {
        "schema_version": 1,
        "min_runtime_version": "0.1.0",
        "name": workflow_name,
        "version": 1,
        "description": description,
        "tags": ["verification", "fast"],
        "visibility": "private",
        "author": "",
        "license": "",
        "steps": steps,
    }
    return _dump_yaml(workflow)


def _displayed_input_fields(model: UISemanticModel) -> tuple[Any, ...]:
    fields: list[Any] = []
    for field in model.form_fields:
        if field.is_sensitive:
            continue
        if any(data_display_matches_field_name(display, field.name) for display in model.data_displays):
            fields.append(field)
    return tuple(fields)


def _submit_actions_to_click(model: UISemanticModel) -> tuple[SubmitAction, ...]:
    if not model.submit_actions:
        return ()
    first = model.submit_actions[0]
    actions = [first]
    if _is_destructive_action(first.text):
        confirm = next((action for action in model.submit_actions[1:] if _is_confirm_action(action.text)), None)
        if confirm:
            actions.append(confirm)
    return tuple(actions)


def _submit_target(submit: SubmitAction) -> dict[str, Any]:
    return {"selector": submit.selector} if submit.selector else {"text": submit.text, "role": "button"}


def _is_destructive_action(text: str) -> bool:
    lower = text.lower()
    return any(keyword in lower for keyword in ("delete", "remove", "archive"))


def _is_confirm_action(text: str) -> bool:
    lower = text.lower()
    return any(keyword in lower for keyword in ("confirm", "delete", "remove", "archive", "确认", "删除"))


def synthesize_negative_workflow(
    model: UISemanticModel,
    workflow_name: str,
    description: str,
    cases: tuple[dict[str, Any], ...],
) -> str:
    steps: list[dict[str, Any]] = []
    submit = model.submit_actions[0] if model.submit_actions else None
    target = None
    if submit:
        target = {"selector": submit.selector} if submit.selector else {"text": submit.text, "role": "button"}
    for case in cases:
        case_id = _slug(str(case.get("id") or "negative_case"))
        inputs = case.get("inputs") if isinstance(case.get("inputs"), dict) else {}
        steps.extend(
            [
                {
                    "id": f"observe_{case_id}",
                    "action": "observe_browser",
                    "url": model.entry_url,
                },
                {
                    "id": f"ready_{case_id}",
                    "action": "assert_browser_ready",
                    "min_text_length": 1,
                    "min_interactive": 1,
                },
            ]
        )
        for field in model.form_fields:
            step: dict[str, Any] = {
                "id": f"fill_{case_id}_{_slug(field.name)}",
                "action": "paste",
                "target": {"label": field.label, "role": "input"},
                "value": str(inputs.get(field.name, "")),
            }
            if field.is_sensitive:
                step["sensitive"] = True
            steps.append(step)
        if target:
            steps.append(
                {
                    "id": f"submit_{case_id}",
                    "action": "click",
                    "target": target,
                    "wait_after_seconds": 0.5,
                    "browser_post_action_observe": True,
                }
            )
            steps.append({"id": f"observe_after_{case_id}", "action": "observe_browser", "reuse_page": True})
        expected = case.get("expected_error_texts") if isinstance(case.get("expected_error_texts"), list) else []
        if expected:
            steps.append(
                {
                    "id": f"assert_error_{case_id}",
                    "action": "assert_text_contract",
                    "required_any": [str(item) for item in expected[:5]],
                }
            )

    workflow = {
        "schema_version": 1,
        "min_runtime_version": "0.1.0",
        "name": f"{workflow_name}_negative_draft",
        "version": 1,
        "description": f"Draft negative validation workflow for: {description}",
        "tags": ["verification", "negative", "draft"],
        "affects": ["negative-validation-draft"],
        "visibility": "private",
        "author": "",
        "license": "",
        "metadata": {
            "draft_only": True,
            "reset_strategy": "fresh_observe_per_case",
        },
        "steps": steps,
    }
    return _dump_yaml(workflow)


def negative_workflow_readiness(cases: tuple[dict[str, Any], ...]) -> tuple[bool, str]:
    if not cases:
        return False, "no_negative_cases"
    if not any(case.get("expected_error_texts") for case in cases):
        return False, "no_negative_oracle"
    return True, "ready"


def build_negative_oracles(model: UISemanticModel) -> list[dict[str, str]]:
    return [
        {"text": str(scrub_secrets(state.text)), "source": str(scrub_secrets(state.source))}
        for state in model.error_states[:5]
    ]


def build_generation_trace(
    model: UISemanticModel,
    *,
    generation_method: str,
    negative_input_case_count: int = 0,
    max_items: int = 10,
) -> list[str]:
    trace: list[str] = [f"generation method -> {generation_method}"]
    if generation_method == "llm":
        trace.append("low static confidence -> llm workflow generation")
    elif generation_method == "static_fallback":
        trace.append("low static confidence -> static fallback workflow")

    for field in model.form_fields:
        sensitive = " sensitive" if field.is_sensitive else ""
        trace.append(f"field {field.name} -> paste input.{field.name}{sensitive}")
        if len(trace) >= max_items:
            return trace[:max_items]

    for submit in _submit_actions_to_click(model):
        trace.append(f"submit {submit.text} -> click")
        if len(trace) >= max_items:
            return trace[:max_items]

    for state in model.success_states:
        if state.kind == "url_redirect":
            trace.append(f"success url {state.value} -> wait_for url")
        else:
            trace.append(f"success text {state.value} -> wait_for/assert_text")
        if len(trace) >= max_items:
            return trace[:max_items]

    if model.error_states:
        trace.append("known error texts -> forbidden_any")

    for field in _displayed_input_fields(model):
        trace.append(f"display {field.name} -> assert_text text_from input.{field.name}")
        if len(trace) >= max_items:
            return trace[:max_items]

    display_summary = summarize_data_displays(model)
    for display in display_summary.unmatched:
        trace.append(f"display {display} -> semantic_summary only")
        if len(trace) >= max_items:
            return trace[:max_items]

    if negative_input_case_count:
        trace.append(f"validation rules -> {negative_input_case_count} draft negative_input_cases")

    return trace[:max_items]


def synthesize_workflow_with_llm(
    model: UISemanticModel,
    ctx: GenerationContext,
    workflow_name: str,
    description: str,
    *,
    model_id: str,
) -> str:
    yaml_text = _generate_with_anthropic(model, ctx, workflow_name, description, model_id=model_id)
    yaml_text = _strip_markdown_fences(yaml_text)
    _validate_generated_yaml(yaml_text)
    return yaml_text


def _generate_with_anthropic(
    model: UISemanticModel,
    ctx: GenerationContext,
    workflow_name: str,
    description: str,
    *,
    model_id: str,
) -> str:
    import anthropic

    client = anthropic.Anthropic()
    code_summary = "\n\n".join(
        f"=== {change.file_path} ({change.change_type}) ===\n{change.after[:2000]}"
        for change in ctx.code_changes
        if change.change_type != "deleted"
    )
    semantic_summary = {
        "entry_url": model.entry_url,
        "framework": model.framework,
        "confidence": model.confidence,
        "fields": [
            {
                "name": field.name,
                "type": field.field_type,
                "required": field.required,
                "sensitive": field.is_sensitive,
                "validation_rules": list(field.validation_rules),
            }
            for field in model.form_fields
        ],
        "submit_actions": [action.text for action in model.submit_actions],
        "success_states": [state.value for state in model.success_states],
        "error_states": [state.text for state in model.error_states],
        "data_displays": list(model.data_displays),
    }
    prompt = (
        f"Task: {ctx.task_description}\n\n"
        f"Workflow name: {workflow_name}\n"
        f"Description: {description}\n\n"
        f"Static semantic summary:\n{json.dumps(semantic_summary, ensure_ascii=False, indent=2)}\n\n"
        f"Changed code:\n{code_summary}\n\n"
        "Generate one Visual Agent workflow YAML that verifies the task. "
        "Use value_from: input.<field> for user-entered values and never hardcode credentials."
    )
    message = client.messages.create(
        model=model_id,
        max_tokens=1800,
        system=LLM_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    content = getattr(message, "content", [])
    if not content:
        raise RuntimeError("Anthropic returned an empty response.")
    first = content[0]
    text = getattr(first, "text", None)
    if text is None and isinstance(first, dict):
        text = first.get("text")
    if not text:
        raise RuntimeError("Anthropic response did not contain text.")
    return str(text)


def _save_workflow(yaml_text: str, workflow_name: str, project_root: Path, output_path: Path | None) -> Path:
    path = output_path or (project_root / "workflows" / f"{workflow_name}.yaml")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml_text.rstrip() + "\n", encoding="utf-8")
    return path


def _save_inputs_example(model: UISemanticModel, workflow_path: Path) -> Path | None:
    if not model.form_fields:
        return None
    workspace_root = workflow_path.parent.parent
    path = workspace_root / "inputs" / f"{workflow_path.stem}_inputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {field.name: input_example_value(field) for field in model.form_fields}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _save_negative_workflow(yaml_text: str | None, workflow_path: Path) -> Path | None:
    if not yaml_text:
        return None
    path = workflow_path.with_name(f"{workflow_path.stem}_negative_draft.yaml")
    path.write_text(yaml_text.rstrip() + "\n", encoding="utf-8")
    parse_workflow_file(path)
    return path


def input_example_value(field: Any) -> str:
    name = str(getattr(field, "name", "") or "").lower()
    label = str(getattr(field, "label", "") or "").lower()
    field_type = str(getattr(field, "field_type", "") or "").lower()
    rules = tuple(str(rule) for rule in (getattr(field, "validation_rules", ()) or ()))
    text = f"{name} {label}"
    if bool(getattr(field, "is_sensitive", False)) or any(
        keyword in text for keyword in ("password", "passwd", "secret", "token", "api_key", "apikey", "key")
    ):
        return ""
    if field_type == "email" or "email" in text or "mail" in text:
        return "demo@example.com"
    if field_type == "tel" or any(keyword in text for keyword in ("phone", "mobile", "tel")):
        return "15500000000"
    if field_type == "number" or any(keyword in text for keyword in ("count", "quantity", "amount", "price", "age")):
        return constrained_number_example(rules)
    if field_type in {"date", "datetime-local"} or "date" in text:
        return "2026-01-01"
    if any(keyword in text for keyword in ("user", "username", "account")):
        return constrained_text_example("demo_user", rules)
    if "name" in text:
        return constrained_text_example("Demo User", rules)
    if any(keyword in text for keyword in ("search", "query", "keyword")):
        return constrained_text_example("demo", rules)
    return constrained_text_example(pattern_example(rules) or "demo", rules)


def build_negative_input_cases(model: UISemanticModel, *, max_cases: int = 8) -> list[dict[str, Any]]:
    base_inputs = {field.name: input_example_value(field) for field in model.form_fields}
    error_texts = [state.text for state in model.error_states[:5]]
    oracle_sources = [state.source for state in model.error_states[:5]]
    cases: list[dict[str, Any]] = []
    for field in model.form_fields:
        for rule in field.validation_rules:
            invalid_value = invalid_input_example_value(field, rule)
            if invalid_value is None:
                continue
            inputs = dict(base_inputs)
            inputs[field.name] = invalid_value
            rule_name = rule.split(":", 1)[0]
            cases.append(
                {
                    "id": f"invalid_{_slug(field.name)}_{_slug(rule_name)}",
                    "field": field.name,
                    "rule": rule,
                    "inputs": inputs,
                    "expected_error_texts": error_texts,
                    "oracle_sources": oracle_sources,
                    "mode": "draft_only",
                    "description": f"Draft negative validation case for {field.name} ({rule}).",
                }
            )
            if len(cases) >= max_cases:
                return cases
    return cases


def invalid_input_example_value(field: Any, rule: str) -> str | None:
    field_type = str(getattr(field, "field_type", "") or "").lower()
    rule_name, _, rule_value = rule.partition(":")
    if bool(getattr(field, "is_sensitive", False)) and rule_name != "required":
        return ""
    if rule_name == "required":
        return ""
    if rule_name == "email_format" or field_type == "email":
        return "not-an-email"
    if rule_name == "min_length":
        try:
            length = max(0, int(rule_value) - 1)
        except ValueError:
            return None
        return "a" * length
    if rule_name == "max_length":
        try:
            length = int(rule_value) + 1
        except ValueError:
            return None
        return "a" * max(1, length)
    if rule_name == "min":
        minimum = _numeric_rule_value((rule,), "min")
        if minimum is None:
            return None
        value = minimum - 1
        return str(int(value) if float(value).is_integer() else value)
    if rule_name == "max":
        maximum = _numeric_rule_value((rule,), "max")
        if maximum is None:
            return None
        value = maximum + 1
        return str(int(value) if float(value).is_integer() else value)
    if rule_name == "pattern":
        return "invalid"
    return None


def constrained_number_example(rules: tuple[str, ...]) -> str:
    minimum = _numeric_rule_value(rules, "min")
    maximum = _numeric_rule_value(rules, "max")
    value = minimum if minimum is not None else 1
    if maximum is not None and value > maximum:
        value = maximum
    return str(int(value) if float(value).is_integer() else value)


def constrained_text_example(base: str, rules: tuple[str, ...]) -> str:
    example = pattern_example(rules) or base
    min_length = _integer_rule_value(rules, "min_length")
    max_length = _integer_rule_value(rules, "max_length")
    if min_length is not None and len(example) < min_length:
        fill = "a" if example else "demo"
        example = example + (fill * (min_length - len(example)))
    if max_length is not None and len(example) > max_length:
        example = example[:max_length]
    return example or "demo"


def pattern_example(rules: tuple[str, ...]) -> str | None:
    pattern = _string_rule_value(rules, "pattern")
    if not pattern:
        return None
    digit_length = re.fullmatch(r"\^?\\d\{(\d+)\}\$?", pattern)
    if digit_length:
        return "1" * int(digit_length.group(1))
    alpha_length = re.fullmatch(r"\^?\[A-Za-z\]\{(\d+)\}\$?", pattern)
    if alpha_length:
        return "A" * int(alpha_length.group(1))
    alnum_length = re.fullmatch(r"\^?\[A-Za-z0-9\]\{(\d+)\}\$?", pattern)
    if alnum_length:
        return "A" * int(alnum_length.group(1))
    return None


def _integer_rule_value(rules: tuple[str, ...], name: str) -> int | None:
    value = _string_rule_value(rules, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _numeric_rule_value(rules: tuple[str, ...], name: str) -> float | None:
    value = _string_rule_value(rules, name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _string_rule_value(rules: tuple[str, ...], name: str) -> str | None:
    prefix = f"{name}:"
    for rule in rules:
        if rule.startswith(prefix):
            return rule[len(prefix) :]
    return None


def _task_to_workflow_name(description: str) -> str:
    words = re.findall(r"[a-zA-Z0-9]+", description.lower())
    if not words:
        words = ["generated", "workflow"]
    base = "_".join(words[:7])[:64].strip("_")
    if not base:
        base = "generated_workflow"
    if not base.endswith("verification"):
        base = f"{base}_verification"
    return base


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return text or "field"


def _dump_yaml(payload: dict[str, Any]) -> str:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for workflow synthesis.") from exc
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def _strip_markdown_fences(text: str) -> str:
    stripped = textwrap.dedent(text).strip()
    stripped = re.sub(r"^```(?:yaml|yml)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _validate_generated_yaml(yaml_text: str) -> None:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for workflow synthesis.") from exc
    payload = yaml.safe_load(yaml_text)
    if not isinstance(payload, dict):
        raise ValueError("YAML root must be an object.")
    workflow_from_dict(payload)
