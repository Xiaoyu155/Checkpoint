from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from time import sleep, strftime, time
from typing import Any

from .workflow import RUNTIME_VERSION, SUPPORTED_WORKFLOW_SCHEMA_VERSION, parse_workflow_file
from .preflight import run_preflight
from .scheduler import submit_queue_task
from .security import redact_secret_text, scrub_secrets
from .validation import validate_workflow_file
from .workflow_diff import workflow_diff_to_markdown, workflow_save_diff
from .workspace import Workspace, run_workspace_workflow


RECORDER_SCRIPT = r"""
(() => {
  if (window.__visualAgentRecorderInstalled) return;
  window.__visualAgentRecorderInstalled = true;

  function selectorFor(el) {
    if (!el || !el.tagName) return "";
    if (el.id) return "#" + CSS.escape(el.id);
    const testId = el.getAttribute("data-testid") || el.getAttribute("data-test") || el.getAttribute("data-qa");
    if (testId) return `[data-testid="${CSS.escape(testId)}"]`;
    const name = el.getAttribute("name");
    if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
    const aria = el.getAttribute("aria-label");
    if (aria) return `${el.tagName.toLowerCase()}[aria-label="${CSS.escape(aria)}"]`;
    let current = el;
    const parts = [];
    while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 4) {
      let part = current.tagName.toLowerCase();
      const parent = current.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(item => item.tagName === current.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
      }
      parts.unshift(part);
      current = parent;
    }
    return parts.join(" > ");
  }

  function labelFor(el) {
    if (!el) return "";
    const id = el.getAttribute("id");
    if (id) {
      const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (label && label.innerText) return label.innerText.trim();
    }
    const wrapped = el.closest("label");
    if (wrapped && wrapped.innerText) return wrapped.innerText.trim();
    return el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.innerText || el.value || "";
  }

  function roleFor(el) {
    if (!el) return "";
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "button") return "button";
    if (tag === "a") return "link";
    if (tag === "input" || tag === "textarea" || tag === "select") return "textbox";
    return tag;
  }

  function emit(type, el) {
    const testId = el && el.getAttribute ? (el.getAttribute("data-testid") || el.getAttribute("data-test") || el.getAttribute("data-qa") || "") : "";
    const payload = {
      type,
      url: location.href,
      selector: selectorFor(el),
      test_id: testId,
      text: (labelFor(el) || "").trim().slice(0, 160),
      role: roleFor(el),
      tag: el && el.tagName ? el.tagName.toLowerCase() : "",
      input_type: el && el.getAttribute ? (el.getAttribute("type") || "") : "",
      name: el && el.getAttribute ? (el.getAttribute("name") || "") : "",
      aria_label: el && el.getAttribute ? (el.getAttribute("aria-label") || "") : "",
      placeholder: el && el.getAttribute ? (el.getAttribute("placeholder") || "") : "",
      value: el && "value" in el ? String(el.value || "") : "",
      timestamp: Date.now() / 1000
    };
    if (window.recordVisualAgentEvent) window.recordVisualAgentEvent(payload);
  }

  document.addEventListener("click", event => emit("click", event.target), true);
  document.addEventListener("change", event => emit("input", event.target), true);
})();
"""


class BrowserRecordingError(RuntimeError):
    def __init__(self, message: str, failure_report: dict[str, Any]) -> None:
        super().__init__(message)
        self.failure_report = failure_report


@dataclass(frozen=True)
class BrowserRecordResult:
    workflow: dict[str, Any]
    workflow_path: Path | None
    inputs_path: Path | None
    event_count: int
    validation: Any
    preflight: Any | None = None
    preview: dict[str, Any] | None = None
    input_keys: tuple[str, ...] = ()
    empty_input_keys: tuple[str, ...] = ()
    selector_report: dict[str, Any] | None = None
    queue_task: dict[str, Any] | None = None
    queue_status: str = "not_requested"
    queue_message: str = ""
    save: dict[str, Any] | None = None


