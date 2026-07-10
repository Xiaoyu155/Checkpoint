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


def auto_intake_backend() -> dict[str, Any] | None:
    """Resolve the best available cheap intake backend from the credential file.

    Returns None when no key is found, so the receptionist works out of the box
    for anyone who has a cheap key configured, without them passing URLs/keys."""
    return resolve_cheap_backend()

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
    domain_fallback = _domain_intake_fallback(clean)
    if domain_fallback is not None and (answers is None or not answers):
        return domain_fallback
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

    # If the caller did not pin a backend, auto-resolve every configured cheap
    # backend and fail over at call time: a configured key can still be an
    # out-of-balance account (DeepSeek HTTP 402), and the next one may work.
    attempts: list[dict[str, Any]]
    if api_key is None and base_url is None and model_id is None:
        attempts = [
            {
                "model_id": auto["model_id"],
                "api_key": auto["api_key"],
                "base_url": auto["base_url"],
                "endpoint": auto["endpoint"],
                "max_tokens": auto["max_tokens"],
            }
            for auto in resolve_cheap_backends()
        ] or [{"model_id": model_id, "api_key": api_key, "base_url": base_url, "endpoint": endpoint, "max_tokens": max_tokens}]
    else:
        attempts = [{"model_id": model_id, "api_key": api_key, "base_url": base_url, "endpoint": endpoint, "max_tokens": max_tokens}]

    parsed: dict[str, Any] | None = None
    resolved_model: str | None = None
    errors: list[str] = []
    for attempt in attempts:
        try:
            text = _call_intake_model(
                clean,
                answers=answers,
                model_id=attempt["model_id"],
                timeout_seconds=timeout_seconds,
                api_key=attempt["api_key"],
                base_url=attempt["base_url"],
                endpoint=attempt["endpoint"],
                max_tokens=attempt["max_tokens"],
            )
            parsed = _parse_intake_json(text)
            resolved_model = attempt["model_id"]
            break
        except Exception as exc:  # noqa: BLE001 - try the next configured backend
            errors.append(f"{attempt['model_id'] or 'default'}: {str(exc)[:120]}")
    if parsed is None:
        fallback["model_error"] = "; ".join(errors)[:300]
        fallback["model_unavailable"] = True
        return fallback

    questions = [str(q).strip() for q in (parsed.get("clarifying_questions") or []) if str(q).strip()][:3]
    suggested = str(parsed.get("suggested_goal") or "").strip() or clean
    return {
        "source": "model",
        "model_id": resolved_model or DEFAULT_INTAKE_MODEL,
        "input_goal": clean,
        "already_clear": bool(clarity["ok"]) and not questions,
        "clarifying_questions": questions,
        "suggested_goal": suggested,
        "acceptance_hint": str(parsed.get("acceptance_hint") or "").strip(),
        "clarity": clarity,
    }


def _domain_intake_fallback(goal: str) -> dict[str, Any] | None:
    """Deterministic reception for high-signal operational tasks."""
    text = str(goal or "").strip()
    if not text:
        return None
    if re.search(r"livekit|真机|弱网|户外|噪声|语音通话|通话|生产主链路|人工验收|现场", text, re.I):
        return {
            "source": "deterministic",
            "input_goal": text,
            "already_clear": False,
            "clarifying_questions": [
                "真机设备、系统版本、测试账号和测试房间如何准备？",
                "弱网场景怎么制造或判定？例如 4G/5G、限速、丢包、移动网络波动。",
                "户外噪声场景怎么记录？例如街边、风噪、多人说话、耳机或扬声器。",
            ],
            "suggested_goal": text,
            "acceptance_hint": "默认只输出真实设备验收记录和是否建议切换生产主链路的结论，不直接改生产开关。",
            "clarity": {"ok": False, "questions": []},
        }
    if re.search(r"手机|数据线|adb|apk|flutter|android|ios|安装|传输", text, re.I):
        return {
            "source": "deterministic",
            "input_goal": text,
            "already_clear": False,
            "clarifying_questions": [
                "目标手机是 Android 还是 iOS？默认按 Android。",
                "是否允许执行构建和安装命令？默认：flutter build apk --release，然后 adb devices，再 adb install -r build/app/outputs/flutter-apk/app-release.apk。",
                "手机是否已经插线、解锁，并打开 USB 调试？",
            ],
            "suggested_goal": text,
            "acceptance_hint": "默认完成标准：APK 构建成功、adb 识别设备、安装成功并能打开 App。",
            "clarity": {"ok": False, "questions": []},
        }
    return None


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
    lines.extend(intake_dialogue_lines(payload))
    return "\n".join(lines)


def intake_dialogue_lines(payload: dict[str, Any], *, answers: list[str] | None = None) -> list[str]:
    """Return the conversational lines the workbench should show.

    The caller can feed the same payload into a Tk transcript, CLI output, or a
    web panel without hard-coding the phrasing in multiple places."""
    lines: list[str] = []
    normalized_answers = [str(item).strip() for item in (answers or []) if str(item).strip()]
    if normalized_answers:
        lines.append("你刚才补充了：")
        lines.extend(f"- {item}" for item in normalized_answers)
        lines.append("")
    if payload.get("already_clear"):
        lines.append("目标已经足够清晰，可以直接派活。")
    else:
        questions = [str(q).strip() for q in (payload.get("clarifying_questions") or []) if str(q).strip()]
        if questions:
            lines.append("我还需要确认这些点：")
            lines.extend(f"- {q}" for q in questions)
        else:
            lines.append("目前还缺少足够的细节。")
    lines.append("")
    suggested = str(payload.get("suggested_goal") or "").strip()
    input_goal = str(payload.get("input_goal") or "").strip()
    if suggested and suggested != input_goal:
        lines.append(f"建议改写：{suggested}")
    hint = str(payload.get("acceptance_hint") or "").strip()
    if hint:
        lines.append(f"建议验收：{hint}")
    return lines


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
