"""Model routing: pick a cost tier for a coding task.

The user cannot reasonably decide which model each task deserves, so DevPacer
routes tasks to a cost tier (cheap / standard / strong). Routing is rule-first
and therefore essentially free — an LLM classifier is only a fallback for
genuinely ambiguous goals (wired later). Getting a route wrong is safe:
Checkpoint verification catches a weak model's mistake and repair escalates the
tier, so a bad route wastes at most one cheap round, never ships garbage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# Signals that a task needs a strong model (reasoning-heavy, high blast radius).
_STRONG_TERMS = {
    "refactor", "architecture", "architect", "redesign", "concurrency", "concurrent",
    "async", "race", "deadlock", "thread", "security", "auth", "authentication",
    "authorization", "crypto", "migration", "migrate", "performance", "optimize algorithm",
    "distributed", "transaction", "schema", "rewrite", "design",
    "重构", "架构", "并发", "异步", "安全", "认证", "鉴权", "加密", "迁移", "性能",
    "重新设计", "分布式", "事务", "线程", "竞态", "死锁", "重写",
}
# Signals that a task is mechanical (cheap model is enough).
_CHEAP_TERMS = {
    "rename", "typo", "wording", "copy", "text", "label", "string", "comment",
    "docstring", "format", "formatting", "lint", "whitespace", "indent", "rename variable",
    "spelling", "punctuation", "constant", "message",
    "文案", "重命名", "改名", "注释", "格式", "错别字", "措辞", "改字", "标点",
    "空格", "缩进", "字符串", "提示语", "文字",
}
_SMALL_SCOPE_TERMS = {
    "small", "small-scope", "small scope", "small change", "offline-testable",
    "offline test", "unit test", "coverage", "single", "one file", "focused",
    "小改", "小任务", "小范围", "离线", "单元测试", "覆盖", "覆盖率",
}
_OBJECTIVE_FILE_RE = re.compile(
    r"(?<![\w.-])[\w./\\-]+\.(?:py|js|jsx|ts|tsx|go|rs|java|kt|cs|rb|php|html|css|md)\b",
    re.IGNORECASE,
)
_FUNCTION_CONTRACT_RE = re.compile(r"\b[a-zA-Z_]\w*\s*\([^()\n]{0,120}\)")


@dataclass(frozen=True)
class RouteDecision:
    tier: str  # cheap | standard | strong
    reason: str
    signals: dict[str, Any] = field(default_factory=dict)


# Ordering: strong beats cheap when both appear (safety-first).
def route_task(
    *,
    objective: str,
    changed_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    repeated_failure: bool = False,
) -> RouteDecision:
    text = str(objective or "").lower()
    files = [str(f) for f in (changed_files or []) if str(f).strip()]
    # Only count real product files, not Checkpoint's own workspace artifacts.
    product_files = [f for f in files if not f.replace("\\", "/").startswith(".agent-workspace")]
    criteria_count = len(acceptance_criteria or [])

    strong_hits = sorted({term for term in _STRONG_TERMS if term in text})
    cheap_hits = sorted({term for term in _CHEAP_TERMS if term in text})
    small_scope_hits = sorted({term for term in _SMALL_SCOPE_TERMS if term in text})
    word_count = len([w for w in re.split(r"\s+", str(objective or "").strip()) if w])
    objective_files = sorted(set(_OBJECTIVE_FILE_RE.findall(str(objective or ""))))
    function_contract = bool(_FUNCTION_CONTRACT_RE.search(str(objective or "")))
    exact_result_contract = "returns exactly" in text or ("返回" in text and "测试" in text)
    narrow_testable_contract = (
        len(objective_files) == 1
        and function_contract
        and exact_result_contract
        and word_count <= 45
    )

    signals = {
        "strong_terms": strong_hits,
        "cheap_terms": cheap_hits,
        "small_scope_terms": small_scope_hits,
        "product_file_count": len(product_files),
        "criteria_count": criteria_count,
        "word_count": word_count,
        "objective_files": objective_files,
        "function_contract": function_contract,
        "narrow_testable_contract": narrow_testable_contract,
        "repeated_failure": bool(repeated_failure),
    }

    # A task that already failed once escalates a tier (repair needs more reasoning,
    # matching the "repair stays strong, never downgrade" principle).
    if repeated_failure:
        return RouteDecision("strong", "A prior attempt failed; escalate to a stronger model for repair.", signals)

    # Strong wins over cheap: touching risky areas or many files needs reasoning.
    if strong_hits:
        return RouteDecision("strong", f"Reasoning-heavy signals: {', '.join(strong_hits)}.", signals)
    if len(product_files) >= 6 and not small_scope_hits:
        return RouteDecision("strong", f"Broad change across {len(product_files)} files.", signals)

    # Cheap only when the task looks mechanical AND small.
    if cheap_hits and len(product_files) <= 2 and not strong_hits:
        return RouteDecision("cheap", f"Mechanical, small-scope signals: {', '.join(cheap_hits)}.", signals)
    if narrow_testable_contract and len(product_files) <= 2:
        return RouteDecision(
            "cheap",
            "Explicit one-file function contract with an exact, offline-testable result.",
            signals,
        )

    return RouteDecision("standard", "No strong or clearly-mechanical signal; use the balanced tier.", signals)


# Cost tier -> capability-profile task_kind (which resolves to a concrete model
# role). "balanced" is the middle tier (e.g. Claude Sonnet).
TIER_TASK_KIND = {"cheap": "fast", "standard": "balanced", "strong": "implementation"}


def tier_task_kind(tier: str) -> str:
    return TIER_TASK_KIND.get(str(tier or "").strip().lower(), "balanced")
