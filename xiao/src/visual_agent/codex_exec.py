"""Small, failure-tolerant helpers for Codex ``exec`` integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    tomllib = None  # type: ignore[assignment]


def load_codex_user_defaults(config_path: str | Path | None = None) -> dict[str, str]:
    """Return the effective Codex model and reasoning effort.

    Codex currently supports both profile tables and sibling ``<name>.config.toml``
    profile layers. Invalid or missing user configuration degrades to empty values.
    """
    path = _codex_config_path(config_path)
    payload = _load_toml(path)
    result = {
        "model": _text(payload.get("model")),
        "reasoning_effort": _reasoning_effort(payload),
        "sandbox": _text(payload.get("sandbox_mode") or payload.get("sandbox")),
        "approval": _text(payload.get("approval_policy") or payload.get("approval")),
    }
    provider = _text(payload.get("model_provider"))
    if provider:
        result["provider"] = provider

    profile_name = _text(payload.get("profile") or payload.get("active_profile"))
    if not profile_name:
        return result

    profiles = payload.get("profiles") if isinstance(payload.get("profiles"), dict) else {}
    table_profile = profiles.get(profile_name) if isinstance(profiles.get(profile_name), dict) else {}
    _merge_defaults(result, table_profile)

    file_profile = _load_toml(path.with_name(f"{profile_name}.config.toml"))
    _merge_defaults(result, file_profile)
    return result


def parse_codex_jsonl(text: str) -> dict[str, Any] | None:
    """Parse Codex JSONL output and normalize its session and token usage."""
    evidence = parse_codex_jsonl_evidence(text)
    usage = evidence["usage"]
    if not evidence["session_id"] and not usage["num_turns"]:
        return None
    return {
        "session_id": evidence["session_id"],
        **usage,
    }


def parse_codex_jsonl_evidence(text: str) -> dict[str, Any]:
    """Parse audit evidence while retaining every completed turn.

    Codex promises JSONL on stdout, but the normal worker integration has to
    tolerate launch banners from older wrappers. Recovered prefixed JSON still
    counts as an invalid line so benchmark manifests can disclose the drift.
    """
    session_id = ""
    session_ids: list[str] = []
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
    }
    turns: list[dict[str, Any]] = []
    event_count = 0
    invalid_line_count = 0

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        event, strict = _json_object_with_strictness(line)
        if event is None:
            invalid_line_count += 1
            continue
        event_count += 1
        if not strict:
            invalid_line_count += 1
        event_type = _text(event.get("type")).lower()
        if event_type == "thread.started":
            candidate = event.get("thread_id") or event.get("session_id")
            thread = event.get("thread") if isinstance(event.get("thread"), dict) else {}
            session_id = _text(candidate or thread.get("id")) or session_id
            if session_id and session_id not in session_ids:
                session_ids.append(session_id)
            continue
        if event_type != "turn.completed":
            continue

        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        turn = event.get("turn") if isinstance(event.get("turn"), dict) else {}
        if not usage and isinstance(turn.get("usage"), dict):
            usage = turn["usage"]
        normalized = _normalize_usage(usage)
        turns.append({"index": len(turns) + 1, "usage": normalized})
        for key in totals:
            totals[key] += normalized[key]

    return {
        "session_id": session_id,
        "session_ids": session_ids,
        "turns": turns,
        "usage": {
            **totals,
            "num_turns": len(turns),
        },
        "event_count": event_count,
        "invalid_line_count": invalid_line_count,
    }


def read_codex_usage(log_path: str | Path, stdout_tail: str = "") -> dict[str, Any] | None:
    """Read a Codex worker log, falling back to the captured stdout tail."""
    text = ""
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        pass
    return parse_codex_jsonl(text if text.strip() else stdout_tail)


def is_resume_session_error(*texts: Any) -> bool:
    """Return true only for errors that clearly identify an unusable session."""
    value = "\n".join(str(item or "") for item in texts).lower()
    if not value.strip():
        return False
    subjects = ("session", "thread", "conversation", "rollout")
    failures = (
        "not found",
        "no saved",
        "does not exist",
        "unknown",
        "expired",
        "invalid",
        "failed to load",
        "could not load",
        "unable to load",
    )
    return any(subject in value for subject in subjects) and any(marker in value for marker in failures)


def is_resume_unavailable_error(*texts: Any) -> bool:
    """Return true when a resume attempt cannot be used by this CLI/session."""
    if is_resume_session_error(*texts):
        return True
    value = "\n".join(str(item or "") for item in texts).lower()
    parser_failures = (
        "unrecognized subcommand",
        "unknown subcommand",
        "invalid subcommand",
        "unexpected argument",
        "wasn't expected",
        "was not expected",
    )
    return "resume" in value and any(marker in value for marker in parser_failures)


def _codex_config_path(config_path: str | Path | None) -> Path:
    if config_path is not None:
        return Path(config_path).expanduser()
    codex_home = os.environ.get("CODEX_HOME")
    root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return root / "config.toml"


def _load_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:
        return {}
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _merge_defaults(result: dict[str, str], source: dict[str, Any]) -> None:
    if "model" in source:
        result["model"] = _text(source.get("model"))
    if "model_provider" in source:
        result["provider"] = _text(source.get("model_provider"))
    if "model_reasoning_effort" in source or "reasoning_effort" in source:
        result["reasoning_effort"] = _reasoning_effort(source)
    if "sandbox_mode" in source or "sandbox" in source:
        result["sandbox"] = _text(source.get("sandbox_mode") or source.get("sandbox"))
    if "approval_policy" in source or "approval" in source:
        result["approval"] = _text(source.get("approval_policy") or source.get("approval"))


def _reasoning_effort(source: dict[str, Any]) -> str:
    return _text(source.get("model_reasoning_effort") or source.get("reasoning_effort"))


def _json_object(raw_line: str) -> dict[str, Any] | None:
    payload, _strict = _json_object_with_strictness(raw_line)
    return payload


def _json_object_with_strictness(raw_line: str) -> tuple[dict[str, Any] | None, bool]:
    line = str(raw_line or "").strip()
    if not line:
        return None, False
    candidates: list[tuple[str, bool]] = [(line, True)]
    if "{" in line and "}" in line:
        recovered = line[line.find("{") : line.rfind("}") + 1]
        if recovered != line:
            candidates.append((recovered, False))
    for candidate, strict in candidates:
        try:
            payload = json.loads(candidate)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload, strict
    return None, False


def _normalize_usage(usage: dict[str, Any]) -> dict[str, int]:
    input_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    output_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    input_tokens = _integer(usage.get("input_tokens", usage.get("prompt_tokens")))
    cached_input_tokens = _integer(
        usage.get(
            "cached_input_tokens",
            usage.get("cache_read_input_tokens", input_details.get("cached_tokens")),
        )
    )
    output_tokens = _integer(usage.get("output_tokens", usage.get("completion_tokens")))
    reasoning_output_tokens = _integer(
        usage.get(
            "reasoning_output_tokens",
            usage.get("reasoning_tokens", output_details.get("reasoning_tokens")),
        )
    )
    explicit_total = _optional_integer(usage.get("total_tokens"))
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": explicit_total if explicit_total is not None else input_tokens + output_tokens,
    }


def _optional_integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _integer(value: Any) -> int:
    return _optional_integer(value) or 0


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""
