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


def test_quoted_script_and_exit_code(tmp_path):
    from visual_agent.command_verification import verification_command_argv
    script = tmp_path / "script with spaces.py"
    script.write_text("raise SystemExit(7)\n", encoding="utf-8")
    argv = verification_command_argv(f'"{sys.executable}" "{script}"', tmp_path)
    result = run_command_verification(command=argv, repo_root=tmp_path)
    assert result["exit_code"] == 7
    assert result["verdict"] == "fail"