def record_browser_session(
    workspace: Workspace,
    *,
    url: str,
    save_as: str,
    timeout_seconds: float = 0.0,
    headed: bool = True,
    assert_text: str | None = None,
    auto_assert: bool = True,
    save_auth_state: str | None = None,
    check: bool = True,
    preview_run: bool = False,
    overwrite: bool = False,
    queue_run: bool = False,
    queue_priority: int = 0,
    queue_max_retries: int = 0,
) -> BrowserRecordResult:
    events: list[dict[str, Any]] = []
    options = {
        "timeout_seconds": timeout_seconds,
        "headed": headed,
        "assert_text": bool(assert_text),
        "auto_assert": auto_assert,
        "save_auth_state": bool(save_auth_state),
        "check": check,
        "preview_run": preview_run,
        "overwrite": overwrite,
        "queue_run": queue_run,
        "queue_priority": queue_priority,
        "queue_max_retries": queue_max_retries,
    }
    try:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed. Run: pip install -e .[web]") from exc

        final_text = ""
        started = time()

        def on_event(_source: Any, payload: Any) -> None:
            if isinstance(payload, dict):
                events.append(payload)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not headed)
            context = browser.new_context()

            def attach(page: Any) -> None:
                page.expose_binding("recordVisualAgentEvent", on_event)
                page.add_init_script(RECORDER_SCRIPT)
                page.on("framenavigated", lambda frame: record_navigation(events, frame, page))

            page = context.new_page()
            attach(page)
            context.on("page", attach)
            page.goto(url, wait_until="domcontentloaded")

            while context.pages:
                if timeout_seconds > 0 and time() - started >= timeout_seconds:
                    break
                sleep(0.2)
            if context.pages:
                final_text = visible_page_text(context.pages[-1])
            context.close()
            browser.close()

        return save_recorded_workflow(
            workspace,
            events,
            save_as=save_as,
            initial_url=url,
            assert_text=assert_text,
            final_text=final_text,
            auto_assert=auto_assert,
            save_auth_state=save_auth_state,
            check=check,
            preview_run=preview_run,
            overwrite=overwrite,
            queue_run=queue_run,
            queue_priority=queue_priority,
            queue_max_retries=queue_max_retries,
        )
    except Exception as exc:
        if isinstance(exc, BrowserRecordingError):
            raise
        report = archive_recording_failure(
            workspace,
            url=url,
            save_as=save_as,
            error=exc,
            events=events,
            options=options,
        )
        raise BrowserRecordingError(f"Browser recording failed: {exc}", report) from exc


