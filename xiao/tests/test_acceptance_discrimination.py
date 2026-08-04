from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from visual_agent.acceptance_discrimination import (
    TIER_REGRESSION_CLEAR,
    TIER_UNVERIFIED,
    TIER_VERIFIED,
    classify_acceptance,
    probe_base_command,
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
    (root / "app.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
    (root / "check_regression.py").write_text(
        "import app\nassert app.greet() == 'hi'\n",
        encoding="utf-8",
    )
    (root / "check_objective.py").write_text(
        "import app\nassert app.farewell() == 'bye'\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root


def _implement_objective(repo: Path) -> None:
    (repo / "app.py").write_text(
        "def greet():\n    return 'hi'\n\n\ndef farewell():\n    return 'bye'\n",
        encoding="utf-8",
    )


# --- the grading rules -------------------------------------------------------


def test_gate_that_was_red_before_the_change_is_real_acceptance() -> None:
    graded = classify_acceptance(
        command_result={"verdict": "pass", "command": "pytest -q"},
        base_probe={"status": "failed_on_base"},
    )

    assert graded["tier"] == TIER_VERIFIED
    assert graded["discriminating"] is True


def test_gate_that_was_already_green_only_proves_no_regression() -> None:
    graded = classify_acceptance(
        command_result={"verdict": "pass", "command": "pytest -q"},
        base_probe={"status": "passed_on_base"},
    )

    assert graded["tier"] == TIER_REGRESSION_CLEAR
    assert graded["reason_code"] == "acceptance_gate_not_discriminating"
    assert graded["discriminating"] is False


def test_unknown_discrimination_cannot_be_upgraded_to_verified() -> None:
    graded = classify_acceptance(
        command_result={"verdict": "pass", "command": "pytest -q"},
        base_probe={"status": "unknown", "reason": "base_worktree_unavailable"},
    )

    assert graded["tier"] == TIER_REGRESSION_CLEAR
    assert graded["reason_code"] == "acceptance_discrimination_unknown"


def test_missing_command_gate_is_unverified() -> None:
    graded = classify_acceptance(command_result={}, base_probe=None)

    assert graded["tier"] == TIER_UNVERIFIED
    assert graded["reason_code"] == "acceptance_no_command_gate"


def test_failed_command_is_not_graded_as_evidence() -> None:
    graded = classify_acceptance(
        command_result={"verdict": "fail", "command": "pytest -q"},
        base_probe={"status": "failed_on_base"},
    )

    assert graded["tier"] == TIER_UNVERIFIED
    assert graded["reason_code"] == "acceptance_command_failed"


# --- the probe against a real git repository ---------------------------------


def test_probe_detects_a_gate_that_encodes_the_objective(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _implement_objective(repo)

    probe = probe_base_command(
        command="python check_objective.py",
        repo_root=repo,
        base_ref=base,
        timeout_seconds=120.0,
    )

    # The objective check could not pass before farewell() existed.
    assert probe["status"] == "failed_on_base"


def test_probe_detects_a_gate_that_proves_nothing_about_the_objective(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _implement_objective(repo)

    probe = probe_base_command(
        command="python check_regression.py",
        repo_root=repo,
        base_ref=base,
        timeout_seconds=120.0,
    )

    # This is the real defect: green before, green after, stamped "verified".
    assert probe["status"] == "passed_on_base"
    graded = classify_acceptance(
        command_result={"verdict": "pass", "command": "python check_regression.py"},
        base_probe=probe,
    )
    assert graded["tier"] == TIER_REGRESSION_CLEAR


def test_probe_treats_a_broken_environment_as_unknown_not_as_evidence(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    probe = probe_base_command(
        command="definitely-not-a-real-binary --run",
        repo_root=repo,
        base_ref=base,
        timeout_seconds=60.0,
    )

    # A base run that fails because the tool is missing must not be read as
    # "the gate was red before", which would manufacture a false verified.
    assert probe["status"] == "unknown"


def test_probe_leaves_no_worktree_behind(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    probe_base_command(
        command="python check_regression.py",
        repo_root=repo,
        base_ref=base,
        timeout_seconds=120.0,
    )

    listed = _git(repo, "worktree", "list").stdout.strip().splitlines()
    assert len(listed) == 1


def test_probe_result_is_cached_per_base_and_command(repo: Path, tmp_path: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = probe_base_command(
        command="python check_regression.py",
        repo_root=repo,
        base_ref=base,
        timeout_seconds=120.0,
        workspace_root=workspace,
    )
    second = probe_base_command(
        command="python check_regression.py",
        repo_root=repo,
        base_ref=base,
        timeout_seconds=120.0,
        workspace_root=workspace,
    )

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["status"] == first["status"]


def test_probe_without_base_ref_reports_unknown(repo: Path) -> None:
    probe = probe_base_command(command="python check_regression.py", repo_root=repo, base_ref="")

    assert probe["status"] == "unknown"
    assert probe["reason"] == "no_base_ref"
