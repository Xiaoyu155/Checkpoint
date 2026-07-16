from __future__ import annotations

import json
import shutil
from pathlib import Path
from time import time
from typing import Any
from uuid import uuid4

from .security import scrub_secrets
from .workflow import parse_workflow_file


HISTORY_FILE = "repair_history.jsonl"


def repair_history_path(workspace_root: str | Path) -> Path:
    return Path(workspace_root).resolve() / HISTORY_FILE


def append_repair_history(workspace_root: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    path = repair_history_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = repair_history_entry(payload)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(scrub_secrets(entry), ensure_ascii=False, default=str) + "\n")
    return entry


def list_repair_history(
    workspace_root: str | Path,
    *,
    limit: int = 20,
    workflow: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    path = repair_history_path(workspace_root)
    entries = []
    corrupt_lines = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                corrupt_lines += 1
                continue
            if not isinstance(item, dict):
                corrupt_lines += 1
                continue
            if workflow and item.get("workflow") != workflow:
                continue
            if status and item.get("status") != status:
                continue
            entries.append(item)
    entries.sort(key=lambda item: float(item.get("created_at") or 0.0), reverse=True)
    bounded_limit = max(1, min(int(limit or 20), 200))
    selected = entries[:bounded_limit]
    return {
        "schema_version": 1,
        "workspace": str(Path(workspace_root).resolve()),
        "history_path": str(path),
        "total_entries": len(entries),
        "returned_entries": len(selected),
        "corrupt_lines": corrupt_lines,
        "entries": selected,
    }


def build_repair_health(
    workspace_root: str | Path,
    *,
    limit: int = 50,
    workflow: str | None = None,
) -> dict[str, Any]:
    history = list_repair_history(workspace_root, limit=limit, workflow=workflow)
    entries = history.get("entries") if isinstance(history.get("entries"), list) else []
    status_counts = count_by(entries, "status")
    classification_counts = count_by(entries, "classification")
    applied_count = sum(1 for item in entries if bool(item.get("applied")))
    verified_count = sum(
        1
        for item in entries
        if item.get("status") == "verified" or item.get("verification_status") == "passed"
    )
    failed_verification_count = sum(
        1
        for item in entries
        if item.get("status") == "applied_unverified" or item.get("verification_status") == "failed"
    )
    rollback_count = sum(
        1
        for item in entries
        if item.get("status") in {"rolled_back", "manual_rolled_back"} or item.get("rollback_status") in {"rolled_back", "manual_rolled_back"}
    )
    reliability_score = round(verified_count / applied_count, 3) if applied_count else None
    recent_risky = [
        compact_health_entry(item)
        for item in entries[:5]
        if item.get("status") in {"applied_unverified", "rolled_back", "rollback_failed", "manual_rolled_back"}
        or item.get("verification_status") == "failed"
    ]
    risk_level = repair_risk_level(
        total=len(entries),
        applied_count=applied_count,
        verified_count=verified_count,
        failed_verification_count=failed_verification_count,
        rollback_count=rollback_count,
        reliability_score=reliability_score,
    )
    return {
        "schema_version": 1,
        "workspace": str(Path(workspace_root).resolve()),
        "workflow": workflow or None,
        "history_path": history.get("history_path"),
        "total_entries": history.get("total_entries"),
        "analyzed_entries": len(entries),
        "corrupt_lines": history.get("corrupt_lines", 0),
        "status_counts": status_counts,
        "classification_counts": classification_counts,
        "applied_count": applied_count,
        "verified_count": verified_count,
        "failed_verification_count": failed_verification_count,
        "rollback_count": rollback_count,
        "reliability_score": reliability_score,
        "risk_level": risk_level,
        "recommendation": repair_health_recommendation(risk_level, applied_count, verified_count, failed_verification_count, rollback_count),
        "latest_entry": compact_health_entry(entries[0]) if entries else None,
        "recent_risky_entries": recent_risky,
    }


def rollback_repair_history_entry(
    workspace_root: str | Path,
    *,
    history_id: str | None = None,
    workflow: str | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace_root).resolve()
    selected = find_repair_history_entry(workspace, history_id=history_id, workflow=workflow)
    if selected is None:
        return {
            "schema_version": 1,
            "workspace": str(workspace),
            "status": "not_found",
            "message": "No repair history entry with a backup was found.",
        }
    path = Path(str(selected.get("path") or "")).resolve()
    backup = Path(str(selected.get("backup_path") or selected.get("rollback_backup_path") or "")).resolve()
    try:
        path.relative_to(workspace)
        backup.relative_to(workspace)
    except ValueError:
        return {
            "schema_version": 1,
            "workspace": str(workspace),
            "status": "rollback_failed",
            "history_id": selected.get("history_id"),
            "reason": f"repair path or backup escapes workspace: path={path}, backup={backup}",
        }
    if not backup.exists():
        return {
            "schema_version": 1,
            "workspace": str(workspace),
            "status": "rollback_failed",
            "history_id": selected.get("history_id"),
            "reason": f"backup not found: {backup}",
        }
    try:
        shutil.copy2(backup, path)
        parse_workflow_file(path)
    except Exception as exc:
        return {
            "schema_version": 1,
            "workspace": str(workspace),
            "status": "rollback_failed",
            "history_id": selected.get("history_id"),
            "reason": f"{type(exc).__name__}: {exc}",
            "path": str(path),
            "backup_path": str(backup),
        }
    payload = {
        "status": "manual_rolled_back",
        "source": "repair_history",
        "workspace": str(workspace),
        "workflow": selected.get("workflow"),
        "run_id": selected.get("run_id"),
        "repair": {
            "classification": selected.get("classification"),
            "confidence": selected.get("confidence"),
            "recommended_fix": f"Manually restored workflow from backup for repair history {selected.get('history_id')}.",
            "apply_supported": False,
        },
        "workflow_repair_plan": {
            "applied": False,
            "apply_requested": False,
            "verify_requested": False,
            "rollback_on_fail": False,
            "path": str(path),
            "backup_path": str(backup),
            "rollback": {
                "status": "manual_rolled_back",
                "backup_path": str(backup),
                "source_history_id": selected.get("history_id"),
            },
        },
    }
    recorded = append_repair_history(workspace, payload)
    return {
        "schema_version": 1,
        "workspace": str(workspace),
        "status": "manual_rolled_back",
        "history_id": selected.get("history_id"),
        "rollback_history_id": recorded.get("history_id"),
        "workflow": selected.get("workflow"),
        "path": str(path),
        "backup_path": str(backup),
        "message": f"Restored workflow from backup: {backup}",
    }


def find_repair_history_entry(
    workspace_root: str | Path,
    *,
    history_id: str | None = None,
    workflow: str | None = None,
) -> dict[str, Any] | None:
    history = list_repair_history(workspace_root, limit=200, workflow=workflow)
    entries = history.get("entries") if isinstance(history.get("entries"), list) else []
    for item in entries:
        if not isinstance(item, dict):
            continue
        if history_id and item.get("history_id") != history_id:
            continue
        if not item.get("backup_path") and not item.get("rollback_backup_path"):
            continue
        if not item.get("path"):
            continue
        return item
    return None


def repair_health_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Repair Health",
        "",
        f"- Workspace: `{payload.get('workspace')}`",
        f"- Workflow: `{payload.get('workflow') or 'all'}`",
        f"- Risk: `{payload.get('risk_level')}`",
        f"- Reliability: `{payload.get('reliability_score')}`",
        f"- Analyzed entries: {payload.get('analyzed_entries')}",
        f"- Applied / verified / failed verify / rollback: {payload.get('applied_count')} / {payload.get('verified_count')} / {payload.get('failed_verification_count')} / {payload.get('rollback_count')}",
        f"- Recommendation: {payload.get('recommendation')}",
    ]
    status_counts = payload.get("status_counts") if isinstance(payload.get("status_counts"), dict) else {}
    if status_counts:
        lines.extend(["", "## Status Counts"])
        for key, value in sorted(status_counts.items()):
            lines.append(f"- `{key}`: {value}")
    risky = payload.get("recent_risky_entries") if isinstance(payload.get("recent_risky_entries"), list) else []
    if risky:
        lines.extend(["", "## Recent Risky Entries"])
        for item in risky:
            lines.append(
                f"- `{item.get('status')}` workflow=`{item.get('workflow')}` "
                f"classification=`{item.get('classification')}` history=`{item.get('history_id')}`"
            )
    return "\n".join(lines).rstrip() + "\n"


