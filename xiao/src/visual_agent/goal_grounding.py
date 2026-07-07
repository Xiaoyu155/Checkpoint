"""Goal grounding — review the project's own plan documents before refusing.

The clarity gate protects tokens by refusing goals with no verifiable
definition of done. But "参照最新的开发计划继续推进" is not noise — the
definition of done *exists*, it just lives in a file the gate never read.
Bouncing that goal back as an error teaches the user the tool is useless.

So before a vague goal is rejected, this module:

1. Scans the repository for plan-looking documents (docs/*.md, roadmaps,
   TODO files) — deterministic, zero tokens.
2. Hands the goal plus document excerpts to the *cheapest* available model
   (MiMo first: plentiful prepaid credits make it the right reviewer for a
   read-heavy task; DeepSeek as fallback) and asks one question: does a
   current plan exist, and if so what is the single next actionable task?
3. Resolves the vague goal into that concrete task — dispatch proceeds — or,
   when no plan exists, produces a proposed plan and discussion questions so
   the user gets a conversation instead of an error.

Degrades to deterministic: with no API key the scan still runs and the user
is shown which documents were found and what pending items they contain.
Only a model-read plan may auto-resolve into a dispatchable goal; the
regex fallback never dispatches work on its own guess.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from .goal_intake import resolve_cheap_backend

# The grounding reviewer prefers MiMo: document review is read-heavy, and MiMo
# credits are prepaid and plentiful, so burning them on reading is free-ish.
GROUNDING_BACKEND_ORDER: tuple[str, ...] = ("mimo", "deepseek")

# Filename fragments that mark a document as plan-shaped. Scores stack.
_PLAN_NAME_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("roadmap", 5),
    ("plan", 4),
    ("计划", 4),
    ("路线", 4),
    ("vision", 3),
    ("愿景", 3),
    ("milestone", 3),
    ("backlog", 3),
    ("todo", 3),
    ("待办", 3),
    ("next", 2),
    ("下一步", 2),
    ("readme", 1),
)

# Headings whose section content counts as pending work in the fallback scan.
_PENDING_HEADING = re.compile(
    r"^#{1,6}\s.*(剩余|待办|下一步|后续|路线|计划|next|todo|remaining|backlog|roadmap|milestone)",
    re.IGNORECASE,
)
_CHECKBOX_PENDING = re.compile(r"^\s*[-*]\s*\[ \]\s*(.+)$")
_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+)$")

_MAX_DOC_EXCERPT_CHARS = 3500
_MAX_PROMPT_DOCS = 4
# MiMo is a reasoning model: reviewing several documents can burn thousands of
# tokens on reasoning before any answer text. A tight cap silently returns an
# empty content field, so give the reviewer real headroom (credits are cheap).
_GROUNDING_MAX_TOKENS = 8000

# A goal that cites a plan document is never self-contained: its definition of
# done lives in that file. Deliberately narrow — "继续开发登录页" is a normal
# goal, but "按照开发计划推进" names a document we must go read.
_PLAN_REFERENCE = re.compile(
    r"(开发计划|路线图|按照?\s*计划|参照[^，。]{0,8}计划|待办清单|roadmap|development\s+plan|dev\s+plan|the\s+plan|按计划)",
    re.IGNORECASE,
)


def goal_references_plan(goal: str) -> bool:
    """True when the goal defers to a written plan instead of stating the task."""
    return bool(_PLAN_REFERENCE.search(str(goal or "")))

_SYSTEM_PROMPT = (
    "You are the document reviewer for an autonomous coding task runner. The user "
    "gave a vague development goal that probably refers to a plan written down in "
    "their repository. You get the goal plus excerpts of candidate documents. "
    "Decide: does any document contain a current, relevant development plan? "
    "Respond with STRICT JSON only, no prose, with keys: "
    '"found" (bool), '
    '"plan_document" (the path of the document you used, or ""), '
    '"next_task_goal" (if found: ONE precise, self-contained task — the next '
    "unfinished item from the plan — written in the user's language, naming the "
    'concrete result to achieve; else ""), '
    '"acceptance_hint" (one sentence on how to verify that task, e.g. a command '
    'or observable state; else ""), '
    '"evidence" (a short quote from the document proving the task is the next '
    'step; else ""), '
    '"proposed_plan" (if NOT found: 3-6 concrete suggested steps for this '
    "repository based on what you saw; else []), "
    '"questions" (if NOT found: at most 3 short questions to settle the plan '
    "with the user; else []). Keep every field short."
)


def discover_plan_documents(repo_root: str | Path, *, limit: int = 6) -> list[dict[str, Any]]:
    """Find plan-shaped markdown files, best first. Deterministic, zero tokens."""
    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir():
        return []
    candidates: dict[Path, dict[str, Any]] = {}
    pools = [root.glob("*.md"), (root / "docs").rglob("*.md") if (root / "docs").is_dir() else []]
    for pool in pools:
        for path in pool:
            if not path.is_file() or path in candidates:
                continue
            name = path.name.lower()
            score = sum(points for keyword, points in _PLAN_NAME_KEYWORDS if keyword in name)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            candidates[path] = {
                "path": str(path),
                "rel_path": path.relative_to(root).as_posix(),
                "score": score,
                "mtime": mtime,
            }
    ranked = sorted(candidates.values(), key=lambda item: (-item["score"], -item["mtime"]))
    scored = [item for item in ranked if item["score"] > 0]
    # Keep unscored-but-recent docs only to fill spare slots; a plan-named file
    # always outranks a random recent note.
    return (scored + [item for item in ranked if item["score"] <= 0])[: max(0, int(limit))]


def extract_pending_items(text: str, *, limit: int = 8) -> list[str]:
    """Pull unfinished-work bullets out of a plan document (fallback path)."""
    items: list[str] = []
    in_pending_section = False
    for line in str(text or "").splitlines():
        checkbox = _CHECKBOX_PENDING.match(line)
        if checkbox:
            items.append(checkbox.group(1).strip())
            continue
        if line.lstrip().startswith("#"):
            in_pending_section = bool(_PENDING_HEADING.match(line.strip()))
            continue
        if in_pending_section:
            bullet = _BULLET.match(line)
            if bullet:
                items.append(bullet.group(1).strip())
    deduped: list[str] = []
    for item in items:
        if item and item not in deduped:
            deduped.append(item)
    return deduped[: max(0, int(limit))]


def ground_goal(
    goal: str,
    *,
    repo_root: str | Path,
    enable_model: bool = True,
    timeout_seconds: float = 90.0,
    model_call: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Review the repo's plan documents and try to land the vague goal on one.

    Never raises. ``resolved`` is True only when a model actually read a plan
    and produced a concrete next task; the deterministic fallback reports what
    it found but leaves the decision to the user."""
    clean = str(goal or "").strip()
    documents = discover_plan_documents(repo_root)
    payload: dict[str, Any] = {
        "resolved": False,
        "source": "deterministic",
        "input_goal": clean,
        "documents_reviewed": [doc["rel_path"] for doc in documents],
        "plan_document": "",
        "grounded_goal": "",
        "acceptance_hint": "",
        "evidence": "",
        "pending_items": [],
        "proposed_plan": [],
        "questions": [],
    }
    if documents:
        best = documents[0]
        pending = extract_pending_items(_read_head(best["path"], 60_000), limit=8)
        payload["plan_document"] = best["rel_path"]
        payload["pending_items"] = pending
        if pending:
            payload["proposed_plan"] = pending
            payload["questions"] = [
                f"在 {best['rel_path']} 里找到这些待办，确认要先做哪一条？（把它写进目标即可派发）",
            ]
        else:
            payload["questions"] = [
                f"找到了 {best['rel_path']}，但没识别出明确的待办清单。计划里下一步要做什么？",
            ]
    else:
        payload["questions"] = [
            "项目里没找到开发计划文档（docs/*.md、roadmap、TODO 都没有）。要先一起定一个计划吗？",
            "这个项目当前最想实现的一个具体功能或修复是什么？",
        ]

    if not clean or not enable_model:
        return payload

    call = model_call or _default_model_call
    if model_call is None and resolve_cheap_backend(GROUNDING_BACKEND_ORDER) is None:
        return payload
    try:
        raw = call(
            goal=clean,
            documents=documents,
            timeout_seconds=timeout_seconds,
        )
        parsed = _parse_grounding_json(raw)
    except Exception as exc:  # noqa: BLE001 - grounding must never break the mission
        payload["model_error"] = str(exc)[:200]
        return payload

    payload["source"] = "model"
    reviewed = set(payload["documents_reviewed"])
    doc = str(parsed.get("plan_document") or "").strip()
    grounded = str(parsed.get("next_task_goal") or "").strip()
    if parsed.get("found") and grounded:
        payload["resolved"] = True
        payload["grounded_goal"] = grounded
        payload["plan_document"] = doc if doc in reviewed else (doc or payload["plan_document"])
        payload["acceptance_hint"] = str(parsed.get("acceptance_hint") or "").strip()
        payload["evidence"] = str(parsed.get("evidence") or "").strip()
        return payload
    proposed = [str(item).strip() for item in (parsed.get("proposed_plan") or []) if str(item).strip()]
    questions = [str(item).strip() for item in (parsed.get("questions") or []) if str(item).strip()][:3]
    if proposed:
        payload["proposed_plan"] = proposed
    if questions:
        payload["questions"] = questions
    return payload


