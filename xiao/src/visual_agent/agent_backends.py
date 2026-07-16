"""Coding-agent backend routing.

Pacer can route selected work to a user-configured low-cost backend instead of
burning the user's primary subscription. Backends are data-defined here and are
only enabled when credentials are present.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .env import env_get
from .model_credentials import DEFAULT_MODEL_CREDENTIAL_FILE


BACKEND_CREDENTIAL_FILENAMES = (
    DEFAULT_MODEL_CREDENTIAL_FILE,
    "ai模型api.txt",
)


# name -> backend definition. `tiers` are the cost tiers this backend serves.
# `cost_is_savings`: the Claude-CLI-reported total_cost_usd is priced at Claude
# rates, but the real spend is the backend's cheap credits — so that number is
# effectively the amount saved, not spent.
BACKENDS: dict[str, dict[str, Any]] = {
    "bugteam": {
        "base_url": "",
        "model": "",
        "provider": "openai",
        "auth_env": "ANTHROPIC_API_KEY",
        "tiers": {"cheap"},
        "cost_is_savings": True,
        "aliases": {"bugteam", "bug-team", "gpt bug team", "gptbugteam"},
        "env_prefix": "BUGTEAM",
        "requires_base_url": True,
    },
    "mimo": {
        "base_url": "https://token-plan-cn.xiaomimimo.com/anthropic",
        "model": "mimo-v2.5-pro",
        "provider": "xiaomimimo",
        "auth_env": "ANTHROPIC_API_KEY",
        "tiers": {"cheap"},
        "cost_is_savings": True,
        "aliases": {"mimo", "xiaomimimo", "xiaomi"},
        "env_prefix": "MIMO",
    },
}


def canonical_backend_name(name: str) -> str:
    value = str(name or "").strip().lower().replace("_", "-")
    for backend_name, cfg in BACKENDS.items():
        aliases = {backend_name, *set(cfg.get("aliases") or set())}
        if value in {str(alias).strip().lower().replace("_", "-") for alias in aliases}:
            return backend_name
    return value


def _backend_credential_candidates(source: str | Path = DEFAULT_MODEL_CREDENTIAL_FILE) -> list[Path]:
    explicit = Path(source)
    candidates = [explicit]
    cwd = Path.cwd()
    package_root = Path(__file__).resolve().parents[2]
    # Home paths matter for the packaged exe: its cwd is wherever the user
    # launched it and package_root points inside the PyInstaller bundle, so
    # without a home fallback the exe can never find the credential file.
    for root in (cwd, package_root, Path.home(), Path.home() / ".pacer"):
        for filename in BACKEND_CREDENTIAL_FILENAMES:
            candidates.append(root / filename)
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path.expanduser().resolve()) if path.exists() else str(path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        unique.append(path.expanduser())
    return unique


def _load_backend_token(name: str, *, source: str | Path = DEFAULT_MODEL_CREDENTIAL_FILE) -> str | None:
    canonical = canonical_backend_name(name)
    cfg = BACKENDS.get(canonical, {})
    env_prefix = str(cfg.get("env_prefix") or canonical).upper()
    env_value = (
        env_get(f"CHECKPOINT_{env_prefix}_TOKEN")
        or env_get(f"CHECKPOINT_{env_prefix}_API_KEY")
        or env_get(f"{env_prefix}_API_KEY")
    )
    if env_value:
        return env_value
    for path in _backend_credential_candidates(source):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if _line_mentions_backend(line, canonical):
                match = re.search(r"((?:sk|tp)-[A-Za-z0-9_\-]{20,})", line)
                if match:
                    return match.group(1)
    return None


def _line_mentions_backend(line: str, name: str) -> bool:
    normalized = line.lower()
    cfg = BACKENDS.get(name, {})
    aliases = {name, *set(cfg.get("aliases") or set())}
    return any(str(alias).lower() in normalized for alias in aliases)


def _load_backend_option(name: str, option: str, *, source: str | Path = DEFAULT_MODEL_CREDENTIAL_FILE) -> str | None:
    canonical = canonical_backend_name(name)
    cfg = BACKENDS.get(canonical, {})
    env_prefix = str(cfg.get("env_prefix") or canonical).upper()
    env_value = env_get(f"CHECKPOINT_{env_prefix}_{option.upper()}") or env_get(f"{env_prefix}_{option.upper()}")
    if env_value:
        return env_value
    pattern = re.compile(rf"\b{re.escape(option)}\s*[:=]\s*([^\s,;]+)", re.IGNORECASE)
    for path in _backend_credential_candidates(source):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not _line_mentions_backend(line, canonical):
                continue
            match = pattern.search(line)
            if match:
                return match.group(1).strip()
    return None


def _resolve_backend_config(name: str, *, source: str | Path = DEFAULT_MODEL_CREDENTIAL_FILE) -> dict[str, Any] | None:
    canonical = canonical_backend_name(name)
    cfg = BACKENDS.get(canonical)
    if not cfg:
        return None
    token = _load_backend_token(canonical, source=source)
    if not token:
        return None
    base_url = _load_backend_option(canonical, "base_url", source=source) or str(cfg.get("base_url") or "")
    model = _load_backend_option(canonical, "model", source=source) or str(cfg.get("model") or "")
    reasoning_effort = _load_backend_option(canonical, "reasoning_effort", source=source) or ""
    if cfg.get("requires_base_url") and not base_url:
        return None
    if not model:
        model = "gpt-4o-mini"
    return {
        "name": canonical,
        "model": model,
        "provider": str(cfg.get("provider") or "openai"),
        "cost_is_savings": bool(cfg.get("cost_is_savings")),
        "reasoning_effort": reasoning_effort,
        "env": {"ANTHROPIC_BASE_URL": base_url, str(cfg.get("auth_env") or "ANTHROPIC_API_KEY"): token},
    }


def resolve_backend_for_tier(tier: str, *, source: str | Path = DEFAULT_MODEL_CREDENTIAL_FILE) -> dict[str, Any] | None:
    """Return the backend to run a tier on, or None to use the subscription.

    A backend is only used when its token is actually configured, so with no
    credentials the system falls back to the subscription (graceful degradation).
    """
    normalized = str(tier or "").strip().lower()
    for name, cfg in BACKENDS.items():
        if normalized in cfg["tiers"]:
            backend = _resolve_backend_config(name, source=source)
            if backend:
                return backend
    return None


def resolve_backend_by_name(name: str, *, source: str | Path = DEFAULT_MODEL_CREDENTIAL_FILE) -> dict[str, Any] | None:
    """Return a configured backend by explicit name.

    This is used when the user intentionally selects a backend alias such as
    ``mimo``. Unlike tier routing, there is no subscription fallback at the call
    site: if the token is missing the caller should block loudly.
    """
    return _resolve_backend_config(name, source=source)


def redact_backend(backend: dict[str, Any] | None) -> dict[str, Any] | None:
    """A log/preview-safe view of a backend (no secret token)."""
    if not backend:
        return None
    return {
        "name": backend.get("name"),
        "model": backend.get("model"),
        "provider": backend.get("provider"),
        "cost_is_savings": backend.get("cost_is_savings"),
        "base_url": (backend.get("env") or {}).get("ANTHROPIC_BASE_URL"),
        "reasoning_effort": backend.get("reasoning_effort"),
    }


def resolve_failover_backend(*, source: str | Path = DEFAULT_MODEL_CREDENTIAL_FILE) -> dict[str, Any] | None:
    """Return a backend to keep working on when the subscription is exhausted.

    Unlike ``resolve_backend_for_tier`` this ignores the task's tier: when the
    Claude subscription is out of quota, running the task on a cheap backend
    (MiMo) beats stopping, even for a non-cheap task. Returns None when no
    backend token is configured (then the mission stops as before)."""
    for name, cfg in BACKENDS.items():
        backend = _resolve_backend_config(name, source=source)
        if backend:
            return backend
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


# ---------------------------------------------------------------------------
# Quota failure tracking: remember which agents recently failed due to quota
# so we can proactively skip them in future dispatches.
# ---------------------------------------------------------------------------

_QUOTA_FAILURE_STORE = Path.home() / ".pacer" / "quota_failures.json"
_QUOTA_FAILURE_TTL_SECONDS = 3600  # 1 hour


def record_quota_failure(agent: str, *, store_path: Path | None = None) -> None:
    """Record that an agent failed due to quota exhaustion."""
    path = store_path or _QUOTA_FAILURE_STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    data[agent.lower()] = time.time()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def has_recent_quota_failure(agent: str, *, store_path: Path | None = None) -> bool:
    """Check if an agent has recently failed due to quota exhaustion."""
    path = store_path or _QUOTA_FAILURE_STORE
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return False
    failure_time = data.get(agent.lower())
    if not failure_time:
        return False
    return (time.time() - failure_time) < _QUOTA_FAILURE_TTL_SECONDS


def clear_quota_failure(agent: str, *, store_path: Path | None = None) -> None:
    """Clear a recorded quota failure (e.g., after a successful run)."""
    path = store_path or _QUOTA_FAILURE_STORE
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return
    data.pop(agent.lower(), None)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_available_agents(*, store_path: Path | None = None) -> list[str]:
    """Return agents that haven't recently failed due to quota exhaustion."""
    from .agent_capabilities import agents_doctor
    doctor = agents_doctor()
    installed = [str(a.get("agent")) for a in doctor if isinstance(a, dict) and a.get("installed")]
    available = []
    for agent in installed:
        if not has_recent_quota_failure(agent, store_path=store_path):
            available.append(agent)
    return available
