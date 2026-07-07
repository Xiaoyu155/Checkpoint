"""Evidence-derived project memory for DevPacer.

Project memory is computed from durable records, not model summaries. That keeps
it auditable: every recommendation points back to a mission, round, or final
report on disk.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .chief_plans_store import load_plan
from .missions import list_missions, load_mission, load_rounds, mission_dir
from .models import to_jsonable


def build_project_memory(
    *,
    workspace_root: str | Path,
    repo_root: str | Path | None = None,
    goal: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    repo_path = Path(repo_root).expanduser().resolve() if repo_root is not None else workspace_path.parent
    summaries = list_missions(workspace_path)
    entries: list[dict[str, Any]] = []
    for summary in summaries:
        mission_id = str(summary.get("mission_id") or "")
        mission = load_mission(workspace_path, mission_id)
        if mission is None:
            continue
        rounds = load_rounds(workspace_path, mission_id)
        plan = load_plan(workspace_path, str(mission.get("plan_id") or "")) if mission.get("plan_id") else None
        entries.append(_memory_entry(workspace_path, mission, rounds, plan, goal=goal))
    entries.sort(key=lambda item: (item.get("relevance_score", 0), item.get("updated_at", "")), reverse=True)
    relevant = [item for item in entries if int(item.get("relevance_score") or 0) > 0]
    pool = relevant if str(goal or "").strip() and relevant else entries
    selected = pool[: max(0, int(limit))]
    return {
        "schema_version": 1,
        "product": "DevPacer",
        "verification_engine": "Checkpoint",
        "workspace_root": str(workspace_path),
        "goal": str(goal or ""),
        "entry_count": len(selected),
        "entries": selected,
        "instruction_memory": load_instruction_memory(repo_path),
        "patterns": _memory_patterns(entries),
        "recommendations": _memory_recommendations(selected),
    }


def project_memory_to_markdown(payload: dict[str, Any]) -> str:
    lines = ["## DevPacer Project Memory", ""]
    goal = str(payload.get("goal") or "").strip()
    if goal:
        lines.append(f"Goal: {goal}")
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    if not entries:
        lines.append("No previous mission evidence found.")
        return "\n".join(lines)
    recommendations = payload.get("recommendations") if isinstance(payload.get("recommendations"), list) else []
    if recommendations:
        lines.extend(["", "### Recommendations", ""])
        lines.extend(f"- {item}" for item in recommendations)
    instruction_memory = payload.get("instruction_memory") if isinstance(payload.get("instruction_memory"), dict) else {}
    instruction_files = instruction_memory.get("files") if isinstance(instruction_memory.get("files"), list) else []
    if instruction_files:
        lines.extend(["", "### Project Instructions", ""])
        for item in instruction_files:
            if not isinstance(item, dict):
                continue
            lines.append(f"- `{item.get('path')}`")
            excerpt = str(item.get("excerpt") or "").strip()
            if excerpt:
                lines.append(f"  {excerpt.splitlines()[0][:180]}")
    lines.extend(["", "### Relevant Missions", ""])
    for item in entries:
        stop = f" / {item.get('stop_reason')}" if item.get("stop_reason") else ""
        lines.append(
            f"- `{item.get('mission_id')}` [{item.get('status')}{stop}] "
            f"{item.get('objective')}"
        )
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        if evidence.get("failed_signatures"):
            lines.append(f"  failed: `{evidence['failed_signatures'][0]}`")
        if item.get("final_report_path"):
            lines.append(f"  report: `{item.get('final_report_path')}`")
    patterns = payload.get("patterns") if isinstance(payload.get("patterns"), dict) else {}
    stop_counts = patterns.get("stop_reason_counts") if isinstance(patterns.get("stop_reason_counts"), dict) else {}
    if stop_counts:
        lines.extend(["", "### Stop Reasons", ""])
        for reason, count in sorted(stop_counts.items(), key=lambda pair: (-int(pair[1]), pair[0])):
            lines.append(f"- `{reason}`: {count}")
    return "\n".join(lines).rstrip()


def project_memory_handoff_notes(payload: dict[str, Any], *, max_items: int = 3) -> list[str]:
    recommendations = payload.get("recommendations") if isinstance(payload.get("recommendations"), list) else []
    notes = [str(item) for item in recommendations[: max(0, max_items)] if str(item).strip()]
    instruction_memory = payload.get("instruction_memory") if isinstance(payload.get("instruction_memory"), dict) else {}
    instruction_files = instruction_memory.get("files") if isinstance(instruction_memory.get("files"), list) else []
    for item in instruction_files:
        if len(notes) >= max(0, max_items):
            break
        if not isinstance(item, dict):
            continue
        excerpt = str(item.get("excerpt") or "").strip()
        if excerpt:
            notes.append(f"Project instruction `{item.get('path')}`: {excerpt.splitlines()[0][:180]}")
    if notes:
        return notes
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    for item in entries[: max(0, max_items)]:
        if not isinstance(item, dict):
            continue
        stop = str(item.get("stop_reason") or "")
        objective = str(item.get("objective") or "")
        if stop:
            notes.append(f"Previous related mission `{item.get('mission_id')}` stopped with `{stop}`: {objective}")
        elif str(item.get("status") or "") == "verified":
            notes.append(f"Previous related mission `{item.get('mission_id')}` verified: {objective}")
    return notes


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2)


def load_instruction_memory(repo_root: str | Path, *, max_files: int = 20, max_file_chars: int = 2000) -> dict[str, Any]:
    """Load human-authored project instructions for worker handoff."""
    root = Path(repo_root).expanduser().resolve()
    files: list[dict[str, Any]] = []
    for path in _instruction_memory_paths(root)[: max(0, max_files)]:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        excerpt = text.strip()
        truncated = len(excerpt) > max_file_chars
        if truncated:
            excerpt = excerpt[:max_file_chars].rstrip() + "\n[truncated]"
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "chars": len(text),
                "truncated": truncated,
                "excerpt": excerpt,
            }
        )
    return {
        "schema_version": 1,
        "repo_root": str(root),
        "file_count": len(files),
        "files": files,
    }


def _memory_entry(
    workspace_root: Path,
    mission: dict[str, Any],
    rounds: list[dict[str, Any]],
    plan: dict[str, Any] | None,
    *,
    goal: str | None,
) -> dict[str, Any]:
    mission_id = str(mission.get("mission_id") or "")
    objective = str(mission.get("objective") or "")
    final_report = mission_dir(workspace_root, mission_id) / "final_report.md"
    failed_signatures = [
        str(item.get("failed_signature") or "")
        for item in rounds
        if isinstance(item, dict) and str(item.get("failed_signature") or "").strip()
    ]
    verification_statuses = [
        str(item.get("status") or "")
        for item in rounds
        if isinstance(item, dict) and str(item.get("type") or "") == "verification"
    ]
    return {
        "mission_id": mission_id,
        "objective": objective,
        "status": str(mission.get("status") or ""),
        "stop_reason": str(mission.get("stop_reason") or ""),
        "plan_id": str(mission.get("plan_id") or ""),
        "updated_at": str(mission.get("updated_at") or mission.get("created_at") or ""),
        "current_round": int(mission.get("current_round") or 0),
        "relevance_score": _relevance_score(goal or "", objective),
        "final_report_path": str(final_report) if final_report.exists() else "",
        "acceptance_criteria": list((plan or {}).get("acceptance_criteria") or [])[:5],
        "selected_workflows": list((plan or {}).get("selected_workflows") or []),
        "evidence": {
            "round_count": len(rounds),
            "failed_signatures": failed_signatures[-3:],
            "verification_statuses": verification_statuses[-3:],
        },
    }


def _instruction_memory_paths(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for name in ("PACER.md", "CHECKPOINT.md", "AGENTS.md"):
        path = root / name
        if path.is_file():
            candidates.append(path)
    for name in ("PACER.md", "memory.md"):
        path = root / ".pacer" / name
        if path.is_file():
            candidates.append(path)
    rules_dir = root / ".pacer" / "rules"
    if rules_dir.is_dir():
        candidates.extend(sorted(path for path in rules_dir.glob("*.md") if path.is_file()))
    seen: set[Path] = set()
    kept: list[Path] = []
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        kept.append(path)
    return kept


def _memory_patterns(entries: list[dict[str, Any]]) -> dict[str, Any]:
    stop_counts = Counter(str(item.get("stop_reason") or "") for item in entries if item.get("stop_reason"))
    failed = Counter(
        signature
        for item in entries
        for signature in ((item.get("evidence") or {}).get("failed_signatures") or [])
        if signature
    )
    return {
        "stop_reason_counts": dict(stop_counts),
        "repeated_failed_signatures": {
            signature: count for signature, count in failed.items() if count > 1
        },
        "verified_missions": sum(1 for item in entries if str(item.get("status") or "") == "verified"),
    }


def _memory_recommendations(entries: list[dict[str, Any]]) -> list[str]:
    recommendations: list[str] = []
    seen: set[str] = set()
    for item in entries:
        stop_reason = str(item.get("stop_reason") or "")
        objective = str(item.get("objective") or "")
        mission_id = str(item.get("mission_id") or "")
        if stop_reason == "coverage_gap":
            _append_once(
                recommendations,
                seen,
                f"Previous related mission `{mission_id}` stopped on coverage_gap; add workflow coverage before dispatching similar work.",
            )
        elif stop_reason == "needs_clarification":
            _append_once(
                recommendations,
                seen,
                f"Previous related mission `{mission_id}` needed clarification; make the observable done-state explicit.",
            )
        elif stop_reason == "same_failure_repeated":
            failed = ((item.get("evidence") or {}).get("failed_signatures") or [""])[-1]
            _append_once(
                recommendations,
                seen,
                f"Previous related mission `{mission_id}` repeated the same failure `{failed}`; inspect that evidence before re-dispatch.",
            )
        elif stop_reason == "worker_error":
            _append_once(
                recommendations,
                seen,
                f"Previous related mission `{mission_id}` hit worker_error; check worker logs before spending another run.",
            )
        elif str(item.get("status") or "") == "verified":
            _append_once(
                recommendations,
                seen,
                f"Previous related mission `{mission_id}` verified successfully; reuse its workflows and acceptance criteria if scope overlaps: {objective}",
            )
    return recommendations[:6]


def _append_once(items: list[str], seen: set[str], value: str) -> None:
    if value not in seen:
        seen.add(value)
        items.append(value)


def _relevance_score(goal: str, objective: str) -> int:
    if not goal.strip():
        return 0
    goal_lower = goal.lower()
    objective_lower = objective.lower()
    if goal_lower and goal_lower in objective_lower:
        return 100
    goal_tokens = _tokens(goal_lower)
    objective_tokens = _tokens(objective_lower)
    return len(goal_tokens & objective_tokens)


def _tokens(value: str) -> set[str]:
    tokens = {item for item in re.split(r"[\s,.;:!?，。；：！？、/\\()\[\]{}_-]+", value) if len(item) >= 3}
    # Preserve useful CJK terms even when there is no whitespace tokenizer.
    for term in ("结账", "下单", "支付", "登录", "注册", "表单", "金额", "总价", "验收", "修复"):
        if term in value:
            tokens.add(term)
    return tokens
