from __future__ import annotations

import pytest
import subprocess

from visual_agent.command_verification import is_test_path, changed_snapshot_files, snapshot_verification_files
from visual_agent.subagent_orchestrator import SubagentOrchestrator, SubagentPlan, SubagentTask


@pytest.mark.parametrize("name", ["__pycache__/test_calc.cpython-311-pytest-9.0.3.pyc", "tests/__pycache__/test_api.pyc",
                                  ".pytest_cache/test_state", "tests/test_old.pyo"])
def test_generated_files_are_not_test_contracts(name):
    assert not is_test_path(name)


def engine(tmp_path, goal="Fix calc.py; do not modify tests"):
    task = SubagentTask("coder", "Implement", "coder", "grok", "Fix calc.py", target_files=["calc.py"],
                        verification_command="python -m pytest -q", max_retries=1)
    return SubagentOrchestrator(SubagentPlan(goal, [task], str(tmp_path)), workspace_root=tmp_path,
                               show_timeline=False, probe_base_discrimination=False)


def test_dirty_tests_are_task_input_but_subsequent_changes_are_rejected(tmp_path, monkeypatch):
    test = tmp_path / "test_calc.py"
    test.write_text("def test_original():\n    assert 1 == 1\n")
    for args in (["git", "init", "-q"], ["git", "add", "test_calc.py"],
                 ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=disabled-hooks", "commit", "-qm", "baseline"]):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)
    test.write_text("def test_user_version():\n    assert 7 == 7\n")
    instance = engine(tmp_path)
    calls = []
    monkeypatch.setattr("visual_agent.subagent_orchestrator.run_command_verification",
                        lambda **kwargs: calls.append(kwargs) or {"verdict": "pass"})
    assert instance._run_verification_command("python -m pytest -q")[0]
    test.write_text("def test_worker_weakened():\n    assert True\n")
    ok, message = instance._run_verification_command("python -m pytest -q")
    assert not ok
    assert "after this task started" in message
    assert len(calls) == 1


def test_existing_modified_acceptance_configuration_is_preserved(tmp_path, monkeypatch):
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\naddopts = -q\n")
    instance = engine(tmp_path)
    monkeypatch.setattr("visual_agent.subagent_orchestrator.run_command_verification", lambda **kw: {"verdict": "pass"})
    assert instance._run_verification_command("python -m pytest -q")[0]
    config.write_text("[pytest]\naddopts = --ignore=tests\n")
    assert not instance._run_verification_command("python -m pytest -q")[0]


def test_new_test_created_during_task_is_not_part_of_original_baseline(tmp_path):
    before = snapshot_verification_files(repo_root=tmp_path)
    (tmp_path / "test_injected.py").write_text("def test_fake():\n    assert True\n")
    after = snapshot_verification_files(repo_root=tmp_path)
    assert changed_snapshot_files(before, after) == ["test_injected.py"]


def test_nested_test_snapshot_works_from_monorepo_root(tmp_path):
    test = tmp_path / "nested/tests/test_api.py"
    test.parent.mkdir(parents=True)
    test.write_text("def test_api():\n    assert 1 == 1\n")
    before = snapshot_verification_files(repo_root=tmp_path)
    test.write_text("def test_api():\n    assert True\n")
    assert changed_snapshot_files(before, snapshot_verification_files(repo_root=tmp_path)) == ["nested/tests/test_api.py"]


def test_run_pytest_then_verify_again_does_not_treat_cache_as_tampering(tmp_path):
    (tmp_path / "test_calc.py").write_text("def test_result():\n    assert 2 + 3 == 5\n")
    instance = engine(tmp_path)
    try:
        command = "python -m pytest -p no:hydra_pytest -q"
        ok, message = instance._run_verification_command(command)
        assert ok, message
        ok, message = instance._run_verification_command(command)
        assert ok, message
    finally:
        instance.guard.unlock_all()


def test_explicit_no_test_edit_wins_over_coder_title(tmp_path):
    task = SubagentTask("coder", "Create unit test", "coder", "grok", "Add calculation test", target_files=["test_calc.py"])
    instance = SubagentOrchestrator(SubagentPlan("Fix product; do not modify tests", [task], str(tmp_path)),
                                   workspace_root=tmp_path, probe_base_discrimination=False)
    assert instance._allowed_test_targets == set()


def test_grok_failed_acceptance_escalates_to_codex(tmp_path, monkeypatch):
    target = tmp_path / "calc.py"
    target.write_text("def broken(:\n")
    instance = engine(tmp_path)
    task = instance.plan.tasks[0]
    task.verification_command = ""
    task.acceptance_exam = [{"id": "syntax", "check_type": "syntax_ast", "target_files": ["calc.py"]}]
    instance.enforce_anti_slop = False
    backends = []

    def dispatch(t):
        backends.append(t.backend)
        t.patched_files = ["calc.py"]
        if len(backends) > 1:
            target.write_text("def add(a, b):\n    return a + b\n")
        return "direct edit", None

    monkeypatch.setattr(instance, "_dispatch_to_backend", dispatch)
    assert instance._execute_task_with_retry(task)
    assert backends == ["grok", "workbuddy"]


def test_codex_prompt_travels_over_stdin_without_cmd_interpretation(tmp_path, monkeypatch):
    instance = engine(tmp_path)
    task = instance.plan.tasks[0]
    prompt = 'Fix calc.py\nDo not execute echo %PATH% & exit.\nUse `a + b`.'
    task._stateless_prompt = prompt
    calls = []
    monkeypatch.setattr("visual_agent.subagent_orchestrator.shutil_which", lambda name: "codex.cmd")
    monkeypatch.setattr("visual_agent.subagent_orchestrator.prepare_subprocess_command", lambda args: args)

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "patched", "")

    monkeypatch.setattr("visual_agent.subagent_orchestrator.guarded_run", run)
    assert instance._run_codex_cli(task) == ("patched", None)
    args, kwargs = calls[0]
    assert args[-1] == "-"
    assert prompt not in args
    assert kwargs["input"] == prompt
    assert "stdin" not in kwargs
