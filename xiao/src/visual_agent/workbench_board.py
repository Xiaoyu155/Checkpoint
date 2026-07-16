"""Kanban board logic for the DevPacer web workbench.

The workbench replicates the UX of Vibe Kanban (BloopAI, Apache-2.0): missions
flow across columns — 待办 / 进行中 / 待验收 / 待合并 / 已完成 — and a
verified mission can be merged from the board with one click. This module holds
the pure, testable decisions: column classification, merge-state discovery, and
the late merge itself. The HTTP layer in ``dashboard.py`` stays thin.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .chief_plans_store import load_plan, load_verification
from .missions import append_round, load_mission, load_rounds, mission_dir, missions_dir, save_mission


BOARD_COLUMNS = ("todo", "in_progress", "in_review", "pending_merge", "done")

_RUNNING_STATUSES = {"running", "preview_running", "starting", "background_running"}
_DONE_MERGE_STATES = {"merged", "nothing_to_merge"}


def board_column(status: str, *, merge_state: str = "") -> str:
    """Map a mission status (plus its merge state) onto a board column.

    - todo: created/queued/pending
    - in_progress: running
    - in_review: stopped/failed/needs_clarification (needs human decision)
    - pending_merge: verified but not yet merged
    - done: merged or nothing_to_merge
    """
    text = str(status or "").strip().lower()
    if text in {"created", "queued", "pending"}:
        return "todo"
    if text in _RUNNING_STATUSES:
        return "in_progress"
    if text == "merged":
        return "done"
    if text == "verified":
        if merge_state in _DONE_MERGE_STATES:
            return "done"
        return "pending_merge"
    # stopped/failed/needs_clarification → needs human review
    return "in_review"


def mission_merge_state(workspace_root: str | Path, mission_id: str) -> str:
    """The last recorded merge outcome for a mission ("" when never attempted).

    Merge rounds are appended by ``chief_run`` (auto-merge) and by
    ``merge_mission_now`` below, so reading the last one covers both paths.
    """
    rounds_path = mission_dir(workspace_root, mission_id) / "rounds.jsonl"
    if not rounds_path.exists():
        return ""
    state = ""
    try:
        lines = rounds_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("type") == "merge":
            state = str(record.get("status") or "")
    return state


def attach_board_fields(workspace_root: str | Path, missions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decorate mission summaries with ``board_column`` / ``merge_state`` /
    ``can_merge`` so the page renders the board without extra requests."""
    decorated: list[dict[str, Any]] = []
    for mission in missions:
        item = dict(mission)
        merge_state = mission_merge_state(workspace_root, str(item.get("mission_id") or ""))
        item["merge_state"] = merge_state
        item["board_column"] = board_column(str(item.get("status") or ""), merge_state=merge_state)
        item["can_merge"] = str(item.get("status") or "") == "verified" and merge_state not in _DONE_MERGE_STATES
        decorated.append(item)
    return decorated


def _implementation_track(plan: dict[str, Any]) -> dict[str, Any] | None:
    tracks = plan.get("worker_tracks") if isinstance(plan.get("worker_tracks"), list) else []
    for track in tracks:
        if isinstance(track, dict) and str(track.get("track_kind") or "implementation") != "inspection":
            return track
    return None


def merge_mission_now(workspace_root: str | Path, mission_id: str) -> dict[str, Any]:
    """Merge a verified mission's isolated branch back, on user request.

    This is the "合并" button on the board: manual merge policy leaves the
    worktree in place after verification, and the worktree/branch names are
    deterministic from (plan_id, track_id), so they can be reconstructed here
    without having persisted them.
    """
    from .chief_dispatch import default_branch_name, default_worktree_path, merge_worktree_branch
    from .missions import append_round, load_rounds

    root = Path(workspace_root).expanduser().resolve()
    mission = load_mission(root, mission_id)
    if not mission:
        return {"ok": False, "error": f"找不到任务：{mission_id}"}
    status = str(mission.get("status") or "")
    if status != "verified":
        return {"ok": False, "error": f"只有验收通过（verified）的任务才能合并，当前状态：{status or '未知'}。"}
    merge_state = mission_merge_state(root, mission_id)
    if merge_state in _DONE_MERGE_STATES:
        return {"ok": False, "error": "该任务已经合并过了。"}

    plan_id = str(mission.get("plan_id") or "")
    plan = load_plan(root, plan_id) if plan_id else None
    if plan is None:
        return {"ok": False, "error": f"找不到任务对应的计划：{plan_id or '(无)'}"}
    track = _implementation_track(plan)
    if track is None:
        return {"ok": False, "error": "计划里没有可合并的实现分支。"}
    track_id = str(track.get("id") or "track_1_codex")
    repo_root = Path(str(plan.get("repo_root") or mission.get("repo_root") or ".")).expanduser().resolve()
    worktree = default_worktree_path(repo_root=repo_root, plan_id=plan_id, track_id=track_id)
    branch = default_branch_name(plan_id=plan_id, track_id=track_id)
    if not worktree.exists():
        return {
            "ok": False,
            "error": f"隔离工作目录已不存在（{worktree}），无法自动合并。可以手动合并分支 {branch}。",
        }

    result = merge_worktree_branch(
        repo_root=repo_root,
        worktree=worktree,
        branch=branch,
        message=str(plan.get("objective") or mission.get("objective") or "DevPacer change"),
    )

    round_no = max((int(r.get("round") or 0) for r in load_rounds(root, mission_id)), default=0) + 1
    append_round(
        root,
        mission_id,
        {
            "round": round_no,
            "type": "merge",
            "status": str(result.get("status") or ""),
            "branch": result.get("branch"),
            "target": result.get("target"),
            "commit": result.get("commit"),
            "reason": result.get("reason"),
        },
    )
    merged = str(result.get("status") or "") in _DONE_MERGE_STATES
    if merged:
        mission["status"] = "merged"
        save_mission(root, mission)
    return {"ok": merged, "merge": result, "mission_status": mission.get("status")}


