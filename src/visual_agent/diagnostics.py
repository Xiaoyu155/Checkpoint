from __future__ import annotations

from pathlib import Path
from typing import Any

from .dom import normalize_text
from .models import Observation, to_jsonable
from .ocr import observe_ocr
from .vlm import observe_vision
from .workflow_types import WorkflowContext


def diagnose_failure(
    *,
    step_id: str,
    action: str,
    params: dict[str, Any],
    error: Exception | None,
    context: WorkflowContext,
) -> dict[str, Any]:
    observation = context.observations.get(str(params.get("observation", ""))) or context.latest_observation_or_none
    screenshot = capture_failure_screenshot(step_id, context)
    ocr_evidence = ocr_failure_evidence(screenshot, context)
    vision_evidence = vision_failure_evidence(screenshot, context, action, params)
    visible_text = summarize_observation_text(observation)
    target = params.get("target")
    expected = expected_label(action, params)
    actual = actual_label(observation, visible_text, context)
    suggestions = recovery_suggestions(action, params, observation, context)
    selector_summary = failure_selector_summary(params, context)
    dom_excerpt = observation_dom_excerpt(observation)

    diagnosis = {
        "type": "deterministic_failure_diagnosis",
        "step_id": step_id,
        "action": action,
        "error": str(error) if error else "Step failed.",
        "expected": expected,
        "actual": actual,
        "target": to_jsonable(target) if target is not None else None,
        "observation": observation_summary(observation, visible_text),
        "dom_excerpt": dom_excerpt,
        "selector_summary": selector_summary,
        "artifacts": {"screenshot": str(screenshot) if screenshot else None},
        "evidence": {
            "ocr": ocr_evidence,
            "vision": vision_evidence,
        },
        "recovery_suggestions": suggestions,
        "model_prompt": build_reflection_prompt(expected, actual),
    }
    return diagnosis


def capture_failure_screenshot(step_id: str, context: WorkflowContext) -> Path | None:
    page = context.resources.get("playwright_page")
    if page is not None:
        path = context.run_dir / f"{step_id}_failure.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return path
        except Exception:
            return None

    observation = context.latest_observation_or_none
    if observation is not None and observation.screenshot_path is not None:
        return observation.screenshot_path
    return None


def summarize_observation_text(observation: Observation | None, *, limit: int = 12) -> list[str]:
    if observation is None:
        return []
    values: list[str] = []
    for element in observation.elements:
        text = normalize_text(element.get("text") or element.get("label") or element.get("name") or "")
        if text and text not in values:
            values.append(text)
        if len(values) >= limit:
            break
    return values


def ocr_failure_evidence(screenshot: Path | None, context: WorkflowContext) -> dict[str, Any]:
    if screenshot is None:
        return {"available": False, "reason": "no screenshot artifact"}
    if not screenshot.exists():
        return {"available": False, "reason": f"screenshot not found: {screenshot}"}
    try:
        observation = observe_ocr(
            {"path": str(screenshot), "engine": "auto"},
            context.run_dir,
            synthetic_on_capture_fail=False,
        )
    except Exception as exc:
        return {"available": False, "reason": str(exc)}

    texts = summarize_observation_text(observation, limit=20)
    return {
        "available": True,
        "engine": observation.metadata.get("engine"),
        "engine_available": observation.metadata.get("engine_available"),
        "source": observation.source,
        "element_count": len(observation.elements),
        "visible_text": texts,
        "install_hint": observation.metadata.get("install_hint"),
    }