def _default_model_call(*, goal: str, documents: list[dict[str, Any]], timeout_seconds: float) -> str:
    from .llm_providers import resolve_llm_backend, run_llm_completion

    backend_spec = resolve_cheap_backend(GROUNDING_BACKEND_ORDER)
    if backend_spec is None:
        raise RuntimeError("no cheap backend configured for grounding")
    backend = resolve_llm_backend(backend_spec["model_id"])
    parts = [f"Vague goal:\n{goal}", ""]
    if documents:
        parts.append("Candidate plan documents from the repository:")
        for doc in documents[:_MAX_PROMPT_DOCS]:
            excerpt = _read_head(doc["path"], _MAX_DOC_EXCERPT_CHARS)
            parts.append(f"--- {doc['rel_path']} ---\n{excerpt}")
    else:
        parts.append("No plan-looking documents were found in the repository.")
    parts.append("Return the JSON now.")
    return run_llm_completion(
        backend=backend,
        system_prompt=_SYSTEM_PROMPT,
        prompt="\n".join(parts),
        max_tokens=max(_GROUNDING_MAX_TOKENS, int(backend_spec.get("max_tokens") or 0)),
        api_key=backend_spec["api_key"],
        base_url=backend_spec["base_url"],
        endpoint=backend_spec["endpoint"],
        timeout_seconds=timeout_seconds,
    )


