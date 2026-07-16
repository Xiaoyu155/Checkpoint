from __future__ import annotations

import subprocess
from pathlib import Path

from visual_agent.chief_dispatch import run_dispatch_verification
from visual_agent.command_verification import changed_test_files, is_test_path
from visual_agent.workspace import init_workspace


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    _git(repo, "init")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def test_is_test_path_classification():
    assert is_test_path("tests/test_calc.py")
    assert is_test_path("src/__tests__/app.test.ts")
    assert is_test_path("src/checkout.spec.js")
    assert is_test_path("conftest.py")
    assert is_test_path("pkg/foo_test.go")
    assert is_test_path("快手/test.js")
    assert is_test_path("eval/service-quality-acceptance.mjs")
    assert is_test_path("regression_tests/index.json")
    assert not is_test_path("src/calc.py")
    assert not is_test_path("src/latest_report.py")
    assert not is_test_path("docs/testing.md")  # 'testing' is only a test marker as a directory name


def test_changed_test_files_detects_edits_and_new_files(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "eval").mkdir()
    (repo / "eval" / "acceptance.mjs").write_text("export const cases = [];\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add acceptance")
    assert changed_test_files(repo_root=repo) == []

    # Editing production code does not trip the guard.
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n", encoding="utf-8")
    assert changed_test_files(repo_root=repo) == []

    # Editing an existing test and adding a new one are both caught.
    (repo / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    (repo / "tests" / "test_new.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (repo / "eval" / "acceptance.mjs").write_text("export const cases = ['weakened'];\n", encoding="utf-8")
    assert changed_test_files(repo_root=repo) == ["eval/acceptance.mjs", "tests/test_calc.py", "tests/test_new.py"]


def test_changed_test_files_catches_committed_edits_via_base(tmp_path):
    repo = _make_repo(tmp_path)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "weaken tests")
    # Working tree is clean, but the diff against the branch base still catches it.
    assert changed_test_files(repo_root=repo) == []
    assert changed_test_files(repo_root=repo, base_ref=base) == ["tests/test_calc.py"]


def test_changed_test_files_keeps_chinese_paths_readable(tmp_path):
    repo = _make_repo(tmp_path)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "快手").mkdir()
    (repo / "快手" / "test.js").write_text("console.log('weakened');\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add kuaishou test")

    assert changed_test_files(repo_root=repo, base_ref=base) == ["快手/test.js"]


def test_verification_refuses_tampered_tests(tmp_path):
    repo = _make_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    (repo / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")

    payload = run_dispatch_verification(
        workspace_root=workspace.root,
        plan_id="20260703-tamper",
        repo_root=repo,
        run_profile="dry-run",
        include_slow=False,
        max_workflows=10,
        test_command="python -c \"raise SystemExit(0)\"",
    )
    assert payload["verdict"] == "fail"
    assert payload["tampered_test_files"] == ["tests/test_calc.py"]
    brief = payload["repair_brief"]
    assert brief["source"] == "test_tampering"
    assert "tests/test_calc.py" in brief["repair_prompt"]


def test_verification_allows_test_edits_when_explicit(tmp_path):
    repo = _make_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    (repo / "tests" / "test_calc.py").write_text("def test_add():\n    assert True\n", encoding="utf-8")

    payload = run_dispatch_verification(
        workspace_root=workspace.root,
        plan_id="20260703-allowed",
        repo_root=repo,
        run_profile="dry-run",
        include_slow=False,
        max_workflows=10,
        test_command="python -c \"raise SystemExit(0)\"",
        allow_test_edits=True,
    )
    assert payload["verdict"] == "pass"
    # The edit is still recorded as evidence, just not fatal.
    assert payload["test_files_changed"] == ["tests/test_calc.py"]


def test_test_command_exempts_coverage_gap(tmp_path):
    # A fresh project has no authored workflows, so plans are flagged
    # needs_workflow_coverage. With a test command that must not block dispatch.
    from visual_agent.chief_dispatch import _dispatch_block_reason

    plan = {"status": "needs_workflow_coverage"}
    track = {"agent": "claude-code", "track_kind": "implementation"}
    assert _dispatch_block_reason(plan=plan, track=track, allow_coverage_gap=False, execute=True, has_test_command=True) == ""
    # Without a test command (and no override) the coverage gate still blocks.
    assert _dispatch_block_reason(plan=plan, track=track, allow_coverage_gap=False, execute=True, has_test_command=False) != ""


def test_verification_passes_untampered_repo(tmp_path):
    repo = _make_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b  # honest fix\n", encoding="utf-8")

    payload = run_dispatch_verification(
        workspace_root=workspace.root,
        plan_id="20260703-clean",
        repo_root=repo,
        run_profile="dry-run",
        include_slow=False,
        max_workflows=10,
        test_command="python -c \"raise SystemExit(0)\"",
    )
    assert payload["verdict"] == "pass"
    assert payload["test_files_changed"] == []
