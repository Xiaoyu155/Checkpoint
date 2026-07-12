from __future__ import annotations

import subprocess
from pathlib import Path

from visual_agent.chief_dispatch import _verification_is_repairable, run_dispatch_verification
from visual_agent.command_verification import changed_acceptance_chain_files
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
    (repo / "src" / "app.js").write_text("export const ok = true;\n", encoding="utf-8")
    (repo / "package.json").write_text(
        '{"scripts":{"test":"node --test"}}\n',
        encoding="utf-8",
    )
    (repo / "conftest.py").write_text("pytest_plugins = []\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def test_npm_command_flags_package_json_change(tmp_path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    (repo / "package.json").write_text('{"scripts":{"test":"node -e true"}}\n', encoding="utf-8")

    assert changed_acceptance_chain_files(repo_root=repo, command="npm test") == ["package.json"]

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("acceptance-chain tampering should block before running the command")

    monkeypatch.setattr("visual_agent.chief_dispatch.run_command_verification", should_not_run)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)

    payload = run_dispatch_verification(
        workspace_root=workspace.root,
        plan_id="p1-package-tamper",
        repo_root=repo,
        run_profile="dry-run",
        include_slow=False,
        max_workflows=10,
        test_command="npm test",
    )

    assert payload["verdict"] == "fail"
    assert payload["tampered_acceptance_chain_files"] == ["package.json"]
    assert payload["repair_brief"]["source"] == "acceptance_chain_tampering"


def test_pytest_command_flags_conftest_change(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "conftest.py").write_text("pytest_plugins = ['weakened']\n", encoding="utf-8")

    assert changed_acceptance_chain_files(repo_root=repo, command="python -m pytest -q") == ["conftest.py"]


def test_unrelated_file_change_not_flagged(tmp_path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "src" / "app.js").write_text("export const ok = 'changed';\n", encoding="utf-8")

    assert changed_acceptance_chain_files(repo_root=repo, command="npm test") == []


def test_allow_test_edits_bypasses_chain_guard(tmp_path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    (repo / "package.json").write_text('{"scripts":{"test":"node -e true"}}\n', encoding="utf-8")
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)

    monkeypatch.setattr(
        "visual_agent.chief_dispatch.run_command_verification",
        lambda **kwargs: {"verdict": "pass", "command": kwargs["command"], "exit_code": 0, "failure_kind": ""},
    )

    payload = run_dispatch_verification(
        workspace_root=workspace.root,
        plan_id="p1-package-allowed",
        repo_root=repo,
        run_profile="dry-run",
        include_slow=False,
        max_workflows=10,
        test_command="npm test",
        allow_test_edits=True,
    )

    assert payload["verdict"] == "pass"
    assert payload["acceptance_chain_files_changed"] == ["package.json"]


def test_acceptance_chain_tampering_is_not_repairable() -> None:
    assert _verification_is_repairable(
        {"repair_brief": {"source": "acceptance_chain_tampering"}}
    ) is False
