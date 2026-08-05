from __future__ import annotations

import subprocess
from pathlib import Path

from visual_agent.chief_dispatch import chief_dispatch_to_markdown, commit_worker_changes
from visual_agent.delivery_check import delivery_markdown_lines, run_delivery_check


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=False)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "lib.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (repo / "check.py").write_text(
        "from lib import helper\nassert helper() == 1\nprint('ok')\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _branch_with(repo: Path, branch: str, files: dict[str, str]) -> None:
    _git(repo, "checkout", "-q", "-b", branch)
    for name, content in files.items():
        (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"work on {branch}")
    _git(repo, "checkout", "-q", "main")


PY = "python check.py"


def test_a_self_contained_change_is_reported_as_delivered(tmp_path) -> None:
    repo = _repo(tmp_path)
    _branch_with(
        repo,
        "work",
        {
            "lib.py": "def helper():\n    return 2\n",
            "check.py": "from lib import helper\nassert helper() == 2\nprint('ok')\n",
        },
    )

    result = run_delivery_check(repo_root=repo, branch="work", command=PY)

    assert result["status"] == "delivered"
    assert result["exit_code"] == 0


def test_a_change_that_needs_uncommitted_work_is_caught(tmp_path) -> None:
    """The 2026-08-04 failure, reproduced.

    The feature imports a symbol that only exists in the developer's working
    tree. Acceptance passes where the worker ran it, and the change cannot start
    from a clean checkout.
    """
    repo = _repo(tmp_path)
    _branch_with(
        repo,
        "work",
        # The committed change calls a helper that was never committed.
        {"check.py": "from lib import missing_helper\nprint(missing_helper())\n"},
    )
    # It "works" here only because of an uncommitted edit, exactly as it did
    # in the real incident.
    (repo / "lib.py").write_text(
        "def helper():\n    return 1\n\n\ndef missing_helper():\n    return 'ok'\n", encoding="utf-8"
    )

    result = run_delivery_check(repo_root=repo, branch="work", command=PY)

    assert result["status"] == "not_delivered"
    assert result["exit_code"] != 0
    assert "missing_helper" in result["output_tail"]


def test_the_dirty_working_tree_never_leaks_into_the_check(tmp_path) -> None:
    repo = _repo(tmp_path)
    _branch_with(repo, "work", {"check.py": "from lib import helper\nassert helper() == 1\nprint('ok')\n"})
    # A file that exists only in the developer's working tree must not be able
    # to rescue the check — that is the whole point of checking out clean.
    (repo / "rescue.py").write_text("SHOULD_NOT_BE_VISIBLE = True\n", encoding="utf-8")

    result = run_delivery_check(
        repo_root=repo,
        branch="work",
        command="python -c \"import os,sys; sys.exit(1 if os.path.exists('rescue.py') else 0)\"",
    )

    assert result["status"] == "delivered"


def test_a_missing_command_is_unchecked_rather_than_passed(tmp_path) -> None:
    repo = _repo(tmp_path)
    _branch_with(repo, "work", {"lib.py": "def helper():\n    return 1\n"})

    result = run_delivery_check(repo_root=repo, branch="work", command="   ")

    # No way to demonstrate delivery is not the same as delivery.
    assert result["status"] == "unchecked"


def test_a_missing_branch_is_unchecked_rather_than_failed(tmp_path) -> None:
    repo = _repo(tmp_path)

    result = run_delivery_check(repo_root=repo, branch="no-such-branch", command=PY)

    # Reporting not_delivered here would be a false red about work that exists.
    assert result["status"] == "unchecked"
    assert "branch not found" in result["reason"]


