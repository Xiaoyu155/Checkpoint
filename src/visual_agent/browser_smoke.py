from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

import yaml

from .dispatcher import selector_from_resolved
from .dom import normalize_text
from .models import Observation
from .product_state import browser_readiness_failure_message, evaluate_browser_readiness, observation_to_state
from .providers import ProviderContext, browser_page_observation, observe_browser
from .selector import SelectorResolver
from .security import redact_secret_text
from .workflow import close_context_resources, target_from_config
from .workflow_types import WorkflowContext


def run_browser_smoke(
    *,
    url: str,
    output_dir: str | Path = ".runs",
    headed: bool = False,
    timeout_ms: int = 10_000,
    wait_until: str = "domcontentloaded",
    min_text_length: int = 1,
    min_interactive: int = 0,
    expect_text: list[str] | None = None,
    expect_url_contains: list[str] | None = None,
    expect_text_after: list[str] | None = None,
    expect_url_contains_after: list[str] | None = None,
    wait_for_text_after: list[str] | None = None,
    wait_for_url_contains_after: list[str] | None = None,
    wait_timeout_seconds: float = 5.0,
    click_text: str | None = None,
    click_selector: str | None = None,
    fill: list[str] | None = None,
    fill_selector: list[str] | None = None,
    require_change_after_click: bool = False,
    wait_after_seconds: float = 0.5,
    save_workflow: str | Path | None = None,
    overwrite_workflow: bool = False,
) -> dict[str, Any]:
    run_dir = browser_smoke_run_dir(output_dir)
    resources: dict[str, Any] = {}
    context = WorkflowContext(run_id=run_dir.name, run_dir=run_dir, resources=resources)
    try:
        observation = observe_browser(
            {
                "url": url,
                "headed": headed,
                "timeout_ms": timeout_ms,
                "wait_until": wait_until,
                "screenshot_label": "initial",
            },
            ProviderContext(run_dir=run_dir, resources=resources),
        )
        context.observations["initial"] = observation
        readiness = evaluate_browser_readiness(
            observation,
            {"min_text_length": min_text_length, "min_interactive": min_interactive},
            network_events=resources.get("network_events", []),
        )
        issues: list[dict[str, Any]] = []
        if not readiness.passed:
            issues.append({"type": "browser_not_ready", "message": browser_readiness_failure_message(readiness)})
        issues.extend(text_issues(observation, expect_text or [], phase="initial"))
        issues.extend(url_contains_issues(observation, expect_url_contains or [], phase="initial"))
        fill_results = execute_browser_smoke_fills(
            observation,
            resources,
            fill_specs=fill or [],
            fill_selector_specs=fill_selector or [],
        )
        click_result = None
        after_observation = None
        wait_results: list[dict[str, Any]] = []
        if click_text or click_selector:
            click_result = execute_browser_smoke_click(
                observation,
                resources,
                click_text=click_text,
                click_selector=click_selector,
                wait_after_seconds=wait_after_seconds,
            )
            wait_results = wait_for_browser_smoke_text(
                resources,
                texts=wait_for_text_after or [],
                timeout_seconds=wait_timeout_seconds,
            )
            wait_results.extend(
                wait_for_browser_smoke_url(
                    resources,
                    contains=wait_for_url_contains_after or [],
                    timeout_seconds=wait_timeout_seconds,
                )
            )
            issues.extend(item for item in wait_results if item.get("status") != "found")
            after_observation = browser_page_observation(
                resources["playwright_page"],
                run_dir=run_dir,
                label="after-click",
                network_events=resources.get("network_events", []),
                console_events=resources.get("console_events", []),
                page_errors=resources.get("page_errors", []),
            )
            context.observations["after_click"] = after_observation
            after_ready = evaluate_browser_readiness(
                after_observation,
                {"min_text_length": min_text_length, "min_interactive": 0},
                network_events=resources.get("network_events", []),
            )
            if not after_ready.passed:
                issues.append({"type": "browser_not_ready_after_click", "message": browser_readiness_failure_message(after_ready)})
            change = browser_observation_change(observation, after_observation)
            if require_change_after_click and not change["changed"]:
                issues.append(
                    {
                        "type": "no_change_after_click",
                        "message": "Click completed but URL, visible text length, and interactive element count did not change.",
                        "change": change,
                    }
                )
            issues.extend(text_issues(after_observation, expect_text_after or [], phase="after_click"))
            issues.extend(url_contains_issues(after_observation, expect_url_contains_after or [], phase="after_click"))
        elif expect_text_after:
            issues.append({"type": "missing_click", "message": "expect_text_after requires click_text or click_selector"})
        elif expect_url_contains_after:
            issues.append({"type": "missing_click", "message": "expect_url_contains_after requires click_text or click_selector"})

        payload = {
            "status": "failed" if issues else "success",
            "url": url,
            "run_dir": str(run_dir),
            "headed": headed,
            "initial": observation_summary(observation),
            "after_click": observation_summary(after_observation) if after_observation is not None else None,
            "fills": fill_results,
            "click": click_result,
            "waits": wait_results,
            "change": browser_observation_change(observation, after_observation) if after_observation is not None else None,
            "issues": issues,
        }
        if save_workflow is not None:
            payload["workflow_export"] = save_browser_smoke_workflow(
                save_workflow,
                url=url,
                timeout_ms=timeout_ms,
                wait_until=wait_until,
                min_text_length=min_text_length,
                min_interactive=min_interactive,
                expect_text=expect_text or [],
                expect_url_contains=expect_url_contains or [],
                expect_text_after=expect_text_after or [],
                expect_url_contains_after=expect_url_contains_after or [],
                wait_for_text_after=wait_for_text_after or [],
                wait_for_url_contains_after=wait_for_url_contains_after or [],
                wait_timeout_seconds=wait_timeout_seconds,
                click_text=click_text,
                click_selector=click_selector,
                fill=fill or [],
                fill_selector=fill_selector or [],
                require_change_after_click=require_change_after_click,
                wait_after_seconds=wait_after_seconds,
                overwrite=overwrite_workflow,
            )
        return redact_browser_smoke_payload(payload, fill or [], fill_selector or [])
    except Exception as exc:
        payload = {
            "status": "error",
            "url": url,
            "run_dir": str(run_dir),
            "message": f"{type(exc).__name__}: {exc}",
            "issues": [{"type": "exception", "message": f"{type(exc).__name__}: {exc}"}],
        }
        return redact_browser_smoke_payload(payload, fill or [], fill_selector or [])
    finally:
        close_context_resources(context)


