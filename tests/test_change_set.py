from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from visual_agent.change_set import collect_repository_change_set


def test_change_set_preserves_rename_untracked_unicode_and_stable_digest(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    original = tmp_path / "old name.py"
    original.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-m",
        "initial",
    )
    _git(tmp_path, "mv", "old name.py", "new name.py")
    (tmp_path / "新增.py").write_text("VALUE = 2\n", encoding="utf-8")

    first = collect_repository_change_set(repo_root=tmp_path)
    second = collect_repository_change_set(repo_root=tmp_path)
    facts = {item.path: item for item in first.changes}

    assert first.complete is True
    assert first.errors == ()
    assert facts["old name.py"].state == "deleted"
    assert facts["old name.py"].renamed_to == "new name.py"
    assert facts["new name.py"].state == "created"
    assert facts["new name.py"].renamed_from == "old name.py"
    assert facts["新增.py"].state == "created"
    assert first.digest == second.digest
    assert len(first.digest) == 64


def test_change_set_invalid_base_is_incomplete_instead_of_empty_success(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    result = collect_repository_change_set(repo_root=tmp_path, base_ref="missing-release-base")

    assert result.complete is False
    assert result.changes == ()
    assert result.errors == ("git_base_unavailable",)


def _init_repo(path: Path) -> None:
    try:
        _git(path, "init")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git is required for this test")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