def save_recorded_workflow(
    workspace: Workspace,
    events: list[dict[str, Any]],
    *,
    save_as: str,
    initial_url: str,
    assert_text: str | None = None,
    final_text: str | None = None,
    auto_assert: bool = True,
    save_auth_state: str | None = None,
    overwrite: bool = False,
    check: bool = True,
    preview_run: bool = False,
    queue_run: bool = False,
    queue_priority: int = 0,
    queue_max_retries: int = 0,
) -> BrowserRecordResult:
    workflow, inputs = recorded_events_to_workflow(
        events,
        workflow_name=safe_workflow_name(save_as),
        initial_url=initial_url,
        assert_text=assert_text,
        final_text=final_text,
        auto_assert=auto_assert,
        save_auth_state=save_auth_state,
    )
    sensitive_values = recording_sensitive_values(events)
    workflow = scrub_recording_payload(workflow, sensitive_values)
    selector_report = scrub_recording_payload(selector_quality_report(events), sensitive_values)
    workflow_path = recorded_workflow_path(workspace, save_as)
    inputs_path = workspace.inputs_dir / f"{workflow_path.stem}_inputs.json" if inputs else None
    if workflow_path.exists() and not overwrite:
        raise FileExistsError(f"Recorded workflow already exists: {workflow_path}")
    workflow_text = redact_secret_text(workflow_to_yaml(workflow), extra_secrets=sensitive_values)
    relative_workflow_path = workflow_path.relative_to(workspace.root).as_posix()
    save = {
        "status": "saved",
        "path": relative_workflow_path,
        "overwrite": bool(overwrite),
        **workflow_save_diff(workflow_path, workflow_text, relative_path=relative_workflow_path),
    }
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(workflow_text, encoding="utf-8")
    if inputs_path is not None:
        inputs_path.write_text(json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8")
    validation = validate_workflow_file(workflow_path)
    preflight = run_preflight(parse_workflow_file(workflow_path)) if check and validation.valid else None
    input_keys = tuple(sorted(inputs))
    empty_input_keys = tuple(sorted(key for key, value in inputs.items() if value is None or value == ""))
    preview = None
    if preview_run and validation.valid and (preflight is None or preflight.ok):
        if empty_input_keys:
            preview = {
                "status": "skipped",
                "ok": True,
                "reason": "input_template_has_empty_values",
                "empty_input_keys": list(empty_input_keys),
            }
        else:
            preview = preview_recorded_workflow(workspace, workflow_path, inputs=inputs)
    queue_task = None
    queue_status = "not_requested"
    queue_message = ""
    if queue_run:
        queue_status, queue_message, queue_task = queue_recorded_workflow(
            workspace,
            workflow_path,
            inputs_path=inputs_path,
            validation_valid=bool(validation.valid),
            preflight_ok=None if preflight is None else bool(preflight.ok),
            preview=preview,
            empty_input_keys=empty_input_keys,
            priority=queue_priority,
            max_retries=queue_max_retries,
        )
    return BrowserRecordResult(
        workflow=workflow,
        workflow_path=workflow_path,
        inputs_path=inputs_path,
        event_count=len(events),
        validation=validation,
        preflight=preflight,
        preview=preview,
        input_keys=input_keys,
        empty_input_keys=empty_input_keys,
        selector_report=selector_report,
        queue_task=queue_task,
        queue_status=queue_status,
        queue_message=queue_message,
        save=save,
    )


def recorded_events_to_workflow(
    events: list[dict[str, Any]],
    *,
    workflow_name: str,
    initial_url: str,
    assert_text: str | None = None,
    final_text: str | None = None,
    auto_assert: bool = True,
    save_auth_state: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {
            "id": "observe_initial",
            "action": "observe_browser",
            "url": initial_url,
        }
    ]
    inputs: dict[str, Any] = {}
    for index, event in enumerate(compact_recorded_events(events), start=1):
        event_type = str(event.get("type") or "")
        if event_type == "click":
            target = target_from_recorded_event(event)
            steps.append({"id": f"click_{index}", "action": "click", "target": target})
        elif event_type == "input":
            target = target_from_recorded_event(event)
            step: dict[str, Any] = {"id": f"type_{index}", "action": "type", "target": target}
            value = str(event.get("value") or "")
            if is_sensitive_recorded_event(event):
                key = safe_input_key(event, index)
                inputs[key] = ""
                step["value_from"] = f"input.{key}"
                step["sensitive"] = True
            else:
                step["value"] = value
            steps.append(step)
        elif event_type == "navigate":
            url = str(event.get("url") or "").strip()
            if url and url != initial_url:
                steps.append({"id": f"observe_navigation_{index}", "action": "observe_browser", "reuse_page": True})
    if len(steps) == 1:
        steps.append(
            {
                "id": "wait_for_page",
                "action": "wait_for",
                "condition": "target",
                "target": {"selector": "body"},
                "timeout_seconds": 0.1,
            }
        )
    verification_text = str(assert_text or "").strip()
    if not verification_text and auto_assert:
        verification_text = inferred_assert_text(events, final_text=final_text)
    if verification_text:
        steps.append({"id": "assert_recorded_result", "action": "assert_text", "text": verification_text})
    if save_auth_state:
        steps.append(
            {
                "id": "save_recorded_auth_state",
                "action": "save_storage_state",
                "path": recorded_auth_state_path(save_auth_state),
                "require_confirm": True,
            }
        )
    return (
        {
            "schema_version": SUPPORTED_WORKFLOW_SCHEMA_VERSION,
            "min_runtime_version": RUNTIME_VERSION,
            "name": workflow_name,
            "version": 1,
            "steps": steps,
        },
        inputs,
    )


def compact_recorded_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    last_url = ""
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type not in {"click", "input", "navigate"}:
            continue
        if event_type == "navigate":
            url = str(event.get("url") or "")
            if not url or url == last_url:
                continue
            if compacted and compacted[-1].get("type") == "navigate":
                compacted[-1] = event
            else:
                compacted.append(event)
            last_url = url
            continue
        selector = str(event.get("selector") or "")
        if not selector:
            continue
        if event.get("url"):
            last_url = str(event.get("url") or "")
        if event_type == "input" and compacted and compacted[-1].get("type") == "input" and compacted[-1].get("selector") == selector:
            compacted[-1] = event
            continue
        compacted.append(event)
    return compacted


def inferred_assert_text(events: list[dict[str, Any]], *, final_text: str | None = None) -> str:
    candidates = visible_text_candidates(final_text or "")
    if candidates:
        return candidates[0]
    for event in reversed(events):
        text = str(event.get("text") or "").strip()
        if text and not is_sensitive_recorded_event(event):
            return text[:120]
    return ""


def visible_text_candidates(text: str) -> list[str]:
    lines = [line.strip() for line in str(text or "").splitlines()]
    candidates = []
    for line in lines:
        if len(line) < 3 or len(line) > 120:
            continue
        lowered = line.lower()
        if any(marker in lowered for marker in ("password", "token", "secret", "cookie", "api key")):
            continue
        if line not in candidates:
            candidates.append(line)
    return candidates


def visible_page_text(page: Any) -> str:
    try:
        value = page.locator("body").inner_text(timeout=1000)
    except Exception:
        return ""
    return str(value or "")


def record_navigation(events: list[dict[str, Any]], frame: Any, page: Any) -> None:
    try:
        if frame != page.main_frame:
            return
        url = str(frame.url or "")
    except Exception:
        return
    if not url or url == "about:blank":
        return
    events.append({"type": "navigate", "url": url, "timestamp": time()})


def target_from_recorded_event(event: dict[str, Any]) -> dict[str, Any]:
    target: dict[str, Any] = {}
    test_id = str(event.get("test_id") or "").strip()
    selector = str(event.get("selector") or "").strip()
    text = str(event.get("text") or "").strip()
    role = str(event.get("role") or "").strip()
    name = str(event.get("name") or "").strip()
    aria_label = str(event.get("aria_label") or "").strip()
    placeholder = str(event.get("placeholder") or "").strip()

    if test_id:
        target["test_id"] = test_id
    elif is_stable_selector(selector):
        target["selector"] = selector
    elif role and text and role not in {"textbox", "input", "textarea", "select"}:
        target["role"] = role
        target["contains_text"] = text
    elif name:
        target["selector"] = f'{str(event.get("tag") or "input").lower()}[name="{css_attr_escape(name)}"]'
    elif aria_label:
        target["label"] = aria_label
    elif placeholder:
        target["label"] = placeholder
    elif text:
        target["contains_text"] = text
    elif selector:
        target["selector"] = selector

    if role:
        target.setdefault("role", role)
    return target


def is_stable_selector(selector: str) -> bool:
    if not selector:
        return False
    if ":nth-of-type" in selector or " > " in selector:
        return False
    return selector.startswith("#") or "[data-testid=" in selector or "[data-test=" in selector or "[data-qa=" in selector


def selector_quality_report(events: list[dict[str, Any]]) -> dict[str, Any]:
    entries = []
    for index, event in enumerate(compact_recorded_events(events), start=1):
        event_type = str(event.get("type") or "")
        if event_type not in {"click", "input"}:
            continue
        target = target_from_recorded_event(event)
        assessment = assess_recorded_target(event, target)
        entries.append(
            {
                "index": index,
                "event_type": event_type,
                "step_id": f"{'click' if event_type == 'click' else 'type'}_{index}",
                "role": str(event.get("role") or ""),
                "text": str(event.get("text") or ""),
                "original_selector": str(event.get("selector") or ""),
                "target": target,
                **assessment,
            }
        )
    counts: dict[str, int] = {}
    for entry in entries:
        level = str(entry["level"])
        counts[level] = counts.get(level, 0) + 1
    weakest = "none"
    for level in ("fragile", "weak", "ok", "good", "excellent"):
        if counts.get(level):
            weakest = level
            break
    return {
        "total_targets": len(entries),
        "counts": counts,
        "weakest_level": weakest,
        "entries": entries,
    }


def assess_recorded_target(event: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    selector = str(event.get("selector") or "")
    if target.get("test_id"):
        return {
            "level": "excellent",
            "score": 100,
            "reason": "Uses data-testid/data-test/data-qa.",
            "suggestion": "",
        }
    if target.get("selector"):
        chosen = str(target.get("selector") or "")
        if is_fragile_selector(chosen):
            return {
                "level": "fragile",
                "score": 25,
                "reason": "Uses a structural selector that can change when layout changes.",
                "suggestion": "Add a data-testid, stable id, accessible label, or name attribute to this element.",
            }
        if chosen.startswith("#") or "[data-" in chosen:
            return {
                "level": "good",
                "score": 85,
                "reason": "Uses a stable id or data attribute selector.",
                "suggestion": "",
            }
        if "[name=" in chosen or "[aria-label=" in chosen:
            return {
                "level": "good",
                "score": 80,
                "reason": "Uses a named or accessible input selector.",
                "suggestion": "",
            }
        return {
            "level": "ok",
            "score": 65,
            "reason": "Uses a selector, but it is not a preferred stable test selector.",
            "suggestion": "Prefer data-testid for frequently automated controls.",
        }
    if target.get("role") and target.get("contains_text"):
        return {
            "level": "ok",
            "score": 70,
            "reason": "Uses role plus visible text.",
            "suggestion": "Prefer data-testid if the visible text may change by locale or content.",
        }
    if target.get("label"):
        return {
            "level": "ok",
            "score": 70,
            "reason": "Uses an accessible label.",
            "suggestion": "Prefer data-testid for critical paths if labels are localized.",
        }
    if target.get("contains_text"):
        return {
            "level": "weak",
            "score": 45,
            "reason": "Uses text without a role or stable selector.",
            "suggestion": "Add role information or data-testid to reduce accidental matches.",
        }
    if is_fragile_selector(selector):
        return {
            "level": "fragile",
            "score": 20,
            "reason": "Only a structural fallback selector was available.",
            "suggestion": "Add a stable id, name, aria-label, or data-testid before relying on this workflow.",
        }
    return {
        "level": "weak",
        "score": 40,
        "reason": "Target has limited identifying information.",
        "suggestion": "Add a stable selector or accessible label.",
    }


def is_fragile_selector(selector: str) -> bool:
    return bool(selector) and (":nth-of-type" in selector or " > " in selector)


def css_attr_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def is_sensitive_recorded_event(event: dict[str, Any]) -> bool:
    text = " ".join(str(event.get(key) or "").lower() for key in ("input_type", "name", "selector", "text"))
    return any(marker in text for marker in ("password", "passwd", "token", "secret", "api_key", "apikey"))


def recording_sensitive_values(events: list[dict[str, Any]]) -> tuple[str, ...]:
    values: list[str] = []
    for event in events:
        if not isinstance(event, dict) or not is_sensitive_recorded_event(event):
            continue
        for key in ("value",):
            value = str(event.get(key) or "")
            if len(value) >= 3 and value not in values:
                values.append(value)
    return tuple(values)


def recording_sensitive_values_from_workflow(workflow: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for step in workflow.get("steps", []) if isinstance(workflow.get("steps"), list) else []:
        if not isinstance(step, dict) or step.get("sensitive") is True:
            continue
        value = step.get("value")
        if isinstance(value, str) and len(value) >= 3 and any(marker in value.lower() for marker in ("password=", "token=", "secret=", "api_key=")):
            values.append(value)
    return tuple(values)


def scrub_recording_payload(value: Any, sensitive_values: tuple[str, ...] | list[str] | set[str] = ()) -> Any:
    return scrub_secrets(value, extra_secrets=sensitive_values)


def safe_input_key(event: dict[str, Any], index: int) -> str:
    raw = str(event.get("name") or event.get("text") or f"recorded_value_{index}")
    safe = re.sub(r"[^0-9a-zA-Z_]+", "_", raw.strip()).strip("_").lower()
    if not safe:
        safe = f"recorded_value_{index}"
    if safe[0].isdigit():
        safe = f"value_{safe}"
    return safe


def safe_workflow_name(value: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z_]+", "_", str(value).strip()).strip("_")
    if not safe:
        safe = "recorded_workflow"
    if safe[0].isdigit():
        safe = f"workflow_{safe}"
    return safe


def recorded_workflow_path(workspace: Workspace, save_as: str) -> Path:
    raw = Path(str(save_as or "").strip())
    if raw.is_absolute():
        raise ValueError("Recorded workflow save path must be relative.")
    if raw.suffix.lower() not in {"", ".yaml", ".yml"}:
        raise ValueError("Recorded workflow must use .yaml or .yml extension.")
    if raw.suffix == "":
        raw = raw.with_suffix(".yaml")
    root = workspace.workflows_dir.resolve()
    path = (root / raw).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Recorded workflow save path must stay under workspace workflows/.")
    return path


def recorded_auth_state_path(name: str) -> str:
    safe = safe_auth_state_name(name)
    return f".agent-auth/{safe}.json"


def safe_auth_state_name(name: str) -> str:
    raw = str(name).strip()
    if "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError("Auth state name cannot contain path separators or traversal.")
    safe = re.sub(r"[^0-9a-zA-Z_-]+", "-", raw).strip("-_")
    if not safe:
        raise ValueError("Auth state name cannot be empty.")
    return safe


def workflow_to_yaml(workflow: dict[str, Any]) -> str:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for YAML workflows. Run: pip install PyYAML") from exc
    return yaml.safe_dump(workflow, allow_unicode=True, sort_keys=False)


def archive_recording_failure(
    workspace: Workspace,
    *,
    url: str,
    save_as: str,
    error: Exception,
    events: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = workspace.reports_dir / "recordings"
    root.mkdir(parents=True, exist_ok=True)
    report_id = f"recording-failure-{strftime('%Y%m%d-%H%M%S')}-{str(abs(hash((url, save_as, time()))) % 1000000).zfill(6)}"
    json_path = root / f"{report_id}.json"
    markdown_path = root / f"{report_id}.md"
    sensitive_values = recording_sensitive_values(events or [])
    selector_report = scrub_recording_payload(selector_quality_report(events or []), sensitive_values)
    safe_error_message = redact_secret_text(str(error), extra_secrets=sensitive_values)
    payload = {
        "schema_version": 1,
        "report_id": report_id,
        "created_at": time(),
        "workspace_root": str(workspace.root),
        "status": "failed",
        "url": url,
        "save_as": save_as,
        "error": {
            "type": error.__class__.__name__,
            "message": safe_error_message,
        },
        "recovery_hint": recorder_recovery_hint(error),
        "event_count": len(events or []),
        "selector_report": selector_report,
        "options": scrub_recording_payload(options or {}, sensitive_values),
        "json_report": str(json_path),
        "markdown_report": str(markdown_path),
    }
    payload = scrub_recording_payload(payload, sensitive_values)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(redact_secret_text(recording_failure_to_markdown(payload), extra_secrets=sensitive_values), encoding="utf-8")
    return payload


def recording_failure_to_markdown(report: dict[str, Any]) -> str:
    error = report.get("error") if isinstance(report.get("error"), dict) else {}
    lines = [
        "# Browser Recording Failure",
        "",
        f"- Report: `{report.get('report_id') or ''}`",
        f"- URL: `{report.get('url') or ''}`",
        f"- Save as: `{report.get('save_as') or ''}`",
        f"- Error type: `{error.get('type') or 'Error'}`",
        f"- Message: {error.get('message') or ''}",
        f"- Recovery: {report.get('recovery_hint') or ''}",
        f"- Recorded events before failure: {int(report.get('event_count') or 0)}",
    ]
    selector_report = report.get("selector_report") if isinstance(report.get("selector_report"), dict) else {}
    entries = selector_report.get("entries") if isinstance(selector_report.get("entries"), list) else []
    if entries:
        lines.extend(["", "## Selector Quality Before Failure", "", "| step | level | target | suggestion |", "| --- | --- | --- | --- |"])
        for entry in entries:
            target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
            lines.append(
                "| "
                f"`{entry.get('step_id') or ''}` | "
                f"`{entry.get('level') or 'unknown'}` | "
                f"`{json.dumps(target, ensure_ascii=False, sort_keys=True)}` | "
                f"{entry.get('suggestion') or ''} |"
            )
    lines.append("")
    return "\n".join(lines)


def preview_recorded_workflow(workspace: Workspace, workflow_path: Path, *, inputs: dict[str, Any]) -> dict[str, Any]:
    try:
        workflow_ref = workflow_path.relative_to(workspace.root).as_posix()
        result = run_workspace_workflow(
            workspace,
            workflow_ref,
            inputs=inputs,
            dry_run=True,
            run_profile="dry-run",
            preflight=False,
            export_report=True,
        )
    except Exception as exc:
        return {
            "status": "error",
            "ok": False,
            "error": {"type": exc.__class__.__name__, "message": str(exc)},
            "recovery_hint": recorder_recovery_hint(exc),
        }
    failed = [step for step in result.steps if str(step.status.value) == "failed"]
    return {
        "status": "success" if not failed else "failed",
        "ok": not failed,
        "run_id": result.run_id,
        "run_dir": str(result.run_dir),
        "failed_step": failed[0].id if failed else None,
        "steps": [
            {
                "id": step.id,
                "action": step.action,
                "status": step.status.value,
                "message": step.message,
            }
            for step in result.steps
        ],
    }


def queue_recorded_workflow(
    workspace: Workspace,
    workflow_path: Path,
    *,
    inputs_path: Path | None,
    validation_valid: bool,
    preflight_ok: bool | None,
    preview: dict[str, Any] | None,
    empty_input_keys: tuple[str, ...],
    priority: int = 0,
    max_retries: int = 0,
) -> tuple[str, str, dict[str, Any] | None]:
    if not validation_valid:
        return "blocked", "Recorded workflow is not valid; fix validation issues before queueing.", None
    if preflight_ok is False:
        return "blocked", "Recorded workflow preflight failed; fix capability blockers before queueing.", None
    if preview is not None and preview.get("ok") is False:
        return "blocked", "Recorded workflow preview failed; fix selectors or assertions before queueing.", None
    if empty_input_keys:
        return "blocked", "Fill the generated inputs template before queueing the recorded workflow.", None
    workflow_ref = workspace_workflow_ref(workspace, workflow_path)
    inputs_ref = inputs_path.name if inputs_path is not None else None
    task = submit_queue_task(
        workspace,
        workflow_ref,
        inputs_file=inputs_ref,
        priority=priority,
        max_retries=max_retries,
        run_profile="dry-run",
        dry_run=True,
        metadata={"source": "browser_recorder", "workflow_path": workflow_ref},
    )
    return "submitted", f"Queued recorded workflow: {task.task_id}.", {
        "task_id": task.task_id,
        "workflow": task.workflow,
        "status": task.status,
        "inputs_file": task.inputs_file,
        "run_profile": task.run_profile,
        "dry_run": task.dry_run,
        "priority": task.priority,
        "max_retries": task.max_retries,
    }


def workspace_workflow_ref(workspace: Workspace, workflow_path: Path) -> str:
    try:
        return workflow_path.relative_to(workspace.root).as_posix()
    except ValueError:
        return workflow_path.name


def recorded_result_to_dict(result: BrowserRecordResult) -> dict[str, Any]:
    workflow_ref = None
    inputs_ref = None
    if result.workflow_path is not None:
        parts = result.workflow_path.parts
        if "workflows" in parts:
            workflow_ref = Path(*parts[parts.index("workflows") :]).as_posix()
        else:
            workflow_ref = result.workflow_path.name
    if result.inputs_path is not None:
        inputs_ref = result.inputs_path.name
    payload = {
        "workflow_path": str(result.workflow_path) if result.workflow_path else None,
        "inputs_path": str(result.inputs_path) if result.inputs_path else None,
        "input_keys": list(result.input_keys),
        "empty_input_keys": list(result.empty_input_keys),
        "suggested_run": {
            "workflow": workflow_ref,
            "inputs_file": inputs_ref,
        },
        "event_count": result.event_count,
        "workflow": result.workflow,
        "validation": {
            "valid": result.validation.valid,
            "workflow_name": result.validation.workflow_name,
            "issues": result.validation.issues,
        },
        "preflight": None
        if result.preflight is None
        else {
            "ok": result.preflight.ok,
            "missing_required_capabilities": result.preflight.missing_required_capabilities,
            "unavailable_used_capabilities": result.preflight.unavailable_used_capabilities,
        },
        "preview": result.preview,
        "selector_report": result.selector_report or {"total_targets": 0, "counts": {}, "weakest_level": "none", "entries": []},
        "save": result.save or {},
        "queue": {
            "status": result.queue_status,
            "message": result.queue_message,
            "task": result.queue_task,
        },
    }
    payload["recovery_hints"] = recorded_recovery_hints(payload)
    return scrub_recording_payload(payload, recording_sensitive_values_from_workflow(result.workflow))


def recorded_result_ok(recording: dict[str, Any]) -> bool:
    validation = recording.get("validation") if isinstance(recording.get("validation"), dict) else {}
    if validation.get("valid") is not True:
        return False
    preflight = recording.get("preflight") if isinstance(recording.get("preflight"), dict) else None
    if preflight is not None and preflight.get("ok") is not True:
        return False
    preview = recording.get("preview") if isinstance(recording.get("preview"), dict) else None
    if preview is not None and preview.get("ok") is not True:
        return False
    return True


def recorded_result_to_markdown(recording: dict[str, Any]) -> str:
    validation = recording.get("validation") if isinstance(recording.get("validation"), dict) else {}
    preflight = recording.get("preflight") if isinstance(recording.get("preflight"), dict) else None
    preview = recording.get("preview") if isinstance(recording.get("preview"), dict) else None
    lines = [
        "# Browser Recording",
        "",
        f"- Status: `{'success' if recorded_result_ok(recording) else 'blocked'}`",
        f"- Workflow: `{recording.get('workflow_path') or ''}`",
        f"- Inputs template: `{recording.get('inputs_path') or 'none'}`",
        f"- Recorded events: {int(recording.get('event_count') or 0)}",
        f"- Validation: `{'valid' if validation.get('valid') else 'invalid'}`",
    ]
    input_keys = recording.get("input_keys") if isinstance(recording.get("input_keys"), list) else []
    empty_input_keys = recording.get("empty_input_keys") if isinstance(recording.get("empty_input_keys"), list) else []
    if input_keys:
        lines.append(f"- Input keys: `{', '.join(str(item) for item in input_keys)}`")
    if empty_input_keys:
        lines.append(f"- Fill before real run: `{', '.join(str(item) for item in empty_input_keys)}`")
    selector_report = recording.get("selector_report") if isinstance(recording.get("selector_report"), dict) else {}
    if selector_report:
        lines.append(
            f"- Selector quality: `{selector_report.get('weakest_level') or 'none'}` "
            f"({int(selector_report.get('total_targets') or 0)} targets)"
        )
    queue = recording.get("queue") if isinstance(recording.get("queue"), dict) else {}
    if queue:
        lines.append(f"- Queue: `{queue.get('status') or 'not_requested'}`")
    save = recording.get("save") if isinstance(recording.get("save"), dict) else {}
    if save and save.get("path"):
        lines.append(f"- Save path: `{save.get('path')}`")
    recovery_hints = recording.get("recovery_hints") if isinstance(recording.get("recovery_hints"), list) else []
    if recovery_hints:
        lines.extend(["", "## Recovery", ""])
        lines.extend(f"- {hint}" for hint in recovery_hints)
    issues = validation.get("issues") if isinstance(validation.get("issues"), list) else []
    if issues:
        lines.extend(["", "## Validation Issues", ""])
        for issue in issues:
            if isinstance(issue, dict):
                lines.append(f"- `{issue.get('code') or 'issue'}` {issue.get('message') or ''}".rstrip())
            else:
                lines.append(f"- {issue}")
    if preflight is not None:
        lines.extend(["", "## Preflight", "", f"- OK: `{bool(preflight.get('ok'))}`"])
        missing = preflight.get("missing_required_capabilities") if isinstance(preflight.get("missing_required_capabilities"), list) else []
        unavailable = preflight.get("unavailable_used_capabilities") if isinstance(preflight.get("unavailable_used_capabilities"), list) else []
        if missing:
            lines.append(f"- Missing required: `{', '.join(str(item) for item in missing)}`")
        if unavailable:
            lines.append(f"- Unavailable used: `{', '.join(str(item) for item in unavailable)}`")
    if preview is not None:
        lines.extend(["", "## Preview Run", "", f"- Status: `{preview.get('status') or 'unknown'}`", f"- Run: `{preview.get('run_id') or ''}`"])
        reason = preview.get("reason")
        if reason:
            lines.append(f"- Reason: `{reason}`")
        failed_step = preview.get("failed_step")
        if failed_step:
            lines.append(f"- Failed step: `{failed_step}`")
        error = preview.get("error") if isinstance(preview.get("error"), dict) else {}
        if error:
            lines.append(f"- Error: {error.get('message') or error.get('type') or 'unknown'}")
    if selector_report:
        entries = selector_report.get("entries") if isinstance(selector_report.get("entries"), list) else []
        lines.extend(["", "## Selector Quality", ""])
        if not entries:
            lines.append("No recorded click/type targets.")
        else:
            lines.extend(["| step | level | score | target | suggestion |", "| --- | --- | ---: | --- | --- |"])
            for entry in entries:
                target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
                target_text = json.dumps(target, ensure_ascii=False, sort_keys=True)
                suggestion = str(entry.get("suggestion") or "")
                lines.append(
                    "| "
                    f"`{entry.get('step_id') or ''}` | "
                    f"`{entry.get('level') or 'unknown'}` | "
                    f"{int(entry.get('score') or 0)} | "
                    f"`{target_text}` | "
                    f"{suggestion or ''} |"
                )
    if queue:
        lines.extend(["", "## Queue", "", f"- Status: `{queue.get('status') or 'not_requested'}`"])
        if queue.get("message"):
            lines.append(f"- Message: {queue.get('message')}")
        task = queue.get("task") if isinstance(queue.get("task"), dict) else None
        if task:
            lines.append(f"- Task: `{task.get('task_id') or ''}`")
            lines.append(f"- Workflow: `{task.get('workflow') or ''}`")
    if save and save.get("diff"):
        lines.append("")
        lines.append(workflow_diff_to_markdown(str(save.get("diff") or "")).rstrip())
    suggested = recording.get("suggested_run") if isinstance(recording.get("suggested_run"), dict) else {}
    workflow = suggested.get("workflow")
    inputs_file = suggested.get("inputs_file")
    if workflow:
        command = f"workspace-run --root <workspace> --workflow {workflow}"
        if inputs_file:
            command += f" --inputs-file {inputs_file}"
        lines.extend(["", "## Next Run", "", f"- `{command}`"])
    lines.append("")
    return "\n".join(lines)


def recorded_recovery_hints(recording: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    empty_input_keys = recording.get("empty_input_keys") if isinstance(recording.get("empty_input_keys"), list) else []
    if empty_input_keys:
        hints.append("Fill the generated inputs template before running the recorded workflow with real credentials.")
    validation = recording.get("validation") if isinstance(recording.get("validation"), dict) else {}
    if validation.get("valid") is False:
        hints.append("Fix the validation issues in the generated workflow before running it.")
    preflight = recording.get("preflight") if isinstance(recording.get("preflight"), dict) else None
    if preflight is not None and preflight.get("ok") is False:
        missing = preflight.get("missing_required_capabilities") if isinstance(preflight.get("missing_required_capabilities"), list) else []
        unavailable = preflight.get("unavailable_used_capabilities") if isinstance(preflight.get("unavailable_used_capabilities"), list) else []
        if missing or unavailable:
            names = ", ".join(str(item) for item in [*missing, *unavailable])
            hints.append(f"Install or enable the missing browser capabilities before previewing: {names}.")
        else:
            hints.append("Resolve the preflight blockers before previewing the recorded workflow.")
    preview = recording.get("preview") if isinstance(recording.get("preview"), dict) else None
    if preview is not None and preview.get("ok") is False:
        hint = str(preview.get("recovery_hint") or "").strip()
        if hint:
            hints.append(hint)
        elif preview.get("failed_step"):
            hints.append(f"Inspect and adjust the selector or assertion for preview step `{preview.get('failed_step')}`.")
        else:
            hints.append("Open the preview run report and adjust the generated selectors or assertions.")
    selector_report = recording.get("selector_report") if isinstance(recording.get("selector_report"), dict) else {}
    entries = selector_report.get("entries") if isinstance(selector_report.get("entries"), list) else []
    fragile_steps = [str(entry.get("step_id") or "") for entry in entries if str(entry.get("level") or "") in {"fragile", "weak"}]
    if fragile_steps:
        hints.append(f"Review weak recorded selectors before relying on this workflow: {', '.join(fragile_steps)}.")
    queue = recording.get("queue") if isinstance(recording.get("queue"), dict) else {}
    if queue.get("status") == "blocked" and queue.get("message"):
        hints.append(str(queue.get("message")))
    return dedupe_hints(hints)


def dedupe_hints(hints: list[str]) -> list[str]:
    deduped = []
    for hint in hints:
        if hint and hint not in deduped:
            deduped.append(hint)
    return deduped


def recorder_recovery_hint(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, FileExistsError) or "already exists" in message:
        return "Choose a different workflow name or rerun with overwrite enabled."
    if "Playwright is not installed" in message:
        return "Install the web extras with `pip install -e .[web]`, then rerun the recorder."
    if "Executable doesn't exist" in message or "playwright install" in message.lower():
        return "Install browser binaries with `python -m playwright install chromium`, then rerun the recorder."
    if "input_template_has_empty_values" in message:
        return "Fill the generated inputs template, then run the workflow with `--inputs-file`."
    return "Review the generated workflow selectors and the latest run report, then rerun the preview."