def wait_for_browser_smoke_text(
    resources: dict[str, Any],
    *,
    texts: list[str],
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    page = resources.get("playwright_page")
    if page is None and texts:
        raise RuntimeError("browser smoke wait requires a Playwright page")
    results = []
    timeout_ms = max(1, int(timeout_seconds * 1000))
    for text in texts:
        try:
            page.wait_for_function(
                "(expected) => document.body && (document.body.innerText || document.body.textContent || '').toLowerCase().includes(String(expected).toLowerCase())",
                arg=text,
                timeout=timeout_ms,
            )
            results.append({"status": "found", "type": "wait_for_text_after", "text": text, "timeout_seconds": timeout_seconds})
        except Exception as exc:
            results.append(
                {
                    "status": "timeout",
                    "type": "wait_for_text_after",
                    "text": text,
                    "timeout_seconds": timeout_seconds,
                    "message": f"Timed out waiting for text after click: {text} ({type(exc).__name__}: {exc})",
                }
            )
    return results


def wait_for_browser_smoke_url(
    resources: dict[str, Any],
    *,
    contains: list[str],
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    page = resources.get("playwright_page")
    if page is None and contains:
        raise RuntimeError("browser smoke URL wait requires a Playwright page")
    results = []
    timeout_ms = max(1, int(timeout_seconds * 1000))
    for fragment in contains:
        try:
            page.wait_for_function(
                "(expected) => String(window.location.href).includes(String(expected))",
                arg=fragment,
                timeout=timeout_ms,
            )
            results.append({"status": "found", "type": "wait_for_url_contains_after", "text": fragment, "timeout_seconds": timeout_seconds})
        except Exception as exc:
            results.append(
                {
                    "status": "timeout",
                    "type": "wait_for_url_contains_after",
                    "text": fragment,
                    "timeout_seconds": timeout_seconds,
                    "message": f"Timed out waiting for URL after click to contain: {fragment} ({type(exc).__name__}: {exc})",
                }
            )
    return results


def execute_browser_smoke_fills(
    observation: Observation,
    resources: dict[str, Any],
    *,
    fill_specs: list[str],
    fill_selector_specs: list[str],
) -> list[dict[str, Any]]:
    page = resources.get("playwright_page")
    if page is None and (fill_specs or fill_selector_specs):
        raise RuntimeError("browser smoke fill requires a Playwright page")
    results = []
    for spec in fill_specs:
        label, value = parse_assignment(spec, "--fill")
        resolved = SelectorResolver().resolve(target_from_config({"label": label, "role": "input"}), observation)
        selector = selector_from_resolved(resolved)
        if not selector:
            raise LookupError(f"Could not resolve an input selector for: {label}")
        page.locator(selector).fill(value)
        results.append({"status": "filled", "target": label, "selector": selector, "value_length": len(value)})
    for spec in fill_selector_specs:
        selector, value = parse_assignment(spec, "--fill-selector")
        page.locator(selector).fill(value)
        results.append({"status": "filled", "target": selector, "selector": selector, "value_length": len(value)})
    return results


def execute_browser_smoke_click(
    observation: Observation,
    resources: dict[str, Any],
    *,
    click_text: str | None,
    click_selector: str | None,
    wait_after_seconds: float,
) -> dict[str, Any]:
    page = resources.get("playwright_page")
    if page is None:
        raise RuntimeError("browser smoke click requires a Playwright page")
    selector = click_selector
    target_label = click_selector or click_text or ""
    if selector is None:
        resolved = SelectorResolver().resolve(target_from_config({"text": click_text, "role": "button"}), observation)
        selector = selector_from_resolved(resolved)
        target_label = resolved.target.display_name
    if not selector:
        raise LookupError(f"Could not resolve a clickable selector for: {target_label}")
    page.locator(selector).click()
    if wait_after_seconds > 0 and hasattr(page, "wait_for_timeout"):
        page.wait_for_timeout(int(wait_after_seconds * 1000))
    return {"status": "clicked", "target": target_label, "selector": selector}


def parse_assignment(value: str, option: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError(f"{option} expects name=value")
    left, right = value.split("=", 1)
    left = left.strip()
    if not left:
        raise ValueError(f"{option} expects a non-empty name before '='")
    return left, right


def redact_browser_smoke_payload(payload: dict[str, Any], fill: list[str], fill_selector: list[str]) -> dict[str, Any]:
    values = tuple(browser_smoke_redaction_values(fill, fill_selector))
    if not values:
        return payload
    return redact_browser_smoke_value(payload, values)


def browser_smoke_redaction_values(fill: list[str], fill_selector: list[str]) -> list[str]:
    values: list[str] = []
    for spec in fill:
        _label, value = parse_assignment(spec, "--fill")
        if value:
            values.append(value)
    for spec in fill_selector:
        _selector, value = parse_assignment(spec, "--fill-selector")
        if value:
            values.append(value)
    return values


def redact_browser_smoke_value(value: Any, redaction_values: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return redact_secret_text(value, extra_secrets=redaction_values)
    if isinstance(value, dict):
        return {key: redact_browser_smoke_value(item, redaction_values) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_browser_smoke_value(item, redaction_values) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_browser_smoke_value(item, redaction_values) for item in value)
    return value


def save_browser_smoke_workflow(
    path: str | Path,
    *,
    overwrite: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    output_path = Path(path).resolve()
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Workflow already exists: {output_path}")
    workflow, export = build_browser_smoke_workflow(workflow_name=workflow_name_from_path(output_path), **kwargs)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(workflow, allow_unicode=True, sort_keys=False), encoding="utf-8")
    inputs_template = export.get("inputs_template") if isinstance(export.get("inputs_template"), dict) else {}
    if inputs_template:
        inputs_template_path = output_path.with_suffix(".inputs.example.json")
        inputs_template_path.write_text(json.dumps(inputs_template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        export["inputs_template_path"] = str(inputs_template_path)
    return {"path": str(output_path), **export}


def build_browser_smoke_workflow(
    *,
    url: str,
    timeout_ms: int,
    wait_until: str,
    min_text_length: int,
    min_interactive: int,
    expect_text: list[str],
    expect_url_contains: list[str],
    expect_text_after: list[str],
    expect_url_contains_after: list[str],
    wait_for_text_after: list[str],
    wait_for_url_contains_after: list[str],
    wait_timeout_seconds: float,
    click_text: str | None,
    click_selector: str | None,
    fill: list[str],
    fill_selector: list[str],
    require_change_after_click: bool,
    wait_after_seconds: float,
    workflow_name: str = "browser_smoke",
) -> tuple[dict[str, Any], dict[str, Any]]:
    input_names: dict[str, str] = {}
    fill_refs = browser_smoke_fill_refs(fill, fill_selector, input_names)
    parameterized_assertions: list[dict[str, Any]] = []
    expect_text_specs = export_assertion_specs(expect_text, "text", "expect_text", fill_refs, parameterized_assertions)
    expect_url_contains_specs = export_assertion_specs(
        expect_url_contains, "url_contains", "expect_url_contains", fill_refs, parameterized_assertions
    )
    expect_text_after_specs = export_assertion_specs(expect_text_after, "text", "expect_text_after", fill_refs, parameterized_assertions)
    expect_url_contains_after_specs = export_assertion_specs(
        expect_url_contains_after, "url_contains", "expect_url_contains_after", fill_refs, parameterized_assertions
    )
    wait_for_text_after_specs = export_assertion_specs(
        wait_for_text_after, "text", "wait_for_text_after", fill_refs, parameterized_assertions
    )
    wait_for_url_contains_after_specs = export_assertion_specs(
        wait_for_url_contains_after, "url_contains", "wait_for_url_contains_after", fill_refs, parameterized_assertions
    )

    steps: list[dict[str, Any]] = [
        {
            "id": "observe_initial",
            "action": "observe_browser",
            "url": url,
            "timeout_ms": timeout_ms,
            "wait_until": wait_until,
        },
        {
            "id": "assert_browser_ready",
            "action": "assert_browser_ready",
            "min_text_length": min_text_length,
            "min_interactive": min_interactive,
        },
    ]
    for index, spec in enumerate(expect_text_specs, start=1):
        steps.append({"id": f"assert_initial_text_{index}", "action": "assert_text", **spec})
    for index, spec in enumerate(expect_url_contains_specs, start=1):
        steps.append(
            {
                "id": f"assert_initial_url_{index}",
                "action": "wait_for",
                "condition": "url",
                **spec,
                "timeout_seconds": 0.1,
            }
        )

    inputs_template: dict[str, str] = {}
    sensitive_fields: list[str] = []
    for item in fill_refs:
        key = item["key"]
        inputs_template[key] = ""
        sensitive = bool(item["sensitive"])
        if sensitive:
            sensitive_fields.append(key)
        step = {"id": f"fill_{key}", "action": "paste", "target": item["target"], "value_from": f"input.{key}"}
        if sensitive:
            step["sensitive"] = True
        steps.append(step)

    if click_text or click_selector:
        click_step: dict[str, Any] = {
            "id": "click_primary",
            "action": "click",
            "wait_after_seconds": wait_after_seconds,
            "browser_post_action_observe": True,
        }
        if click_selector:
            click_step["target"] = {"selector": click_selector}
        else:
            click_step["target"] = {"text": click_text, "role": "button"}
        if require_change_after_click:
            click_step["post_action_observe"] = {"wait_seconds": wait_after_seconds}
        steps.append(click_step)
        for index, spec in enumerate(wait_for_text_after_specs, start=1):
            steps.append({"id": f"wait_after_text_{index}", "action": "wait_for", "condition": "text", **spec, "timeout_seconds": wait_timeout_seconds})
        for index, spec in enumerate(wait_for_url_contains_after_specs, start=1):
            steps.append(
                {
                    "id": f"wait_after_url_{index}",
                    "action": "wait_for",
                    "condition": "url",
                    **spec,
                    "timeout_seconds": wait_timeout_seconds,
                }
            )
        steps.append({"id": "observe_after_click", "action": "observe_browser", "reuse_page": True, "screenshot_label": "after-click"})
        for index, spec in enumerate(expect_text_after_specs, start=1):
            steps.append({"id": f"assert_after_text_{index}", "action": "assert_text", **spec})
        for index, spec in enumerate(expect_url_contains_after_specs, start=1):
            steps.append(
                {
                    "id": f"assert_after_url_{index}",
                    "action": "wait_for",
                    "condition": "url",
                    **spec,
                    "timeout_seconds": 0.1,
                }
            )

    workflow = {
        "schema_version": 1,
        "name": workflow_name,
        "version": 1,
        "description": "Generated from a passing browser-smoke run. Fill values are supplied through inputs.",
        "steps": steps,
    }
    export = {
        "status": "saved",
        "inputs_template": inputs_template,
        "sensitive_fields": sensitive_fields,
        "parameterized_assertions": parameterized_assertions,
        "note": "Fill values were not written to the workflow. Pass them as workflow inputs.",
    }
    return workflow, export


def browser_smoke_fill_refs(fill: list[str], fill_selector: list[str], input_names: dict[str, str]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for index, spec in enumerate(fill, start=1):
        label, value = parse_assignment(spec, "--fill")
        key = unique_input_name(input_name_from_target(label, index), input_names)
        input_names[label] = key
        refs.append(
            {
                "key": key,
                "value": value,
                "target": {"label": label, "role": "input"},
                "sensitive": is_sensitive_target(label),
            }
        )
    for index, spec in enumerate(fill_selector, start=len(fill) + 1):
        selector, value = parse_assignment(spec, "--fill-selector")
        key = unique_input_name(input_name_from_target(selector, index), input_names)
        input_names[selector] = key
        refs.append(
            {
                "key": key,
                "value": value,
                "target": {"selector": selector},
                "sensitive": is_sensitive_target(selector),
            }
        )
    return refs


def export_assertion_specs(
    values: list[str],
    field_key: str,
    field: str,
    fill_refs: list[dict[str, Any]],
    parameterized: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for value in values:
        ref = matching_fill_ref(value, fill_refs)
        if ref is not None:
            specs.append({f"{field_key}_from": f"input.{ref['key']}"})
            parameterized.append({"field": field, "source": f"input.{ref['key']}", "reason": "contains_fill_value"})
        else:
            specs.append({field_key: value})
    return specs


def matching_fill_ref(value: str, fill_refs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for ref in fill_refs:
        fill_value = str(ref.get("value") or "")
        if fill_value and fill_value in value:
            return ref
    return None


def workflow_name_from_path(path: Path) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]+", "_", path.stem).strip("_").lower()
    return name or "browser_smoke"


def unique_input_name(name: str, existing: dict[str, str]) -> str:
    used = set(existing.values())
    candidate = name
    index = 2
    while candidate in used:
        candidate = f"{name}_{index}"
        index += 1
    return candidate


def input_name_from_target(target: str, index: int) -> str:
    lowered = target.lower()
    if any(hint in target for hint in ("密码", "口令")):
        return "password"
    if any(hint in target for hint in ("用户", "账号", "账户")):
        return "username"
    parts = re.findall(r"[a-zA-Z0-9]+", lowered)
    clean = "_".join(part for part in parts if part not in {"input", "field", "form", "css", "id", "name"})
    return clean[:48].strip("_") or f"field_{index}"


def is_sensitive_target(target: str) -> bool:
    lowered = target.lower()
    return any(hint in lowered for hint in ("password", "passwd", "pwd", "token", "secret", "api_key", "apikey", "密码", "口令", "密钥"))


def text_issues(observation: Observation, expected: list[str], *, phase: str) -> list[dict[str, str]]:
    state = observation_to_state(observation)
    haystack = normalize_text(" ".join(state["visible_text"]) + " " + str(observation.metadata.get("visible_text") or ""))
    issues = []
    for text in expected:
        if normalize_text(text) not in haystack:
            issues.append({"type": "missing_text", "phase": phase, "message": f"Text not found: {text}"})
    return issues


def url_contains_issues(observation: Observation, expected: list[str], *, phase: str) -> list[dict[str, str]]:
    url = str(observation.metadata.get("url") or observation.source)
    issues = []
    for fragment in expected:
        if fragment not in url:
            issues.append({"type": "missing_url_fragment", "phase": phase, "message": f"URL does not contain: {fragment}", "url": url})
    return issues


def browser_observation_change(before: Observation, after: Observation) -> dict[str, Any]:
    before_url = str(before.metadata.get("url") or before.source)
    after_url = str(after.metadata.get("url") or after.source)
    before_text = normalize_text(str(before.metadata.get("visible_text") or ""))
    after_text = normalize_text(str(after.metadata.get("visible_text") or ""))
    before_interactive = int(before.metadata.get("interactive_count") or len(before.elements))
    after_interactive = int(after.metadata.get("interactive_count") or len(after.elements))
    return {
        "changed": before_url != after_url or before_text != after_text or before_interactive != after_interactive,
        "url_changed": before_url != after_url,
        "visible_text_changed": before_text != after_text,
        "interactive_count_changed": before_interactive != after_interactive,
        "before_url": before_url,
        "after_url": after_url,
        "before_visible_text_length": len(before_text),
        "after_visible_text_length": len(after_text),
        "before_interactive_count": before_interactive,
        "after_interactive_count": after_interactive,
    }


def observation_summary(observation: Observation | None) -> dict[str, Any] | None:
    if observation is None:
        return None
    state = observation_to_state(observation)
    return {
        "url": observation.metadata.get("url") or observation.source,
        "title": state["title"],
        "screenshot_path": str(observation.screenshot_path) if observation.screenshot_path is not None else None,
        "html_path": observation.metadata.get("html_path"),
        "visible_text_path": observation.metadata.get("visible_text_path"),
        "visible_text_length": observation.metadata.get("visible_text_length"),
        "interactive_count": observation.metadata.get("interactive_count", len(observation.elements)),
        "primary_actions": list(state["primary_actions"]),
        "errors": list(state["errors"]),
        "failed_request_count": observation.metadata.get("failed_request_count", 0),
        "console_errors": list(observation.metadata.get("console_errors") or ()),
        "page_errors": list(observation.metadata.get("page_errors") or ()),
    }


def browser_smoke_run_dir(output_dir: str | Path) -> Path:
    root = Path(output_dir).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    for index in range(100):
        suffix = "" if index == 0 else f"-{index}"
        run_dir = root / f"browser-smoke-{stamp}{suffix}"
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            return run_dir
        except FileExistsError:
            continue
    raise FileExistsError(f"Could not create unique browser smoke run directory under {root}")


def browser_smoke_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Browser Smoke",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- URL: `{payload.get('url')}`",
        f"- Run dir: `{payload.get('run_dir')}`",
    ]
    initial = payload.get("initial") if isinstance(payload.get("initial"), dict) else {}
    if initial:
        lines.extend(
            [
                f"- Title: `{initial.get('title') or ''}`",
                f"- Visible text length: `{initial.get('visible_text_length')}`",
                f"- Interactive elements: `{initial.get('interactive_count')}`",
                f"- Screenshot: `{initial.get('screenshot_path') or ''}`",
            ]
        )
    click = payload.get("click") if isinstance(payload.get("click"), dict) else None
    if click:
        lines.extend(["", "## Click", "", f"- Target: `{click.get('target')}`", f"- Selector: `{click.get('selector')}`"])
    waits = payload.get("waits") if isinstance(payload.get("waits"), list) else []
    if waits:
        lines.extend(["", "## Waits", ""])
        for wait in waits:
            if isinstance(wait, dict):
                lines.append(f"- `{wait.get('status')}` text `{wait.get('text')}`")
    fills = payload.get("fills") if isinstance(payload.get("fills"), list) else []
    if fills:
        lines.extend(["", "## Fills", ""])
        for fill in fills:
            if isinstance(fill, dict):
                lines.append(f"- `{fill.get('target')}` via `{fill.get('selector')}` ({fill.get('value_length')} chars)")
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    if issues:
        lines.extend(["", "## Issues", ""])
        for issue in issues:
            if isinstance(issue, dict):
                lines.append(f"- `{issue.get('type')}`: {issue.get('message')}")
    return "\n".join(lines).rstrip() + "\n"
