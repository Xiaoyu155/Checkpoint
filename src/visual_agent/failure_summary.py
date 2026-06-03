from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from .security import scrub_secrets
from .session import clamp_ai_text, load_agent_session


def build_failure_summary(workspace: Path, *, max_chars: int = 1600) -> dict[str, Any]:
    session = load_agent_session(workspace)
    if session is None or session.latest_failure is None:
        return {"status": "no_failure", "message": "No recent failures found."}

    failure = session.latest_failure
    expected = clamp_ai_text(failure.expected, max_chars=max(80, max_chars // 8), suffix="...[truncated]")
    actual = clamp_ai_text(failure.actual, max_chars=max(80, max_chars // 8), suffix="...[truncated]")
    hint = clamp_ai_text(failure.hint, max_chars=max(80, max_chars // 8), suffix="...[truncated]")
    prompt = (
        f"The workflow '{failure.workflow}' fails at step '{failure.step_id}' ({failure.action}). "
        f"Expected: {expected}. Actual: {actual}. "
        f"Suggested fix: {hint}"
    )
    prompt = clamp_ai_text(prompt, max_chars=max(200, max_chars // 2), suffix="...[truncated]")
    payload = {
        "status": "found",
        "workflow": failure.workflow,
        "run_id": failure.run_id,
        "failed_step": {"id": failure.step_id, "action": failure.action},
        "expected": expected,
        "actual": actual,
        "hint": hint,
        "artifacts": failure.artifact_dir,
        "suggested_next_prompt": prompt,
        "token_estimate": len(prompt) // 4,
    }
    safe_payload = scrub_secrets(payload)
    while len(json.dumps(safe_payload, ensure_ascii=False, default=str)) > max_chars:
        current_prompt = str(safe_payload.get("suggested_next_prompt") or "")
        if len(current_prompt) > 120:
            safe_payload["suggested_next_prompt"] = clamp_ai_text(current_prompt, max_chars=max(120, len(current_prompt) - 120), suffix="...[truncated]")
            safe_payload["token_estimate"] = len(str(safe_payload["suggested_next_prompt"])) // 4
            continue
        for key in ("expected", "actual", "hint"):
            value = str(safe_payload.get(key) or "")
            if len(value) > 80:
                safe_payload[key] = clamp_ai_text(value, max_chars=80, suffix="...")
                break
        else:
            break
    return safe_payload
