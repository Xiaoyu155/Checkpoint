from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from visual_agent.git_diff import affected_workflows, changed_files, collect_code_changes, workflow_affects_changed_path


@dataclass(frozen=True)
class Ref:
    name: str
    affects: tuple[str, ...] = ()


def test_workflow_affects_changed_path_matches_directory_and_exact_file() -> None:
    changed = ["src/payment/checkout.py", "templates/cart.html"]

    assert workflow_affects_changed_path("src/payment/", changed) is True
    assert workflow_affects_changed_path("templates/cart.html", changed) is True
    assert workflow_affects_changed_path("src/profile/", changed) is False


def test_workflow_affects_changed_path_matches_glob() -> None:
    assert workflow_affects_changed_path("src/**/*.py", ["src/payment/checkout.py"]) is True


def test_affected_workflows_keeps_unscoped_and_matching_workflows() -> None:
    workflows = [
        Ref("always"),
        Ref("checkout", ("src/payment/",)),
        Ref("profile", ("src/profile/",)),
    ]

    selected = affected_workflows(workflows, changed=["src/payment/checkout.py"])

    assert [item.name for item in selected] == ["always", "checkout"]


def test_affected_workflows_returns_all_when_changed_files_unknown() -> None:
    workflows = [Ref("always"), Ref("checkout", ("src/payment/",))]

    assert affected_workflows(workflows, changed=[]) == workflows


def test_collect_code_changes_reads_before_and_after_from_git_diff(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    page = tmp_path / "login.html"
    page.write_text("<form><input name='email'></form>\n", encoding="utf-8")
    git(tmp_path, "add", "login.html")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    page.write_text("<form><input name='email'><button>Save</button></form>\n", encoding="utf-8")

    changes = collect_code_changes(cwd=tmp_path)

    assert len(changes) == 1
    assert changes[0].file_path == "login.html"
    assert changes[0].change_type == "modified"
    assert "<button>Save</button>" in changes[0].after
    assert "<button>Save</button>" not in (changes[0].before or "")


def test_collect_code_changes_includes_untracked_files(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    page = tmp_path / "new.html"
    page.write_text("<form><input name='email'></form>\n", encoding="utf-8")

    changes = collect_code_changes(cwd=tmp_path)

    assert len(changes) == 1
    assert changes[0].file_path == "new.html"
    assert changes[0].change_type == "added"
    assert changes[0].before is None


def test_changed_files_includes_untracked_files_by_default(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("old\n", encoding="utf-8")
    git(tmp_path, "add", "tracked.txt")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    tracked.write_text("new\n", encoding="utf-8")
    (tmp_path / "new_module.py").write_text("print('new')\n", encoding="utf-8")

    changed = changed_files(cwd=tmp_path)

    assert changed == ["new_module.py", "tracked.txt"]
    assert changed_files(cwd=tmp_path, include_untracked=False) == ["tracked.txt"]


def test_changed_files_preserves_non_ascii_untracked_names(tmp_path: Path) -> None:
    init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    (tmp_path / "强制测试记录.md").write_text("# record\n", encoding="utf-8")

    assert changed_files(cwd=tmp_path) == ["强制测试记录.md"]


def init_git_repo(path: Path) -> None:
    try:
        git(path, "init")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git is required for this test")


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
