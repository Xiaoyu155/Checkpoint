"""Subscription quota awareness (5-hour / 7-day windows) — NOT the cost ledger.

The cost ledger records dollars actually spent per worker run (API-style
accounting). This module tracks something categorically different: how much of
the user's Claude *subscription* rate-limit windows is already consumed.
Anthropic exposes no query API for that, but Claude Code pipes ``rate_limits``
(``five_hour`` / ``seven_day`` with ``used_percentage`` and ``resets_at``) into
any configured statusLine command. ``checkpoint quota-statusline`` is that
command: every render persists a snapshot, so DevPacer can read the latest one
before dispatching a worker and warn instead of running blindly into a limit.

Honest boundary: snapshots age. A reading is only as fresh as the last Claude
Code statusline render, so consumers always get the age alongside the numbers
and must not hard-fail on stale data.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUSLINE_SETTINGS_HINT = (
    'Add to ~/.claude/settings.json: {"statusLine": {"type": "command", '
    '"command": "checkpoint quota-statusline"}} — every statusline render then '
    "feeds DevPacer the live 5-hour/7-day usage."
)
CODEX_STATUS_COMMAND_HINT = (
    "Set PACER_CODEX_STATUS_COMMAND to a command that prints Codex `/usage` or "
    "`/status` output, then run `checkpoint quota --refresh-codex`."
)

_WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d"}
_CODEX_WINDOW_ALIASES = {
    "five_hour": ("five_hour", "5h", "5-hour", "5 hour", "five hour", "5小时", "五小时", "五小时额度"),
    "seven_day": (
        "seven_day",
        "7d",
        "7-day",
        "7 day",
        "weekly",
        "week",
        "weekly limit",
        "周",
        "周额度",
        "每周",
        "一周",
    ),
    "daily": ("daily", "day", "today", "今日", "今天"),
    "cumulative": ("cumulative", "total", "all time", "累计", "总计"),
}


def quota_store_path() -> Path:
    override = os.environ.get("CHECKPOINT_QUOTA_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    # Account-level, not repo-level: one subscription serves every project.
    return Path.home() / ".checkpoint" / "subscription_quota.json"


def record_statusline_snapshot(payload: dict[str, Any], *, store_path: Path | None = None) -> dict[str, Any] | None:
    """Persist the rate_limits block from one statusline stdin payload."""
    rate_limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(rate_limits, dict) or not rate_limits:
        return None
    windows: dict[str, Any] = {}
    for name, block in rate_limits.items():
        if not isinstance(block, dict):
            continue
        used = block.get("used_percentage")
        if not isinstance(used, (int, float)):
            continue
        windows[str(name)] = {
            "used_percentage": float(used),
            "resets_at": block.get("resets_at"),
        }
    if not windows:
        return None
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    provider = {
        "schema_version": 1,
        "source": "claude-code-statusline",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "claude_code_version": str(payload.get("version") or ""),
        "model": str(model.get("display_name") or model.get("id") or ""),
        "rate_limits": windows,
    }
    target = store_path or quota_store_path()
    snapshot = _merge_provider_snapshot(target, "claude-code", provider)
    return snapshot


def load_quota_snapshot(*, store_path: Path | None = None) -> dict[str, Any] | None:
    target = store_path or quota_store_path()
    try:
        snapshot = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(snapshot, dict):
        return None
    snapshot = _normalize_snapshot(snapshot)
    if not isinstance(snapshot.get("rate_limits"), dict) and not isinstance(snapshot.get("providers"), dict):
        return None
    snapshot["age_minutes"] = _age_minutes(str(snapshot.get("recorded_at") or ""))
    for provider in dict(snapshot.get("providers") or {}).values():
        if isinstance(provider, dict):
            provider["age_minutes"] = _age_minutes(str(provider.get("recorded_at") or ""))
    return snapshot


def quota_status(snapshot: dict[str, Any] | None, *, warn_at: float = 80.0) -> dict[str, Any]:
    """Summarize a snapshot into level ok/warn/unknown plus human messages."""
    if snapshot is None:
        return {
            "level": "unknown",
            "messages": [
                "No subscription quota snapshot yet. " + STATUSLINE_SETTINGS_HINT,
                CODEX_STATUS_COMMAND_HINT,
            ],
        }
    messages: list[str] = []
    level = "ok"
    providers = _providers(snapshot)
    for provider_name, provider in providers.items():
        for name, block in dict(provider.get("rate_limits") or {}).items():
            used = _used_percentage(block)
            if used is None:
                continue
            label = _WINDOW_LABELS.get(name, name)
            if used >= warn_at:
                level = "warn"
                reset_text = _format_reset(block.get("resets_at"))
                messages.append(
                    f"{_provider_label(provider_name)} {label} window is {used:.0f}% used{reset_text}; prefer cheap-tier"
                    " routing or postpone non-urgent missions."
                )
        if str(provider.get("status") or "") in {"unconfigured", "failed", "unavailable"}:
            if level == "ok":
                level = "unknown"
            if provider.get("message"):
                messages.append(str(provider["message"]))
    age = snapshot.get("age_minutes")
    if isinstance(age, (int, float)) and age > 360:
        messages.append(f"Quota snapshot is {age / 60.0:.1f}h old; treat the numbers as approximate.")
    return {"level": level, "messages": messages}


def format_statusline(payload: dict[str, Any], snapshot: dict[str, Any] | None) -> str:
    """One-line statusline text; Claude Code displays whatever we print."""
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    parts = [str(model.get("display_name") or model.get("id") or "Claude")]
    for name, block in dict((snapshot or {}).get("rate_limits") or {}).items():
        label = _WINDOW_LABELS.get(name, name)
        parts.append(f"{label} {float(block.get('used_percentage') or 0.0):.0f}%")
    if snapshot is None:
        parts.append("quota n/a")
    return " | ".join(parts)


def quota_to_markdown(snapshot: dict[str, Any] | None) -> str:
    lines = ["## Subscription Quota (rate-limit windows)", ""]
    if snapshot is None:
        lines.append("No snapshot recorded yet.")
        lines.append("")
        lines.append(STATUSLINE_SETTINGS_HINT)
        lines.append(CODEX_STATUS_COMMAND_HINT)
        return "\n".join(lines)
    lines.append(f"Recorded: {snapshot.get('recorded_at')} ({_age_text(snapshot.get('age_minutes'))})")
    lines.append("")
    for provider_name, provider in _providers(snapshot).items():
        lines.append(f"### {_provider_label(provider_name)}")
        if provider.get("model"):
            lines.append(f"- Model: {provider.get('model')}")
        if provider.get("source"):
            lines.append(f"- Source: `{provider.get('source')}`")
        if provider.get("status"):
            lines.append(f"- Status: `{provider.get('status')}`")
        for name, block in dict(provider.get("rate_limits") or {}).items():
            label = _WINDOW_LABELS.get(name, name)
            used = _used_percentage(block)
            if used is None:
                continue
            remaining = 100.0 - used
            lines.append(f"- {label} window: {used:.1f}% used / {remaining:.1f}% remaining{_format_reset(block.get('resets_at'))}")
        if provider.get("raw_excerpt"):
            lines.append(f"- Raw excerpt: `{provider.get('raw_excerpt')}`")
        if not provider.get("rate_limits") and provider.get("message"):
            lines.append(f"- {provider.get('message')}")
        lines.append("")
    status = quota_status(snapshot)
    for message in status["messages"]:
        lines.append(f"- ⚠ {message}")
    lines.append("")
    lines.append("Note: this is the subscription window, not API spend — the cost ledger tracks dollars separately.")
    return "\n".join(lines)


def record_codex_usage_snapshot(
    text: str,
    *,
    store_path: Path | None = None,
    source: str = "codex-usage-output",
) -> dict[str, Any]:
    """Persist Codex `/usage` or `/status` text after parsing quota windows."""
    windows = parse_codex_usage_text(text)
    provider: dict[str, Any] = {
        "schema_version": 1,
        "source": source,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok" if windows else "unparsed",
        "rate_limits": windows,
        "raw_excerpt": _excerpt(text),
    }
    if not windows:
        provider["message"] = "Codex output did not expose parseable quota windows."
    return _merge_provider_snapshot(store_path or quota_store_path(), "codex", provider)


def refresh_codex_quota_snapshot(
    *,
    command: str | None = None,
    store_path: Path | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Run a user-provided Codex status command and persist the parsed result.

    Codex currently documents `/usage` and `/status` as TUI slash commands, not
    as stable non-interactive subcommands. Pacer therefore accepts an explicit
    local command wrapper instead of guessing at private CLI internals.
    """
    resolved = str(command or os.environ.get("PACER_CODEX_STATUS_COMMAND") or os.environ.get("CHECKPOINT_CODEX_STATUS_COMMAND") or "").strip()
    if not resolved:
        provider = {
            "schema_version": 1,
            "source": "codex-status-command",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "status": "unconfigured",
            "rate_limits": {},
            "message": CODEX_STATUS_COMMAND_HINT,
        }
        snapshot = _merge_provider_snapshot(store_path or quota_store_path(), "codex", provider)
        return {"ok": False, "reason": "codex_status_command_missing", "snapshot": snapshot, "message": CODEX_STATUS_COMMAND_HINT}
    try:
        completed = subprocess.run(
            resolved,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        provider = {
            "schema_version": 1,
            "source": "codex-status-command",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "status": "failed",
            "rate_limits": {},
            "message": f"Codex status command timed out after {timeout_seconds:.0f}s.",
        }
        snapshot = _merge_provider_snapshot(store_path or quota_store_path(), "codex", provider)
        return {"ok": False, "reason": "codex_status_command_timeout", "snapshot": snapshot}
    output = (completed.stdout or completed.stderr or "").strip()
    snapshot = record_codex_usage_snapshot(output, store_path=store_path, source="codex-status-command")
    provider = dict((snapshot.get("providers") or {}).get("codex") or {})
    if completed.returncode != 0:
        provider["status"] = "failed"
        provider["message"] = f"Codex status command exited {completed.returncode}."
        snapshot = _merge_provider_snapshot(store_path or quota_store_path(), "codex", provider)
        return {"ok": False, "reason": "codex_status_command_failed", "exit_code": completed.returncode, "snapshot": snapshot}
    return {"ok": bool(provider.get("rate_limits")), "reason": "" if provider.get("rate_limits") else "codex_status_unparsed", "snapshot": snapshot}


def parse_codex_usage_text(text: str) -> dict[str, Any]:
    """Parse common Codex `/usage` and `/status` quota wording into windows."""
    raw = str(text or "")
    windows: dict[str, Any] = {}
    for canonical, aliases in _CODEX_WINDOW_ALIASES.items():
        for alias in aliases:
            found = _find_window_percent(raw, alias)
            if found is None:
                continue
            used, remaining = found
            windows[canonical] = {
                "used_percentage": used,
                "remaining_percentage": remaining,
            }
            break
    return windows


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def quota_summary(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    providers = _providers(snapshot or {})
    max_used = 0.0
    provider_count = 0
    windows: list[dict[str, Any]] = []
    for provider_name, provider in providers.items():
        provider_count += 1
        for name, block in dict(provider.get("rate_limits") or {}).items():
            used = _used_percentage(block)
            if used is None:
                continue
            max_used = max(max_used, used)
            windows.append(
                {
                    "provider": provider_name,
                    "label": _WINDOW_LABELS.get(name, name),
                    "used_percentage": round(used, 1),
                    "remaining_percentage": round(100.0 - used, 1),
                }
            )
    status = quota_status(snapshot)
    return {
        "level": status.get("level"),
        "max_used_percentage": round(max_used, 1),
        "provider_count": provider_count,
        "windows": windows,
        "messages": status.get("messages", []),
    }


def _age_minutes(recorded_at: str) -> float | None:
    try:
        recorded = datetime.fromisoformat(recorded_at)
    except ValueError:
        return None
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - recorded).total_seconds() / 60.0)