def test_a_non_git_directory_is_unchecked(tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    assert run_delivery_check(repo_root=plain, branch="main", command=PY)["status"] == "unchecked"


def test_the_check_leaves_no_worktree_behind(tmp_path) -> None:
    repo = _repo(tmp_path)
    _branch_with(repo, "work", {"lib.py": "def helper():\n    return 1\n"})

    run_delivery_check(repo_root=repo, branch="work", command=PY)

    listed = _git(repo, "worktree", "list").stdout
    assert "pacer-delivery-" not in listed


def test_the_branch_stays_usable_after_the_check(tmp_path) -> None:
    repo = _repo(tmp_path)
    _branch_with(repo, "work", {"lib.py": "def helper():\n    return 1\n"})

    run_delivery_check(repo_root=repo, branch="work", command=PY)

    # A non-detached checkout would lock the branch and break the later merge.
    assert _git(repo, "checkout", "-q", "work").returncode == 0


def test_commit_worker_changes_keeps_pacer_runtime_files_out(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / ".agent-workspace").mkdir()
    (repo / ".agent-workspace" / "state.json").write_text("{}", encoding="utf-8")

    result = commit_worker_changes(worktree=repo, message="Pacer verified change: add feature")

    assert result["status"] == "committed"
    tracked = _git(repo, "ls-files").stdout
    assert "feature.py" in tracked
    assert ".agent-workspace" not in tracked


def test_commit_worker_changes_reports_when_there_is_nothing_to_commit(tmp_path) -> None:
    repo = _repo(tmp_path)

    assert commit_worker_changes(worktree=repo, message="x")["status"] == "nothing_to_commit"


def test_not_delivered_is_told_to_the_user_without_claiming_done() -> None:
    markdown = chief_dispatch_to_markdown(
        {
            "status": "verified",
            "delivery_check": {
                "status": "not_delivered",
                "exit_code": 1,
                "output_tail": "ImportError: cannot import name 'missing_helper'",
            },
        }
    )

    assert "交付验证：没通过" in markdown
    assert "现在还不能说这件事做完了" in markdown
    assert "missing_helper" in markdown


def test_unchecked_delivery_does_not_look_like_a_pass() -> None:
    lines = "\n".join(delivery_markdown_lines({"status": "unchecked", "reason": "no acceptance command"}))

    assert "没能做" in lines
    assert "不覆盖" in lines


def test_the_user_story_stops_saying_done_when_delivery_failed() -> None:
    from visual_agent.pacer_voice import user_story

    done = user_story(stop_reason="verified", goal="x")
    blocked = user_story(stop_reason="verified", goal="x", delivery_status="not_delivered")

    assert "可以收工了" in done["headline"]
    assert "可以收工" not in blocked["headline"]
    assert blocked["technical_tag"] == "verified_not_delivered"


def test_a_broken_delivery_check_never_fails_the_dispatch(tmp_path) -> None:
    from visual_agent.chief_dispatch import _run_dispatch_delivery_check

    # The worktree is gone. Earlier this raised out of the dispatch and turned a
    # verified mission into a crash — the check must degrade, not destroy.
    result = _run_dispatch_delivery_check(
        status="verified",
        repo_root=tmp_path,
        worktree=tmp_path / "missing",
        worktree_project_root=tmp_path / "missing",
        branch="work",
        objective="x",
        plan_id="p1",
        mission_id="m1",
        command="python -c pass",
        env=None,
        timeout_seconds=60.0,
        enabled=True,
    )

    assert result["status"] == "unchecked"


def test_the_check_is_skipped_when_the_dispatch_did_not_pass(tmp_path) -> None:
    from visual_agent.chief_dispatch import _run_dispatch_delivery_check

    # Asking whether a failed change is deliverable answers a question nobody
    # asked, and costs a full test run.
    assert _run_dispatch_delivery_check(
        status="verification_failed",
        repo_root=tmp_path,
        worktree=tmp_path,
        worktree_project_root=tmp_path,
        branch="work",
        objective="x",
        plan_id="p1",
        mission_id="m1",
        command="python -c pass",
        env=None,
        timeout_seconds=60.0,
        enabled=True,
    ) is None


def test_the_commit_records_which_mission_produced_it() -> None:
    from visual_agent.chief_dispatch import _delivery_commit_message

    message = _delivery_commit_message(
        objective="给宠物加生日贺卡", plan_id="p-42", mission_id="20260804-043801"
    )

    # Attribution is the half of delivery a person cannot reconstruct later.
    assert "Pacer-Mission: 20260804-043801" in message
    assert "Pacer-Plan: p-42" in message
    assert message.splitlines()[0].endswith("给宠物加生日贺卡")
