"""Reclaim Pacer isolation worktrees.

Every mission runs in its own git worktree under ``<repo>.checkpoint-worktrees``.
Nothing removed them, so a month of dogfooding left dozens of full repo copies
registered in git and on disk. This module finds those worktrees and removes the
ones that are finished and carry no unmerged work.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


WORKTREE_ROOT_SUFFIX = ".checkpoint-worktrees"


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )


def list_pacer_worktrees(repo_root: str | Path) -> list[dict[str, Any]]:
    """Every registered worktree that Pacer created for mission isolation."""

    root = Path(repo_root).expanduser().resolve()
    completed = _git(root, "worktree", "list", "--porcelain")
    if completed.returncode != 0:
        return []
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in completed.stdout.splitlines():
        line = line.rstrip()
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                entries.append(current)
            current = {"path": value}
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            current["branch"] = ""
        elif key == "HEAD":
            current["head"] = value
    if current:
        entries.append(current)

    pacer: list[dict[str, Any]] = []
    for entry in entries:
        path = Path(str(entry.get("path") or ""))
        if not any(part.endswith(WORKTREE_ROOT_SUFFIX) for part in path.parts):
            continue
        entry["path"] = str(path)
        entry["exists"] = path.is_dir()
        entry["age_days"] = _age_days(path)
        entry["dirty"] = _is_dirty(path) if path.is_dir() else False
        entry["merged"] = _is_merged(root, str(entry.get("branch") or ""))
        pacer.append(entry)
    return pacer


def collect_reapable(
    repo_root: str | Path,
    *,
    keep_days: float = 2.0,
    keep_last: int = 2,
) -> dict[str, Any]:
    """Split Pacer worktrees into the ones safe to remove and the ones to keep.

    Safe means: older than ``keep_days``, not one of the ``keep_last`` most
    recent, and carrying no uncommitted changes. Uncommitted work is always kept,
    merged branch or not — the working tree is the only copy of it. Committed
    work survives worktree removal because the branch stays in the repo.
    """

    worktrees = list_pacer_worktrees(repo_root)
    worktrees.sort(key=lambda item: float(item.get("age_days") or 0.0))
    reapable: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for index, entry in enumerate(worktrees):
        reason = ""
        if index < max(keep_last, 0):
            reason = "recent"
        elif float(entry.get("age_days") or 0.0) < keep_days:
            reason = "younger_than_keep_days"
        elif entry.get("dirty"):
            reason = "uncommitted_changes"
        if reason:
            kept.append({**entry, "keep_reason": reason})
        else:
            reapable.append(entry)
    return {
        "repo_root": str(Path(repo_root).expanduser().resolve()),
        "total": len(worktrees),
        "reapable": reapable,
        "kept": kept,
    }


def reap_worktrees(
    repo_root: str | Path,
    *,
    keep_days: float = 2.0,
    keep_last: int = 2,
    dry_run: bool = True,
    delete_branches: bool = True,
) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve()
    plan = collect_reapable(root, keep_days=keep_days, keep_last=keep_last)
    removed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    if not dry_run:
        for entry in plan["reapable"]:
            outcome = remove_worktree(root, entry.get("path") or "", branch=str(entry.get("branch") or "") if delete_branches else "")
            (removed if outcome.get("removed") else failed).append({**entry, **outcome})
        _git(root, "worktree", "prune")
        _prune_empty_roots(root)
    return {
        **plan,
        "dry_run": dry_run,
        "removed": removed,
        "failed": failed,
        "removed_count": len(removed),
    }


def remove_worktree(repo_root: str | Path, worktree: str | Path, *, branch: str = "") -> dict[str, Any]:
    """Remove one isolation worktree, and its branch when it is fully merged."""

    root = Path(repo_root).expanduser().resolve()
    path = Path(worktree).expanduser()
    completed = _git(root, "worktree", "remove", "--force", str(path))
    removed = completed.returncode == 0
    error = "" if removed else (completed.stderr or completed.stdout).strip()
    if not removed and not path.exists():
        # Directory already gone; drop the stale registration instead.
        _git(root, "worktree", "prune")
        removed = True
        error = ""
    branch_deleted = False
    if removed and branch:
        # -d refuses to delete unmerged work; that refusal is the safety net.
        branch_result = _git(root, "branch", "-d", branch)
        branch_deleted = branch_result.returncode == 0
    return {"removed": removed, "error": error, "branch_deleted": branch_deleted}


def reap_mission_worktree(repo_root: str | Path, worktree: str | Path, *, branch: str = "") -> dict[str, Any]:
    """Best-effort cleanup for a single finished mission.

    Called at mission close, where a cleanup failure must never turn a delivered
    mission into a failed one.
    """

    try:
        path = Path(worktree).expanduser().resolve()
    except OSError:
        return {"removed": False, "error": "invalid_path", "branch_deleted": False}
    if not any(part.endswith(WORKTREE_ROOT_SUFFIX) for part in path.parts):
        return {"removed": False, "error": "not_a_pacer_worktree", "branch_deleted": False}
    if _is_dirty(path):
        return {"removed": False, "error": "worktree_dirty", "branch_deleted": False}
    try:
        result = remove_worktree(repo_root, path, branch=branch)
    except OSError as exc:
        return {"removed": False, "error": str(exc), "branch_deleted": False}
    if result.get("removed"):
        _prune_empty_roots(Path(repo_root).expanduser().resolve())
    return result


def worktree_report_to_markdown(payload: dict[str, Any]) -> str:
    reapable = payload.get("reapable") if isinstance(payload.get("reapable"), list) else []
    kept = payload.get("kept") if isinstance(payload.get("kept"), list) else []
    lines = [
        "# Pacer 隔离 worktree",
        "",
        f"- 仓库：`{payload.get('repo_root')}`",
        f"- 注册总数：`{payload.get('total', 0)}` · 可回收：`{len(reapable)}` · 保留：`{len(kept)}`",
    ]
    if payload.get("dry_run") is False:
        lines.append(f"- 本次已删除：`{payload.get('removed_count', 0)}`")
        if payload.get("failed"):
            lines.append(f"- 删除失败：`{len(payload['failed'])}`")
    lines.append("")
    if reapable:
        lines.append("## 可回收" if payload.get("dry_run") else "## 已回收")
        for entry in reapable[:50]:
            lines.append(f"- `{entry.get('path')}` · {float(entry.get('age_days') or 0):.1f} 天 · branch `{entry.get('branch') or '-'}`")
        lines.append("")
    if kept:
        lines.append("## 保留")
        for entry in kept[:50]:
            lines.append(f"- `{entry.get('path')}` · {entry.get('keep_reason')}")
    return "\n".join(lines)


def _age_days(path: Path) -> float:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return 0.0
    return max((datetime.now(timezone.utc) - modified) / timedelta(days=1), 0.0)


def _is_dirty(path: Path) -> bool:
    # Without core.quotepath=false git escapes non-ASCII paths into octal, and
    # Pacer's own 强制测试记录.md would never match the skip list below.
    completed = _git(path, "-c", "core.quotepath=false", "status", "--porcelain")
    if completed.returncode != 0:
        return True
    for line in completed.stdout.splitlines():
        entry = line[3:].strip().strip('"')
        if not entry:
            continue
        # Pacer's own runtime artifacts are not user work.
        if entry.startswith((".agent-workspace", "强制测试记录.md")) or "__pycache__" in entry:
            continue
        return True
    return False


def _is_merged(repo_root: Path, branch: str) -> bool:
    if not branch:
        return False
    completed = _git(repo_root, "branch", "--merged", "HEAD", "--format=%(refname:short)")
    if completed.returncode != 0:
        return False
    return branch in {line.strip() for line in completed.stdout.splitlines()}


def _prune_empty_roots(repo_root: Path) -> None:
    root = repo_root.parent / f"{repo_root.name}{WORKTREE_ROOT_SUFFIX}"
    if not root.is_dir():
        return
    for child in sorted(root.iterdir(), reverse=True):
        if child.is_dir():
            try:
                child.rmdir()
            except OSError:
                continue
    try:
        root.rmdir()
    except OSError:
        pass
