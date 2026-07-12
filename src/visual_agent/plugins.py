from __future__ import annotations

import warnings
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dispatcher import ActionDispatcher
    from .providers import ProviderRegistry


def load_action_plugins(dispatcher: "ActionDispatcher") -> dict[str, str]:
    loaded: dict[str, str] = {}
    try:
        eps = entry_points(group="visual_agent.actions")
    except Exception:
        return loaded
    for ep in eps:
        try:
            dispatcher.register(ep.name, ep.load())
            loaded[ep.name] = ep.value
        except Exception as exc:
            warnings.warn(
                f"visual_agent: failed to load action plugin '{ep.name}' from '{ep.value}': {exc}",
                stacklevel=2,
            )
    return loaded


def load_provider_plugins(registry: "ProviderRegistry") -> dict[str, str]:
    loaded: dict[str, str] = {}
    try:
        eps = entry_points(group="visual_agent.providers")
    except Exception:
        return loaded
    for ep in eps:
        try:
            registry.register(ep.name, ep.load())
            loaded[ep.name] = ep.value
        except Exception as exc:
            warnings.warn(
                f"visual_agent: failed to load provider plugin '{ep.name}' from '{ep.value}': {exc}",
                stacklevel=2,
            )
    return loaded
