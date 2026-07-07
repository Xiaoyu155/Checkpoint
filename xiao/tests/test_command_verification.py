from __future__ import annotations

import sys

from visual_agent.chief_dispatch import run_dispatch_verification
from visual_agent.codex_check import CodexCheckResult
from visual_agent.command_verification import command_repair_brief, run_command_verification
from visual_agent.workspace import init_workspace


def test_command_verification_pass(tmp_path) -> None:
    r = run_command_verification(command=f'"{sys.executable}" -c "exit(0)"', repo_root=tmp_path)
    assert r["verdict"] == "pass"
    assert r["exit_code"] == 0


def test_command_verification_fail_keeps_output(tmp_path) -> None:
    r = run_command_verification(command=f'"{sys.executable}" -c "print(chr(66)+chr(65)+chr(68)); exit(3)"', repo_root=tmp_path)
    assert r["verdict"] == "fail"
    assert r["exit_code"] == 3
    assert "BAD" in r["output_tail"]
    brief = command_repair_brief(r)
    assert brief["source"] == "test_command"
    assert "failed" in brief["message"].lower()


def test_command_verification_classifies_invalid_command(tmp_path) -> None:
    r = run_command_verification(command="definitely-not-a-real-devpacer-command-xyz", repo_root=tmp_path)
    assert r["verdict"] == "fail"
    assert r["failure_kind"] == "test_command_invalid"


def _no_workflow_runner(*_args, **_kwargs):
    return CodexCheckResult(changed_files=[], selected_workflows=[], skipped_slow_workflows=[], coverage={"status": "no_changed_files"}, results=[])


def test_dispatch_verification_command_pass_is_acceptance_without_workflows(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    payload = run_dispatch_verification(
        workspace_root=workspace.root,
        plan_id="p1",
        repo_root=tmp_path,
        run_profile="dry-run",
        include_slow=False,
        max_workflows=5,
        codex_runner=_no_workflow_runner,
        test_command=f'"{sys.executable}" -c "exit(0)"',
    )
    # Command passed and there are no product workflows -> the command is the gate.
    assert payload["verdict"] == "pass"
    assert payload["command_verification"]["verdict"] == "pass"


def test_dispatch_verification_command_fail_short_circuits(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)

    def _should_not_run(*_a, **_k):
        raise AssertionError("workflow verification should be skipped when the test command fails")

    payload = run_dispatch_verification(
        workspace_root=workspace.root,
        plan_id="p1",
        repo_root=tmp_path,
        run_profile="dry-run",
        include_slow=False,
        max_workflows=5,
        codex_runner=_should_not_run,
        test_command=f'"{sys.executable}" -c "exit(1)"',
    )
    assert payload["verdict"] == "fail"
    assert payload["repair_brief"]["source"] == "test_command"
