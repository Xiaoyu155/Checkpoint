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


BACKGROUND_TERMINAL_STATUSES = frozenset({"completed", "failed", "timeout", "orphaned", "aborted"})


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
    base_probe_enabled: bool = True,
    dependency_bootstrap_enabled: bool = True,
    merge: bool = False,
    skip_liveness_probe: bool = False,
    execution_policy: dict[str, Any] | None = None,
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
    # Fail closed when the coding assistant cannot spend tokens (quota/login).
    if not skip_liveness_probe:
        from .provider_liveness import liveness_block_payload, probe_worker_agent_liveness

        agent_hint = ""
        if agents:
            agent_hint = str(agents[0] or "")
        if not agent_hint:
            agent_hint = str(mission.get("agent") or "codex")
        probe = probe_worker_agent_liveness(agent_hint)
        if not probe.get("ok"):
            return liveness_block_payload(probe=probe, mission=mission)

    directory = mission_dir(workspace_path, mission_id)
    launch_lock = RunLock(directory, name="background-launch.lock", ttl_seconds=30.0)
    try:
        launch_lock.acquire(owner=f"background-launch:{mission_id}")
    except RuntimeError:
        return _background_already_running_payload(mission, load_background_record(workspace_path, mission_id))
    try:
        existing = load_background_record(workspace_path, mission_id)
        if _background_record_is_alive(existing, mission_id):
            return _background_already_running_payload(mission, existing)
        # Dead or PID-reused record: mark stale so resume/start can continue.
        if isinstance(existing, dict) and str(existing.get("status") or "") in {"starting", "running"}:
            stale_pid = int(existing.get("worker_pid") or existing.get("pid") or 0)
            if stale_pid and not _background_record_is_alive(existing, mission_id):
                existing = {
                    **existing,
                    "status": "orphaned",
                    "process_state": "stale_before_relaunch",
                    "alive": False,
                    "stale_reason": "dead_or_pid_reused",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
                save_background_record(workspace_path, mission_id, existing)
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
            execution_policy=execution_policy,
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
    base_probe_enabled: bool = True,
    dependency_bootstrap_enabled: bool = True,
    merge: bool = False,
    execution_policy: dict[str, Any] | None = None,
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
    effective_allow_dirty = bool(allow_dirty or mission.get("allow_dirty"))
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
    if effective_allow_dirty:
        argv.append("--allow-dirty")
    if allow_coverage_gap:
        argv.append("--allow-coverage-gap")
    if effective_test_command:
        argv.extend(["--test-command", effective_test_command])
    if effective_allow_test_edits:
        argv.append("--allow-test-edits")
    if effective_merge:
        argv.append("--merge")
    if isinstance(execution_policy, dict) and execution_policy:
        argv.extend(["--execution-policy-json", json.dumps(execution_policy, ensure_ascii=False)])

    env = os.environ.copy()
    src_dir = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = _prepend_path(env.get("PYTHONPATH", ""), src_dir)
    env["PYTHONUNBUFFERED"] = "1"
    # Prefer the verification Python on PATH so nested workers inherit a
    # pytest-capable interpreter (dogfood: wrong D:\\python.exe broke gates).
    env = _prefer_test_command_python_env(env, effective_test_command)
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
        "allow_dirty": effective_allow_dirty,
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
    mission["allow_dirty"] = effective_allow_dirty
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
    base_probe_enabled: bool = True,
    dependency_bootstrap_enabled: bool = True,
    merge: bool = False,
    watchdog_interval_seconds: float = 60.0,
    watchdog_terminator: Any = None,
    execution_policy: dict[str, Any] | None = None,
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
            base_probe_enabled=base_probe_enabled,
            dependency_bootstrap_enabled=dependency_bootstrap_enabled,
            merge=merge or bool(background.get("merge")),
            execution_policy=execution_policy,
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
    if background.get("status") in BACKGROUND_TERMINAL_STATUSES:
        return {
            **background,
            "process_state": str(background.get("status")),
            "alive": False,
            "budget_exceeded": False,
        }

    pid = int(background.get("worker_pid") or background.get("pid") or 0)
    using_default_probe = process_probe is None
    probe = process_probe or process_status
    process = probe(pid)
    budget_exceeded = bool(mission and mission_wall_budget_exceeded(mission, background))
    # Ownership: a recycled PID owned by another process must not look "alive".
    # Custom process_probe (tests / advanced callers) can set belongs_to_mission.
    owned = True
    if process.get("alive"):
        if "belongs_to_mission" in process:
            owned = bool(process.get("belongs_to_mission"))
        elif using_default_probe and mission_id:
            try:
                owned = process_belongs_to_mission(pid, mission_id, record=background)
            except Exception:
                owned = True
        if not owned:
            process = {
                "pid": pid,
                "alive": False,
                "exit_code": process.get("exit_code"),
                "ownership": "pid_reused_or_foreign",
            }
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

    orphan_reason = _classify_orphan_stop_reason(workspace_path, mission_id, background)
    process_state = "pid_reused_or_foreign" if not owned else "exited_unknown"
    background.update(
        {
            "status": "orphaned",
            "process_state": process_state,
            "alive": False,
            "budget_exceeded": budget_exceeded,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "exit_code": process.get("exit_code"),
            "orphan_reason": orphan_reason,
        }
    )
    if update:
        save_background_record(workspace_path, mission_id, background)
        save_mission_progress(
            workspace_path,
            mission_id,
            stage="blocked",
            stage_label="Worker exited or lost ownership",
            status="orphaned",
            blocker=orphan_reason,
            last_activity_at=background["completed_at"],
        )
        _mark_mission_from_background(
            workspace_path,
            mission_id,
            status="stopped",
            stop_reason=orphan_reason,
            round_status=process_state,
            background=background,
        )
    return background


def _classify_orphan_stop_reason(
    workspace_root: Path,
    mission_id: str,
    background: dict[str, Any],
) -> str:
    """Map orphan tails to a user-facing stop_reason (quota vs generic orphan)."""
    from .agent_backends import looks_like_provider_5xx, looks_like_quota_exhaustion

    chunks: list[str] = []
    for key in ("stderr_log", "stdout_log"):
        path_text = str(background.get(key) or "").strip()
        if not path_text:
            continue
        path = Path(path_text)
        if not path.is_file():
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace")[-4000:])
        except OSError:
            continue
    # Also skim latest background logs in mission dir
    log_dir = mission_dir(workspace_root, mission_id) / "logs"
    if log_dir.is_dir():
        for path in sorted(log_dir.glob("chief-run-background-*.err.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:2]:
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace")[-4000:])
            except OSError:
                pass
    if looks_like_quota_exhaustion(*chunks):
        return "quota_exhausted"
    if looks_like_provider_5xx(*chunks):
        return "provider_5xx"
    return "worker_orphaned"


# A foreground mission that is genuinely alive touches its record far more often
# than this; anything quieter has been dead since the process went away.
_ABANDONED_AFTER_SECONDS = 30 * 60


def _mission_idle_seconds(mission: dict[str, Any]) -> float | None:
    stamp = str(mission.get("updated_at") or mission.get("created_at") or "").strip()
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def reconcile_workspace_backgrounds(
    workspace_root: str | Path,
    *,
    update: bool = True,
    limit: int = 50,
    auto_resume: bool = True,
    max_auto_resumes: int = 3,
    max_auto_resume_attempts: int | None = None,
) -> list[dict[str, Any]]:
    """Scan recent missions and reconcile dead/PID-reused background workers.

    When *auto_resume* is true, missions marked ``worker_orphaned`` (not quota /
    provider death) are background-resumed at most once per mission.
    """
    workspace_path = Path(workspace_root).expanduser().resolve()
    root = workspace_path / "missions"
    if not root.is_dir():
        return []
    results: list[dict[str, Any]] = []
    auto_resume_budget = max(0, int(max_auto_resumes))
    missions = sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in missions[: max(1, int(limit))]:
        mid = path.name
        record = load_background_record(workspace_path, mid)
        mission = load_mission(workspace_path, mid) or {}
        status = str(mission.get("status") or "")
        stop = str(mission.get("stop_reason") or "")

        # Path A: still claims running background → inspect/reconcile
        if isinstance(record, dict) and str(record.get("status") or "") in {"starting", "running"}:
            if status in {"verified", "merged", "stopped", "preview"}:
                continue
            state = inspect_background_state(
                workspace_root=workspace_path,
                mission_id=mid,
                update=update,
            )
            entry: dict[str, Any] = {"mission_id": mid, "background": state}
            if str(state.get("status") or "") in {"orphaned", "timeout", "completed", "failed"}:
                if (
                    auto_resume
                    and auto_resume_budget > 0
                    and str(state.get("status") or "") == "orphaned"
                    and str(state.get("orphan_reason") or state.get("stop_reason") or "")
                    in {"worker_orphaned", ""}
                ):
                    resumed = maybe_auto_resume_orphaned_mission(
                        workspace_root=workspace_path,
                        mission_id=mid,
                        max_attempts=max_auto_resume_attempts,
                    )
                    if resumed is not None:
                        entry["auto_resume"] = resumed
                        if resumed.get("status") == "background_started":
                            auto_resume_budget -= 1
                results.append(entry)
            continue

        # Path A2: claims running but left no background record at all. A
        # foreground run that was interrupted never writes one, so nothing above
        # can see it — and the host counts it as an active mission forever. One
        # such mission from 27 days ago was enough to block a hosted session from
        # launching any work at all.
        if not isinstance(record, dict) and status in {"running", "background_running"}:
            idle_seconds = _mission_idle_seconds(mission)
            if idle_seconds is not None and idle_seconds >= _ABANDONED_AFTER_SECONDS:
                entry = {
                    "mission_id": mid,
                    "abandoned": {
                        "status": "abandoned",
                        "idle_seconds": round(idle_seconds),
                        "reason": "no_background_record",
                    },
                }
                if update:
                    mission["status"] = "stopped"
                    mission["stop_reason"] = "worker_orphaned"
                    save_mission(workspace_path, mission)
                results.append(entry)
            continue

        # Path B: already stopped as orphan → one-shot auto resume
        if (
            auto_resume
            and auto_resume_budget > 0
            and status == "stopped"
            and stop == "worker_orphaned"
        ):
            resumed = maybe_auto_resume_orphaned_mission(
                workspace_root=workspace_path,
                mission_id=mid,
                max_attempts=max_auto_resume_attempts,
            )
            if resumed is not None:
                results.append({"mission_id": mid, "auto_resume": resumed})
                if resumed.get("status") == "background_started":
                    auto_resume_budget -= 1
    return results


def maybe_auto_resume_orphaned_mission(
    *,
    workspace_root: str | Path,
    mission_id: str,
    max_attempts: int | None = None,
) -> dict[str, Any] | None:
    """Background-resume an orphaned mission at most *max_attempts* times.

    Skips quota/provider death and refuses when the agent liveness probe fails.
    Returns the launch payload, or None when resume is not appropriate.

    Default attempts: env ``PACER_AUTO_RESUME_MAX`` or 2.
    """
    workspace_path = Path(workspace_root).expanduser().resolve()
    mission = load_mission(workspace_path, mission_id)
    if mission is None:
        return None
    stop = str(mission.get("stop_reason") or "")
    background = load_background_record(workspace_path, mission_id) or {}
    bg_status = str(background.get("status") or "")
    orphan_reason = str(background.get("orphan_reason") or "")
    is_orphan = stop == "worker_orphaned" or (
        bg_status == "orphaned" and orphan_reason in {"worker_orphaned", "", "exited_unknown"}
    )
    if not is_orphan:
        return None
    if stop in {
        "quota_exhausted",
        "provider_5xx",
        "budget_exhausted",
        "not_authenticated",
        "agent_unavailable",
    }:
        return None
    if orphan_reason in {"quota_exhausted", "provider_5xx"}:
        return None

    if max_attempts is None:
        try:
            max_attempts = max(1, int(os.environ.get("PACER_AUTO_RESUME_MAX") or "2"))
        except ValueError:
            max_attempts = 2

    attempts = int(background.get("auto_resume_count") or mission.get("auto_resume_count") or 0)
    if attempts >= max(1, int(max_attempts)):
        return {
            "schema_version": 1,
            "status": "skipped",
            "stop_reason": "auto_resume_exhausted",
            "message": f"Auto-resume already used {attempts} time(s) for this mission.",
            "mission_id": mission_id,
        }

    agent = str(mission.get("agent") or "")
    if not agent:
        agents = background.get("agents") if isinstance(background.get("agents"), list) else []
        agent = str(agents[0]) if agents else "codex"
    from .provider_liveness import probe_worker_agent_liveness

    probe = probe_worker_agent_liveness(agent)
    if not probe.get("ok"):
        return {
            "schema_version": 1,
            "status": "skipped",
            "stop_reason": str(probe.get("stop_reason") or "agent_unavailable"),
            "message": str(probe.get("message") or "Agent not available for auto-resume."),
            "mission_id": mission_id,
            "provider_liveness": probe,
        }

    # Persist attempt counter before launch to survive crashes.
    attempts += 1
    background = dict(background)
    background["auto_resume_count"] = attempts
    background["auto_resume_at"] = datetime.now(timezone.utc).isoformat()
    background["status"] = "orphaned"
    save_background_record(workspace_path, mission_id, background)
    mission["auto_resume_count"] = attempts
    mission["status"] = "stopped"
    mission["stop_reason"] = "worker_orphaned"
    save_mission(workspace_path, mission)

    agents_tuple: tuple[str, ...] = ()
    raw_agents = background.get("agents")
    if isinstance(raw_agents, list) and raw_agents:
        agents_tuple = tuple(str(a) for a in raw_agents if str(a or "").strip())
    elif agent:
        agents_tuple = (agent,)

    # Host/practice defaults often omit allow_dirty; prefer explicit False over
    # `or True` which made the flag impossible to disable.
    allow_dirty = mission.get("allow_dirty")
    if allow_dirty is None:
        allow_dirty = background.get("allow_dirty")
    if allow_dirty is None:
        allow_dirty = True
    payload = start_background_chief_run(
        workspace_root=workspace_path,
        mission_id=mission_id,
        agents=agents_tuple,
        allow_dirty=bool(allow_dirty),
        test_command=str(background.get("test_command") or mission.get("test_command") or "") or None,
        allow_test_edits=bool(background.get("allow_test_edits") or mission.get("allow_test_edits")),
        merge=bool(background.get("merge") or mission.get("merge")),
        skip_liveness_probe=False,
    )
    payload = dict(payload)
    payload["auto_resume"] = True
    payload["auto_resume_count"] = attempts
    append_round(
        workspace_path,
        mission_id,
        {
            "round": _next_round_number(workspace_path, mission_id),
            "type": "auto_resume",
            "status": str(payload.get("status") or ""),
            "stop_reason": str(payload.get("stop_reason") or ""),
            "attempt": attempts,
        },
    )
    return payload


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


def _background_record_is_alive(
    record: dict[str, Any] | None,
    mission_id: str = "",
) -> bool:
    """True only when the recorded PID is live *and* belongs to this mission.

    PID-only checks are unsafe on Windows: OS reuses PIDs and a dead mission can
    block resume forever while another process holds the recycled id.
    """
    if not isinstance(record, dict) or str(record.get("status") or "") not in {"starting", "running"}:
        return False
    pid = int(record.get("worker_pid") or record.get("pid") or 0)
    if not process_status(pid).get("alive"):
        return False
    if mission_id and not process_belongs_to_mission(pid, mission_id, record=record):
        return False
    return True


def process_command_line(pid: int) -> str:
    """Best-effort command line for a process (empty when unreadable)."""
    if pid <= 0:
        return ""
    if os.name == "nt":
        try:
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\").CommandLine",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except Exception:
            return ""
        return (completed.stdout or "").strip()
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def process_belongs_to_mission(
    pid: int,
    mission_id: str,
    *,
    record: dict[str, Any] | None = None,
    allow_unreadable: bool = True,
    require_worker_marker: bool = False,
) -> bool:
    """Return whether *pid* is this mission's background worker.

    When the command line cannot be read, fall back to True if the process is
    still alive (avoid false orphans under restricted permissions). When the
    command line is readable, require the mission id (and prefer the worker
    marker ``chief-background-worker``).
    """
    mid = str(mission_id or "").strip()
    if pid <= 0 or not mid:
        return False
    if not process_status(pid).get("alive"):
        return False
    try:
        cmd = process_command_line(pid)
    except Exception:
        cmd = ""
    if not cmd:
        # Unreadable cmdline: keep conservative "alive" for inspect heartbeats,
        # but start_background still requires ownership when cmd is available.
        return bool(allow_unreadable)
    if mid not in cmd:
        return False
    lowered = cmd.lower()
    if "chief-background-worker" in lowered or "visual_agent" in lowered:
        return True
    # Stored argv fingerprint as secondary signal
    if isinstance(record, dict):
        argv = record.get("argv")
        if isinstance(argv, list) and mid in " ".join(str(part) for part in argv):
            # Process cmdline has mission id but is not clearly our worker —
            # still accept if argv was ours and pid matches recorded worker.
            recorded = int(record.get("worker_pid") or record.get("pid") or 0)
            return recorded == pid
    return not require_worker_marker and mid in cmd


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
        if str(background.get("status") or "") in BACKGROUND_TERMINAL_STATUSES:
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


def _prefer_test_command_python_env(env: dict[str, str], test_command: str | None) -> dict[str, str]:
    """If test_command starts with an absolute python path, put its dir on PATH."""
    import re

    cmd = str(test_command or "").strip()
    if not cmd or "pytest" not in cmd.lower():
        return env
    match = re.match(r'^["\']?([A-Za-z]:\\[^"\']+?python(?:\d+(?:\.\d+)*)?\.exe)', cmd, re.I)
    if not match:
        match = re.match(r'^["\']?(/[^"\']+?/python(?:\d+(?:\.\d+)*)?)(?:\s|$)', cmd)
    if not match:
        return env
    python_path = Path(match.group(1))
    if not python_path.is_file():
        return env
    out = dict(env)
    out["PATH"] = _prepend_path(out.get("PATH", ""), python_path.parent)
    out["PACER_VERIFICATION_PYTHON"] = str(python_path)
    return out


def _next_round_number(workspace_root: Path, mission_id: str) -> int:
    from .missions import load_rounds

    rounds = load_rounds(workspace_root, mission_id)
    if not rounds:
        return 0
    return max((int(item.get("round") or 0) for item in rounds), default=-1) + 1
