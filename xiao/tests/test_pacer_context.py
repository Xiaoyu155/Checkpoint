from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from visual_agent import pacer_context
from visual_agent.pacer_context import run_compact_command_batch


def test_compact_command_batch_keeps_full_logs_and_returns_bounded_tail(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    script = "print('old-line-' * 1000); print('FINAL-SIGNAL')"

    payload = run_compact_command_batch(
        workspace_root=workspace,
        repo_root=repo,
        steps=[{"name": "verbose", "argv": [sys.executable, "-c", script]}],
        tail_chars=300,
    )

    record = payload["records"][0]
    assert payload["status"] == "passed"
    assert payload["context_policy"]["full_output_local"] is True
    assert payload["context_policy"]["redaction_fail_closed"] is True
    assert len(record["stdout_tail"]) <= 300
    assert "FINAL-SIGNAL" in record["stdout_tail"]
    assert Path(record["stdout_log"]).stat().st_size > len(record["stdout_tail"])


def test_compact_command_batch_runs_acceptance_in_one_call(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    payload = run_compact_command_batch(
        workspace_root=repo / ".agent-workspace",
        repo_root=repo,
        steps=[
            {"name": "pass-one", "argv": [sys.executable, "-c", "print('one')"]},
            {"name": "pass-two", "argv": [sys.executable, "-c", "print('two')"]},
        ],
    )

    assert payload["executed_steps"] == 2
    assert payload["passed"] == 2
    assert payload["status"] == "passed"


def test_non_git_diff_check_is_not_applicable_without_failing_batch(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = run_compact_command_batch(
        workspace_root=repo / ".agent-workspace",
        repo_root=repo,
        steps=[
            {"name": "tests", "argv": [sys.executable, "-c", "print('passed')"]},
            {"name": "diff", "argv": ["git", "diff", "--check"]},
        ],
    )
    assert payload["status"] == "passed"
    assert payload["passed"] == 1
    assert payload["not_applicable"] == 1
    assert payload["records"][1]["status"] == "not_applicable"
    assert payload["records"][1]["reason"] == "not_a_git_worktree"


def test_non_git_batch_still_fails_when_real_check_fails(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    payload = run_compact_command_batch(
        workspace_root=repo / ".agent-workspace",
        repo_root=repo,
        steps=[
            {"name": "tests", "argv": [sys.executable, "-c", "raise SystemExit(2)"]},
            {"name": "diff", "argv": ["git", "diff", "--check"]},
        ],
    )
    assert payload["status"] == "failed"
    assert payload["failed"] == 1
    assert payload["not_applicable"] == 1


def test_compact_command_batch_rejects_cwd_outside_repo(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(ValueError, match="inside repo_root"):
        run_compact_command_batch(
            workspace_root=repo / ".agent-workspace",
            repo_root=repo,
            steps=[{"name": "escape", "argv": [sys.executable, "-V"], "cwd": str(tmp_path)}],
        )


def test_compact_command_batch_redacts_secrets_in_full_local_logs(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = "sk-test-secret-value-1234567890"

    payload = run_compact_command_batch(
        workspace_root=repo / ".agent-workspace",
        repo_root=repo,
        steps=[{"name": "secret", "argv": [sys.executable, "-c", f"print('OPENAI_API_KEY={secret}')"]}],
    )

    stdout_path = Path(payload["records"][0]["stdout_log"])
    saved = stdout_path.read_text(encoding="utf-8")
    assert secret not in saved
    assert "[REDACTED]" in saved


def test_log_redaction_replace_failure_removes_raw_output(tmp_path, monkeypatch) -> None:
    path = tmp_path / "output.log"
    secret = "sk-test-redaction-failure-1234567890"
    path.write_text(f"OPENAI_API_KEY={secret}\n", encoding="utf-8")
    real_replace = Path.replace

    def fail_redacting_replace(source, target):
        if source.suffix == ".redacting":
            raise OSError("replace denied")
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_redacting_replace)

    assert pacer_context._redact_log_file(path) is False
    assert secret not in path.read_text(encoding="utf-8")
    assert "OUTPUT REMOVED" in path.read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.redacting")) == []


def test_redaction_failure_returns_no_raw_tail_or_log_path(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = "sk-test-no-tail-after-redaction-failure-1234567890"
    monkeypatch.setattr(pacer_context, "_redact_log_file", lambda _path: False)
    monkeypatch.setattr(
        pacer_context,
        "_log_tail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("raw tail must not be read")),
    )

    payload = run_compact_command_batch(
        workspace_root=repo / ".agent-workspace",
        repo_root=repo,
        steps=[{"name": "redaction-failure", "argv": [sys.executable, "-c", f"print('{secret}')"]}],
        tail_chars=300,
    )
    record = payload["records"][0]

    assert payload["status"] == "failed"
    assert payload["context_policy"]["full_output_local"] is False
    assert payload["context_policy"]["redaction_fail_closed"] is True
    assert record["status"] == "failed"
    assert record["reason"] == "log_redaction_failed"
    assert record["logs_redacted"] is False
    assert record["stdout_tail"] == ""
    assert record["stderr_tail"] == ""
    assert record["stdout_log"] == ""
    assert record["stderr_log"] == ""
    assert secret not in json.dumps(record)


def test_compact_step_interrupt_terminates_fake_process_and_redacts_logs(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    run_dir = tmp_path / "commands" / "run"
    repo.mkdir()
    run_dir.mkdir(parents=True)
    secret = "sk-test-interrupt-secret-1234567890"
    events: list[str] = []

    class FakeProcess:
        def __init__(self, *args, stdout, stderr, **kwargs) -> None:
            self.running = True
            self.stdout = stdout
            self.stderr = stderr
            stdout.write(f"OPENAI_API_KEY={secret}\n")
            stderr.write(f"Authorization: Bearer {secret}\n")
            stdout.flush()
            stderr.flush()

        def wait(self, timeout=None):
            raise KeyboardInterrupt

        def poll(self):
            return None if self.running else -9

    fake_process: FakeProcess | None = None

    def fake_popen(*args, **kwargs):
        nonlocal fake_process
        fake_process = FakeProcess(*args, **kwargs)
        return fake_process

    def fake_terminate(process) -> bool:
        events.append("terminated")
        process.running = False
        return True

    monkeypatch.setattr(pacer_context.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(pacer_context, "terminate_process_tree", fake_terminate)

    with pytest.raises(KeyboardInterrupt):
        pacer_context._run_compact_step(
            {"name": "interrupt", "argv": ["fake-command"]},
            index=1,
            repo_root=repo,
            run_dir=run_dir,
            tail_chars=300,
        )

    assert fake_process is not None
    assert events == ["terminated"]
    assert fake_process.running is False
    assert fake_process.stdout.closed is True
    assert fake_process.stderr.closed is True
    for path in (run_dir / "01-interrupt.stdout.log", run_dir / "01-interrupt.stderr.log"):
        saved = path.read_text(encoding="utf-8")
        assert secret not in saved
        assert "[REDACTED]" in saved
    assert list(run_dir.glob("*.redacting")) == []
