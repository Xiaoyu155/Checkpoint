from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import ActionResult
from .sdk import VisualSession


_registry: dict[str, dict[str, Any]] = {}


def workflow(
    *,
    name: str,
    affects: list[str] | None = None,
    tags: list[str] | None = None,
) -> Callable[[Callable[[VisualSession], None]], Callable[[VisualSession], None]]:
    def decorator(fn: Callable[[VisualSession], None]) -> Callable[[VisualSession], None]:
        _registry[name] = {"fn": fn, "affects": affects or [], "tags": tags or []}
        return fn

    return decorator


def run_dsl_workflow(
    name: str,
    *,
    workspace: str = ".agent-workspace",
    dry_run: bool = False,
) -> list[ActionResult]:
    entry = _registry.get(name)
    if entry is None:
        raise KeyError(f"DSL workflow not found: {name!r}. Did you import the module that defines it?")
    with VisualSession(workspace=workspace, dry_run=dry_run) as session:
        entry["fn"](session)
        return session.results


def list_dsl_workflows() -> list[str]:
    return list(_registry)