def _read_head(path: str | Path, max_chars: int) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[: max(0, int(max_chars))]


def _parse_grounding_json(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw.startswith("{"):
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group(0)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("grounding model did not return a JSON object")
    return parsed


def grounding_to_markdown(payload: dict[str, Any]) -> str:
    """Render the review for the mission report — a conversation, not an error."""
    lines = ["### 计划审查（先看项目里写了什么，再决定）", ""]
    reviewed = payload.get("documents_reviewed") or []
    if reviewed:
        lines.append("审查过的文档：" + "、".join(f"`{item}`" for item in reviewed[:6]))
    else:
        lines.append("项目里没有找到计划类文档。")
    if payload.get("model_error"):
        lines.append(f"（模型审查失败，已回退本地扫描：{payload['model_error']}）")
    lines.append("")
    if payload.get("resolved"):
        lines.append(f"**已从 `{payload.get('plan_document')}` 落地为具体任务：** {payload.get('grounded_goal')}")
        if payload.get("evidence"):
            lines.append(f"依据：{payload['evidence']}")
        if payload.get("acceptance_hint"):
            lines.append(f"建议验收：{payload['acceptance_hint']}")
        return "\n".join(lines)
    proposed = payload.get("proposed_plan") or []
    if proposed:
        lines.append("**建议的开发计划（待你确认）：**")
        lines.extend(f"{idx}. {item}" for idx, item in enumerate(proposed, start=1))
        lines.append("")
    questions = payload.get("questions") or []
    if questions:
        lines.append("**需要和你确认：**")
        lines.extend(f"- {item}" for item in questions)
    return "\n".join(lines)