def repair_rollback_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Repair Rollback",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Workflow: `{payload.get('workflow') or ''}`",
    ]
    if payload.get("history_id"):
        lines.append(f"- Source history: `{payload.get('history_id')}`")
    if payload.get("rollback_history_id"):
        lines.append(f"- Rollback history: `{payload.get('rollback_history_id')}`")
    if payload.get("path"):
        lines.append(f"- Path: `{payload.get('path')}`")
    if payload.get("backup_path"):
        lines.append(f"- Backup: `{payload.get('backup_path')}`")
    if payload.get("reason"):
        lines.append(f"- Reason: {payload.get('reason')}")
    if payload.get("message"):
        lines.append(f"- Message: {payload.get('message')}")
    return "\n".join(lines).rstrip() + "\n"


def repair_history_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Repair History",
        "",
        f"- Workspace: `{payload.get('workspace')}`",
        f"- Total entries: {payload.get('total_entries')}",
        f"- Returned entries: {payload.get('returned_entries')}",
    ]
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    if not entries:
        lines.extend(["", "No repair history found."])
        return "\n".join(lines).rstrip() + "\n"
    lines.extend(["", "| status | workflow | run | classification | applied | verified | rollback |", "| --- | --- | --- | --- | --- | --- | --- |"])
    for item in entries:
        if not isinstance(item, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(item.get("status")),
                    markdown_cell(item.get("workflow")),
                    markdown_cell(item.get("run_id")),
                    markdown_cell(item.get("classification")),
                    markdown_cell(item.get("applied")),
                    markdown_cell(item.get("verification_status")),
                    markdown_cell(item.get("rollback_status")),
                ]
            )
            + " |"
        )
    return "\n".join(lines).rstrip() + "\n"


