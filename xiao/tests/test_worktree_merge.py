from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from visual_agent.chief_dispatch import create_worktree, merge_worktree_branch


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=False)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    if _git(repo, "init").returncode != 0:
        pytest.skip("git not available")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "checkout", "-b", "main")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "seed")
    return repo


def test_merge_verified_branch_lands_on_main(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    worktree = tmp_path / "wt"
    branch = "checkpoint/p1/track-1"
    setup = create_worktree(repo_root=repo, worktree=worktree, branch=branch)
    assert setup["status"] == "created"

    # Worker fixes the bug in the isolated worktree (uncommitted, like Claude Code).
    (worktree / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    result = merge_worktree_branch(repo_root=repo, worktree=worktree, branch=branch, message="fix add")

    assert result["status"] == "merged"
    assert result["target"] == "main"
    # The fix is now on main.
    assert "a + b" in (repo / "calc.py").read_text(encoding="utf-8")


def test_merge_refuses_dirty_target(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    worktree = tmp_path / "wt"
    branch = "checkpoint/p1/track-1"
    create_worktree(repo_root=repo, worktree=worktree, branch=branch)
    (worktree / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    # Main has an unrelated uncommitted change.
    (repo / "other.py").write_text("x = 1\n", encoding="utf-8")

    result = merge_worktree_branch(repo_root=repo, worktree=worktree, branch=branch, message="fix")

    assert result["status"] == "blocked"
    assert "uncommitted" in result["reason"]


def test_merge_nothing_when_no_change(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    worktree = tmp_path / "wt"
    branch = "checkpoint/p1/track-1"
    create_worktree(repo_root=repo, worktree=worktree, branch=branch)
    # Worker changed nothing.
    result = merge_worktree_branch(repo_root=repo, worktree=worktree, branch=branch, message="noop")
    assert result["status"] == "nothing_to_merge"