def _age_text(age: Any) -> str:
    if not isinstance(age, (int, float)):
        return "age unknown"
    if age < 60:
        return f"{age:.0f} min ago"
    return f"{age / 60.0:.1f}h ago"


def _format_reset(resets_at: Any) -> str:
    if not isinstance(resets_at, (int, float)) or resets_at <= 0:
        return ""
    try:
        moment = datetime.fromtimestamp(float(resets_at), tz=timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return ""
    return f", resets {moment.strftime('%m-%d %H:%M')}"


def _merge_provider_snapshot(target: Path, provider_name: str, provider: dict[str, Any]) -> dict[str, Any]:
    existing = _read_snapshot(target) or {}
    snapshot = _normalize_snapshot(existing)
    providers = dict(snapshot.get("providers") or {})
    providers[provider_name] = dict(provider)
    latest_recorded = str(provider.get("recorded_at") or snapshot.get("recorded_at") or "")
    snapshot.update(
        {
            "schema_version": 2,
            "source": "multi-provider",
            "recorded_at": latest_recorded,
            "providers": providers,
        }
    )
    claude = providers.get("claude-code") if isinstance(providers.get("claude-code"), dict) else None
    if claude:
        snapshot["rate_limits"] = claude.get("rate_limits", {})
        snapshot["model"] = claude.get("model", "")
    else:
        # Top-level rate_limits is the legacy Claude compatibility field.  Do
        # not mirror Codex into it, or old readers will mistake Codex quota for
        # a Claude Code statusLine snapshot.
        snapshot.pop("rate_limits", None)
        snapshot.pop("model", None)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return _normalize_snapshot(snapshot)


def _read_snapshot(target: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _normalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    result = dict(snapshot)
    raw_providers = result.get("providers") if isinstance(result.get("providers"), dict) else {}
    providers = {str(name): dict(value) for name, value in raw_providers.items() if isinstance(value, dict)}
    providers = _drop_bogus_legacy_claude_provider(providers)
    if providers:
        result["providers"] = providers
        return result
    if _should_synthesize_legacy_claude(result):
        providers = {
            **providers,
            "claude-code": {
                "schema_version": 1,
                "source": str(result.get("source") or "claude-code-statusline"),
                "recorded_at": str(result.get("recorded_at") or ""),
                "model": str(result.get("model") or ""),
                "rate_limits": dict(result.get("rate_limits") or {}),
            },
        }
    if providers:
        result["providers"] = providers
    return result


def _providers(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    providers = snapshot.get("providers") if isinstance(snapshot.get("providers"), dict) else {}
    result = {str(name): dict(value) for name, value in providers.items() if isinstance(value, dict)}
    if not result and isinstance(snapshot.get("rate_limits"), dict):
        result["claude-code"] = {
            "source": str(snapshot.get("source") or "claude-code-statusline"),
            "recorded_at": str(snapshot.get("recorded_at") or ""),
            "model": str(snapshot.get("model") or ""),
            "rate_limits": dict(snapshot.get("rate_limits") or {}),
        }
    return result


def _should_synthesize_legacy_claude(snapshot: dict[str, Any]) -> bool:
    rate_limits = snapshot.get("rate_limits")
    if not isinstance(rate_limits, dict) or not rate_limits:
        return False
    source = str(snapshot.get("source") or "").lower()
    if "codex" in source or source == "multi-provider":
        return False
    schema_version = snapshot.get("schema_version")
    if schema_version == 2 or str(schema_version) == "2":
        return "claude" in source or "statusline" in source
    return not source or "claude" in source or "statusline" in source


def _drop_bogus_legacy_claude_provider(providers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Repair snapshots produced by the first multi-provider migration.

    That migration returned a normalized Codex-only snapshot with a synthetic
    ``claude-code`` provider copied from top-level Codex rate_limits.  A real
    Claude Code provider is sourced from ``claude-code-statusline``; the bogus
    one has source ``multi-provider`` and exactly mirrors Codex.
    """
    claude = providers.get("claude-code")
    codex = providers.get("codex")
    if not isinstance(claude, dict) or not isinstance(codex, dict):
        return providers
    if (
        str(claude.get("source") or "") == "multi-provider"
        and not str(claude.get("model") or "")
        and dict(claude.get("rate_limits") or {}) == dict(codex.get("rate_limits") or {})
    ):
        repaired = dict(providers)
        repaired.pop("claude-code", None)
        return repaired
    return providers


def _used_percentage(block: Any) -> float | None:
    if not isinstance(block, dict):
        return None
    used = block.get("used_percentage")
    if isinstance(used, (int, float)):
        return max(0.0, min(100.0, float(used)))
    remaining = block.get("remaining_percentage")
    if isinstance(remaining, (int, float)):
        return max(0.0, min(100.0, 100.0 - float(remaining)))
    return None


def _provider_label(provider_name: str) -> str:
    return {"claude-code": "Claude Code", "codex": "Codex"}.get(provider_name, provider_name)


def _find_window_percent(text: str, alias: str) -> tuple[float, float] | None:
    escaped = rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])"
    patterns = [
        rf"(?i){escaped}[^\n\r%]{{0,80}}?(\d+(?:\.\d+)?)\s*%\s*(?:used|usage|已用|使用)",
        rf"(?i){escaped}[^\n\r%]{{0,80}}?(?:used|usage|已用|使用)[^\n\r%]{{0,40}}?(\d+(?:\.\d+)?)\s*%",
        rf"(?i){escaped}[^\n\r%]{{0,80}}?(\d+(?:\.\d+)?)\s*%\s*(?:remaining|left|剩余|可用)",
        rf"(?i){escaped}[^\n\r%]{{0,80}}?(?:remaining|left|剩余|可用)[^\n\r%]{{0,40}}?(\d+(?:\.\d+)?)\s*%",
        rf"(?i){escaped}[^\n\r%]{{0,40}}?(\d+(?:\.\d+)?)\s*%",
        rf"(?i)(\d+(?:\.\d+)?)\s*%[^\n\r]{{0,40}}?{escaped}[^\n\r]{{0,30}}?(?:used|usage|limit|window|额度|已用|使用)",
    ]
    for idx, pattern in enumerate(patterns):
        match = re.search(pattern, text)
        if not match:
            continue
        value = max(0.0, min(100.0, float(match.group(1))))
        if 2 <= idx <= 3:
            return (100.0 - value, value)
        return (value, 100.0 - value)
    return None


def _excerpt(text: str, limit: int = 240) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    return clean[:limit]