def vision_failure_evidence(
    screenshot: Path | None,
    context: WorkflowContext,
    action: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    if screenshot is None:
        return {"available": False, "reason": "no screenshot artifact"}
    if not screenshot.exists():
        return {"available": False, "reason": f"screenshot not found: {screenshot}"}
    prompt = build_reflection_prompt(expected_label(action, params), f"screenshot={screenshot}")
    try:
        observation = observe_vision(
            {"path": str(screenshot), "engine": "auto", "prompt": prompt},
            context.run_dir,
            synthetic_on_capture_fail=False,
        )
    except Exception as exc:
        return {"available": False, "reason": str(exc)}

    return {
        "available": True,
        "engine": observation.metadata.get("engine"),
        "engine_available": observation.metadata.get("engine_available"),
        "source": observation.source,
        "status": observation.metadata.get("status"),
        "description": observation.metadata.get("description"),
        "install_hint": observation.metadata.get("install_hint"),
        "fallback_chain": observation.metadata.get("fallback_chain"),
    }


def expected_label(action: str, params: dict[str, Any]) -> str:
    if action == "assert_text":
        return f"expected text: {params.get('text')}"
    if action == "assert_response":
        parts = []
        for key in ("url_contains", "method", "status", "status_min", "status_max", "ok"):
            if key in params:
                parts.append(f"{key}={params[key]}")
        return "expected network response: " + (", ".join(parts) if parts else "any response")
    if action == "assert_file_exists":
        return f"expected file: {params.get('path') or params.get('from_download') or 'latest download'}"
    if action == "wait_for":
        return "expected wait_for " + wait_for_expected_label(params)
    if "target" in params:
        return f"expected target: {params.get('target')}"
    return f"expected action to succeed: {action}"


def wait_for_expected_label(params: dict[str, Any]) -> str:
    conditions = params.get("conditions")
    if isinstance(conditions, list) and conditions:
        labels = [wait_condition_expected_label(condition) for condition in conditions if isinstance(condition, dict)]
        match = params.get("match") or params.get("mode") or "all"
        return f"{match}: " + "; ".join(labels)
    if params.get("condition") == "text":
        return f"text: {params.get('text')}"
    if params.get("condition") == "target":
        return f"target: {params.get('target')}"
    return wait_condition_expected_label(params)


def wait_condition_expected_label(condition: dict[str, Any]) -> str:
    condition_type = condition.get("condition") or condition.get("type")
    if condition_type == "text":
        return f"text={condition.get('text')}"
    if condition_type == "target":
        return f"target={condition.get('target')}"
    if condition_type == "selector":
        return f"selector={condition.get('selector')}"
    if condition_type == "url":
        return "url=" + str(condition.get("url") or condition.get("url_contains") or condition.get("url_regex"))
    if condition_type == "response":
        parts = []
        for key in ("url_contains", "method", "status", "status_min", "status_max", "ok"):
            if key in condition:
                parts.append(f"{key}={condition[key]}")
        return "response=" + (", ".join(parts) if parts else "any response")
    return f"{condition_type}: {condition.get('text') or condition.get('target')}"


def actual_label(observation: Observation | None, visible_text: list[str], context: WorkflowContext) -> str:
    if observation is None:
        return "no observation is available"
    parts = [
        f"provider={observation.provider.value}",
        f"source={observation.source}",
        f"elements={len(observation.elements)}",
    ]
    if visible_text:
        parts.append("visible_text=" + " | ".join(visible_text[:5]))
    events = context.resources.get("network_events")
    if isinstance(events, list) and events:
        latest = events[-1]
        parts.append(f"latest_network={latest.get('method')} {latest.get('url')} status={latest.get('status')}")
    return "; ".join(parts)


def observation_summary(observation: Observation | None, visible_text: list[str]) -> dict[str, Any]:
    if observation is None:
        return {"available": False}
    return {
        "available": True,
        "provider": observation.provider.value,
        "source": observation.source,
        "width": observation.width,
        "height": observation.height,
        "element_count": len(observation.elements),
        "visible_text": visible_text,
    }


def observation_dom_excerpt(observation: Observation | None, *, limit: int = 8) -> list[dict[str, Any]]:
    if observation is None:
        return []
    excerpt: list[dict[str, Any]] = []
    for index, element in enumerate(observation.elements[:limit]):
        if not isinstance(element, dict):
            continue
        excerpt.append(
            {
                "index": element.get("index", index),
                "role": element.get("role") or element.get("tag") or element.get("control_type"),
                "text": element.get("text") or element.get("label") or element.get("name"),
                "selector": element.get("selector"),
                "test_id": element.get("test_id"),
                "bounds": element.get("bounds"),
            }
        )
    return excerpt


def failure_selector_summary(params: dict[str, Any], context: WorkflowContext) -> dict[str, Any] | None:
    target = params.get("target")
    latest = context.latest_resolved_target_or_none
    summary: dict[str, Any] = {}
    if target is not None:
        summary["target"] = to_jsonable(target)
    if latest is not None:
        summary["latest_resolved_target"] = latest.target.display_name
        summary["provider"] = latest.evidence.provider.value
        summary["confidence"] = latest.evidence.confidence
        summary["handle"] = latest.evidence.handle
        resolution = latest.evidence.metadata.get("selector_resolution")
        if isinstance(resolution, dict):
            summary["selector_resolution"] = resolution
    if not summary:
        return None
    return summary


def recovery_suggestions(
    action: str,
    params: dict[str, Any],
    observation: Observation | None,
    context: WorkflowContext,
) -> list[str]:
    suggestions: list[str] = []
    if observation is None:
        suggestions.append("Add or fix an observe_* step before this action.")
    if action in {"click", "type", "paste", "expect_download", "wait_for"} and "target" in params:
        suggestions.append("Check target text, role, label, selector, test_id, row, and column constraints.")
        suggestions.append("Prefer selector/test_id for web pages, then text/role fallback.")
    if action in {"assert_text", "wait_for"}:
        suggestions.append("Verify whether the expected text changed, is delayed, or appears after another action.")
    if action == "assert_response":
        events = context.resources.get("network_events")
        if isinstance(events, list) and events:
            suggestions.append("Compare the latest captured network event with the expected method, URL, and status.")
        else:
            suggestions.append("Ensure observe_browser is used before assert_response so network events are captured.")
    if action == "assert_file_exists":
        suggestions.append("Check whether expect_download saved the file and whether extension/min_bytes are correct.")
    if not suggestions:
        suggestions.append("Review the previous observation and step parameters, then retry from checkpoint.")
    return suggestions


def build_reflection_prompt(expected: str, actual: str) -> str:
    return (
        "原本预期 [" + expected + "]，现在实际看到 [" + actual + "]。"
        "请给出恢复建议，并优先选择结构化信息、DOM/UIA/API，其次才使用视觉。"
    )
