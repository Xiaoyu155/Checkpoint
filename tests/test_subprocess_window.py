from __future__ import annotations

import ast
from pathlib import Path
import subprocess

import pytest

from visual_agent import subprocess_window
from visual_agent import context_profile, delivery_check, subagent_orchestrator


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

#: The modules whose background call sites must not flash a console window.
HIDDEN_CALL_SITE_FILES = (
    "src/visual_agent/subagent_orchestrator.py",
    "src/visual_agent/delivery_check.py",
    "src/visual_agent/context_profile.py",
)


def _force_windows(monkeypatch) -> None:
    """Make ``hidden_subprocess_kwargs()`` behave exactly as it does on Windows.

    The real function is exercised (not stubbed) so a change that stops
    returning console-suppressing flags fails here rather than in the field.
    """
    monkeypatch.setattr(subprocess_window, "_is_windows", lambda: True)


def _record_run(monkeypatch, responder):
    calls: list[tuple[object, dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return responder(argv, kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _assert_hidden(kwargs: dict) -> None:
    flags = int(kwargs.get("creationflags") or 0)
    assert flags & CREATE_NO_WINDOW, f"CREATE_NO_WINDOW missing from subprocess kwargs: {kwargs!r}"


def test_hidden_subprocess_kwargs_is_opt_in_off_windows(monkeypatch) -> None:
    """Never suppress a terminal on POSIX — the flag is Windows-only by design."""
    monkeypatch.setattr(subprocess_window, "_is_windows", lambda: False)

    assert subprocess_window.hidden_subprocess_kwargs() == {}


def test_context_profile_git_probes_hide_windows(monkeypatch, tmp_path) -> None:
    _force_windows(monkeypatch)

    def responder(argv, _kwargs):
        if "--abbrev-ref" in argv:
            return subprocess.CompletedProcess(argv, 0, "main\n", "")
        if argv[-1] == "HEAD":
            return subprocess.CompletedProcess(argv, 0, "abc1234\n", "")
        if "--show-prefix" in argv:
            return subprocess.CompletedProcess(argv, 0, "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    calls = _record_run(monkeypatch, responder)
    manager = context_profile.ContextProfileManager(tmp_path)

    branch, head, _dirty, _modified, _untracked = manager.get_git_state()

    assert len(calls) == 4, "expected branch/head/status/prefix probes"
    for argv, kwargs in calls:
        assert argv[0] == "git"
        _assert_hidden(kwargs)
    assert branch == "main"
    assert head == "abc1234"


def test_delivery_check_git_helper_hides_windows(monkeypatch, tmp_path) -> None:
    _force_windows(monkeypatch)
    calls = _record_run(
        monkeypatch, lambda argv, _kw: subprocess.CompletedProcess(argv, 0, "ok", "")
    )

    completed = delivery_check._git(tmp_path, "rev-parse", "HEAD")

    assert completed.returncode == 0
    assert calls[0][0][:2] == ["git", "-C"]
    _assert_hidden(calls[0][1])


def test_delivery_check_shell_command_hides_windows(monkeypatch, tmp_path) -> None:
    _force_windows(monkeypatch)
    calls = _record_run(
        monkeypatch, lambda argv, _kw: subprocess.CompletedProcess(argv, 0, "done", "")
    )

    result = delivery_check._run(
        "pytest -q", cwd=tmp_path, env=None, timeout_seconds=5.0, runner=None
    )

    assert result["exit_code"] == 0
    assert calls[0][1]["shell"] is True
    _assert_hidden(calls[0][1])


def test_delivery_check_junction_creation_hides_windows(monkeypatch, tmp_path) -> None:
    _force_windows(monkeypatch)

    def _symlink_unavailable(*_args, **_kwargs):
        raise OSError("symlink privilege not held")

    monkeypatch.setattr(delivery_check.os, "symlink", _symlink_unavailable)
    monkeypatch.setattr(delivery_check.sys, "platform", "win32")
    calls = _record_run(
        monkeypatch, lambda argv, _kw: subprocess.CompletedProcess(argv, 0, "", "")
    )

    assert delivery_check._link_dir(tmp_path / "node_modules", tmp_path / "checkout") is True

    assert calls[0][0][:2] == ["cmd", "/c"]
    _assert_hidden(calls[0][1])


def test_acceptance_execution_check_hides_windows(monkeypatch, tmp_path) -> None:
    _force_windows(monkeypatch)
    calls = _record_run(
        monkeypatch, lambda argv, _kw: subprocess.CompletedProcess(argv, 0, "ok", "")
    )
    task = subagent_orchestrator.SubagentTask(
        id="exam-1",
        title="exam",
        role="coder",
        backend="local",
        instructions="run the assertion",
        acceptance_exam=[
            {
                "id": "e1",
                "question": "does it run?",
                "check_type": "execution",
                "assertion_cmd": "pytest -q",
                "expected_exit_code": 0,
            }
        ],
    )

    passed, _results, _critique = subagent_orchestrator.verify_acceptance_exam(task, tmp_path)

    assert passed is True
    assert calls[0][0] == "pytest -q"
    assert calls[0][1]["shell"] is True
    _assert_hidden(calls[0][1])


def test_codex_exec_launch_hides_windows(monkeypatch, tmp_path) -> None:
    """``codex exec`` is batch mode: it must never own a visible console."""
    _force_windows(monkeypatch)
    monkeypatch.setattr(subagent_orchestrator, "shutil_which", lambda _name: r"C:\Tools\codex.exe")
    recorded: dict[str, object] = {}

    def fake_guarded_run(command, **kwargs):
        recorded["command"] = command
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "done", "")

    monkeypatch.setattr(subagent_orchestrator, "guarded_run", fake_guarded_run)

    class _StubOrchestrator:
        workspace_root = tmp_path

        def _codex_timeout(self) -> float:
            return 30.0

    task = subagent_orchestrator.SubagentTask(
        id="codex-1",
        title="codex",
        role="coder",
        backend="codex",
        instructions="do it",
    )

    stdout, error = subagent_orchestrator.SubagentOrchestrator._run_codex_cli(
        _StubOrchestrator(), task
    )

    assert error is None
    assert stdout == "done"
    assert recorded["command"][1] == "exec"
    _assert_hidden(recorded["kwargs"])


@pytest.mark.parametrize("rel_path", HIDDEN_CALL_SITE_FILES)
def test_background_call_sites_never_hide_a_user_facing_console(rel_path: str) -> None:
    """Every hidden call must also capture output.

    A call that does not capture output is by definition attached to a console a
    human is watching, so hiding it would blank out the interactive Claude,
    Gemini and ``pacer task`` windows we are required to preserve.
    """
    source = Path(rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source)

    call_sites: list[tuple[int, bool, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ):
            continue
        named = {kw.arg for kw in node.keywords if kw.arg is not None}
        forwards_hidden = any(kw.arg is None for kw in node.keywords)
        call_sites.append((node.lineno, "capture_output" in named, forwards_hidden))

    assert call_sites, f"{rel_path} no longer contains a subprocess.run call site"
    for lineno, captures, forwards_hidden in call_sites:
        assert forwards_hidden, (
            f"{rel_path}:{lineno} subprocess.run does not forward hidden_subprocess_kwargs()"
        )
        assert captures, (
            f"{rel_path}:{lineno} subprocess.run does not capture output; "
            "hiding it would suppress a user-facing console"
        )


def test_prepare_subprocess_command_wraps_windows_cmd_shims(monkeypatch) -> None:
    monkeypatch.setattr(subprocess_window, "_is_windows", lambda: True)
    monkeypatch.setattr(subprocess_window.shutil, "which", lambda _name: r"C:\Tools\agent.cmd")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    command = subprocess_window.prepare_subprocess_command(["agent", "--json", "-"])

    assert command[:4] == [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c"]
    assert r"C:\Tools\agent.cmd --json -" == command[4]


def test_prepare_subprocess_command_keeps_native_executable(monkeypatch) -> None:
    monkeypatch.setattr(subprocess_window, "_is_windows", lambda: True)
    monkeypatch.setattr(subprocess_window.shutil, "which", lambda _name: r"C:\Tools\agent.exe")

    assert subprocess_window.prepare_subprocess_command(["agent", "--version"]) == ["agent", "--version"]


def test_isolated_process_group_kwargs_adds_windows_process_group(monkeypatch) -> None:
    monkeypatch.setattr(subprocess_window, "_is_windows", lambda: True)
    monkeypatch.setattr(subprocess_window, "hidden_subprocess_kwargs", lambda: {"creationflags": 8})
    monkeypatch.setattr(subprocess_window.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)

    kwargs = subprocess_window.isolated_process_group_kwargs()

    assert kwargs["creationflags"] == 520


def test_terminate_process_tree_uses_rooted_windows_taskkill(monkeypatch) -> None:
    monkeypatch.setattr(subprocess_window, "_is_windows", lambda: True)
    calls = []

    class FakeProcess:
        pid = 4321
        stopped = False
        killed = False

        def poll(self):
            return 1 if self.stopped else None

        def wait(self, timeout=None):
            self.stopped = True
            return 1

        def kill(self):
            self.killed = True
            self.stopped = True

    process = FakeProcess()

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess_window.subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess_window, "hidden_subprocess_kwargs", lambda: {"creationflags": 8})

    assert subprocess_window.terminate_process_tree(process) is True
    assert calls[0][0] == ["taskkill", "/F", "/T", "/PID", "4321"]
    assert calls[0][1]["creationflags"] == 8
    assert process.killed is False


def test_terminate_process_tree_checks_windows_descendants_after_root_exit(monkeypatch) -> None:
    monkeypatch.setattr(subprocess_window, "_is_windows", lambda: True)
    calls = []

    class ExitedProcess:
        pid = 5678

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess_window.subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess_window, "hidden_subprocess_kwargs", lambda: {})

    assert subprocess_window.terminate_process_tree(ExitedProcess()) is True
    assert calls[0][0] == ["taskkill", "/F", "/T", "/PID", "5678"]


def test_terminate_process_tree_only_kills_owned_posix_group(monkeypatch) -> None:
    monkeypatch.setattr(subprocess_window, "_is_windows", lambda: False)
    monkeypatch.setattr(subprocess_window.signal, "SIGKILL", 9, raising=False)
    killed_groups = []

    class FakeProcess:
        pid = 1234
        stopped = False
        killed = False

        def poll(self):
            return -9 if self.stopped else None

        def wait(self, timeout=None):
            self.stopped = True
            return -9

        def kill(self):
            self.killed = True
            self.stopped = True

    owned = FakeProcess()
    monkeypatch.setattr(subprocess_window.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        subprocess_window.os,
        "killpg",
        lambda pgid, sig: killed_groups.append((pgid, sig)),
        raising=False,
    )

    assert subprocess_window.terminate_process_tree(owned) is True
    assert killed_groups == [(1234, 9)]
    assert owned.killed is False

    unrelated = FakeProcess()
    monkeypatch.setattr(subprocess_window.os, "getpgid", lambda _pid: 9999)
    assert subprocess_window.terminate_process_tree(unrelated) is True
    assert killed_groups == [(1234, 9)]
    assert unrelated.killed is True
