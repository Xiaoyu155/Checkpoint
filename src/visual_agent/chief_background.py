"""Background execution helpers for DevPacer missions."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .chief_run import run_chief_mission
from .locks import RunLock
from .missions import append_round, load_mission, load_rounds, mission_dir, save_mission
from .models import to_jsonable
from .mission_progress import save_mission_progress
from .subprocess_window import hidden_subprocess_kwargs


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
    launch_lock = RunLock(directory, name="background-launch.lock", ttl_seconds=30.0)
    try:
        launch_lock.acquire(owner=f"background-launch:{mission_id}")
    except RuntimeError:
        return _background_already_running_payload(mission, load_background_record(workspace_path, mission_id))
    try:
        existing = load_background_record(workspace_path, mission_id)
        if _background_record_is_alive(existing):
            return _background_already_running_payload(mission, existing)
        return _start_background_chief_run_locked(
            workspace_root=workspace_path,
            mission_id=mission_id,
            agents=agents,
            run_profile=run_profile,
            include_slow=include_slow,
            max_workflows=max_workflows,
            timeout_seconds=timeout_seconds,
            allow_dirty=allow_dirty,
            allow_coverage_gap=allow_coverage_gap,
            test_command=test_command,
            allow_test_edits=allow_test_edits,
            merge=merge,
        )
    finally:
        launch_lock.release()


def _start_background_chief_run_locked(
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
    launch_time = datetime.now(timezone.utc)
    stamp = launch_time.strftime("%Y%m%d-%H%M%S-%f")
    launch_id = uuid4().hex
    stdout_path = logs_dir / f"chief-run-background-{stamp}.out.log"
    stderr_path = logs_dir / f"chief-run-background-{stamp}.err.log"
    effective_test_command = str(test_command or mission.get("test_command") or "").strip()
    effective_allow_test_edits = bool(allow_test_edits or mission.get("allow_test_edits"))
    effective_merge = bool(merge or mission.get("merge"))
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
    if effective_test_command:
        argv.extend(["--test-command", effective_test_command])
    if effective_allow_test_edits:
        argv.append("--allow-test-edits")
    if effective_merge:
        argv.append("--merge")

    env = os.environ.copy()
    src_dir = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = _prepend_path(env.get("PYTHONPATH", ""), src_dir)
    cwd = Path(str(mission.get("repo_root") or Path.cwd())).expanduser().resolve()

    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    try:
        start_new_session = False
        launch_kwargs = hidden_subprocess_kwargs(detached=True)
        if os.name == "nt":
            start_new_session = False
        else:
            start_new_session = True
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            stdin=subprocess.DEVNULL,
            start_new_session=start_new_session,
            **launch_kwargs,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()

    record = {
        "schema_version": 1,
        "status": "running",
        "launch_id": launch_id,
        "pid": int(process.pid),
        "argv": argv,
        "cwd": str(cwd),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "started_at": launch_time.isoformat(),
        "budget_started_at": str(mission.get("budget_started_at") or launch_time.isoformat()),
        "agents": [str(agent) for agent in agents if str(agent or "").strip()],
        "test_command": effective_test_command,
        "allow_test_edits": effective_allow_test_edits,
        "merge": effective_merge,
    }
    save_background_record(workspace_path, mission_id, record)
    save_mission_progress(
        workspace_path,
        mission_id,
        stage="background_started",
        stage_label="Background started",
        status="background_running",
        plan_id=str(mission.get("plan_id") or mission_id),
        started_at=record["started_at"],
        last_activity_at=record["started_at"],
        agent=",".join(record["agents"]),
    )
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
    mission.setdefault("budget_started_at", record["budget_started_at"])
    if effective_test_command:
        mission["test_command"] = effective_test_command
    mission["allow_test_edits"] = effective_allow_test_edits
    mission["merge"] = effective_merge
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
    watchdog_interval_seconds: float = 60.0,
    watchdog_terminator: Any = None,
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
    save_mission_progress(
        workspace_path,
        mission_id,
        stage="worker_started",
        stage_label="Worker started",
        status="running",
        plan_id=mission_id,
        last_activity_at=background["worker_started_at"],
        agent=",".join(str(agent) for agent in (agents or tuple(background.get("agents") or ()))),
    )

    watchdog_stop = threading.Event()
    watchdog_thread = threading.Thread(
        target=_background_watchdog_loop,
        name=f"pacer-background-watchdog-{mission_id}",
        args=(workspace_path, mission_id, watchdog_stop, watchdog_interval_seconds, watchdog_terminator),
        daemon=True,
    )
    watchdog_thread.start()
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
        latest_background = load_background_record(workspace_path, mission_id) or background
        if str(latest_background.get("status") or "") == "timeout":
            return {
                **payload,
                "status": "timeout",
                "stop_reason": "budget_exhausted",
                "background": latest_background,
            }
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
        save_mission_progress(
            workspace_path,
            mission_id,
            stage="verified" if exit_code == 0 else "blocked",
            stage_label="Verified" if exit_code == 0 else "Stopped",
            status=str(payload.get("status") or ""),
            blocker=str(payload.get("stop_reason") or "") if exit_code != 0 else "",
            last_activity_at=background["completed_at"],
        )
        return {**payload, "background": background}
    except Exception as exc:
        latest_background = load_background_record(workspace_path, mission_id) or background
        if str(latest_background.get("status") or "") == "timeout":
            return {
                "schema_version": 1,
                "status": "timeout",
                "stop_reason": "budget_exhausted",
                "background": latest_background,
            }
        background.update(
            {
                "status": "failed",
                "exit_code": 1,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        save_background_record(workspace_path, mission_id, background)
        save_mission_progress(
            workspace_path,
            mission_id,
            stage="blocked",
            stage_label="Worker failed",
            status="failed",
            blocker=f"{type(exc).__name__}: {exc}",
            last_activity_at=background["completed_at"],
        )
        mission = load_mission(workspace_path, mission_id)
        if mission is not None:
            mission["status"] = "stopped"
            mission["stop_reason"] = "worker_error"
            save_mission(workspace_path, mission)
        raise
    finally:
        watchdog_stop.set()
        watchdog_thread.join(timeout=0.2)


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
        return _mark_background_timeout_state(
            workspace_path,
            mission_id,
            background,
            terminated=bool(terminated),
            update=update,
        )
    if process.get("alive"):
        background.update(
            {
                "process_state": "running",
                "alive": True,
                "budget_exceeded": False,
                "exit_code": process.get("exit_code"),
            }
        )
        save_mission_progress(
            workspace_path,
            mission_id,
            stage="worker_running",
            stage_label="Worker running",
            status="running",
            heartbeat_at=datetime.now(timezone.utc).isoformat(),
        )
        return background

    if _mission_has_terminal_progress(workspace_path, mission_id, mission):
        mission_status = str((mission or {}).get("status") or "verified")
        stop_reason = str((mission or {}).get("stop_reason") or "")
        exit_code = 0 if mission_status in {"verified", "merged"} or stop_reason == "verified" else process.get("exit_code")
        background.update(
            {
                "status": "completed",
                "process_state": "reconciled_from_mission",
                "alive": False,
                "budget_exceeded": budget_exceeded,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "exit_code": exit_code,
                "result_status": mission_status,
                "result_stop_reason": stop_reason,
            }
        )
        if update:
            save_background_record(workspace_path, mission_id, background)
            save_mission_progress(
                workspace_path,
                mission_id,
                stage="verified" if exit_code == 0 else "blocked",
                stage_label="Mission already completed",
                status=mission_status,
                blocker="" if exit_code == 0 else stop_reason,
                last_activity_at=background["completed_at"],
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
        save_mission_progress(
            workspace_path,
            mission_id,
            stage="blocked",
            stage_label="Worker exited unexpectedly",
            status="orphaned",
            blocker="worker_error",
            last_activity_at=background["completed_at"],
        )
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
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(to_jsonable(record), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {"path": str(path), "record": dict(record)}


def _background_record_is_alive(record: dict[str, Any] | None) -> bool:
    if not isinstance(record, dict) or str(record.get("status") or "") not in {"starting", "running"}:
        return False
    pid = int(record.get("worker_pid") or record.get("pid") or 0)
    return bool(process_status(pid).get("alive"))


def _background_already_running_payload(
    mission: dict[str, Any],
    background: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "product": "Pacer",
        "verification_engine": "Checkpoint",
        "status": "blocked",
        "stop_reason": "background_already_running",
        "message": "This mission already has a background launch or live worker.",
        "mission": mission,
        "background": background or {"status": "starting"},
    }


def _background_watchdog_loop(
    workspace_root: Path,
    mission_id: str,
    stop_event: threading.Event,
    interval_seconds: float,
    terminator: Any,
) -> None:
    try:
        interval = max(0.01, float(interval_seconds))
    except (TypeError, ValueError):
        interval = 60.0
    while not stop_event.wait(interval):
        heartbeat_at = datetime.now(timezone.utc).isoformat()
        background = load_background_record(workspace_root, mission_id) or {}
        if str(background.get("status") or "") in {"completed", "failed", "timeout", "orphaned"}:
            return
        save_mission_progress(
            workspace_root,
            mission_id,
            heartbeat_at=heartbeat_at,
            last_activity_at=heartbeat_at,
        )
        mission = load_mission(workspace_root, mission_id)
        if mission is None or not mission_wall_budget_exceeded(mission, background):
            continue
        background = _mark_background_timeout_state(
            workspace_root,
            mission_id,
            background,
            terminated=True,
            heartbeat_at=heartbeat_at,
            update=True,
        )
        term = terminator or os._exit
        term(124)
        return


def _mark_background_timeout_state(
    workspace_root: Path,
    mission_id: str,
    background: dict[str, Any],
    *,
    terminated: bool,
    update: bool = True,
    heartbeat_at: str | None = None,
) -> dict[str, Any]:
    completed_at = datetime.now(timezone.utc).isoformat()
    background.update(
        {
            "status": "timeout",
            "process_state": "timeout",
            "alive": False,
            "budget_exceeded": True,
            "terminated": bool(terminated),
            "completed_at": completed_at,
            "exit_code": 124,
        }
    )
    if heartbeat_at:
        background["heartbeat_at"] = heartbeat_at
    if update:
        save_background_record(workspace_root, mission_id, background)
        save_mission_progress(
            workspace_root,
            mission_id,
            stage="blocked",
            stage_label="Budget exhausted",
            status="timeout",
            blocker="budget_exhausted",
            last_activity_at=background["completed_at"],
            heartbeat_at=heartbeat_at,
        )
        _mark_mission_from_background(
            workspace_root,
            mission_id,
            status="stopped",
            stop_reason="budget_exhausted",
            round_status="budget_exhausted",
            background=background,
        )
    return background


def mission_wall_budget_exceeded(mission: dict[str, Any], background: dict[str, Any]) -> bool:
    budget = mission.get("budget_policy") if isinstance(mission.get("budget_policy"), dict) else {}
    max_minutes = float(budget.get("max_wall_minutes") or 0)
    if max_minutes <= 0:
        return False
    started = parse_iso_datetime(
        str(
            mission.get("budget_started_at")
            or background.get("budget_started_at")
            or background.get("started_at")
            or mission.get("created_at")
            or ""
        )
    )
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


def _mission_has_terminal_progress(workspace_root: Path, mission_id: str, mission: dict[str, Any] | None) -> bool:
    if mission is not None:
        status = str(mission.get("status") or "")
        stop_reason = str(mission.get("stop_reason") or "")
        if status in {"verified", "verified_blocked", "merged"} or stop_reason == "verified":
            return True
    try:
        rounds = load_rounds(workspace_root, mission_id)
    except OSError:
        return False
    for item in reversed(rounds):
        if not isinstance(item, dict):
            continue
        round_type = str(item.get("type") or "")
        status = str(item.get("status") or "")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        verdict = str(payload.get("verdict") or "")
        if round_type == "verification" and (status == "pass" or verdict == "pass"):
            return True
        if round_type == "merge" and status in {"merged", "blocked", "conflict", "nothing_to_merge"}:
            return True
    return False


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
