from __future__ import annotations

import os
import signal
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _is_windows() -> bool:
    return os.name == "nt"


def prepare_subprocess_command(argv: list[str]) -> list[str]:
    """Return an argv that Windows can launch without a shell lookup.

    npm exposes command-line tools as ``.cmd`` shims. ``CreateProcess`` does not
    resolve or execute those shims when Python receives a list argv, so route
    them through ``cmd.exe`` while keeping native executables unchanged.
    """
    command = [str(item) for item in argv]
    if not _is_windows() or not command:
        return command
    resolved = shutil.which(command[0])
    if not resolved or Path(resolved).suffix.lower() not in {".bat", ".cmd"}:
        return command
    command_line = subprocess.list2cmdline([resolved, *command[1:]])
    return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command_line]


def resolve_codex_executable() -> str | None:
    """Prefer Codex's bundled native executable over the npm ``.cmd`` shim on Windows."""
    resolved = shutil.which("codex") or shutil.which("codex.cmd") or shutil.which("codex.exe")
    if not resolved:
        return None
    path = Path(resolved)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        vendor = path.parent / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-x64" / "vendor"
        try:
            native = sorted(vendor.glob("*/bin/codex.exe"))
        except OSError:
            native = []
        if native:
            return str(native[0])
    return str(path)


def hidden_subprocess_kwargs(*, detached: bool = False) -> dict[str, Any]:
    """Return Popen kwargs that keep Windows background workers off-screen."""
    if not _is_windows():
        return {}

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if detached:
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)

    kwargs: dict[str, Any] = {"creationflags": creationflags}
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo
    return kwargs


def isolated_process_group_kwargs() -> dict[str, Any]:
    """Return launch kwargs for a child tree that can be terminated safely."""
    if not _is_windows():
        return {"start_new_session": True}
    kwargs = hidden_subprocess_kwargs()
    kwargs["creationflags"] = int(kwargs.get("creationflags") or 0) | getattr(
        subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0x00000200,
    )
    return kwargs


def terminate_process_tree(process: Any, *, wait_seconds: float = 5.0) -> bool:
    """Force-stop only the isolated process tree rooted at ``process.pid``."""
    try:
        process_exited = process.poll() is not None
        if process_exited and not _is_windows():
            return True
    except (AttributeError, OSError):
        process_exited = False

    pid = int(getattr(process, "pid", 0) or 0)
    tree_signal_sent = False
    if pid > 0 and _is_windows():
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                check=False,
                timeout=max(0.1, float(wait_seconds)),
                **hidden_subprocess_kwargs(),
            )
            tree_signal_sent = completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            tree_signal_sent = False
    elif pid > 0 and not process_exited:
        try:
            process_group = os.getpgid(pid)
            if process_group == pid:
                os.killpg(process_group, signal.SIGKILL)
                tree_signal_sent = True
        except (AttributeError, OSError, ProcessLookupError):
            tree_signal_sent = False

    if not tree_signal_sent:
        try:
            if process.poll() is None:
                process.kill()
        except (AttributeError, OSError):
            pass

    try:
        process.wait(timeout=max(0.1, float(wait_seconds)))
    except (AttributeError, OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=1.0)
        except (AttributeError, OSError, subprocess.SubprocessError):
            pass
    try:
        return process.poll() is not None
    except (AttributeError, OSError):
        return tree_signal_sent
