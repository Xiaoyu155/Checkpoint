"""Goal intake — a cheap-model "receptionist" that sharpens a vague goal.

DevPacer refuses to dispatch a goal with no verifiable definition of done (that
refusal saves tokens). But rejecting the user with generic questions puts the
burden back on them. This module optionally routes the rough goal through the
*cheapest* available model to (a) ask 2-3 targeted clarifying questions and
(b) rewrite the goal into a precise, machine-ready objective plus a suggested
acceptance approach.

Two hard rules:
- Cheap by design. It uses a small model (Haiku by default, or any configured
  cheap backend), because intake must never cost more than the work.
- Degrades to deterministic. No API key, no network, or any error → it falls
  back to the existing rule-based clarity questions. The product still works
  fully offline; the model is an enhancement layer, never a dependency.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .chief_engineer import assess_goal_clarity

# Small, cheap default. Intake is a tiny classification/rewrite task; a large
# model here would violate the whole point (spend less, not more).
DEFAULT_INTAKE_MODEL = "claude-haiku-4-5-20251001"

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
        "max_tokens": 600,
    },
    {
        "labels": ("mimo", "xiaomimimo", "小米"),
        "model_id": "xiaomimimo:mimo-v2.5",
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "endpoint": "/chat/completions",
        "key_re": r"(?:sk|tp)-[A-Za-z0-9_\-]{16,}",
        "max_tokens": 1200,
    },
]


def _credential_candidates() -> list[Path]:
    paths: list[Path] = []
    override = os.environ.get("CHECKPOINT_MODEL_CREDENTIALS", "").strip()
    if override:
        paths.append(Path(override).expanduser())
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


def auto_intake_backend() -> dict[str, Any] | None:
    """Resolve the best available cheap intake backend from the credential file.

    Returns None when no key is found, so the receptionist works out of the box
    for anyone who has a cheap key configured, without them passing URLs/keys."""
    for candidate in _credential_candidates():
        try:
            text = candidate.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for spec in _INTAKE_BACKENDS:
            for line in text.splitlines():
                low = line.lower()
                if any(label in low for label in spec["labels"]):
                    match = re.search(spec["key_re"], line)
                    if match:
                        return {
                            "model_id": spec["model_id"],
                            "api_key": match.group(0),
                            "base_url": spec["base_url"],
                            "endpoint": spec["endpoint"],
                            "max_tokens": spec["max_tokens"],
                        }
    return None

_SYSTEM_PROMPT = (
    "You are a requirements intake assistant for an autonomous coding task runner. "
    "The user gives a rough development goal. Your job is to make it precise enough to "
    "verify, not to solve it. Respond with STRICT JSON only, no prose, with keys: "
    '"clarifying_questions" (array of at most 3 short questions; empty if the goal is '
    'already clear), "suggested_goal" (one precise, self-contained rewrite of the goal '
    "naming the concrete result/state to achieve; keep the user's language), "
    '"acceptance_hint" (one sentence suggesting how to verify it, e.g. a test or '
    "observable state). Keep every field short."
)


def refine_goal(
    goal: str,
    *,
    answers: list[str] | None = None,
    model_id: str | None = None,
    enable_model: bool = True,
    timeout_seconds: float = 20.0,
    api_key: str | None = None,
    base_url: str | None = None,
    endpoint: str | None = None,
    max_tokens: int = 600,
) -> dict[str, Any]:
    """Return a sharpened-goal payload, using a cheap model when available.

    ``base_url`` / ``api_key`` / ``endpoint`` let a caller point the receptionist
    at any Anthropic/OpenAI-compatible cheap endpoint (e.g. MiMo, DeepSeek).
    Never raises: on any failure it returns the deterministic fallback so the
    caller (CLI, desktop app, web) can always show *something* useful."""
    clean = str(goal or "").strip()
    clarity = assess_goal_clarity(clean, answers=answers)
    fallback = {
        "source": "deterministic",
        "input_goal": clean,
        "already_clear": bool(clarity["ok"]),
        "clarifying_questions": list(clarity["questions"]),
        "suggested_goal": clean,
        "acceptance_hint": "",
        "clarity": clarity,
    }
    if not clean or not enable_model:
        return fallback

    # If the caller did not pin a backend, auto-resolve a cheap one (e.g. the
    # user's DeepSeek key) so the receptionist just works for a novice.
    resolved_model = model_id
    if api_key is None and base_url is None and model_id is None:
        auto = auto_intake_backend()
        if auto:
            resolved_model = auto["model_id"]
            api_key = auto["api_key"]
            base_url = auto["base_url"]
            endpoint = auto["endpoint"]
            max_tokens = auto["max_tokens"]

    try:
        text = _call_intake_model(
            clean,
            answers=answers,
            model_id=resolved_model,
            timeout_seconds=timeout_seconds,
            api_key=api_key,
            base_url=base_url,
            endpoint=endpoint,
            max_tokens=max_tokens,
        )
        parsed = _parse_intake_json(text)
    except Exception as exc:  # noqa: BLE001 - any failure degrades to deterministic
        fallback["model_error"] = str(exc)[:200]
        return fallback

    questions = [str(q).strip() for q in (parsed.get("clarifying_questions") or []) if str(q).strip()][:3]
    suggested = str(parsed.get("suggested_goal") or "").strip() or clean
    return {
        "source": "model",
        "model_id": resolved_model or DEFAULT_INTAKE_MODEL,
        "input_goal": clean,
        "already_clear": not questions,
        "clarifying_questions": questions,
        "suggested_goal": suggested,
        "acceptance_hint": str(parsed.get("acceptance_hint") or "").strip(),
        "clarity": clarity,
    }


def _call_intake_model(
    goal: str,
    *,
    answers: list[str] | None,
    model_id: str | None,
    timeout_seconds: float,
    api_key: str | None = None,
    base_url: str | None = None,
    endpoint: str | None = None,
    max_tokens: int = 600,
) -> str:
    from .llm_providers import resolve_llm_backend, run_llm_completion

    backend = resolve_llm_backend(model_id or DEFAULT_INTAKE_MODEL)
    prompt = f"Rough goal:\n{goal}\n"
    if answers:
        prompt += "\nUser already answered:\n" + "\n".join(f"- {a}" for a in answers if str(a).strip())
    prompt += "\nReturn the JSON now."
    return run_llm_completion(
        backend=backend,
        system_prompt=_SYSTEM_PROMPT,
        prompt=prompt,
        max_tokens=max_tokens,
        api_key=api_key,
        base_url=base_url,
        endpoint=endpoint,
        timeout_seconds=timeout_seconds,
    )


def _parse_intake_json(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    # Models sometimes wrap JSON in prose or fences; extract the first object.
    if not raw.startswith("{"):
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group(0)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("intake model did not return a JSON object")
    return parsed


def intake_to_markdown(payload: dict[str, Any]) -> str:
    lines = ["## 目标接待（帮你把目标说清楚）", ""]
    lines.append(f"来源：{'便宜模型' if payload.get('source') == 'model' else '本地规则（未调用模型）'}")
    if payload.get("model_error"):
        lines.append(f"（模型调用失败，已回退：{payload['model_error']}）")
    lines.append("")
    if payload.get("already_clear"):
        lines.append("目标已经足够清晰，可以直接派活。")
    else:
        lines.append("建议先回答这几个问题：")
        lines.extend(f"- {q}" for q in payload.get("clarifying_questions") or [])
    lines.append("")
    if payload.get("suggested_goal") and payload["suggested_goal"] != payload.get("input_goal"):
        lines.append(f"**建议改写为：** {payload['suggested_goal']}")
    if payload.get("acceptance_hint"):
        lines.append(f"**建议验收方式：** {payload['acceptance_hint']}")
    return "\n".join(lines)


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
