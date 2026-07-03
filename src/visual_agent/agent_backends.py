"""Coding-agent backend routing.

Claude Code can point at an Anthropic-compatible backend via env vars. This lets
DevPacer run a *cheap-tier* task on a low-cost backend (e.g. MiMo) that burns
that provider's credits instead of the user's Claude subscription — the "borrow
the shell, swap the brain" trick. Standard/strong tiers stay on the
subscription. Backends are data-defined here; adding one is a dict entry.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .env import env_get
from .model_credentials import DEFAULT_MODEL_CREDENTIAL_FILE


# name -> backend definition. `tiers` are the cost tiers this backend serves.
# `cost_is_savings`: the Claude-CLI-reported total_cost_usd is priced at Claude
# rates, but the real spend is the backend's cheap credits — so that number is
# effectively the amount saved, not spent.
BACKENDS: dict[str, dict[str, Any]] = {
    "mimo": {
        "base_url": "https://token-plan-cn.xiaomimimo.com/anthropic",
        "model": "mimo-v2.5-pro",
        "auth_env": "ANTHROPIC_API_KEY",
        "tiers": {"cheap"},
        "cost_is_savings": True,
    },
}


def _load_backend_token(name: str, *, source: str | Path = DEFAULT_MODEL_CREDENTIAL_FILE) -> str | None:
    env_value = env_get(f"CHECKPOINT_{name.upper()}_TOKEN")
    if env_value:
        return env_value
    path = Path(source)
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if name.lower() in line.lower():
            match = re.search(r"((?:sk|tp)-[A-Za-z0-9_\-]{20,})", line)
            if match:
                return match.group(1)
    return None


def resolve_backend_for_tier(tier: str, *, source: str | Path = DEFAULT_MODEL_CREDENTIAL_FILE) -> dict[str, Any] | None:
    """Return the backend to run a tier on, or None to use the subscription.

    A backend is only used when its token is actually configured, so with no
    credentials the system falls back to the subscription (graceful degradation).
    """
    normalized = str(tier or "").strip().lower()
    for name, cfg in BACKENDS.items():
        if normalized in cfg["tiers"]:
            token = _load_backend_token(name, source=source)
            if token:
                return {
                    "name": name,
                    "model": cfg["model"],
                    "cost_is_savings": bool(cfg.get("cost_is_savings")),
                    "env": {"ANTHROPIC_BASE_URL": cfg["base_url"], cfg["auth_env"]: token},
                }
    return None


def redact_backend(backend: dict[str, Any] | None) -> dict[str, Any] | None:
    """A log/preview-safe view of a backend (no secret token)."""
    if not backend:
        return None
    return {
        "name": backend.get("name"),
        "model": backend.get("model"),
        "cost_is_savings": backend.get("cost_is_savings"),
        "base_url": (backend.get("env") or {}).get("ANTHROPIC_BASE_URL"),
    }


def resolve_failover_backend(*, source: str | Path = DEFAULT_MODEL_CREDENTIAL_FILE) -> dict[str, Any] | None:
    """Return a backend to keep working on when the subscription is exhausted.

    Unlike ``resolve_backend_for_tier`` this ignores the task's tier: when the
    Claude subscription is out of quota, running the task on a cheap backend
    (MiMo) beats stopping, even for a non-cheap task. Returns None when no
    backend token is configured (then the mission stops as before)."""
    for name, cfg in BACKENDS.items():
        token = _load_backend_token(name, source=source)
        if token:
            return {
                "name": name,
                "model": cfg["model"],
                "cost_is_savings": bool(cfg.get("cost_is_savings")),
                "env": {"ANTHROPIC_BASE_URL": cfg["base_url"], cfg["auth_env"]: token},
            }
    return None


# Signatures Claude Code emits when the subscription hits its usage cap or is
# rate-limited. Matched case-insensitively against the worker's stdout/stderr.
_QUOTA_SIGNATURES = (
    "usage limit reached",
    "usage limit",
    "rate limit",
    "rate_limit",
    "rate-limited",
    "429",
    "quota",
    "insufficient credit",
    "credit balance is too low",
    "overloaded_error",
    "resets at",
    "upgrade to increase your usage",
)


def looks_like_quota_exhaustion(*texts: str) -> bool:
    """True when worker output looks like a subscription quota / rate-limit block.

    Deliberately broad: a false positive only means one extra cheap-backend
    retry, while a miss would leave the user stuck exactly when they wanted to
    keep going."""
    blob = " ".join(str(t or "") for t in texts).lower()
    return any(sig in blob for sig in _QUOTA_SIGNATURES)
