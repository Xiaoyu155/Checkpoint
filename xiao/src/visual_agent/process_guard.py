"""Keep agent process trees from outliving the launcher that started them.

``terminate_process_tree`` in :mod:`.subprocess_window` only runs when the
launcher gets to finish cleanly. When the launcher dies hard -- Task Manager,
``Stop-Process -Force``, a crash -- nothing tears the tree down, and Windows
does not reparent or reap it. Codex and Claude both spawn
``python -m visual_agent.mcp_server`` as an MCP stdio child, so every hard-killed
session leaves those servers running until the machine reboots. They hold the
workspace open and block reinstalling ``pacer.exe``.

A Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` moves the teardown into
the kernel: when the last handle to the job closes -- including when the process
holding it is force-killed -- every process in the job dies with it. On POSIX the
equivalent guarantee comes from a session/process group, which
``subprocess_window`` already sets up.

The guard is best-effort by design: any failure degrades to an unguarded launch
rather than blocking the user from starting an agent.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from typing import Any

__all__ = ["ProcessGuard", "guarded_run", "orphan_reason"]


def _is_windows() -> bool:
    return os.name == "nt"


# JOBOBJECTINFOCLASS.JobObjectExtendedLimitInformation
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


if _is_windows():  # pragma: no cover - structure layout is Windows-only

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class ProcessGuard:
    """Own a kill-on-close job that adopted children cannot outlive.

    Use as a context manager around the whole lifetime of the child. Closing the
    guard closes the job handle, which is what triggers the kernel teardown, so
    the guard must stay open at least as long as the child should live.
    """

    def __init__(self) -> None:
        self._job: int | None = None
        self.available = False
        self.reason = "posix_session" if not _is_windows() else "not_started"

    def __enter__(self) -> "ProcessGuard":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start(self) -> bool:
        """Create the job object. Returns whether guarding is active."""
        if not _is_windows():
            # start_new_session / CREATE_NEW_PROCESS_GROUP already give POSIX the
            # grouping we need; there is nothing extra to allocate.
            self.available = False
            self.reason = "posix_session"
            return False
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                self.available = False
                self.reason = f"create_job_failed errno={ctypes.get_last_error()}"
                return False

            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                wintypes.HANDLE(job),
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                errno = ctypes.get_last_error()
                kernel32.CloseHandle(wintypes.HANDLE(job))
                self.available = False
                self.reason = f"set_job_limit_failed errno={errno}"
                return False

            self._job = int(job)
            self.available = True
            self.reason = "job_object"
            return True
        except (OSError, AttributeError, ValueError) as exc:
            self.available = False
            self.reason = f"job_unavailable {type(exc).__name__}"
            return False

    def adopt(self, pid: int) -> bool:
        """Put an already-running process (and its future children) in the job."""
        if self._job is None or int(pid) <= 0:
            return False
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid)
            )
            if not handle:
                return False
            try:
                return bool(
                    kernel32.AssignProcessToJobObject(
                        wintypes.HANDLE(self._job), wintypes.HANDLE(handle)
                    )
                )
            finally:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
        except (OSError, AttributeError, ValueError):
            return False

    def close(self) -> None:
        """Close the job handle, killing every process still inside it."""
        job, self._job = self._job, None
        if job is None:
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle(wintypes.HANDLE(job))
        except (OSError, AttributeError, ValueError):
            pass


def guarded_run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` whose child tree cannot outlive this process.

    Accepts and returns exactly what ``subprocess.run`` does. ``input`` is
    supported; ``stdin`` may not be combined with it, matching stdlib behaviour.
    """
    guard = ProcessGuard()
    guard.start()
    try:
        if not guard.available:
            return subprocess.run(command, **kwargs)

        # Popen is required so the child exists (suspended work is not needed --
        # AssignProcessToJobObject applies to descendants spawned after the call,
        # and the agent CLI spawns its MCP servers well after startup).
        run_input = kwargs.pop("input", None)
        if run_input is not None:
            kwargs["stdin"] = subprocess.PIPE
        check = bool(kwargs.pop("check", False))
        timeout = kwargs.pop("timeout", None)

        with subprocess.Popen(command, **kwargs) as process:
            guard.adopt(process.pid)
            try:
                stdout, stderr = process.communicate(run_input, timeout=timeout)
            except BaseException:
                process.kill()
                process.wait()
                raise
            completed = subprocess.CompletedProcess(
                process.args, int(process.returncode or 0), stdout, stderr
            )
        if check:
            completed.check_returncode()
        return completed
    finally:
        guard.close()


def orphan_reason(pid: int, parent_pid: int, *, alive: Any) -> str | None:
    """Classify a Pacer helper process as orphaned, or ``None`` if it is fine.

    ``alive`` is a callable taking a pid so callers can supply a cheap probe (and
    tests can supply a fake). A helper whose parent is gone has nobody left to
    shut it down, which is exactly the leak this module exists to stop.
    """
    if int(pid) <= 0:
        return None
    parent = int(parent_pid or 0)
    if parent <= 0:
        return "no_parent"
    if not alive(parent):
        return f"dead_parent={parent}"
    return None
