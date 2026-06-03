from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .actions import DesktopActions
from .models import ActionResult, ActionStatus, ResolvedTarget
from .security import text_metadata
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
            if not bool(params.get("dry_run", context.dry_run)):
                page.locator(selector).click()
            return ActionResult(
                action="click",
                status=ActionStatus.DRY_RUN if bool(params.get("dry_run", context.dry_run)) else ActionStatus.SUCCESS,
                target=resolved.target.display_name,
                point=None,
                provider=resolved.evidence.provider,
                message="playwright click skipped by dry-run"
                if bool(params.get("dry_run", context.dry_run))
                else "playwright clicked",
                metadata={"execution": "playwright", "selector": selector},
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
            if not bool(params.get("dry_run", context.dry_run)):
                page.locator(selector).fill(value)
            return ActionResult(
                action="type",
                status=ActionStatus.DRY_RUN if bool(params.get("dry_run", context.dry_run)) else ActionStatus.SUCCESS,
                target=resolved.target.display_name,
                point=None,
                provider=resolved.evidence.provider,
                message="playwright fill skipped by dry-run"
                if bool(params.get("dry_run", context.dry_run))
                else "playwright filled",
                metadata={"execution": "playwright", "selector": selector, **text_metadata(value, sensitive=sensitive)},
            )
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
            if not bool(params.get("dry_run", context.dry_run)):
                page.locator(selector).fill(value)
            return ActionResult(
                action="paste",
                status=ActionStatus.DRY_RUN if bool(params.get("dry_run", context.dry_run)) else ActionStatus.SUCCESS,
                target=resolved.target.display_name,
                point=None,
                provider=resolved.evidence.provider,
                message="playwright fill skipped by dry-run"
                if bool(params.get("dry_run", context.dry_run))
                else "playwright filled",
                metadata={"execution": "playwright", "selector": selector, **text_metadata(value, sensitive=sensitive)},
            )
        return self.actions.paste_text(
            value,
            resolved.target,
            point=resolved.click_point,
            provider=resolved.evidence.provider,
            dry_run=bool(params.get("dry_run", context.dry_run)),
            sensitive=sensitive,
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


def selector_from_resolved(resolved: ResolvedTarget) -> str | None:
    if resolved.evidence.handle:
        return resolved.evidence.handle
    element = resolved.evidence.metadata.get("element")
    if isinstance(element, dict) and element.get("selector"):
        return str(element["selector"])
    return None


def read_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(f"Input value not found: {path}")
        current = current[part]
    return current
