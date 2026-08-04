from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from visual_agent.worktree_gc import (
    collect_reapable,
    list_pacer_worktrees,
    reap_mission_worktree,
    reap_worktrees,
    worktree_report_to_markdown,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "pacer@example.com")
    _git(root, "config", "user.name", "Pacer Test")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-m", "init")
    return root


def _add_worktree(repo_root: Path, plan_id: str, track: str = "track-1-codex") -> tuple[Path, str]:
    path = repo_root.parent / f"{repo_root.name}.checkpoint-worktrees" / plan_id / track
    branch = f"checkpoint/{plan_id}/{track}"
    _git(repo_root, "worktree", "add", "-b", branch, str(path))
    return path, branch


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


def test_list_pacer_worktrees_ignores_the_main_checkout(repo: Path) -> None:
    path, branch = _add_worktree(repo, "20260801-aaa")

    entries = list_pacer_worktrees(repo)

    assert [entry["path"] for entry in entries] == [str(path)]
    assert entries[0]["branch"] == branch
    assert entries[0]["dirty"] is False


def test_collect_reapable_keeps_recent_and_young_worktrees(repo: Path) -> None:
    fresh, _ = _add_worktree(repo, "20260803-fresh")
    old, _ = _add_worktree(repo, "20260701-old")
    _age(old, 20)

    plan = collect_reapable(repo, keep_days=2.0, keep_last=1)

    assert [entry["path"] for entry in plan["reapable"]] == [str(old)]
    assert [entry["path"] for entry in plan["kept"]] == [str(fresh)]


def test_collect_reapable_keeps_uncommitted_worker_output(repo: Path) -> None:
    path, _ = _add_worktree(repo, "20260701-dirty")
    (path / "app.py").write_text("value = 2\n", encoding="utf-8")
    _age(path, 20)

    plan = collect_reapable(repo, keep_days=2.0, keep_last=0)

    assert plan["reapable"] == []
    assert plan["kept"][0]["keep_reason"] == "uncommitted_changes"


def test_collect_reapable_ignores_pacer_runtime_artifacts(repo: Path) -> None:
    path, _ = _add_worktree(repo, "20260701-runtime")
    (path / ".agent-workspace").mkdir()
    (path / ".agent-workspace" / "state.json").write_text("{}", encoding="utf-8")
    (path / "强制测试记录.md").write_text("record", encoding="utf-8")
    _age(path, 20)

    plan = collect_reapable(repo, keep_days=2.0, keep_last=0)

    assert [entry["path"] for entry in plan["reapable"]] == [str(path)]


def test_reap_worktrees_dry_run_removes_nothing(repo: Path) -> None:
    path, _ = _add_worktree(repo, "20260701-old")
    _age(path, 20)

    payload = reap_worktrees(repo, keep_days=2.0, keep_last=0, dry_run=True)

    assert payload["dry_run"] is True
    assert payload["removed_count"] == 0
    assert path.is_dir()


def test_reap_worktrees_removes_and_deregisters(repo: Path) -> None:
    path, branch = _add_worktree(repo, "20260701-old")
    _age(path, 20)

    payload = reap_worktrees(repo, keep_days=2.0, keep_last=0, dry_run=False)

    assert payload["removed_count"] == 1
    assert not path.exists()
    assert list_pacer_worktrees(repo) == []
    # Nothing was committed on this branch, so deleting it loses nothing.
    assert branch not in _branches(repo)


def test_reap_worktrees_preserves_branches_with_unmerged_commits(repo: Path) -> None:
    path, branch = _add_worktree(repo, "20260701-committed")
    (path / "app.py").write_text("value = 2\n", encoding="utf-8")
    _git(path, "add", "app.py")
    _git(path, "commit", "-m", "worker change")
    _age(path, 20)

    payload = reap_worktrees(repo, keep_days=2.0, keep_last=0, dry_run=False)

    assert payload["removed_count"] == 1
    assert not path.exists()
    # The commits are the worker's output; `git branch -d` refuses to drop them.
    assert branch in _branches(repo)


def _branches(repo: Path) -> str:
    return subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_reap_mission_worktree_refuses_dirty_and_foreign_paths(repo: Path, tmp_path: Path) -> None:
    dirty, branch = _add_worktree(repo, "20260801-dirty")
    (dirty / "app.py").write_text("value = 3\n", encoding="utf-8")

    assert reap_mission_worktree(repo, dirty, branch=branch)["error"] == "worktree_dirty"
    assert reap_mission_worktree(repo, tmp_path / "elsewhere")["error"] == "not_a_pacer_worktree"
    assert dirty.is_dir()


def test_reap_mission_worktree_removes_clean_worktree(repo: Path) -> None:
    path, branch = _add_worktree(repo, "20260801-clean")

    result = reap_mission_worktree(repo, path, branch=branch)

    assert result["removed"] is True
    assert not path.exists()


def test_worktree_report_markdown_lists_both_buckets(repo: Path) -> None:
    old, _ = _add_worktree(repo, "20260701-old")
    _age(old, 20)
    _add_worktree(repo, "20260803-fresh")

    text = worktree_report_to_markdown(reap_worktrees(repo, keep_days=2.0, keep_last=1, dry_run=True))

    assert "可回收" in text
    assert "保留" in text
