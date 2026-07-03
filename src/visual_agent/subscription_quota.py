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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUSLINE_SETTINGS_HINT = (
    'Add to ~/.claude/settings.json: {"statusLine": {"type": "command", '
    '"command": "checkpoint quota-statusline"}} — every statusline render then '
    "feeds DevPacer the live 5-hour/7-day usage."
)

_WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d"}


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
    snapshot = {
        "schema_version": 1,
        "source": "claude-code-statusline",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "claude_code_version": str(payload.get("version") or ""),
        "model": str(model.get("display_name") or model.get("id") or ""),
        "rate_limits": windows,
    }
    target = store_path or quota_store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return snapshot


def load_quota_snapshot(*, store_path: Path | None = None) -> dict[str, Any] | None:
    target = store_path or quota_store_path()
    try:
        snapshot = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("rate_limits"), dict):
        return None
    snapshot["age_minutes"] = _age_minutes(str(snapshot.get("recorded_at") or ""))
    return snapshot


def quota_status(snapshot: dict[str, Any] | None, *, warn_at: float = 80.0) -> dict[str, Any]:
    """Summarize a snapshot into level ok/warn/unknown plus human messages."""
    if snapshot is None:
        return {
            "level": "unknown",
            "messages": ["No subscription quota snapshot yet. " + STATUSLINE_SETTINGS_HINT],
        }
    messages: list[str] = []
    level = "ok"
    for name, block in dict(snapshot.get("rate_limits") or {}).items():
        used = float(block.get("used_percentage") or 0.0)
        label = _WINDOW_LABELS.get(name, name)
        if used >= warn_at:
            level = "warn"
            reset_text = _format_reset(block.get("resets_at"))
            messages.append(
                f"Subscription {label} window is {used:.0f}% used{reset_text}; prefer cheap-tier"
                " routing or postpone non-urgent missions."
            )
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
    lines = ["## Claude Subscription Quota (rate-limit windows)", ""]
    if snapshot is None:
        lines.append("No snapshot recorded yet.")
        lines.append("")
        lines.append(STATUSLINE_SETTINGS_HINT)
        return "\n".join(lines)
    lines.append(f"Recorded: {snapshot.get('recorded_at')} ({_age_text(snapshot.get('age_minutes'))})")
    if snapshot.get("model"):
        lines.append(f"Session model: {snapshot.get('model')}")
    lines.append("")
    for name, block in dict(snapshot.get("rate_limits") or {}).items():
        label = _WINDOW_LABELS.get(name, name)
        used = float(block.get("used_percentage") or 0.0)
        lines.append(f"- {label} window: {used:.1f}% used{_format_reset(block.get('resets_at'))}")
    status = quota_status(snapshot)
    for message in status["messages"]:
        lines.append(f"- ⚠ {message}")
    lines.append("")
    lines.append("Note: this is the subscription window, not API spend — the cost ledger tracks dollars separately.")
    return "\n".join(lines)


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


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