def repair_history_entry(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else payload
    repair = payload.get("repair") if isinstance(payload.get("repair"), dict) else {}
    plan = payload.get("workflow_repair_plan") if isinstance(payload.get("workflow_repair_plan"), dict) else {}
    verification = plan.get("verification") if isinstance(plan.get("verification"), dict) else {}
    rollback = plan.get("rollback") if isinstance(plan.get("rollback"), dict) else {}
    failed_step = evidence.get("failed_step") if isinstance(evidence.get("failed_step"), dict) else {}
    candidates = repair.get("candidates") if isinstance(repair.get("candidates"), list) else []
    return {
        "history_id": str(uuid4()),
        "created_at": time(),
        "status": str(payload.get("status") or ""),
        "source": str(payload.get("source") or ""),
        "workflow": str(payload.get("workflow") or evidence.get("workflow") or ""),
        "run_id": str(payload.get("run_id") or evidence.get("run_id") or ""),
        "failed_step": {
            "id": failed_step.get("id"),
            "action": failed_step.get("action"),
            "message": failed_step.get("message"),
        },
        "classification": repair.get("classification"),
        "confidence": repair.get("confidence"),
        "recommended_fix": repair.get("recommended_fix"),
        "apply_supported": repair.get("apply_supported"),
        "selected_candidate_id": repair.get("selected_candidate_id") or plan.get("candidate_id"),
        "candidate_count": len(candidates),
        "applied": bool(plan.get("applied", False)),
        "apply_requested": bool(plan.get("apply_requested", False)),
        "verify_requested": bool(plan.get("verify_requested", False)),
        "rollback_on_fail": bool(plan.get("rollback_on_fail", False)),
        "path": plan.get("path"),
        "backup_path": plan.get("backup_path"),
        "verification_status": verification.get("status"),
        "verification_run_id": verification.get("run_id"),
        "rollback_status": rollback.get("status"),
        "rollback_backup_path": rollback.get("backup_path"),
    }


def markdown_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ")[:120]


def count_by(entries: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in entries:
        value = str(item.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def compact_health_entry(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "history_id": item.get("history_id"),
        "created_at": item.get("created_at"),
        "status": item.get("status"),
        "workflow": item.get("workflow"),
        "run_id": item.get("run_id"),
        "classification": item.get("classification"),
        "confidence": item.get("confidence"),
        "applied": item.get("applied"),
        "verification_status": item.get("verification_status"),
        "rollback_status": item.get("rollback_status"),
    }


def repair_risk_level(
    *,
    total: int,
    applied_count: int,
    verified_count: int,
    failed_verification_count: int,
    rollback_count: int,
    reliability_score: float | None,
) -> str:
    if total == 0:
        return "unknown"
    if rollback_count > 0 or failed_verification_count > 0:
        return "high"
    if applied_count == 0:
        return "medium"
    if reliability_score is not None and reliability_score < 0.5:
        return "high"
    if verified_count == 0:
        return "medium"
    return "low"


def repair_health_recommendation(
    risk_level: str,
    applied_count: int,
    verified_count: int,
    failed_verification_count: int,
    rollback_count: int,
) -> str:
    if risk_level == "unknown":
        return "No repair history yet. Start with diagnose_failure and repair_workflow without apply."
    if rollback_count or failed_verification_count:
        return "Require --verify and --rollback-on-fail before trusting automatic repair patches."
    if applied_count and verified_count == applied_count:
        return "Automatic repair has verified cleanly in the analyzed window."
    if applied_count and verified_count == 0:
        return "Apply only in supervised mode until at least one repaired workflow verifies successfully."
    return "Review recent repair history before enabling unattended apply."
