"""Background execution helpers for DevPacer missions."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .chief_run import run_chief_mission
from .missions import append_round, load_mission, mission_dir, save_mission
from .models import to_jsonable


def start_background_chief_run(
    *,
    workspace_root: str | Path,
    mission_id: str,
    agents: tuple[str, ...] = (),
    run_profile: str = "dry-run",
    include_slow: bool = False,
    max_workflows: int = 10,
    timeout_seconds: float = 1800.0,
    allow_dirty: bool = False,
    allow_coverage_gap: bool = False,
    test_command: str | None = None,
    allow_test_edits: bool = False,
    merge: bool = False,
) -> dict[str, Any]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    mission = load_mission(workspace_path, mission_id)
    if mission is None:
        return {
            "schema_version": 1,
            "status": "blocked",
            "stop_reason": "missing_mission",
            "message": f"No saved mission found: {mission_id}",
        }

    directory = mission_dir(workspace_path, mission_id)
    logs_dir = directory / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stdout_path = logs_dir / f"chief-run-background-{stamp}.out.log"
    stderr_path = logs_dir / f"chief-run-background-{stamp}.err.log"
    argv = [
        sys.executable,
        "-m",
        "visual_agent.cli",
        "chief-background-worker",
        "--mission",
        mission_id,
        "--workspace-root",
        str(workspace_path),
        "--run-profile",
        run_profile,
        "--max-workflows",
        str(int(max_workflows)),
        "--timeout-seconds",
        str(float(timeout_seconds)),
        "--format",
        "markdown",
    ]
    if include_slow:
        argv.append("--include-slow")
    for agent in agents:
        clean_agent = str(agent or "").strip()
        if clean_agent:
            argv.extend(["--agent", clean_agent])
    if allow_dirty:
        argv.append("--allow-dirty")
    if allow_coverage_gap:
        argv.append("--allow-coverage-gap")
    if test_command:
        argv.extend(["--test-command", str(test_command)])
    if allow_test_edits:
        argv.append("--allow-test-edits")
    if merge:
        argv.append("--merge")

    env = os.environ.copy()
    src_dir = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = _prepend_path(env.get("PYTHONPATH", ""), src_dir)
    cwd = Path(str(mission.get("repo_root") or Path.cwd())).expanduser().resolve()

    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    try:
        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        else:
            start_new_session = True
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()

    record = {
        "schema_version": 1,
        "status": "running",
        "pid": int(process.pid),
        "argv": argv,
        "cwd": str(cwd),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "agents": [str(agent) for agent in agents if str(agent or "").strip()],
        "test_command": str(test_command or ""),
        "allow_test_edits": bool(allow_test_edits),
        "merge": bool(merge),
    }
    (directory / "background.json").write_text(json.dumps(to_jsonable(record), ensure_ascii=False, indent=2), encoding="utf-8")
    append_round(
        workspace_path,
        mission_id,
        {
            "round": _next_round_number(workspace_path, mission_id),
            "type": "background",
            "status": "started",
            "pid": int(process.pid),
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
        },
    )
    mission["status"] = "background_running"
    mission["stop_reason"] = ""
    save_mission(workspace_path, mission)
    return {
        "schema_version": 1,
        "product": "Pacer",
        "verification_engine": "Checkpoint",
        "status": "background_started",
        "stop_reason": "",
        "message": "Mission is running in the background. Use chief-status to inspect it.",
        "mission": mission,
        "background": record,
    }


def run_background_worker(
    *,
    workspace_root: str | Path,
    mission_id: str,
    agents: tuple[str, ...] = (),
    run_profile: str = "dry-run",
    include_slow: bool = False,
    max_workflows: int = 10,
    timeout_seconds: float = 1800.0,
    allow_dirty: bool = False,
    allow_coverage_gap: bool = False,
    test_command: str | None = None,
    allow_test_edits: bool = False,
    merge: bool = False,
) -> dict[str, Any]:
    """Run a mission in a child process and write a completion receipt."""
    workspace_path = Path(workspace_root).expanduser().resolve()
    background = load_background_record(workspace_path, mission_id) or {
        "schema_version": 1,
        "status": "running",
        "pid": os.getpid(),
    }
    background["worker_pid"] = os.getpid()
    background["worker_started_at"] = datetime.now(timezone.utc).isoformat()
    save_background_record(workspace_path, mission_id, background)

    try:
        payload = run_chief_mission(
            workspace_root=workspace_path,
            resume_mission_id=mission_id,
            execute=True,
            dry_run=False,
            run_profile=run_profile,
            include_slow=include_slow,
            max_workflows=max_workflows,
            timeout_seconds=timeout_seconds,
            agents=tuple(agents or tuple(background.get("agents") or ())),
            allow_dirty=allow_dirty,
            allow_coverage_gap=allow_coverage_gap,
            test_command=test_command or str(background.get("test_command") or "") or None,
            allow_test_edits=allow_test_edits or bool(background.get("allow_test_edits")),
            merge=merge or bool(background.get("merge")),
        )
        exit_code = 0 if str(payload.get("status") or "") == "verified" else 1
        background.update(
            {
                "status": "completed",
                "exit_code": exit_code,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "result_status": str(payload.get("status") or ""),
                "result_stop_reason": str(payload.get("stop_reason") or ""),
                "final_report_path": str(payload.get("final_report_path") or ""),
            }
        )
        save_background_record(workspace_path, mission_id, background)
        return {**payload, "background": background}
    except Exception as exc:
        background.update(
            {
                "status": "failed",
                "exit_code": 1,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        save_background_record(workspace_path, mission_id, background)
        mission = load_mission(workspace_path, mission_id)
        if mission is not None:
            mission["status"] = "stopped"
            mission["stop_reason"] = "worker_error"
            save_mission(workspace_path, mission)
        raise


def inspect_background_state(
    *,
    workspace_root: str | Path,
    mission_id: str,
    update: bool = True,
    process_probe: Any = None,
    terminator: Any = None,
) -> dict[str, Any]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    mission = load_mission(workspace_path, mission_id)
    background = load_background_record(workspace_path, mission_id)
    if background is None:
        return {"status": "none", "message": "No background record for this mission."}
    if background.get("status") in {"completed", "failed", "timeout", "orphaned"}:
        return {
            **background,
            "process_state": str(background.get("status")),
            "alive": False,
            "budget_exceeded": False,
        }

    pid = int(background.get("worker_pid") or background.get("pid") or 0)
    probe = process_probe or process_status
    process = probe(pid)
    budget_exceeded = bool(mission and mission_wall_budget_exceeded(mission, background))
    if process.get("alive") and budget_exceeded:
        term = terminator or terminate_process
        terminated = term(pid)
        background.update(
            {
                "status": "timeout",
                "process_state": "timeout",
                "alive": False,
                "budget_exceeded": True,
                "terminated": bool(terminated),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "exit_code": 124,
            }
        )
        if update:
            save_background_record(workspace_path, mission_id, background)
            _mark_mission_from_background(
                workspace_path,
                mission_id,
                status="stopped",
                stop_reason="budget_exhausted",
                round_status="budget_exhausted",
                background=background,
            )
        return background
    if process.get("alive"):
        background.update(
            {
                "process_state": "running",
                "alive": True,
                "budget_exceeded": False,
                "exit_code": process.get("exit_code"),
            }
        )
        return background

    background.update(
        {
            "status": "orphaned",
            "process_state": "exited_unknown",
            "alive": False,
            "budget_exceeded": budget_exceeded,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "exit_code": process.get("exit_code"),
        }
    )
    if update:
        save_background_record(workspace_path, mission_id, background)
        _mark_mission_from_background(
            workspace_path,
            mission_id,
            status="stopped",
            stop_reason="worker_error",
            round_status="exited_unknown",
            background=background,
        )
    return background


def load_background_record(workspace_root: str | Path, mission_id: str) -> dict[str, Any] | None:
    path = mission_dir(workspace_root, mission_id) / "background.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def save_background_record(workspace_root: str | Path, mission_id: str, record: dict[str, Any]) -> dict[str, Any]:
    directory = mission_dir(workspace_root, mission_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "background.json"
    path.write_text(json.dumps(to_jsonable(record), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(path), "record": dict(record)}


def mission_wall_budget_exceeded(mission: dict[str, Any], background: dict[str, Any]) -> bool:
    budget = mission.get("budget_policy") if isinstance(mission.get("budget_policy"), dict) else {}
    max_minutes = float(budget.get("max_wall_minutes") or 0)
    if max_minutes <= 0:
        return False
    started = parse_iso_datetime(str(background.get("started_at") or mission.get("created_at") or ""))
    if started is None:
        return False
    elapsed = datetime.now(timezone.utc) - started
    return elapsed.total_seconds() > max_minutes * 60.0


def parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def process_status(pid: int) -> dict[str, Any]:
    if pid <= 0:
        return {"pid": pid, "alive": False, "exit_code": None}
    if os.name == "nt":
        return _windows_process_status(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {"pid": pid, "alive": False, "exit_code": None}
    except PermissionError:
        return {"pid": pid, "alive": True, "exit_code": None}
    return {"pid": pid, "alive": True, "exit_code": None}


def terminate_process(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _windows_process_status(pid: int) -> dict[str, Any]:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return {"pid": pid, "alive": False, "exit_code": None}

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return {"pid": pid, "alive": False, "exit_code": None}
    exit_code = wintypes.DWORD()
    try:
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        if not ok:
            return {"pid": pid, "alive": True, "exit_code": None}
        code = int(exit_code.value)
        return {"pid": pid, "alive": code == still_active, "exit_code": None if code == still_active else code}
    finally:
        kernel32.CloseHandle(handle)


def _mark_mission_from_background(
    workspace_root: Path,
    mission_id: str,
    *,
    status: str,
    stop_reason: str,
    round_status: str,
    background: dict[str, Any],
) -> None:
    mission = load_mission(workspace_root, mission_id)
    if mission is not None:
        mission["status"] = status
        mission["stop_reason"] = stop_reason
        save_mission(workspace_root, mission)
    append_round(
        workspace_root,
        mission_id,
        {
            "round": _next_round_number(workspace_root, mission_id),
            "type": "background_health",
            "status": round_status,
            "stop_reason": stop_reason,
            "pid": background.get("pid"),
            "worker_pid": background.get("worker_pid"),
            "exit_code": background.get("exit_code"),
        },
    )


def _prepend_path(existing: str, path: Path) -> str:
    value = str(path)
    if not existing:
        return value
    parts = existing.split(os.pathsep)
    if value in parts:
        return existing
    return value + os.pathsep + existing


def _next_round_number(workspace_root: Path, mission_id: str) -> int:
    from .missions import load_rounds

    rounds = load_rounds(workspace_root, mission_id)
    if not rounds:
        return 0
    return max((int(item.get("round") or 0) for item in rounds), default=-1) + 1
