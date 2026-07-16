from __future__ import annotations

import subprocess

from visual_agent import subprocess_window


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
    assert calls[0][0] == ["taskkill", "/PID", "4321", "/T", "/F"]
    assert calls[0][1]["creationflags"] == 8
    assert process.killed is False


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
