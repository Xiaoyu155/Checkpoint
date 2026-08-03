#!/usr/bin/env python3
"""Lightweight agent/token liveness probes for long-host safety.

Used before background launch / dispatch / wave starts so Pacer fails closed
with a human reason when Codex/Claude is unusable (not installed, logged out,
or recently quota-killed) instead of spawning orphan workers.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .agent_backends import clear_quota_failure, has_recent_quota_failure


def normalize_agent_name(agent: str | None) -> str:
    name = str(agent or "codex").strip().lower().replace("_", "-")
    if name in {"", "auto", "default"}:
        return "codex"
    if name in {"claude", "claude-code", "anthropic"}:
        return "claude-code"
    if name in {"codex", "openai", "gpt"}:
        return "codex"
    return name


def quota_cache_agent_keys(agent: str | None) -> tuple[str, ...]:
    agent_norm = normalize_agent_name(agent)
    if agent_norm == "codex":
        return ("codex", "openai")
    if agent_norm == "claude-code":
        return ("claude-code", "claude")
    return (agent_norm,)


def clear_worker_agent_quota_cache(
    agent: str | None,
    *,
    store_path: Path | None = None,
) -> dict[str, Any]:
    keys = quota_cache_agent_keys(agent)
    for key in keys:
        clear_quota_failure(key, store_path=store_path)
    return {
        "agent": normalize_agent_name(agent),
        "cleared_keys": list(keys),
    }


def probe_worker_agent_liveness(
    agent: str | None = "codex",
    *,
    use_cache: bool = True,
    account_inspector: Callable[..., dict[str, Any]] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Return ``{ok, stop_reason, message, agent, details}`` for *agent*."""
    agent_norm = normalize_agent_name(agent)
    which_fn = which or shutil.which
    checked_at = datetime.now(timezone.utc).isoformat()
    base = {
        "schema_version": 1,
        "agent": agent_norm,
        "checked_at": checked_at,
        "ok": True,
        "stop_reason": "",
        "message": "",
        "details": {},
    }

    quota_cached = (
        has_recent_quota_failure(agent_norm)
        or (agent_norm == "claude-code" and has_recent_quota_failure("claude"))
        or (agent_norm == "codex" and has_recent_quota_failure("openai"))
    )
    if quota_cached:
        return {
            **base,
            "ok": False,
            "stop_reason": "quota_exhausted",
            "message": (
                "This coding assistant recently hit a usage/quota limit. "
                "Not a project bug — retry after the quota resets or switch accounts."
            ),
            "details": {"source": "recent_quota_failure"},
        }

    if agent_norm == "codex":
        inspector = account_inspector
        if inspector is None:
            from .pacer_support import inspect_codex_account

            inspector = inspect_codex_account
        inspected = inspector(use_cache=use_cache)
        account = inspected if isinstance(inspected, dict) else {"raw": inspected}
        details = dict(account) if isinstance(account, dict) else {"raw": account}
        if not details.get("installed"):
            return {
                **base,
                "ok": False,
                "stop_reason": "agent_unavailable",
                "message": "Codex CLI is not installed or not on PATH.",
                "details": details,
            }
        status = str(details.get("status") or "")
        if status == "timeout":
            return {
                **base,
                "ok": False,
                "stop_reason": "agent_unavailable",
                "message": "Codex login status probe timed out.",
                "details": details,
            }
        if status in {"probe_failed", "not_installed"}:
            return {
                **base,
                "ok": False,
                "stop_reason": "agent_unavailable",
                "message": "Could not probe Codex installation.",
                "details": details,
            }
        if not details.get("authenticated") or status == "not_authenticated":
            return {
                **base,
                "ok": False,
                "stop_reason": "not_authenticated",
                "message": (
                    "Codex is installed but not logged in. "
                    "Long-host cannot spend tokens until you sign in again."
                ),
                "details": details,
            }
        return {**base, "ok": True, "details": details}

    if agent_norm == "claude-code":
        found = which_fn("claude") or which_fn("claude.cmd")
        if not found:
            return {
                **base,
                "ok": False,
                "stop_reason": "agent_unavailable",
                "message": "Claude Code CLI is not installed or not on PATH.",
                "details": {"installed": False},
            }
        return {
            **base,
            "ok": True,
            "details": {"installed": True, "executable": found},
        }

    # Unknown agents: do not hard-block (keeps extensibility).
    return {
        **base,
        "ok": True,
        "details": {"note": "no dedicated liveness probe for this agent"},
    }


def liveness_block_payload(
    *,
    probe: dict[str, Any],
    mission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Standard blocked payload when a liveness probe fails."""
    return {
        "schema_version": 1,
        "product": "Pacer",
        "verification_engine": "Checkpoint",
        "status": "blocked",
        "stop_reason": str(probe.get("stop_reason") or "agent_unavailable"),
        "message": str(probe.get("message") or "Coding assistant is not available."),
        "mission": mission,
        "provider_liveness": probe,
    }
