"""Read-only differential review of a worker's change.

The acceptance command answers one question: does the suite still pass. It says
nothing about lock ordering, swallowed exceptions, a cancel path that does not
actually cancel, or a worker that quietly did twice what was asked. Until now
Pacer had no reviewer role at all — ``reviewer_model`` was empty everywhere and
"verified" meant "exit code 0".

Three rules shape this module, and each one comes from a mistake already made:

1. **It never blocks.** A reviewer that can fail a dispatch reproduces the false
   red that the acceptance work spent a month removing. Findings are routed to a
   human; the change stands.
2. **It runs against a baseline.** A model handed a file will always find
   something. A finding that the same reviewer also reports on the pre-change
   code is a pre-existing property of the repository, not something this change
   did, and reporting it as a review finding is noise that trains people to
   ignore the report.
3. **It is read-only.** The reviewer sees the diff. It does not edit, and it
   does not get to decide what "correct product behavior" is — it cannot know
   whether the spec itself was wrong.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .git_diff import git_show_file

MAX_DIFF_CHARS = 60_000
MAX_BASE_CHARS = 60_000

#: What the reviewer is asked to look for. Deliberately narrow: these are defects
#: readable from a diff, not opinions about design.
REVIEW_CATEGORIES: dict[str, str] = {
    "concurrency": "lock ordering, races, state shared across threads or tasks",
    "swallowed_error": "exceptions caught and dropped, errors turned into silent defaults",
    "cancellation": "a cancel/timeout/abort path that does not actually stop the work",
    "scope_drift": "the change does materially more, or materially less, than the objective",
    "sensitive_surface": "auth, secrets, permissions, payments, deletion, or data exposure",
}

_SYSTEM_PROMPT = (
    "You are a principal engineer doing a read-only review. You report defects you can "
    "demonstrate from the code shown; you do not rewrite the code, and you do not comment "
    "on style, naming, or taste. If you cannot describe a concrete input or state that "
    "produces a wrong result, do not report it."
)

ReviewCompletion = Callable[[str, str], str]


def build_review_prompt(*, objective: str, code_text: str, mode: str) -> str:
    """Prompt for one review pass.

    ``mode`` is ``"change"`` (review the diff) or ``"base"`` (review the same
    files as they were before the change, to find what was already wrong).
    """
    lines: list[str] = []
    if mode == "base":
        lines.extend(
            [
                "Below is code as it exists BEFORE a change was made. Report defects that are",
                "already present in it. This is a baseline pass: its only purpose is to find",
                "what the repository was already doing, so those are not blamed on the change.",
            ]
        )
    else:
        lines.extend(
            [
                f"Objective the change was supposed to accomplish: {objective or '(not stated)'}",
                "",
                "Below is the diff of a change. Report defects introduced or left by this diff.",
            ]
        )
    lines.extend(["", "Look only for these categories:"])
    lines.extend(f"- {key}: {description}" for key, description in REVIEW_CATEGORIES.items())
    lines.extend(
        [
            "",
            "Answer with a JSON array and nothing else. Each element:",
            '  {"file": "path/as/shown", "line": 123, "category": "<one of the above>",',
            '   "summary": "one sentence stating the defect",',
            '   "failure_scenario": "concrete input or state -> wrong result"}',
            "An empty array is a valid and expected answer. Do not pad it.",
            "",
            "```",
            code_text,
            "```",
        ]
    )
    return "\n".join(lines)


def parse_review_findings(text: str) -> list[dict[str, Any]]:
    """Pull the findings array out of a model reply, tolerating prose and fences."""
    payload = _first_json_array(str(text or ""))
    if payload is None:
        return []
    findings: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        scenario = str(item.get("failure_scenario") or "").strip()
        if not summary or not scenario:
            # No demonstrable failure means it is an opinion, and opinions are
            # exactly the noise that makes a review report get ignored.
            continue
        findings.append(
            {
                "file": str(item.get("file") or "").replace("\\", "/").strip(),
                "line": _as_line(item.get("line")),
                "category": _normalized_category(item.get("category")),
                "summary": summary,
                "failure_scenario": scenario,
            }
        )
    return findings


def _first_json_array(text: str) -> list[Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, list):
            return parsed
    return None


def _as_line(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _normalized_category(value: Any) -> str:
    candidate = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return candidate if candidate in REVIEW_CATEGORIES else "other"


def subtract_baseline(
    *,
    change_findings: list[dict[str, Any]],
    base_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Drop findings the reviewer also reports on the pre-change code.

    Matching is on (file, category), not on wording: the same defect described
    twice by a model rarely comes back in the same words, and the point of the
    baseline is to answer "did this change cause it", which the category and the
    file already answer.
    """
    baseline_keys = {(item.get("file") or "", item.get("category") or "") for item in base_findings}
    new_findings: list[dict[str, Any]] = []
    pre_existing: list[dict[str, Any]] = []
    for finding in change_findings:
        key = (finding.get("file") or "", finding.get("category") or "")
        if key in baseline_keys:
            pre_existing.append({**finding, "verdict": "pre_existing"})
        else:
            new_findings.append({**finding, "verdict": "introduced_or_left"})
    return {"findings": new_findings, "pre_existing": pre_existing}


