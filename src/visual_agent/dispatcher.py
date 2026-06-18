from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable

from .actions import DesktopActions
from .models import ActionResult, ActionStatus, ResolvedTarget
from .ocr import observe_ocr as observe_ocr_image
from .security import text_metadata
from .selector import OCRSelectorStrategy
from .models import Target
from .workflow_types import WorkflowContext


ActionHandler = Callable[[ResolvedTarget, dict[str, Any], "ActionDispatchContext"], ActionResult]


@dataclass(frozen=True)
class ActionDispatchContext:
    workflow_context: WorkflowContext
    dry_run: bool


class ActionDispatcher:
    def __init__(self, actions: DesktopActions | None = None) -> None:
        self.actions = actions or DesktopActions()
        self._handlers: dict[str, ActionHandler] = {}
        self.register("click", self._click)
        self.register("type", self._type)
        self.register("paste", self._paste)
        self.register("press_key", self._press_key)
        self.register("refresh_browser", self._refresh_browser)
        self.register("click_text", self._click_text)
        self.register("wait_for_text", self._wait_for_text)
        self.register("upload_file", self._upload_file)
        self.register("select_option", self._select_option)
        self.register("drag", self._drag)
        from .plugins import load_action_plugins

        load_action_plugins(self)

    def register(self, action: str, handler: ActionHandler) -> None:
        self._handlers[action] = handler

    def execute(
        self,
        action: str,
        resolved: ResolvedTarget,
        params: dict[str, Any],
        context: ActionDispatchContext,
    ) -> ActionResult:
        if action not in self._handlers:
            raise ValueError(f"Unsupported action: {action}")
        return self._handlers[action](resolved, params, context)

    @property
    def actions_available(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def _click(
        self,
        resolved: ResolvedTarget,
        params: dict[str, Any],
        context: ActionDispatchContext,
    ) -> ActionResult:
        page = context.workflow_context.resources.get("playwright_page")
        selector = selector_from_resolved(resolved)
        if page is not None and selector:
            locator = browser_locator(page, selector, params)
            actionability = playwright_actionability_snapshot(locator, action="click")
            if not bool(params.get("dry_run", context.dry_run)):
                locator.click()
                wait_after_browser_action(page, params)
            return ActionResult(
                action="click",
                status=ActionStatus.DRY_RUN if bool(params.get("dry_run", context.dry_run)) else ActionStatus.SUCCESS,
                target=resolved.target.display_name,
                point=None,
                provider=resolved.evidence.provider,
                message="playwright click skipped by dry-run"
                if bool(params.get("dry_run", context.dry_run))
                else "playwright clicked",
                metadata={"execution": "playwright", "selector": selector, "actionability": actionability},
            )
        return self.actions.click(
            resolved.click_point,
            resolved.target,
            provider=resolved.evidence.provider,
            dry_run=bool(params.get("dry_run", context.dry_run)),
        )

    def _type(
        self,
        resolved: ResolvedTarget,
        params: dict[str, Any],
        context: ActionDispatchContext,
    ) -> ActionResult:
        value = str(resolve_step_value(params, context.workflow_context))
        sensitive = is_sensitive(params, context.workflow_context)
        page = context.workflow_context.resources.get("playwright_page")
        selector = selector_from_resolved(resolved)
        if page is not None and selector:
            locator = browser_locator(page, selector, params)
            actionability = playwright_actionability_snapshot(locator, action="type")
            if not bool(params.get("dry_run", context.dry_run)):
                locator.fill(value)
                wait_after_browser_action(page, params)
            return ActionResult(
                action="type",
                status=ActionStatus.DRY_RUN if bool(params.get("dry_run", context.dry_run)) else ActionStatus.SUCCESS,
                target=resolved.target.display_name,
                point=None,
                provider=resolved.evidence.provider,
                message="playwright fill skipped by dry-run"
                if bool(params.get("dry_run", context.dry_run))
                else "playwright filled",
                metadata={"execution": "playwright", "selector": selector, "actionability": actionability, **text_metadata(value, sensitive=sensitive)},
            )
        ensure_desktop_text_input_allowed("type", params, context)
        return self.actions.type_text(
            value,
            resolved.target,
            point=resolved.click_point,
            provider=resolved.evidence.provider,
            dry_run=bool(params.get("dry_run", context.dry_run)),
            interval_seconds=float(params.get("interval_seconds", 0.01)),
            sensitive=sensitive,
        )

    def _paste(
        self,
        resolved: ResolvedTarget,
        params: dict[str, Any],
        context: ActionDispatchContext,
    ) -> ActionResult:
        value = str(resolve_step_value(params, context.workflow_context))
        sensitive = is_sensitive(params, context.workflow_context)
        page = context.workflow_context.resources.get("playwright_page")
        selector = selector_from_resolved(resolved)
        if page is not None and selector:
            locator = browser_locator(page, selector, params)
            actionability = playwright_actionability_snapshot(locator, action="paste")
            if not bool(params.get("dry_run", context.dry_run)):
                locator.fill(value)
                wait_after_browser_action(page, params)
            return ActionResult(
                action="paste",
                status=ActionStatus.DRY_RUN if bool(params.get("dry_run", context.dry_run)) else ActionStatus.SUCCESS,
                target=resolved.target.display_name,
                point=None,
                provider=resolved.evidence.provider,
                message="playwright fill skipped by dry-run"
                if bool(params.get("dry_run", context.dry_run))
                else "playwright filled",
                metadata={"execution": "playwright", "selector": selector, "actionability": actionability, **text_metadata(value, sensitive=sensitive)},
            )
        ensure_desktop_text_input_allowed("paste", params, context)
        return self.actions.paste_text(
            value,
            resolved.target,
            point=resolved.click_point,
            provider=resolved.evidence.provider,
            dry_run=bool(params.get("dry_run", context.dry_run)),
            sensitive=sensitive,
            use_clipboard=bool(params.get("clipboard", False)),
        )


    def _press_key(
        self,
        resolved: ResolvedTarget,
        params: dict[str, Any],
        context: ActionDispatchContext,
    ) -> ActionResult:
        keys_raw = params.get("keys") or params.get("key")
        if keys_raw is None:
            raise ValueError("press_key requires a 'keys' parameter (e.g. 'enter', 'ctrl+c', or ['ctrl','shift','s'])")
        keys: str | list[str] = keys_raw if isinstance(keys_raw, list) else str(keys_raw)
        return self.actions.press_key(
            keys,
            resolved.target,
            provider=resolved.evidence.provider,
            dry_run=bool(params.get("dry_run", context.dry_run)),
        )

    def _refresh_browser(
        self,
        resolved: ResolvedTarget,
        params: dict[str, Any],
        context: ActionDispatchContext,
    ) -> ActionResult:
        page = context.workflow_context.resources.get("playwright_page")
        if page is None:
            raise RuntimeError("refresh_browser requires observe_browser.")
        is_dry_run = bool(params.get("dry_run", context.dry_run))
        if not is_dry_run:
            page.reload(wait_until=str(params.get("wait_until") or "domcontentloaded"), timeout=int(params.get("timeout_ms", 10_000)))
            wait_after_browser_action(page, params)
        return ActionResult(
            action="refresh_browser",
            status=ActionStatus.DRY_RUN if is_dry_run else ActionStatus.SUCCESS,
            target=resolved.target.display_name,
            point=None,
            provider=resolved.evidence.provider,
            message="refresh skipped by dry-run" if is_dry_run else "browser refreshed",
            metadata={"execution": "playwright", "url": getattr(page, "url", None)},
        )

    def _click_text(
        self,
        resolved: ResolvedTarget,
        params: dict[str, Any],
        context: ActionDispatchContext,
    ) -> ActionResult:
        text = str(params.get("text") or params.get("label") or "").strip()
        contains_text = str(params.get("contains_text") or "").strip()
        if not text and not contains_text:
            raise ValueError("click_text requires text, label, or contains_text.")
        target = Target(text=text or None, contains_text=contains_text or None, preferred=(resolved.evidence.provider,))
        observation = observe_ocr_image(
            ocr_params(params, exclude={"text", "label", "contains_text", "dry_run", "timeout_seconds"}),
            Path(context.workflow_context.run_dir),
            synthetic_on_capture_fail=bool(params.get("synthetic_on_capture_fail", False)),
        )
        evidence = OCRSelectorStrategy().locate(target, observation)
        if evidence is None or evidence.click_point is None:
            raise LookupError(f"click_text could not find OCR text: {target.display_name}")
        result = self.actions.click(
            evidence.click_point,
            target,
            provider=evidence.provider,
            dry_run=bool(params.get("dry_run", context.dry_run)),
        )
        return replace(
            result,
            metadata={
                **result.metadata,
                "execution": "desktop",
                "selector_evidence": {
                    "provider": evidence.provider.value,
                    "confidence": evidence.confidence,
                    "reason": evidence.reason,
                    "source": observation.source,
                    "screenshot_path": str(observation.screenshot_path) if observation.screenshot_path is not None else None,
                    "engine": observation.metadata.get("engine"),
                    "engine_available": observation.metadata.get("engine_available"),
                },
            },
        )

    def _upload_file(
        self,
        resolved: ResolvedTarget,
        params: dict[str, Any],
        context: ActionDispatchContext,
    ) -> ActionResult:
        page = context.workflow_context.resources.get("playwright_page")
        if page is None:
            raise RuntimeError("upload_file requires a live browser session. Run observe_browser first.")
        raw_paths = params.get("path") or params.get("paths")
        if not raw_paths:
            raise ValueError("upload_file requires 'path' (a file path or list of file paths).")
        path_list = [raw_paths] if isinstance(raw_paths, (str, Path)) else [str(item) for item in raw_paths]
        file_paths = [Path(item).resolve() for item in path_list]
        missing = [str(item) for item in file_paths if not item.is_file()]
        if missing:
            raise FileNotFoundError(f"upload_file source file(s) not found: {', '.join(missing)}")
        selector = str(params.get("selector") or "") or selector_from_resolved(resolved)
        if not selector:
            raise ValueError("upload_file requires 'selector' (or a resolvable target) for the file input or chooser trigger.")
        is_dry_run = bool(params.get("dry_run", context.dry_run))
        if not is_dry_run:
            files = [str(item) for item in file_paths]
            if bool(params.get("via_chooser", False)):
                # the control opens a native chooser instead of being an <input type=file>
                with page.expect_file_chooser(timeout=int(params.get("timeout_ms", 10_000))) as chooser_info:
                    browser_locator(page, selector, params).click()
                chooser_info.value.set_files(files)
            else:
                browser_locator(page, selector, params).set_input_files(files)
            wait_after_browser_action(page, params)
        return ActionResult(
            action="upload_file",
            status=ActionStatus.DRY_RUN if is_dry_run else ActionStatus.SUCCESS,
            target=selector,
            point=None,
            provider=resolved.evidence.provider,
            message="upload skipped by dry-run" if is_dry_run else f"uploaded {len(file_paths)} file(s)",
            metadata={
                "execution": "playwright",
                "selector": selector,
                "files": [{"name": item.name, "size_bytes": item.stat().st_size} for item in file_paths],
                "via_chooser": bool(params.get("via_chooser", False)),
            },
        )

    def _select_option(
        self,
        resolved: ResolvedTarget,
        params: dict[str, Any],
        context: ActionDispatchContext,
    ) -> ActionResult:
        page = context.workflow_context.resources.get("playwright_page")
        if page is None:
            raise RuntimeError("select_option requires a live browser session. Run observe_browser first.")
        selector = str(params.get("selector") or "") or selector_from_resolved(resolved)
        if not selector:
            raise ValueError("select_option requires 'selector' (or a resolvable target).")
        option_args: dict[str, Any] = {}
        if params.get("value") is not None:
            option_args["value"] = params["value"]
        elif params.get("label") is not None:
            option_args["label"] = params["label"]
        elif params.get("index") is not None:
            option_args["index"] = int(params["index"])
        else:
            raise ValueError("select_option requires one of: value, label, index.")
        is_dry_run = bool(params.get("dry_run", context.dry_run))
        selected: list[str] = []
        if not is_dry_run:
            selected = list(browser_locator(page, selector, params).select_option(**option_args) or [])
            wait_after_browser_action(page, params)
        option_label = next(iter(option_args.items()))
        return ActionResult(
            action="select_option",
            status=ActionStatus.DRY_RUN if is_dry_run else ActionStatus.SUCCESS,
            target=selector,
            point=None,
            provider=resolved.evidence.provider,
            message="select skipped by dry-run" if is_dry_run else f"selected {option_label[0]}={option_label[1]}",
            metadata={"execution": "playwright", "selector": selector, "option": dict([option_label]), "selected_values": selected},
        )

    def _drag(
        self,
        resolved: ResolvedTarget,
        params: dict[str, Any],
        context: ActionDispatchContext,
    ) -> ActionResult:
        page = context.workflow_context.resources.get("playwright_page")
        if page is None:
            raise RuntimeError("drag requires a live browser session. Run observe_browser first.")
        source = str(params.get("selector") or "") or selector_from_resolved(resolved)
        destination = str(params.get("to_selector") or "")
        if not source or not destination:
            raise ValueError("drag requires 'selector' (source) and 'to_selector' (destination).")
        is_dry_run = bool(params.get("dry_run", context.dry_run))
        if not is_dry_run:
            browser_locator(page, source, params).drag_to(browser_locator(page, destination, params))
            wait_after_browser_action(page, params)
        return ActionResult(
            action="drag",
            status=ActionStatus.DRY_RUN if is_dry_run else ActionStatus.SUCCESS,
            target=f"{source} -> {destination}",
            point=None,
            provider=resolved.evidence.provider,
            message="drag skipped by dry-run" if is_dry_run else f"dragged {source} to {destination}",
            metadata={"execution": "playwright", "selector": source, "to_selector": destination},
        )

    def _wait_for_text(
        self,
        resolved: ResolvedTarget,
        params: dict[str, Any],
        context: ActionDispatchContext,
    ) -> ActionResult:
        text = str(params.get("text") or "").strip()
        contains_text = str(params.get("contains_text") or "").strip()
        if not text and not contains_text:
            raise ValueError("wait_for_text requires text or contains_text.")
        target = Target(text=text or None, contains_text=contains_text or None, preferred=(resolved.evidence.provider,))
        timeout_seconds = float(params.get("timeout_seconds", 10.0))
        poll_seconds = max(0.05, float(params.get("poll_seconds", 1.0)))
        deadline = monotonic() + timeout_seconds
        ocr_options = ocr_params(
            params,
            exclude={"text", "contains_text", "timeout_seconds", "poll_seconds", "dry_run"},
        )
        while True:
            observation = observe_ocr_image(
                ocr_options,
                Path(context.workflow_context.run_dir),
                synthetic_on_capture_fail=bool(params.get("synthetic_on_capture_fail", False)),
            )
            evidence = OCRSelectorStrategy().locate(target, observation)
            if evidence is not None:
                return ActionResult(
                    action="wait_for_text",
                    status=ActionStatus.SUCCESS,
                    target=target.display_name,
                    point=evidence.click_point,
                    provider=evidence.provider,
                    message=f"text appeared: {target.display_name}",
                    metadata={
                        "confidence": evidence.confidence,
                        "source": observation.source,
                    },
                )
            if monotonic() >= deadline:
                raise TimeoutError(f"wait_for_text timed out after {timeout_seconds:.3f}s: {target.display_name}")
            sleep(min(poll_seconds, max(0.0, deadline - monotonic())))


def resolve_step_value(params: dict[str, Any], context: WorkflowContext) -> Any:
    if "value" in params:
        return params["value"]
    if "value_from" not in params:
        raise ValueError("Missing required parameter: value or value_from")

    path = str(params["value_from"])
    if path.startswith("input."):
        return read_path(context.inputs, path.removeprefix("input."))
    raise ValueError(f"Unsupported value_from path: {path}")


def is_sensitive(params: dict[str, Any], context: WorkflowContext) -> bool:
    if bool(params.get("sensitive", False)):
        return True
    value_from = str(params.get("value_from", ""))
    if value_from.startswith("input."):
        path = value_from.removeprefix("input.")
        return path in context.sensitive_fields
    return False


def ensure_desktop_text_input_allowed(action: str, params: dict[str, Any], context: ActionDispatchContext) -> None:
    if bool(params.get("dry_run", context.dry_run)):
        return
    if bool(params.get("allow_desktop_input", False)):
        return
    raise RuntimeError(
        f"Desktop {action} is blocked by default to avoid typing into the wrong window. "
        "Use observe_browser for web UI, or set allow_desktop_input: true for intentional desktop text input."
    )


def browser_locator(page: Any, selector: str, params: dict[str, Any]) -> Any:
    """Locate an element on the page or inside an iframe (frame_selector)."""
    frame_selector = str(params.get("frame_selector") or "")
    scope = page.frame_locator(frame_selector) if frame_selector else page
    return scope.locator(selector)


def playwright_actionability_snapshot(locator: Any, *, action: str = "") -> dict[str, Any]:
    snapshot: dict[str, Any] = {"checked": True}
    checks = {
        "count": lambda: locator.count(),
        "visible": lambda: locator.first.is_visible(),
        "enabled": lambda: locator.first.is_enabled(),
        "bounding_box": lambda: locator.first.bounding_box(),
    }
    if action in {"type", "paste"}:
        checks["editable"] = lambda: locator.first.is_editable()
    for key, getter in checks.items():
        try:
            value = getter()
            if key == "bounding_box" and isinstance(value, dict):
                snapshot[key] = {name: value.get(name) for name in ("x", "y", "width", "height")}
            else:
                snapshot[key] = value
        except Exception as exc:
            snapshot[f"{key}_error"] = str(exc)[:200]
    return snapshot


def selector_from_resolved(resolved: ResolvedTarget) -> str | None:
    if resolved.evidence.handle:
        return resolved.evidence.handle
    element = resolved.evidence.metadata.get("element")
    if isinstance(element, dict) and element.get("selector"):
        return str(element["selector"])
    return None


def wait_after_browser_action(page: Any, params: dict[str, Any]) -> None:
    wait_seconds = float(params.get("wait_after_seconds", 0.2) or 0.0)
    if wait_seconds > 0 and hasattr(page, "wait_for_timeout"):
        page.wait_for_timeout(int(wait_seconds * 1000))
    if bool(params.get("wait_for_network_idle", False)) and hasattr(page, "wait_for_load_state"):
        page.wait_for_load_state("networkidle", timeout=int(params.get("network_idle_timeout_ms", 5_000)))


def ocr_params(params: dict[str, Any], *, exclude: set[str]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if key not in exclude}


def read_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(f"Input value not found: {path}")
        current = current[part]
    return current
