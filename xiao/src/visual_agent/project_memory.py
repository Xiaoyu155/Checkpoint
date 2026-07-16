"""Evidence-derived, token-budgeted project memory for DevPacer.

Memory is an advisory index over durable mission artifacts. It is deliberately
not a file allowlist: workers can inspect the repository normally and can open
the source evidence behind any memory episode when more detail is useful.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .chief_plans_store import load_plan, load_verification, load_worker_records, plan_dir
from .missions import list_missions, load_mission, load_rounds, mission_dir
from .models import to_jsonable


MEMORY_SCHEMA_VERSION = 2
INDEX_FORMAT_VERSION = 4
INDEX_DIRNAME = "project_memory"
INDEX_FILENAME = "index.json"
DEFAULT_HANDOFF_CHARS = 1200
MIN_RELEVANCE_SCORE = 24
_PREVIEW_STATUSES = {"preview", "inspection_only"}
_PREVIEW_STOP_REASONS = {"preview_only", "inspection_only"}
_STOPWORDS = {
    "about",
    "after",
    "before",
    "current",
    "from",
    "into",
    "mission",
    "project",
    "test",
    "tests",
    "that",
    "this",
    "with",
    "without",
    "修复",
    "任务",
    "当前",
    "继续",
    "开发",
    "功能",
    "更新",
    "新增",
    "增加",
    "支持",
    "实现",
    "测试",
    "项目",
    "验证",
}


def build_project_memory(
    *,
    workspace_root: str | Path,
    repo_root: str | Path | None = None,
    goal: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    repo_path = Path(repo_root).expanduser().resolve() if repo_root is not None else workspace_path.parent
    cached = _load_index(workspace_path)
    cached_entries = cached.get("entries") if isinstance(cached.get("entries"), dict) else {}
    next_cached_entries: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    reused_entries = 0
    rebuilt_entries = 0

    for summary in list_missions(workspace_path):
        mission_id = str(summary.get("mission_id") or "")
        plan_id = str(summary.get("plan_id") or "")
        signature, source_paths = _source_signature(workspace_path, mission_id, plan_id)
        cached_item = cached_entries.get(mission_id) if isinstance(cached_entries, dict) else None
        base_entry: dict[str, Any] | None = None
        if (
            isinstance(cached_item, dict)
            and cached_item.get("signature") == signature
            and isinstance(cached_item.get("entry"), dict)
        ):
            base_entry = dict(cached_item["entry"])
            reused_entries += 1
        else:
            mission = load_mission(workspace_path, mission_id)
            if mission is None:
                continue
            rounds = load_rounds(workspace_path, mission_id)
            plan = load_plan(workspace_path, plan_id) if plan_id else None
            workers = load_worker_records(workspace_path, plan_id) if plan_id else []
            verification = load_verification(workspace_path, plan_id) if plan_id else None
            base_entry = _memory_entry(
                workspace_path,
                mission,
                rounds,
                plan,
                workers,
                verification,
                source_paths=source_paths,
            )
            rebuilt_entries += 1
        next_cached_entries[mission_id] = {"signature": signature, "entry": base_entry}
        scored = dict(base_entry)
        relevance = score_memory_entry(goal or "", scored)
        scored["relevance_score"] = relevance["score"]
        scored["match_reasons"] = relevance["match_reasons"]
        scored["relevance"] = relevance
        entries.append(scored)

    _save_index(workspace_path, next_cached_entries)
    entries.sort(key=lambda item: (int(item.get("relevance_score") or 0), item.get("updated_at", "")), reverse=True)
    query_present = bool(str(goal or "").strip())
    if query_present:
        pool = [item for item in entries if int(item.get("relevance_score") or 0) >= MIN_RELEVANCE_SCORE]
    else:
        durable = [item for item in entries if _is_durable(item)]
        pool = durable or entries
    selected = pool[: max(0, int(limit))]
    selected_ids = {str(item.get("memory_id") or "") for item in selected}
    ranking = [
        {
            "rank": rank,
            "memory_id": str(item.get("memory_id") or ""),
            "score": int(item.get("relevance_score") or 0),
            "match_reasons": [str(reason) for reason in item.get("match_reasons") or []],
            "judgment": str((item.get("relevance") or {}).get("judgment") or "unjudged"),
            "relevant": (item.get("relevance") or {}).get("relevant"),
            "selected": str(item.get("memory_id") or "") in selected_ids,
        }
        for rank, item in enumerate(entries[: max(0, int(limit))], start=1)
    ]
    cache_status = _entry_cache_status(
        reused_entries=reused_entries,
        rebuilt_entries=rebuilt_entries,
        stored_entries=len(next_cached_entries),
    )
    return {
        "schema_version": MEMORY_SCHEMA_VERSION,
        "product": "DevPacer",
        "verification_engine": "Checkpoint",
        "workspace_root": str(workspace_path),
        "goal": str(goal or ""),
        "entry_count": len(selected),
        "entries": selected,
        "instruction_memory": load_instruction_memory(repo_path),
        "patterns": _memory_patterns(entries),
        "recommendations": _memory_recommendations(selected),
        "disclosure": {
            "mode": "progressive",
            "advisory_only": True,
            "default_handoff_chars": DEFAULT_HANDOFF_CHARS,
            "message": "Memory is advisory and non-exhaustive; inspect any repository files needed for the task.",
        },
        "entry_cache": {
            "status": cache_status,
            "path": str(_index_path(workspace_path)),
            "reused_entries": reused_entries,
            "rebuilt_entries": rebuilt_entries,
            "stored_entries": len(next_cached_entries),
        },
        "lookup": {
            "status": "succeeded",
            "hit": bool(entries),
            "lookup_hit": bool(entries),
            "candidate_count": len(entries),
        },
        "relevance": {
            "status": "estimated" if query_present else "unjudged",
            "hit": bool(pool) if query_present else None,
            "relevant_hit": bool(pool) if query_present else None,
            "threshold": MIN_RELEVANCE_SCORE,
            "eligible_count": len(pool) if query_present else None,
            "returned_count": len(selected),
            "ranking": ranking,
        },
        "index": {
            "path": str(_index_path(workspace_path)),
            # Compatibility aliases. These are entry-cache counters, not
            # evidence that the returned memories are relevant to the task.
            "hits": reused_entries,
            "misses": rebuilt_entries,
            "entry_count": len(next_cached_entries),
        },
    }


def project_memory_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "## DevPacer Project Memory",
        "",
        "> Advisory, non-exhaustive context. It does not limit repository exploration; open source evidence as needed.",
    ]
    goal = str(payload.get("goal") or "").strip()
    if goal:
        lines.extend(["", f"Goal: {goal}"])
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
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
    if not entries:
        lines.extend(["", "No relevant durable mission evidence found."])
        return "\n".join(lines)
    recommendations = payload.get("recommendations") if isinstance(payload.get("recommendations"), list) else []
    if recommendations:
        lines.extend(["", "### Recommendations", ""])
        lines.extend(f"- {item}" for item in recommendations)
    lines.extend(["", "### Relevant Episodes", ""])
    for item in entries:
        stop = f" / {item.get('stop_reason')}" if item.get("stop_reason") else ""
        lines.append(
            f"- `{item.get('memory_id')}` [{item.get('status')}{stop}; score={item.get('relevance_score', 0)}] "
            f"{item.get('objective')}"
        )
        changed_files = item.get("changed_files") if isinstance(item.get("changed_files"), list) else []
        symbols = item.get("symbols") if isinstance(item.get("symbols"), list) else []
        verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
        if changed_files:
            lines.append(f"  files: {', '.join(f'`{path}`' for path in changed_files[:4])}")
        if symbols:
            lines.append(f"  symbols: {', '.join(f'`{symbol}`' for symbol in symbols[:5])}")
        if verification.get("command"):
            lines.append(f"  verification [{verification.get('verdict') or 'unknown'}]: `{verification.get('command')}`")
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        if evidence.get("failed_signatures"):
            lines.append(f"  failed: `{evidence['failed_signatures'][0]}`")
        if item.get("source_paths"):
            lines.append(f"  evidence: `{item['source_paths'][0]}`")
    return "\n".join(lines).rstrip()


def project_memory_handoff_notes(
    payload: dict[str, Any],
    *,
    max_items: int = 3,
    max_chars: int = DEFAULT_HANDOFF_CHARS,
) -> list[str]:
    """Return a compact disclosure layer while keeping full evidence addressable."""
    item_limit = max(0, int(max_items))
    char_limit = max(0, int(max_chars))
    if item_limit == 0 or char_limit == 0:
        return []
    candidates: list[str] = []
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    instruction_memory = payload.get("instruction_memory") if isinstance(payload.get("instruction_memory"), dict) else {}
    instruction_files = instruction_memory.get("files") if isinstance(instruction_memory.get("files"), list) else []
    entry_slots = item_limit if not instruction_files else max(1, item_limit - 1)
    for item in entries[:entry_slots]:
        if not isinstance(item, dict):
            continue
        objective = _one_line(str(item.get("objective") or ""), 180)
        note = f"Related `{item.get('memory_id') or item.get('mission_id')}` [{item.get('status')}]: {objective}"
        verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
        if verification.get("verdict"):
            note += f"; verification={verification.get('verdict')}"
        failed = ((item.get("evidence") or {}).get("failed_signatures") or [])
        if failed:
            note += f"; failure={_one_line(str(failed[-1]), 100)}"
        candidates.append(note)
    for item in instruction_files:
        if not isinstance(item, dict):
            continue
        excerpt = _one_line(str(item.get("excerpt") or ""), 180)
        if excerpt:
            candidates.append(f"Project instruction `{item.get('path')}`: {excerpt}")
            break
    return _fit_notes(candidates, max_items=item_limit, max_chars=char_limit)


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
    workers: list[dict[str, Any]],
    verification: dict[str, Any] | None,
    *,
    source_paths: list[str],
) -> dict[str, Any]:
    mission_id = str(mission.get("mission_id") or "")
    plan_payload = plan or {}
    verification_payload = verification or {}
    has_diff_evidence = isinstance(verification_payload.get("diff_summary"), dict)
    diff_summary = verification_payload.get("diff_summary") if has_diff_evidence else {}
    command_verification = (
        verification_payload.get("command_verification")
        if isinstance(verification_payload.get("command_verification"), dict)
        else {}
    )
    failed_signatures = _unique(
        str(item.get("failed_signature") or "")
        for item in rounds
        if isinstance(item, dict) and str(item.get("failed_signature") or "").strip()
    )
    verification_statuses = [
        str(item.get("status") or "")
        for item in rounds
        if isinstance(item, dict) and str(item.get("type") or "") == "verification"
    ]
    scope_files = _unique(_changed_file_value(item) for item in plan_payload.get("changed_files") or [])[:20]
    actual_files = _unique(_changed_file_value(item) for item in diff_summary.get("changed_files") or [])[:20]
    changed_files = actual_files if has_diff_evidence else scope_files
    symbols = _unique(str(item) for item in diff_summary.get("functions_touched") or [])[:30]
    worker = _latest_worker(workers)
    final_report = mission_dir(workspace_root, mission_id) / "final_report.md"
    command = str(command_verification.get("command") or "")
    if not command:
        commands = plan_payload.get("verification_commands") or []
        command = str(commands[0]) if commands else ""
    verdict = str(command_verification.get("verdict") or verification_payload.get("verdict") or "")
    budget = mission.get("budget_policy") if isinstance(mission.get("budget_policy"), dict) else {}
    execution_policy = budget.get("execution_policy") if isinstance(budget.get("execution_policy"), dict) else {}
    return {
        "memory_id": f"mission:{mission_id}",
        "mission_id": mission_id,
        "objective": str(mission.get("objective") or ""),
        "status": str(mission.get("status") or ""),
        "stop_reason": str(mission.get("stop_reason") or ""),
        "plan_id": str(mission.get("plan_id") or ""),
        "repo_root": str(mission.get("repo_root") or ""),
        "updated_at": str(mission.get("updated_at") or mission.get("created_at") or ""),
        "current_round": int(mission.get("current_round") or 0),
        "final_report_path": str(final_report) if final_report.exists() else "",
        "source_paths": source_paths,
        "program_context": {
            key: str(execution_policy.get(key) or "")
            for key in ("program_id", "task_id", "source_plan", "source_plan_sha256")
            if str(execution_policy.get(key) or "")
        },
        "acceptance_criteria": [str(item) for item in plan_payload.get("acceptance_criteria") or []][:5],
        "selected_workflows": [str(item) for item in plan_payload.get("selected_workflows") or []],
        "changed_files": changed_files,
        "scope_files": scope_files,
        "symbols": symbols,
        "verification": {
            "command": _one_line(command, 500),
            "verdict": verdict,
            "exit_code": command_verification.get("exit_code"),
        },
        "worker_outcome": {
            "status": str(worker.get("status") or ""),
            "cwd": str(worker.get("cwd") or ""),
            "summary": _worker_summary(worker),
        },
        "evidence": {
            "round_count": len(rounds),
            "failed_signatures": failed_signatures[-3:],
            "verification_statuses": verification_statuses[-3:],
        },
    }


def _source_signature(workspace_root: Path, mission_id: str, plan_id: str) -> tuple[str, list[str]]:
    mission_path = mission_dir(workspace_root, mission_id)
    paths = [mission_path / "mission.json", mission_path / "rounds.jsonl", mission_path / "final_report.md"]
    if plan_id:
        plan_path = plan_dir(workspace_root, plan_id)
        paths.extend([plan_path / "plan.json", plan_path / "workers.jsonl", plan_path / "verification.json"])
    parts: list[str] = []
    existing: list[str] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            parts.append(f"{path.name}:missing")
            continue
        parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
        existing.append(str(path))
    return "|".join(parts), existing


def _index_path(workspace_root: Path) -> Path:
    return workspace_root / INDEX_DIRNAME / INDEX_FILENAME


def _load_index(workspace_root: Path) -> dict[str, Any]:
    path = _index_path(workspace_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != MEMORY_SCHEMA_VERSION
        or payload.get("index_format_version") != INDEX_FORMAT_VERSION
    ):
        return {}
    return payload


def _save_index(workspace_root: Path, entries: dict[str, Any]) -> None:
    path = _index_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": MEMORY_SCHEMA_VERSION,
        "index_format_version": INDEX_FORMAT_VERSION,
        "entries": entries,
    }
    temporary = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
    try:
        temporary.write_text(json.dumps(to_jsonable(payload), ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


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
        "repeated_failed_signatures": {signature: count for signature, count in failed.items() if count > 1},
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
            _append_once(recommendations, seen, f"Previous related mission `{mission_id}` stopped on coverage_gap; add workflow coverage before dispatching similar work.")
        elif stop_reason == "needs_clarification":
            _append_once(recommendations, seen, f"Previous related mission `{mission_id}` needed clarification; make the observable done-state explicit.")
        elif stop_reason == "same_failure_repeated":
            failed = ((item.get("evidence") or {}).get("failed_signatures") or [""])[-1]
            _append_once(recommendations, seen, f"Previous related mission `{mission_id}` repeated the same failure `{failed}`; inspect that evidence before re-dispatch.")
        elif stop_reason == "worker_error":
            _append_once(recommendations, seen, f"Previous related mission `{mission_id}` hit worker_error; check worker logs before spending another run.")
        elif str(item.get("status") or "") == "verified":
            _append_once(recommendations, seen, f"Previous related mission `{mission_id}` verified successfully; inspect its evidence if scope overlaps: {objective}")
    return recommendations[:6]


def score_memory_entry(
    goal: str,
    entry: dict[str, Any],
    *,
    threshold: int = MIN_RELEVANCE_SCORE,
) -> dict[str, Any]:
    """Score one memory candidate without interpreting cache reuse as relevance."""
    query_text = str(goal or "").strip()
    if not query_text:
        return {
            "judgment": "unjudged",
            "score": 0,
            "threshold": max(0, int(threshold)),
            "relevant": None,
            "match_reasons": [],
        }
    query = query_text.lower()
    score = 0
    reasons: list[str] = []
    mission_id = str(entry.get("mission_id") or entry.get("batch_run_id") or "").lower()
    memory_id = str(entry.get("memory_id") or entry.get("batch_run_id") or "").lower()
    if (mission_id and mission_id in query) or (memory_id and memory_id in query):
        score += 120
        reasons.append("explicit_memory_id")
    paths = [str(item).replace("\\", "/").lower() for item in entry.get("changed_files") or []]
    symbols = [str(item).lower() for item in entry.get("symbols") or []]
    query_paths = _path_terms(query)
    query_symbols = _symbol_terms(query)
    if any(path in query or any(term == path or term.endswith("/" + path) for term in query_paths) for path in paths):
        score += 60
        reasons.append("exact_path")
    if set(query_symbols) & set(symbols):
        score += 50
        reasons.append("exact_symbol")
    objective = str(entry.get("objective") or entry.get("goal") or "").lower()
    if query in objective or (len(objective) >= 12 and objective in query):
        score += 100
        reasons.append("objective_phrase")
    objective_overlap = _tokens(query) & _tokens(objective)
    if objective_overlap:
        score += _token_overlap_score(objective_overlap)
        reasons.append("objective_terms")
    failure_text = " ".join(str(item) for item in ((entry.get("evidence") or {}).get("failed_signatures") or [])).lower()
    failure_overlap = _tokens(query) & _tokens(failure_text)
    if failure_overlap:
        score += min(45, 15 * len(failure_overlap))
        reasons.append("failure_signature")
    criteria_text = " ".join(str(item) for item in entry.get("acceptance_criteria") or []).lower()
    criteria_overlap = _tokens(query) & _tokens(criteria_text)
    if criteria_overlap:
        score += min(20, 4 * len(criteria_overlap))
        reasons.append("acceptance_terms")
    if score > 0 and str(entry.get("status") or "") == "verified":
        score += 5
        reasons.append("verified")
    if str(entry.get("status") or "") in _PREVIEW_STATUSES or str(entry.get("stop_reason") or "") in _PREVIEW_STOP_REASONS:
        score = max(0, score - 20)
        reasons.append("preview_penalty")
    reasons = reasons if score > 0 else []
    relevance_threshold = max(0, int(threshold))
    return {
        "judgment": "estimated",
        "score": score,
        "threshold": relevance_threshold,
        "relevant": score >= relevance_threshold,
        "match_reasons": reasons,
    }


def _entry_cache_status(*, reused_entries: int, rebuilt_entries: int, stored_entries: int) -> str:
    if stored_entries <= 0:
        return "empty"
    if reused_entries > 0 and rebuilt_entries > 0:
        return "partial"
    if reused_entries > 0:
        return "warm"
    return "cold"


def _tokens(value: str) -> set[str]:
    tokens = {
        item.lower()
        for item in re.findall(r"[A-Za-z0-9_]{3,}", value)
        if item.lower() not in _STOPWORDS
    }
    for run in re.findall(r"[\u3400-\u9fff]+", value):
        if run not in _STOPWORDS and len(run) <= 6:
            tokens.add(run)
        for size in (2, 3, 4):
            tokens.update(
                run[index : index + size]
                for index in range(max(0, len(run) - size + 1))
                if run[index : index + size] not in _STOPWORDS
            )
    return tokens


def _token_overlap_score(tokens: set[str]) -> int:
    score = 0
    for token in tokens:
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            # A shared two-character Chinese domain term often carries the same
            # signal as a complete English identifier; generic verbs are removed
            # by _STOPWORDS before this point.
            score += 24 if len(token) == 2 else 18
        else:
            score += 8
    return min(40, score)


def _path_terms(value: str) -> set[str]:
    return {
        item.replace("\\", "/").lower().rstrip(".,:;)")
        for item in re.findall(r"(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+", value)
    }


def _symbol_terms(value: str) -> set[str]:
    return {item.lower() for item in re.findall(r"\b_?[A-Za-z][A-Za-z0-9_]*_[A-Za-z0-9_]+\b", value)}


def _is_durable(entry: dict[str, Any]) -> bool:
    return not (
        str(entry.get("status") or "") in _PREVIEW_STATUSES
        or str(entry.get("stop_reason") or "") in _PREVIEW_STOP_REASONS
    )


def _latest_worker(workers: list[dict[str, Any]]) -> dict[str, Any]:
    for worker in reversed(workers):
        if isinstance(worker, dict) and str(worker.get("status") or "") in {"completed", "failed", "timed_out"}:
            return worker
    return workers[-1] if workers and isinstance(workers[-1], dict) else {}


def _worker_summary(worker: dict[str, Any]) -> str:
    stdout = str(worker.get("stdout_tail") or "").strip()
    stderr = str(worker.get("stderr_tail") or "").strip()
    value = stdout or stderr
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return _one_line(_redact_sensitive(" ".join(lines)), 600)


def _redact_sensitive(value: str) -> str:
    redacted = re.sub(
        r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+",
        r"\1[redacted]",
        value,
    )
    redacted = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", redacted)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "sk-[redacted]", redacted)


def _fit_notes(candidates: list[str], *, max_items: int, max_chars: int) -> list[str]:
    notes: list[str] = []
    for index, candidate in enumerate(candidates):
        if len(notes) >= max_items:
            break
        used = len(" ".join(notes))
        remaining_chars = max_chars - used - (1 if notes else 0)
        remaining_slots = max(1, min(max_items - len(notes), len(candidates) - index))
        if remaining_chars <= 0:
            break
        allowance = max(1, remaining_chars // remaining_slots)
        note = _one_line(candidate, allowance)
        if not note:
            continue
        notes.append(note)
    return notes


def _one_line(value: str, limit: int) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= max(0, limit):
        return compact
    if limit <= 3:
        return compact[: max(0, limit)]
    return compact[: limit - 3].rstrip() + "..."


def _unique(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _changed_file_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("path") or value.get("file") or "")
    return str(value or "")


def _append_once(items: list[str], seen: set[str], value: str) -> None:
    if value not in seen:
        seen.add(value)
        items.append(value)
