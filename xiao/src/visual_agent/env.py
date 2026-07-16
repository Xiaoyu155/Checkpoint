from __future__ import annotations

import os
from collections.abc import Mapping


def checkpoint_env_name(name: str) -> str:
    if name.startswith("VISUAL_AGENT_"):
        return "CHECKPOINT_" + name.removeprefix("VISUAL_AGENT_")
    return name


def env_get(name: str, default: str | None = None, *, environ: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if environ is None else environ
    checkpoint_name = checkpoint_env_name(name)
    value = source.get(checkpoint_name)
    if value is not None:
        return value
    return source.get(name, default)


def env_present(name: str, *, environ: Mapping[str, str] | None = None) -> bool:
    value = env_get(name, environ=environ)
    return bool(value)


def provider_api_key_env_names(provider: str) -> tuple[str, str]:
    suffix = f"{provider.upper()}_API_KEY"
    return f"CHECKPOINT_{suffix}", f"VISUAL_AGENT_{suffix}"