def worktree_diff_text(worktree: Path, *, base_ref: str = "HEAD") -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", base_ref],
            cwd=str(worktree),
            capture_output=True,
            timeout=60.0,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "")[:MAX_DIFF_CHARS]


def baseline_text(worktree: Path, *, base_ref: str, paths: list[str]) -> str:
    """Pre-change content of the changed files, for the baseline pass."""
    chunks: list[str] = []
    remaining = MAX_BASE_CHARS
    for path in paths:
        content = git_show_file(base_ref, path, cwd=worktree)
        if not content:
            continue
        chunk = f"--- {path} (before the change) ---\n{content}\n"
        chunks.append(chunk[:remaining])
        remaining -= len(chunk)
        if remaining <= 0:
            break
    return "\n".join(chunks)


def run_diff_review(
    *,
    objective: str,
    worktree: Path,
    base_ref: str = "HEAD",
    changed_paths: list[str] | None = None,
    completion: ReviewCompletion,
) -> dict[str, Any]:
    """Run the change pass and the baseline pass, and report the difference.

    ``completion`` takes ``(system_prompt, prompt)`` and returns the model's
    text. It is injected so the caller owns model choice — the review is meant
    to run on a stronger model than the implementation did.
    """
    diff_text = worktree_diff_text(worktree, base_ref=base_ref)
    if not diff_text.strip():
        return {"schema_version": 1, "status": "no_diff", "findings": [], "pre_existing": []}
    try:
        change_reply = completion(_SYSTEM_PROMPT, build_review_prompt(objective=objective, code_text=diff_text, mode="change"))
    except Exception as error:  # noqa: BLE001 - a reviewer outage must not fail the dispatch
        return {"schema_version": 1, "status": "review_unavailable", "error": str(error), "findings": [], "pre_existing": []}
    change_findings = parse_review_findings(change_reply)
    if not change_findings:
        return {"schema_version": 1, "status": "clean", "findings": [], "pre_existing": []}

    base_source = baseline_text(
        worktree,
        base_ref=base_ref,
        paths=sorted({str(item.get("file") or "") for item in change_findings if item.get("file")}),
    )
    base_findings: list[dict[str, Any]] = []
    baseline_status = "compared"
    if base_source.strip():
        try:
            base_reply = completion(_SYSTEM_PROMPT, build_review_prompt(objective=objective, code_text=base_source, mode="base"))
        except Exception:  # noqa: BLE001
            baseline_status = "baseline_unavailable"
        else:
            base_findings = parse_review_findings(base_reply)
    else:
        # New files have no "before", so nothing can be pre-existing in them.
        baseline_status = "no_baseline_content"

    split = subtract_baseline(change_findings=change_findings, base_findings=base_findings)
    return {
        "schema_version": 1,
        # A reviewer never fails a dispatch; "findings" is a routing signal for a
        # human, which is why the status word is not pass/fail.
        "status": "findings" if split["findings"] else "clean",
        "baseline": baseline_status,
        "findings": split["findings"],
        "pre_existing": split["pre_existing"],
    }


def review_markdown_lines(review: dict[str, Any] | None) -> list[str]:
    if not isinstance(review, dict):
        return []
    status = str(review.get("status") or "")
    findings = review.get("findings") if isinstance(review.get("findings"), list) else []
    pre_existing = review.get("pre_existing") if isinstance(review.get("pre_existing"), list) else []
    if status in {"no_diff", ""} or (not findings and not pre_existing and status != "review_unavailable"):
        return []
    lines = ["", "### 差异评审（只读，不拦截）", ""]
    if status == "review_unavailable":
        lines.append(f"评审没跑成，所以这次没有评审结论（{review.get('error') or '原因未知'}）。别把「没报问题」当成「没问题」。")
        return lines
    if findings:
        lines.append("下面这些是这次改动带来的，base 上跑同一套检查没有报：")
        lines.append("")
        for item in findings:
            location = f"{item.get('file') or '?'}:{item.get('line') or 0}"
            lines.append(f"- `{location}` **{item.get('category') or 'other'}** — {item.get('summary') or ''}")
            lines.append(f"  - 会怎么坏：{item.get('failure_scenario') or ''}")
    if pre_existing:
        lines.extend(
            [
                "",
                f"另有 {len(pre_existing)} 条在改动之前就存在（历史遗留，不算这次的账）：",
                "",
            ]
        )
        for item in pre_existing[:10]:
            lines.append(f"- `{item.get('file') or '?'}` {item.get('category') or ''} — {item.get('summary') or ''}")
    if str(review.get("baseline") or "") == "baseline_unavailable":
        lines.extend(["", "注意：基线那一趟没跑成，所以上面哪些是历史遗留没能分开。"])
    return lines