def archive_mission_now(workspace_root: str | Path, mission_id: str) -> dict[str, Any]:
    """Hide a mission from the board while keeping its local trace on disk."""
    root = Path(workspace_root).expanduser().resolve()
    mission = load_mission(root, mission_id)
    if not mission:
        return {"ok": False, "error": f"找不到任务：{mission_id}"}

    status = str(mission.get("status") or "")
    if status in _RUNNING_STATUSES:
        return {"ok": False, "error": f"运行中的任务不能删除，当前状态：{status or '未知'}。"}
    if status == "archived" or bool(mission.get("hidden")):
        return {"ok": False, "error": "该任务已经被删除/归档过了。"}

    round_no = max((int(r.get("round") or 0) for r in load_rounds(root, mission_id)), default=0) + 1
    append_round(
        root,
        mission_id,
        {
            "round": round_no,
            "type": "archive",
            "status": "archived",
            "reason": "user_deleted",
        },
    )
    mission["status"] = "archived"
    mission["stop_reason"] = "archived"
    mission["hidden"] = True
    mission["archived_at"] = datetime.now(timezone.utc).isoformat()
    save_mission(root, mission)
    try:
        from .dashboard.data import invalidate_dashboard_data_cache

        invalidate_dashboard_data_cache(root)
    except Exception:
        pass
    return {"ok": True, "mission_status": mission.get("status"), "mission_id": mission_id}


def archive_all_missions_now(workspace_root: str | Path) -> dict[str, Any]:
    """Hide all non-running missions from the board while preserving traces."""
    root = Path(workspace_root).expanduser().resolve()
    directory = missions_dir(root)
    if not directory.exists():
        return {"ok": True, "archived": 0, "skipped_running": 0, "errors": []}

    archived = 0
    skipped_running = 0
    errors: list[dict[str, str]] = []
    for mission_json in sorted(directory.glob("*/mission.json")):
        mission_id = mission_json.parent.name
        mission = load_mission(root, mission_id) or {}
        status = str(mission.get("status") or "")
        if status in _RUNNING_STATUSES:
            skipped_running += 1
            continue
        if status in {"verified", "merged"}:
            continue
        if status == "archived" or bool(mission.get("hidden")):
            continue
        result = archive_mission_now(root, mission_id)
        if result.get("ok"):
            archived += 1
        else:
            errors.append({"mission_id": mission_id, "error": str(result.get("error") or "unknown")})
    try:
        from .dashboard.data import invalidate_dashboard_data_cache

        invalidate_dashboard_data_cache(root)
    except Exception:
        pass
    return {"ok": not errors, "archived": archived, "skipped_running": skipped_running, "errors": errors}


def mission_review_payload(workspace_root: str | Path, mission: dict[str, Any]) -> dict[str, Any]:
    """Verification evidence for the detail drawer: verdict, the command that
    ran, and the Layer-2 diff summary (files touched, +/- lines, checklist)."""
    plan_id = str(mission.get("plan_id") or "")
    if not plan_id:
        return {}
    verification = load_verification(Path(workspace_root), plan_id)
    if not isinstance(verification, dict):
        return {}
    command = (
        verification.get("command_verification", {}).get("command")
        if isinstance(verification.get("command_verification"), dict)
        else ""
    )
    return {
        "verdict": str(verification.get("verdict") or ""),
        "command": str(command or ""),
        "markdown": str(verification.get("markdown") or ""),
        "diff_summary": verification.get("diff_summary") if isinstance(verification.get("diff_summary"), dict) else {},
        "warnings": [str(w) for w in (verification.get("warnings") or [])],
        "recorded_at": str(verification.get("recorded_at") or ""),
    }
