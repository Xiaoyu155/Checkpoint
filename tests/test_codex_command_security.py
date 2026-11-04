import sys
from subprocess import TimeoutExpired

from visual_agent.command_verification import run_command_verification


def test_rejects_strings_and_shell_operators(tmp_path):
    assert run_command_verification(command="pytest -q", repo_root=tmp_path)["verdict"] == "fail"
    assert run_command_verification(command=["sh", "-c", "echo ok"], repo_root=tmp_path)["verdict"] == "fail"


def test_timeout_is_failure(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["shell"] is False
        raise TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr("visual_agent.command_verification.guarded_run", timeout)
    result = run_command_verification(command=[sys.executable, "-m", "pytest"], repo_root=tmp_path, timeout_seconds=1)
    assert result["failure_kind"] == "command_timeout"
