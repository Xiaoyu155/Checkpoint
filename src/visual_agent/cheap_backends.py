"""Cheap third-party model backends — credential resolution only.

Extracted from the old goal-intake receptionist when dispatch was removed.
What survives is the part that is still needed: finding a configured cheap
model (env var or model_api_keys.txt) so read-heavy checks can run on it
instead of burning subscription quota.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


# Cheap third-party endpoints the receptionist prefers, in order. DeepSeek first:
# in testing it was fast (~3s), answered in the user's language, and is cheap.
# MiMo works too but is a slower reasoning model that drifts to English, so it
# ranks lower for intake (its real strength is as a coding backend).
_INTAKE_BACKENDS = [
    {
        "labels": ("deepseek",),
        "model_id": "deepseek:deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "endpoint": "/chat/completions",
        "key_re": r"sk-[A-Za-z0-9_\-]{20,}",
        "env_names": (
            "CHECKPOINT_DEEPSEEK_API_KEY",
            "VISUAL_AGENT_DEEPSEEK_API_KEY",
            "DEEPSEEK_API_KEY",
        ),
        "max_tokens": 600,
    },
    {
        "labels": ("mimo", "xiaomimimo", "小米"),
        "model_id": "xiaomimimo:mimo-v2.5",
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "endpoint": "/chat/completions",
        "key_re": r"(?:sk|tp)-[A-Za-z0-9_\-]{16,}",
        "env_names": (
            "CHECKPOINT_MIMO_TOKEN",
            "CHECKPOINT_MIMO_API_KEY",
            "CHECKPOINT_XIAOMIMIMO_API_KEY",
            "VISUAL_AGENT_XIAOMIMIMO_API_KEY",
            "XIAOMIMIMO_API_KEY",
            "MIMO_API_KEY",
        ),
        "max_tokens": 1200,
    },
]


def _credential_candidates() -> list[Path]:
    paths: list[Path] = []
    override = os.environ.get("CHECKPOINT_MODEL_CREDENTIALS", "").strip()
    if override:
        # An explicit override is exclusive: whoever set it (a user pinning
        # credentials, or a test isolating from the developer's real keys)
        # does not want fallback files silently picked up.
        return [Path(override).expanduser()]
    paths.append(Path.cwd() / "model_api_keys.txt")
    # The DevPacer install root (…/src/visual_agent/goal_intake.py -> repo root).
    paths.append(Path(__file__).resolve().parents[2] / "model_api_keys.txt")
    paths.append(Path.home() / "model_api_keys.txt")
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def resolve_cheap_backends(order: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """All configured cheap backends from the credential file, in rank order.

    Returning every match (not just the best) lets the caller fail over at
    call time — a configured key can still be an out-of-balance account
    (e.g. DeepSeek HTTP 402), and the next backend may work."""
    specs = sorted(
        _INTAKE_BACKENDS,
        key=lambda spec: next(
            (idx for idx, label in enumerate(order) if label in spec["labels"]),
            len(order),
        ),
    )
    resolved: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    for spec in specs:
        api_key = next(
            (
                os.environ[name].strip()
                for name in spec.get("env_names", ())
                if os.environ.get(name, "").strip()
            ),
            "",
        )
        if not api_key or not re.fullmatch(spec["key_re"], api_key):
            continue
        seen_models.add(spec["model_id"])
        resolved.append({
            "model_id": spec["model_id"],
            "api_key": api_key,
            "base_url": spec["base_url"],
            "endpoint": spec["endpoint"],
            "max_tokens": spec["max_tokens"],
        })
    for candidate in _credential_candidates():
        try:
            text = candidate.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for spec in specs:
            if spec["model_id"] in seen_models:
                continue
            for line in text.splitlines():
                low = line.lower()
                if any(label in low for label in spec["labels"]):
                    match = re.search(spec["key_re"], line)
                    if match:
                        seen_models.add(spec["model_id"])
                        resolved.append({
                            "model_id": spec["model_id"],
                            "api_key": match.group(0),
                            "base_url": spec["base_url"],
                            "endpoint": spec["endpoint"],
                            "max_tokens": spec["max_tokens"],
                        })
                        break
    resolved.sort(key=lambda item: next(
        (idx for idx, spec in enumerate(specs) if spec["model_id"] == item["model_id"]),
        len(specs),
    ))
    return resolved


def resolve_cheap_backend(order: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """Resolve the single best configured cheap backend (None when no key)."""
    backends = resolve_cheap_backends(order)
    return backends[0] if backends else None
