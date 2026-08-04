"""Official long-host orchestration for Pacer.

Product path for multi-hour managed development:
- start goals as background missions (Codex-primary by default)
- reconcile orphans + one/few-shot auto-resume
- probe agent liveness before launch
- expose a human dashboard (CLI / interactive)

This replaces ad-hoc overnight scripts for the happy path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .agent_capabilities import load_agent_profile
from .models import to_jsonable
from .missions import list_missions, load_mission, mission_dir
from .provider_liveness import normalize_agent_name, probe_worker_agent_liveness


HOST_DIR_NAME = "host"
HOST_SESSION_FILE = "session.json"
HOST_POLICY_FILE = "host_policy.json"
HOST_LOG_FILE = "host.log"
HOST_STOP_FLAG = "STOP"

# ---------------------------------------------------------------------------
# Host modes: default is economy (save tokens). Aggressive is opt-in.
# ---------------------------------------------------------------------------
# economy  — 省额度：单并发、少 resume、不竞速、不拆目标（推广默认）
# standard — 均衡：轻度并行 + 有限自愈
# unleash  — 激进：回血续跑 / 多 resume / 自愈插队 / 可 merge（显式开启）
# race     — 最吃额度：双助手竞速（必须显式 --race 或 mode=race）
# yolo     — Claude Code 全权限：bypassPermissions（必须显式 yolo）

HOST_MODE_PROFILES: dict[str, dict[str, Any]] = {
    "economy": {
        "schema_version": 1,
        "mode": "economy",
        "token_cost": "low",
        "token_cost_label": "省额度",
        "agent": "codex",
        "max_auto_resumes_per_mission": 1,
        "max_auto_resumes_per_tick": 2,
        "poll_seconds": 120,
        "allow_dirty": True,
        "allow_test_edits": False,
        "merge": False,
        "max_rounds": 2,
        "max_repair_rounds": 1,
        "max_wall_minutes": 40,
        "max_worker_minutes": 30,
        "reasoning_effort": "low",
        "model_policy": {
            "implementation": "standard",
            "repair": "strong",
            "classification": "fast",
            "visual_review": "multimodal",
        },
        "unleash": False,
        "wake_on_quota": False,
        "wake_poll_seconds": 180,
        "race": False,
        "race_agents": ["codex", "claude-code"],
        "race_abort_losers": True,
        "race_settle": True,
        "self_heal_pytest": False,
        "self_heal_preempt": False,
        "max_self_heal_attempts": 0,
        "self_heal_probe_interval_seconds": 0,
        "auto_split_goals": False,
        "max_active": 1,
    },
    "standard": {
        "schema_version": 1,
        "mode": "standard",
        "token_cost": "medium",
        "token_cost_label": "均衡",
        "agent": "codex",
        "max_auto_resumes_per_mission": 2,
        "max_auto_resumes_per_tick": 3,
        "poll_seconds": 90,
        "allow_dirty": True,
        "allow_test_edits": False,
        "merge": False,
        "max_rounds": 3,
        "max_repair_rounds": 1,
        "max_wall_minutes": 50,
        "max_worker_minutes": 35,
        "reasoning_effort": "medium",
        "model_policy": {
            "implementation": "standard",
            "repair": "strong",
            "classification": "fast",
            "visual_review": "multimodal",
        },
        "unleash": False,
        "wake_on_quota": True,
        "wake_poll_seconds": 120,
        "race": False,
        "race_agents": ["codex", "claude-code"],
        "race_abort_losers": True,
        "race_settle": True,
        "self_heal_pytest": True,
        "self_heal_preempt": False,  # heal without killing others
        "max_self_heal_attempts": 1,
        "self_heal_probe_interval_seconds": 600,
        "auto_split_goals": False,
        "max_active": 2,
    },
    "unleash": {
        "schema_version": 1,
        "mode": "unleash",
        "token_cost": "high",
        "token_cost_label": "吃额度·换效率",
        "agent": "codex",
        "max_auto_resumes_per_mission": 5,
        "max_auto_resumes_per_tick": 8,
        "poll_seconds": 60,
        "allow_dirty": True,
        "allow_test_edits": True,
        "merge": True,
        "max_rounds": 4,
        "max_repair_rounds": 2,
        "max_wall_minutes": 75,
        "max_worker_minutes": 50,
        "reasoning_effort": "high",
        "model_policy": {
            "implementation": "strong",
            "repair": "strong",
            "classification": "fast",
            "visual_review": "multimodal",
        },
        "unleash": True,
        "wake_on_quota": True,
        "wake_poll_seconds": 90,
        "race": False,
        "race_agents": ["codex", "claude-code"],
        "race_abort_losers": True,
        "race_settle": True,
        "self_heal_pytest": True,
        "self_heal_preempt": True,
        "max_self_heal_attempts": 2,
        "self_heal_probe_interval_seconds": 300,
        "auto_split_goals": True,
        "max_active": 4,
    },
    "race": {
        "schema_version": 1,
        "mode": "race",
        "token_cost": "very_high",
        "token_cost_label": "很吃额度·竞速",
        "agent": "codex",
        "max_auto_resumes_per_mission": 3,
        "max_auto_resumes_per_tick": 6,
        "poll_seconds": 45,
        "allow_dirty": True,
        "allow_test_edits": True,
        "merge": True,
        "max_rounds": 3,
        "max_repair_rounds": 1,
        "max_wall_minutes": 60,
        "max_worker_minutes": 40,
        "reasoning_effort": "high",
        "model_policy": {
            "implementation": "strong",
            "repair": "strong",
            "classification": "fast",
            "visual_review": "multimodal",
        },
        "unleash": True,
        "wake_on_quota": True,
        "wake_poll_seconds": 90,
        "race": True,
        "race_agents": ["codex", "claude-code"],
        "race_abort_losers": True,
        "race_settle": True,
        "self_heal_pytest": True,
        "self_heal_preempt": True,
        "max_self_heal_attempts": 2,
        "self_heal_probe_interval_seconds": 300,
        "auto_split_goals": False,  # race is already 2x; don't multiply goals
        "max_active": 4,
    },
    "yolo": {
        "schema_version": 1,
        "mode": "yolo",
        "token_cost": "very_high",
        "token_cost_label": "YOLO 全权限",
        "agent": "claude-code",
        "max_auto_resumes_per_mission": 5,
        "max_auto_resumes_per_tick": 8,
        "poll_seconds": 60,
        "allow_dirty": True,
        "allow_test_edits": False,
        "merge": True,
        "max_rounds": 4,
        "max_repair_rounds": 2,
        "max_wall_minutes": 75,
        "max_worker_minutes": 50,
        "reasoning_effort": "high",
        "model_policy": {
            "implementation": "strong",
            "repair": "strong",
            "classification": "fast",
            "visual_review": "multimodal",
        },
        "execution_policy": {
            "permission_mode": "bypassPermissions",
            "tool_permissions": "default",
            "memory_mode": "enabled",
        },
        "unleash": True,
        "wake_on_quota": True,
        "wake_poll_seconds": 90,
        "race": False,
        "race_agents": ["codex", "claude-code"],
        "race_abort_losers": True,
        "race_settle": True,
        "self_heal_pytest": True,
        "self_heal_preempt": True,
        "max_self_heal_attempts": 2,
        "self_heal_probe_interval_seconds": 300,
        "auto_split_goals": True,
        "max_active": 4,
    },
}

# Back-compat aliases
DEFAULT_HOST_POLICY = dict(HOST_MODE_PROFILES["economy"])
UNLEASH_HOST_POLICY = dict(HOST_MODE_PROFILES["unleash"])


def normalize_host_mode(mode: str | None, *, unleash_flag: bool = False, race_flag: bool = False) -> str:
    """Resolve user intent into a named mode. Default: economy (save tokens)."""
    name = str(mode or "").strip().lower().replace("-", "_")
    if race_flag:
        return "race"
    if name in {"yolo", "full", "danger", "danger_full_access", "dangerously_skip_permissions"}:
        return "yolo"
    if unleash_flag:
        return "unleash"
    aliases = {
        "": "economy",
        "default": "economy",
        "save": "economy",
        "saver": "economy",
        "eco": "economy",
        "economy": "economy",
        "cheap": "economy",
        "balanced": "standard",
        "standard": "standard",
        "normal": "standard",
        "wild": "unleash",
        "unleash": "unleash",
        "feral": "unleash",
        "race": "race",
        "dual": "race",
        "yolo": "yolo",
        "full": "yolo",
        "danger": "yolo",
        "danger_full_access": "yolo",
        "danger-full-access": "yolo",
    }
    resolved = aliases.get(name, name)
    if resolved not in HOST_MODE_PROFILES:
        return "economy"
    return resolved


def mode_profile(mode: str | None = None, **flags: Any) -> dict[str, Any]:
    resolved = normalize_host_mode(
        mode,
        unleash_flag=bool(flags.get("unleash")),
        race_flag=bool(flags.get("race")),
    )
    return dict(HOST_MODE_PROFILES[resolved])


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def host_dir(workspace_root: str | Path) -> Path:
    path = Path(workspace_root).expanduser().resolve() / HOST_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_host_policy(
    workspace_root: str | Path,
    *,
    mode: str | None = None,
    unleash: bool = False,
    race: bool = False,
) -> dict[str, Any]:
    """Load host policy. Default mode is **economy** (save tokens)."""
    resolved = normalize_host_mode(mode, unleash_flag=unleash, race_flag=race)
    if os.environ.get("PACER_HOST_MODE"):
        resolved = normalize_host_mode(os.environ.get("PACER_HOST_MODE"))
    if os.environ.get("PACER_HOST_UNLEASH", "").strip() in {"1", "true", "yes", "on"}:
        resolved = "unleash"
    policy = mode_profile(resolved)
    path = host_dir(workspace_root) / HOST_POLICY_FILE
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                # File may set default mode only; explicit CLI mode still wins.
                file_mode = data.get("mode")
                if not mode and not unleash and not race and file_mode:
                    resolved = normalize_host_mode(str(file_mode))
                    policy = mode_profile(resolved)
                # Allow soft overrides that don't force expensive modes on users
                soft_keys = {
                    "agent",
                    "poll_seconds",
                    "max_wall_minutes",
                    "max_worker_minutes",
                    "max_rounds",
                    "test_command",
                }
                for key in soft_keys:
                    if key in data and data[key] is not None:
                        policy[key] = data[key]
                # Hard cost knobs only apply when file mode matches resolved mode
                if str(data.get("mode") or resolved) == resolved:
                    for key, value in data.items():
                        if value is not None and key in policy:
                            policy[key] = value
        except (OSError, json.JSONDecodeError):
            pass
    # Env overrides for ops
    if os.environ.get("PACER_AUTO_RESUME_MAX"):
        try:
            policy["max_auto_resumes_per_mission"] = max(
                1, int(os.environ["PACER_AUTO_RESUME_MAX"])
            )
        except ValueError:
            pass
    if os.environ.get("PACER_HOST_AGENT"):
        policy["agent"] = normalize_agent_name(os.environ["PACER_HOST_AGENT"])
    policy["mode"] = resolved
    policy["unleash"] = resolved in {"unleash", "race", "yolo"}
    policy["race"] = resolved == "race" or bool(race)
    return policy


def save_host_policy(workspace_root: str | Path, policy: dict[str, Any]) -> Path:
    path = host_dir(workspace_root) / HOST_POLICY_FILE
    path.write_text(json.dumps(to_jsonable(policy), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def append_host_log(workspace_root: str | Path, message: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    path = host_dir(workspace_root) / HOST_LOG_FILE
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_host_session(workspace_root: str | Path) -> dict[str, Any] | None:
    path = host_dir(workspace_root) / HOST_SESSION_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_host_session(workspace_root: str | Path, session: dict[str, Any]) -> Path:
    path = host_dir(workspace_root) / HOST_SESSION_FILE
    path.write_text(json.dumps(to_jsonable(session), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _live_host_owner(workspace_root: str | Path) -> int:
    """PID of a host that still owns this workspace, or 0 when it is free."""
    session = load_host_session(workspace_root) or {}
    if str(session.get("status") or "") != "running":
        return 0
    try:
        pid = int(session.get("pid") or 0)
    except (TypeError, ValueError):
        return 0
    if pid <= 0 or pid == os.getpid():
        return 0
    from .chief_background import process_status

    state = process_status(pid)
    alive = bool(state.get("alive")) if isinstance(state, dict) else False
    return pid if alive else 0


def request_host_stop(workspace_root: str | Path) -> Path:
    path = host_dir(workspace_root) / HOST_STOP_FLAG
    path.write_text(utc_now() + "\n", encoding="utf-8")
    append_host_log(workspace_root, "STOP requested")
    session = load_host_session(workspace_root) or {}
    session["status"] = "stopping"
    session["stop_requested_at"] = utc_now()
    save_host_session(workspace_root, session)
    return path


def clear_host_stop(workspace_root: str | Path) -> None:
    path = host_dir(workspace_root) / HOST_STOP_FLAG
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def host_stop_requested(workspace_root: str | Path) -> bool:
    return (host_dir(workspace_root) / HOST_STOP_FLAG).is_file()


def _mission_counts(workspace_root: Path) -> dict[str, int]:
    rows = list_missions(workspace_root)
    verified = stopped = running = preview = other = 0
    orphans = 0
    for row in rows:
        status = str(row.get("status") or "")
        stop = str(row.get("stop_reason") or "")
        if status == "verified" or stop == "verified":
            verified += 1
        elif status == "stopped":
            stopped += 1
            if stop == "worker_orphaned":
                orphans += 1
        elif status in {"running", "created", "background_running"}:
            running += 1
        elif status == "preview":
            preview += 1
        else:
            other += 1
    return {
        "total": len(rows),
        "verified": verified,
        "stopped": stopped,
        "running": running,
        "preview": preview,
        "other": other,
        "orphaned_stop": orphans,
    }


def _last_success_meta(workspace_root: Path) -> dict[str, Any]:
    rows = list_missions(workspace_root)
    for row in rows:
        status = str(row.get("status") or "")
        stop = str(row.get("stop_reason") or "")
        if status == "verified" or stop == "verified":
            mid = str(row.get("mission_id") or "")
            progress_path = mission_dir(workspace_root, mid) / "progress.json"
            last_activity = str(row.get("updated_at") or row.get("created_at") or "")
            if progress_path.is_file():
                try:
                    progress = json.loads(progress_path.read_text(encoding="utf-8-sig"))
                    last_activity = str(
                        progress.get("last_activity_at")
                        or progress.get("heartbeat_at")
                        or last_activity
                    )
                except (OSError, json.JSONDecodeError):
                    pass
            return {
                "mission_id": mid,
                "objective": str(row.get("objective") or "")[:120],
                "last_activity_at": last_activity,
                "agent": str(row.get("agent") or ""),
            }
    return {}


def _run_pytest_probe(repo_root: Path, python: str | None = None) -> dict[str, Any]:
    py = python or sys.executable
    try:
        completed = subprocess.run(
            [py, "-m", "pytest", "-q", "--tb=no"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "exit_code": 1, "tail": str(exc)}
    tail = ((completed.stdout or "") + (completed.stderr or ""))[-300:]
    return {"ok": completed.returncode == 0, "exit_code": int(completed.returncode), "tail": tail.strip()}


def _host_agent_capability(agent: str, *, mode: str | None = None) -> dict[str, Any]:
    agent_name = normalize_agent_name(agent)
    profile = load_agent_profile(agent_name)
    if not isinstance(profile, dict):
        # API-backed patch workers (mimo, bugteam) are dispatched directly rather
        # than through a CLI, so they have no CLI profile to register. Dispatch
        # already accepts them; requiring a profile here rejected exactly the
        # cheap backends a user picks to keep a long hosted run affordable.
        from .agent_backends import resolve_backend_by_name

        backend = resolve_backend_by_name(agent_name)
        if isinstance(backend, dict) and backend:
            return {
                "ok": True,
                "agent": agent_name,
                "primary_role": "implementation",
                "worker_kind": "api_backend",
                "model": backend.get("model") or "",
            }
        return {
            "ok": False,
            "agent": agent_name,
            "stop_reason": "agent_unsupported",
            "message": f"No hosted implementation profile is registered for '{agent_name}'.",
        }
    primary_role = str(profile.get("primary_role") or "implementation").strip()
    headless = profile.get("headless") if isinstance(profile.get("headless"), dict) else {}
    if primary_role == "multimodal_inspection":
        return {
            "ok": False,
            "agent": agent_name,
            "stop_reason": "agent_inspection_only",
            "message": (
                f"{profile.get('display_name') or agent_name} is configured as a read-only "
                "inspection lane, not a hosted implementation worker."
            ),
            "primary_role": primary_role,
        }
    if not str(headless.get("command") or "").strip():
        return {
            "ok": False,
            "agent": agent_name,
            "stop_reason": "agent_headless_unsupported",
            "message": f"'{agent_name}' has no headless worker command.",
            "primary_role": primary_role,
        }
    if mode == "yolo":
        from .agent_capabilities import probe_agent
        probe = probe_agent(profile)
        if probe.get("bypass_permissions_supported") is False:
            return {
                "ok": False,
                "agent": agent_name,
                "stop_reason": "bypass_permissions_unsupported",
                "message": (
                    f"'{agent_name}' is installed but --permission-mode bypassPermissions is not "
                    "supported by this version. Run `claude --version` and update Claude Code."
                ),
                "primary_role": primary_role,
            }
    return {
        "ok": True,
        "agent": agent_name,
        "stop_reason": "",
        "message": "",
        "primary_role": primary_role,
    }


def build_host_dashboard(
    *,
    workspace_root: str | Path,
    repo_root: str | Path | None = None,
    agent: str | None = None,
    run_pytest: bool = False,
    python: str | None = None,
    mode: str | None = None,
    auto_resume: bool = False,
) -> dict[str, Any]:
    """Build host status; worker recovery is opt-in for active host ticks only."""
    ws = Path(workspace_root).expanduser().resolve()
    repo = Path(repo_root or Path.cwd()).expanduser().resolve()
    policy = load_host_policy(ws, mode=mode)
    agent_name = normalize_agent_name(agent or policy.get("agent") or "codex")
    effective_mode = mode or str(policy.get("mode") or "")
    agent_capability = _host_agent_capability(agent_name, mode=effective_mode)
    liveness = probe_worker_agent_liveness(agent_name)
    stop_requested = host_stop_requested(ws)
    resume_enabled = bool(
        auto_resume
        and liveness.get("ok")
        and agent_capability.get("ok")
        and not stop_requested
    )
    counts = _mission_counts(ws)
    session = load_host_session(ws) or {}
    last_ok = _last_success_meta(ws)
    latest_journey: dict[str, Any] = {}
    try:
        from .mission_journey import build_latest_mission_journey

        latest_journey = build_latest_mission_journey(ws)
    except Exception:  # noqa: BLE001 - host liveness must not depend on this projection.
        latest_journey = {}
    memory_fallback: dict[str, Any] = {}
    journey_phases = latest_journey.get("phases") if isinstance(latest_journey, dict) else []
    if isinstance(journey_phases, list):
        memory_phase = next(
            (item for item in journey_phases if isinstance(item, dict) and item.get("id") == "memory"),
            None,
        )
        if isinstance(memory_phase, dict):
            details = memory_phase.get("details") if isinstance(memory_phase.get("details"), dict) else {}
            memory_fallback = {
                "status": str(memory_phase.get("status") or ""),
                "summary": str(memory_phase.get("summary") or ""),
                "reason_codes": [str(item) for item in memory_phase.get("reason_codes") or []],
                "selected_entries": int(details.get("selected_entries") or 0),
                "dispatch_injected": bool(details.get("dispatch_injected")),
                "memory_ids": [str(item) for item in details.get("memory_ids") or []],
            }

    # Opportunistic reconcile (product path, not optional script)
    reconcile_summary: dict[str, Any] = {"count": 0, "auto_resumed": 0, "items": []}
    try:
        from .chief_background import reconcile_workspace_backgrounds

        items = reconcile_workspace_backgrounds(
            ws,
            update=True,
            limit=40,
            auto_resume=resume_enabled,
            max_auto_resumes=int(policy.get("max_auto_resumes_per_tick") or 2),
            max_auto_resume_attempts=int(policy.get("max_auto_resumes_per_mission") or 1),
        )
        auto_n = sum(
            1
            for item in items
            if isinstance(item.get("auto_resume"), dict)
            and item["auto_resume"].get("status") == "background_started"
        )
        reconcile_summary = {
            "count": len(items),
            "auto_resumed": auto_n,
            "items": [
                {
                    "mission_id": item.get("mission_id"),
                    "background": (item.get("background") or {}).get("status"),
                    "auto_resume": (item.get("auto_resume") or {}).get("status"),
                    "stop": (item.get("auto_resume") or {}).get("stop_reason")
                    or (item.get("background") or {}).get("orphan_reason"),
                }
                for item in items[:12]
            ],
        }
        # Re-count after reconcile
        counts = _mission_counts(ws)
    except Exception as exc:  # noqa: BLE001
        reconcile_summary = {"count": 0, "auto_resumed": 0, "error": f"{type(exc).__name__}: {exc}"}

    pytest_info: dict[str, Any] | None = None
    if run_pytest:
        pytest_info = _run_pytest_probe(repo, python=python)

    ready = (
        bool(liveness.get("ok"))
        and bool(agent_capability.get("ok"))
        and counts["running"] < 20
        and not stop_requested
    )
    blockers: list[str] = []
    if not liveness.get("ok"):
        blockers.append(str(liveness.get("stop_reason") or "agent_unavailable"))
    if not agent_capability.get("ok"):
        blockers.append(str(agent_capability.get("stop_reason") or "agent_unsupported"))
    if stop_requested:
        blockers.append("stop_requested")

    return {
        "schema_version": 1,
        "product": "Pacer",
        "kind": "host_dashboard",
        "checked_at": utc_now(),
        "workspace_root": str(ws),
        "repo_root": str(repo),
        "ready_for_host": ready,
        "blockers": blockers,
        "agent": agent_name,
        "agent_capability": agent_capability,
        "mode": policy.get("mode") or "economy",
        "token_cost": policy.get("token_cost") or "low",
        "token_cost_label": policy.get("token_cost_label") or "省额度",
        "provider_liveness": liveness,
        "missions": counts,
        "last_success": last_ok,
        "latest_journey": latest_journey,
        "memory_fallback": memory_fallback,
        "session": {
            "status": session.get("status") or "idle",
            "session_id": session.get("session_id") or "",
            "started_at": session.get("started_at") or "",
            "hours": session.get("hours"),
            "launched": session.get("launched_count") or 0,
            "goals_total": len(session.get("goals") or []),
            "goals_done_index": session.get("goal_index") or 0,
            "mode": session.get("mode") or policy.get("mode"),
        },
        "policy": {
            "mode": policy.get("mode"),
            "token_cost": policy.get("token_cost"),
            "token_cost_label": policy.get("token_cost_label"),
            "max_active": policy.get("max_active"),
            "max_auto_resumes_per_mission": policy.get("max_auto_resumes_per_mission"),
            "max_auto_resumes_per_tick": policy.get("max_auto_resumes_per_tick"),
            "poll_seconds": policy.get("poll_seconds"),
            "allow_dirty": policy.get("allow_dirty"),
            "merge": policy.get("merge"),
            "race": policy.get("race"),
            "self_heal_pytest": policy.get("self_heal_pytest"),
            "max_self_heal_attempts": policy.get("max_self_heal_attempts"),
        },
        "reconcile": reconcile_summary,
        "auto_resume_enabled": resume_enabled,
        "pytest": pytest_info,
        "stop_requested": stop_requested,
    }


def maybe_split_goal(goal: str, *, enabled: bool = True) -> list[str]:
    """Cheap heuristic: turn 'A and B and C' into parallel-ish subgoals.

    Wild path only — not a full planner. Keeps each piece actionable.
    """
    text = str(goal or "").strip()
    if not enabled or not text:
        return [text] if text else []
    if len(text) < 24:
        return [text]
    # Split on Chinese/English conjunctions and numbered lists.
    import re

    parts = re.split(
        r"(?:；|;|，|,|\n|\r|\band then\b|\bthen\b|然后|并且|同时|另外|以及|、|再|接着)",
        text,
        flags=re.I,
    )
    cleaned = [p.strip(" -\t，,。.") for p in parts if p and len(p.strip(" -\t，,。.")) >= 6]
    if len(cleaned) <= 1:
        return [text]
    # Cap explosion
    return cleaned[:6]


def wait_for_agent_liveness(
    agent: str,
    *,
    timeout_seconds: float = 3600.0,
    poll_seconds: float = 90.0,
    stop_check: Any = None,
    log: Any = None,
) -> dict[str, Any]:
    """Block until agent can spend tokens again (quota wake / login recovery)."""
    agent_name = normalize_agent_name(agent)
    deadline = time.time() + max(30.0, float(timeout_seconds))
    poll = max(15.0, float(poll_seconds))
    last: dict[str, Any] = {}
    while time.time() < deadline:
        if callable(stop_check) and stop_check():
            return {**last, "ok": False, "stop_reason": "stop_requested", "woke": False}
        last = probe_worker_agent_liveness(agent_name, use_cache=False)
        if last.get("ok"):
            if callable(log):
                log(f"WAKE liveness restored agent={agent_name}")
            return {**last, "woke": True}
        if callable(log):
            log(
                f"WAKE_WAIT agent={agent_name} reason={last.get('stop_reason')} "
                f"sleep={poll}s"
            )
        time.sleep(min(poll, max(5.0, deadline - time.time())))
    return {**(last or {}), "ok": False, "woke": False, "stop_reason": last.get("stop_reason") or "wake_timeout"}


def abort_hosted_mission(
    *,
    workspace_root: str | Path,
    mission_id: str,
    reason: str = "race_lost",
    message: str = "",
) -> dict[str, Any]:
    """Hard-stop a background mission (kill worker PID + mark stopped).

    Used by race loser abort and feral priority preemption. Aggressive by design.
    """
    from .chief_background import (
        load_background_record,
        process_belongs_to_mission,
        process_status,
        save_background_record,
        terminate_process,
    )
    from .missions import save_mission
    from .mission_progress import save_mission_progress

    ws = Path(workspace_root).expanduser().resolve()
    mid = str(mission_id or "").strip()
    if not mid:
        return {"status": "skipped", "stop_reason": "missing_mission"}
    mission = load_mission(ws, mid)
    if mission is None:
        return {"status": "skipped", "stop_reason": "missing_mission", "mission_id": mid}
    bg = load_background_record(ws, mid) or {}
    pid = int(bg.get("worker_pid") or bg.get("pid") or 0)
    if str(mission.get("status") or "") in {"verified", "merged"} or str(
        mission.get("stop_reason") or ""
    ) == "verified":
        return {
            "status": "skipped",
            "stop_reason": "already_verified",
            "mission_id": mid,
            "pid": pid,
        }

    killed = False
    if pid > 0:
        process = process_status(pid)
        if process.get("alive") and not process_belongs_to_mission(
            pid,
            mid,
            record=bg,
            allow_unreadable=False,
            require_worker_marker=True,
        ):
            append_host_log(ws, f"ABORT_BLOCKED mission={mid} reason=ownership pid={pid}")
            return {
                "status": "blocked",
                "stop_reason": "process_ownership_unverified",
                "mission_id": mid,
                "pid": pid,
                "message": "Refused to terminate a PID that is not proven to belong to this mission.",
            }
        if process.get("alive"):
            try:
                killed = bool(terminate_process(pid))
            except Exception:
                killed = False
            if not killed:
                append_host_log(ws, f"ABORT_FAILED mission={mid} reason=terminate_failed pid={pid}")
                return {
                    "status": "blocked",
                    "stop_reason": "abort_failed",
                    "mission_id": mid,
                    "pid": pid,
                    "killed": False,
                    "message": "Worker termination failed; PID and mission state were preserved.",
                }
            if not _wait_for_process_exit(pid, process_status):
                append_host_log(ws, f"ABORT_FAILED mission={mid} reason=exit_unconfirmed pid={pid}")
                return {
                    "status": "blocked",
                    "stop_reason": "abort_unconfirmed",
                    "mission_id": mid,
                    "pid": pid,
                    "killed": True,
                    "message": "Termination was requested, but process exit could not be confirmed.",
                }
    now = utc_now()
    mission["status"] = "stopped"
    mission["stop_reason"] = reason
    mission["aborted_at"] = now
    mission["abort_message"] = message or reason
    save_mission(ws, mission)
    bg = dict(bg)
    bg.update(
        {
            "status": "aborted",
            "alive": False,
            "abort_reason": reason,
            "completed_at": now,
            "pid": 0,
            "worker_pid": 0,
        }
    )
    save_background_record(ws, mid, bg)
    try:
        save_mission_progress(
            ws,
            mid,
            stage="blocked",
            stage_label=f"Aborted: {reason}",
            status="stopped",
            blocker=reason,
            last_activity_at=now,
        )
    except Exception:
        pass
    append_host_log(ws, f"ABORT mission={mid} reason={reason} killed={killed} pid={pid}")
    return {
        "status": "aborted",
        "stop_reason": reason,
        "mission_id": mid,
        "pid": pid,
        "killed": killed,
        "message": message or reason,
    }


def _wait_for_process_exit(pid: int, probe: Any, *, timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        if not probe(pid).get("alive"):
            return True
        time.sleep(0.05)
    return not probe(pid).get("alive")


def _mission_is_terminal_win(workspace_root: Path, mission_id: str) -> bool:
    mission = load_mission(workspace_root, mission_id)
    if not mission:
        return False
    status = str(mission.get("status") or "")
    stop = str(mission.get("stop_reason") or "")
    return status in {"verified", "merged"} or stop == "verified"


def _mission_is_dead(workspace_root: Path, mission_id: str) -> bool:
    mission = load_mission(workspace_root, mission_id)
    if not mission:
        return True
    status = str(mission.get("status") or "")
    stop = str(mission.get("stop_reason") or "")
    if status in {"verified", "merged", "stopped", "preview"}:
        return True
    if stop in {"verified", "worker_orphaned", "quota_exhausted", "race_lost", "preempted"}:
        return True
    return False


def settle_race(
    *,
    workspace_root: str | Path,
    legs: list[dict[str, Any]],
    poll_seconds: float = 12.0,
    timeout_seconds: float = 3600.0,
    abort_losers: bool = True,
    stop_check: Any = None,
    log: Any = None,
) -> dict[str, Any]:
    """Wait until one race leg verifies; optionally hard-abort the rest.

    Winner = first mission that reaches verified. Losers get ``race_lost`` + kill.
    """
    deadline = time.time() + max(30.0, float(timeout_seconds))
    poll = max(3.0, float(poll_seconds))
    latest: dict[str, Any] = {
        "schema_version": 1,
        "status": "race_unsettled",
        "winner": None,
        "aborted": [],
        "legs": legs,
    }

    while time.time() < deadline:
        if callable(stop_check) and stop_check():
            break
        latest = poll_race(
            workspace_root=workspace_root,
            legs=legs,
            abort_losers=abort_losers,
            log=log,
        )
        if latest.get("status") != "race_running":
            return latest
        if callable(log):
            log(f"RACE_WAIT legs={len(legs)} poll={poll}s")
        time.sleep(min(poll, max(1.0, deadline - time.time())))

    return {**latest, "status": "race_unsettled"}


def poll_race(
    *,
    workspace_root: str | Path,
    legs: list[dict[str, Any]],
    abort_losers: bool = True,
    log: Any = None,
) -> dict[str, Any]:
    """Inspect one race without sleeping; active host loops call this once per tick."""
    ws = Path(workspace_root).expanduser().resolve()
    active = [
        leg
        for leg in legs
        if isinstance(leg, dict)
        and str(leg.get("mission_id") or "").strip()
        and str(leg.get("status") or "") in {"background_started", "running", "race_started"}
    ]
    if not active:
        active = [leg for leg in legs if isinstance(leg, dict) and leg.get("mission_id")]
    winner = next(
        (
            {**leg, "won_at": utc_now()}
            for leg in active
            if _mission_is_terminal_win(ws, str(leg.get("mission_id") or ""))
        ),
        None,
    )
    aborted: list[dict[str, Any]] = []
    if winner and abort_losers:
        win_id = str(winner.get("mission_id") or "")
        for leg in active:
            mid = str(leg.get("mission_id") or "")
            if not mid or mid == win_id:
                continue
            if _mission_is_terminal_win(ws, mid):
                continue  # rare double-win: leave it
            aborted.append(
                abort_hosted_mission(
                    workspace_root=ws,
                    mission_id=mid,
                    reason="race_lost",
                    message=f"Race lost to {win_id}",
                )
            )
            if callable(log):
                log(f"RACE_ABORT loser={mid} winner={win_id}")
    all_dead = bool(active) and all(_mission_is_dead(ws, str(leg.get("mission_id") or "")) for leg in active)
    return {
        "schema_version": 1,
        "status": "race_won" if winner else "race_unsettled" if all_dead or not active else "race_running",
        "winner": winner,
        "aborted": aborted,
        "legs": legs,
    }


def race_hosted_goal(
    *,
    workspace_root: str | Path,
    repo_root: str | Path,
    goal: str,
    agents: list[str] | None = None,
    test_command: str | None = None,
    allow_dirty: bool = True,
    allow_test_edits: bool = True,
    merge: bool = False,
    abort_losers: bool = True,
    settle: bool = True,
    settle_timeout_seconds: float = 2400.0,
    settle_poll_seconds: float = 12.0,
    stop_check: Any = None,
    log: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fire the same goal at two agents; first verified wins, losers die.

    Aggressive by default when *abort_losers* and *settle* are true.
    """
    agent_list = agents or ["codex", "claude-code"]
    results: list[dict[str, Any]] = []
    race_id = uuid4().hex[:8]
    for agent in agent_list[:2]:
        name = normalize_agent_name(agent)
        probe = probe_worker_agent_liveness(name)
        if not probe.get("ok"):
            results.append(
                {
                    "status": "skipped",
                    "stop_reason": probe.get("stop_reason"),
                    "agent": name,
                    "goal": goal,
                    "message": probe.get("message"),
                    "race_id": race_id,
                }
            )
            continue
        launched = start_hosted_goal(
            workspace_root=workspace_root,
            repo_root=repo_root,
            goal=f"[race:{race_id}:{name}] {goal}",
            agent=name,
            test_command=test_command,
            allow_dirty=allow_dirty,
            allow_test_edits=allow_test_edits,
            merge=merge,
            require_liveness=False,
            **{k: v for k, v in kwargs.items() if k in {
                "max_rounds",
                "max_repair_rounds",
                "max_wall_minutes",
                "max_worker_minutes",
                "reasoning_effort",
                "model_policy",
                "execution_policy",
            }},
        )
        launched = dict(launched)
        launched["agent"] = name
        launched["race_id"] = race_id
        # Tag mission for priority/race bookkeeping
        mid = str(launched.get("mission_id") or "")
        if mid:
            mission = load_mission(workspace_root, mid)
            if mission is not None:
                mission["race_id"] = race_id
                mission["race_agent"] = name
                mission["host_priority"] = int(mission.get("host_priority") or 0)
                from .missions import save_mission

                save_mission(workspace_root, mission)
        results.append(launched)
    started = [r for r in results if str(r.get("status") or "") in {"background_started", "running"}]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "race_started" if started else "blocked",
        "stop_reason": "" if started else "race_no_agent",
        "goal": goal,
        "race_id": race_id,
        "results": results,
        "started_count": len(started),
        "abort_losers": bool(abort_losers),
    }
    if started and settle and abort_losers:
        settlement = settle_race(
            workspace_root=workspace_root,
            legs=started,
            poll_seconds=settle_poll_seconds,
            timeout_seconds=settle_timeout_seconds,
            abort_losers=True,
            stop_check=stop_check,
            log=log,
        )
        payload["settlement"] = settlement
        payload["status"] = settlement.get("status") or payload["status"]
        if settlement.get("winner"):
            payload["winner_mission_id"] = settlement["winner"].get("mission_id")
    return payload


def maybe_self_heal_pytest(
    *,
    workspace_root: str | Path,
    repo_root: str | Path,
    agent: str = "codex",
    test_command: str | None = None,
    python: str | None = None,
    allow_dirty: bool = True,
    priority: int = 100,
    preempt_non_priority: bool = False,
) -> dict[str, Any] | None:
    """If main pytest is red, spawn a high-priority fix mission. Wild mode only.

    When *preempt_non_priority* is true, abort non-priority running missions so
    the heal job jumps the queue (feral).
    """
    repo = Path(repo_root).expanduser().resolve()
    ws = Path(workspace_root).expanduser().resolve()
    probe = _run_pytest_probe(repo, python=python)
    if probe.get("ok"):
        return None
    # Already have a priority heal running?
    for row in list_missions(ws):
        obj = str(row.get("objective") or "")
        status = str(row.get("status") or "")
        if "[priority:heal]" in obj and status in {"running", "background_running", "created"}:
            return {
                "status": "skipped",
                "stop_reason": "heal_already_running",
                "mission_id": row.get("mission_id"),
                "goal": obj[:80],
            }
    if preempt_non_priority:
        for row in list_missions(ws):
            status = str(row.get("status") or "")
            if status not in {"running", "background_running", "created"}:
                continue
            mid = str(row.get("mission_id") or "")
            mission = load_mission(ws, mid) if mid else None
            pri = int((mission or {}).get("host_priority") or 0)
            if pri >= priority:
                continue
            abort_hosted_mission(
                workspace_root=ws,
                mission_id=mid,
                reason="preempted",
                message="Preempted by pytest self-heal priority job",
            )
    tail = str(probe.get("tail") or "")[:500]
    goal = (
        "[priority:heal] Main-branch tests are currently RED. This is a priority interrupt. "
        "Diagnose from pytest output and make the minimal fixes so pytest -q is green. "
        "Do not expand scope.\n"
        f"Recent pytest tail:\n{tail}"
    )
    launched = start_hosted_goal(
        workspace_root=workspace_root,
        repo_root=repo_root,
        goal=goal,
        agent=agent,
        test_command=test_command or f"{python or sys.executable} -m pytest -q",
        allow_dirty=allow_dirty,
        allow_test_edits=True,
        merge=False,
        require_liveness=True,
    )
    mid = str(launched.get("mission_id") or "")
    if mid:
        mission = load_mission(ws, mid)
        if mission is not None:
            mission["host_priority"] = int(priority)
            mission["host_kind"] = "self_heal"
            from .missions import save_mission

            save_mission(ws, mission)
    launched = dict(launched)
    launched["host_priority"] = int(priority)
    launched["self_heal"] = True
    return launched


def self_heal_probe_allowed(
    *,
    attempts: int,
    max_attempts: int,
    last_probe_at: float | None,
    interval_seconds: float,
    now: float | None = None,
) -> bool:
    if max_attempts <= 0 or attempts >= max_attempts:
        return False
    if last_probe_at is None:
        return True
    current = time.monotonic() if now is None else float(now)
    return current - last_probe_at >= max(0.0, float(interval_seconds))


def priority_heal_is_active(workspace_root: str | Path) -> bool:
    """True when a high-priority heal mission is still in flight."""
    ws = Path(workspace_root).expanduser().resolve()
    for row in list_missions(ws):
        obj = str(row.get("objective") or "")
        status = str(row.get("status") or "")
        if "[priority:heal]" not in obj:
            continue
        if status in {"running", "background_running", "created"}:
            return True
        mid = str(row.get("mission_id") or "")
        mission = load_mission(ws, mid) if mid else None
        if mission and int(mission.get("host_priority") or 0) >= 100:
            if str(mission.get("status") or "") in {"running", "background_running", "created"}:
                return True
    return False


def host_dashboard_to_markdown(dashboard: dict[str, Any]) -> str:
    live = dashboard.get("provider_liveness") if isinstance(dashboard.get("provider_liveness"), dict) else {}
    missions = dashboard.get("missions") if isinstance(dashboard.get("missions"), dict) else {}
    session = dashboard.get("session") if isinstance(dashboard.get("session"), dict) else {}
    last = dashboard.get("last_success") if isinstance(dashboard.get("last_success"), dict) else {}
    journey = dashboard.get("latest_journey") if isinstance(dashboard.get("latest_journey"), dict) else {}
    recon = dashboard.get("reconcile") if isinstance(dashboard.get("reconcile"), dict) else {}
    pytest_info = dashboard.get("pytest") if isinstance(dashboard.get("pytest"), dict) else None
    ready = "可以托管" if dashboard.get("ready_for_host") else "先别托管"
    mode = dashboard.get("mode") or "economy"
    cost = dashboard.get("token_cost_label") or "省额度"
    lines = [
        "# Pacer 托管仪表",
        "",
        f"- 检查时间：`{dashboard.get('checked_at')}`",
        f"- 结论：**{ready}**",
        f"- 模式：`{mode}` · 额度消耗：**{cost}**（默认 economy 省额度；unleash/race/yolo 需显式开启）",
        f"- 助手：`{dashboard.get('agent')}` · liveness=`{live.get('ok')}` · `{live.get('stop_reason') or 'ok'}`",
        f"- 任务：verified={missions.get('verified')} running={missions.get('running')} "
        f"stopped={missions.get('stopped')} orphan停={missions.get('orphaned_stop')} total={missions.get('total')}",
        f"- 会话：`{session.get('status') or 'idle'}` "
        f"已发={session.get('launched')} 目标进度={session.get('goals_done_index')}/{session.get('goals_total')}",
        f"- 策略：并发={dashboard.get('policy', {}).get('max_active')} "
        f"auto_resume/任务={dashboard.get('policy', {}).get('max_auto_resumes_per_mission')} "
        f"每轮最多={dashboard.get('policy', {}).get('max_auto_resumes_per_tick')}",
        f"- 本轮 reconcile：{recon.get('count')} · auto_resume 成功={recon.get('auto_resumed')}",
    ]
    if dashboard.get("blockers"):
        lines.append(f"- 阻塞：`{', '.join(str(b) for b in dashboard.get('blockers') or [])}`")
    if live.get("message") and not live.get("ok"):
        lines.append(f"- 供应说明：{live.get('message')}")
    if last:
        lines.append(
            f"- 最近成功：`{last.get('mission_id')}` @ {last.get('last_activity_at')} — {last.get('objective')}"
        )
    if journey:
        lines.append(f"- 最近闭环：`{journey.get('status')}` · {journey.get('summary')}")
    memory = dashboard.get("memory_fallback") if isinstance(dashboard.get("memory_fallback"), dict) else {}
    if memory:
        lines.append(
            f"- 本地记忆兜底：`{memory.get('status') or 'unknown'}` · "
            f"选中={memory.get('selected_entries') or 0} · 交接={memory.get('dispatch_injected') is True}"
        )
    if pytest_info is not None:
        mark = "绿" if pytest_info.get("ok") else "红"
        lines.append(f"- 主仓 pytest：{mark} (exit={pytest_info.get('exit_code')})")
        if pytest_info.get("tail"):
            lines.append(f"  `{str(pytest_info.get('tail'))[:200]}`")
    lines.extend(
        [
            "",
            "## 你可以怎么做",
            "",
            "- 省额度托管（默认）：`pacer host run --goal \"...\" --execute`",
            "- 均衡：`pacer host run --mode standard --goal \"...\" --hours 2 --execute`",
            "- 吃额度换效率：`pacer host unleash --goal \"...\" --hours 3 --execute`",
            "- 竞速（最吃额度）：`pacer host run --mode race --goal \"...\" --execute`",
            "- Claude Code 全权限：`pacer host yolo --goal \"...\" --execute`",
            "- 只看仪表：`pacer host status` · 停：`pacer host stop` · 预检：`pacer host doctor`",
            "",
        ]
    )
    if live.get("ok") is False and str(live.get("stop_reason") or "") == "quota_exhausted":
        lines.append("> 额度/账号限制中：这不是项目坏了。等额度恢复或换账号后再 `host run`。")
    if live.get("ok") is False and str(live.get("stop_reason") or "") == "not_authenticated":
        lines.append("> 助手未登录：先 `codex login`，再 `pacer host doctor`。")
    return "\n".join(lines)


def start_hosted_goal(
    *,
    workspace_root: str | Path,
    repo_root: str | Path,
    goal: str,
    agent: str = "codex",
    test_command: str | None = None,
    allow_dirty: bool = True,
    allow_test_edits: bool = False,
    merge: bool = False,
    max_rounds: int = 3,
    max_repair_rounds: int = 1,
    max_wall_minutes: int = 50,
    max_worker_minutes: int = 35,
    reasoning_effort: str | None = None,
    model_policy: dict[str, Any] | None = None,
    execution_policy: dict[str, Any] | None = None,
    require_liveness: bool = True,
) -> dict[str, Any]:
    """Preview+background-start one goal under host policy."""
    from .chief_background import start_background_chief_run
    from .chief_run import run_chief_mission

    ws = Path(workspace_root).expanduser().resolve()
    repo = Path(repo_root).expanduser().resolve()
    agent_name = normalize_agent_name(agent)
    capability = _host_agent_capability(agent_name)
    if not capability.get("ok"):
        return {
            "schema_version": 1,
            "status": "blocked",
            "stop_reason": capability.get("stop_reason") or "agent_unsupported",
            "message": capability.get("message") or "Agent cannot run hosted implementation.",
            "agent_capability": capability,
            "goal": goal,
        }
    if require_liveness:
        probe = probe_worker_agent_liveness(agent_name)
        if not probe.get("ok"):
            return {
                "schema_version": 1,
                "status": "blocked",
                "stop_reason": probe.get("stop_reason") or "agent_unavailable",
                "message": probe.get("message") or "Agent not available",
                "provider_liveness": probe,
                "goal": goal,
            }

    py = sys.executable
    test_cmd = test_command or f"{py} -m pytest -q"
    preview = run_chief_mission(
        goal=goal,
        workspace_root=ws,
        repo_root=repo,
        agents=(agent_name,),
        execute=False,
        dry_run=True,
        allow_dirty=allow_dirty,
        test_command=test_cmd,
        allow_test_edits=allow_test_edits,
        merge=merge,
        max_rounds=max_rounds,
        max_repair_rounds=max_repair_rounds,
        max_wall_minutes=max_wall_minutes,
        max_worker_minutes=max_worker_minutes,
        reasoning_effort=reasoning_effort,
        model_policy=model_policy,
        execution_policy=execution_policy,
    )
    if str(preview.get("status") or "") != "preview":
        return {
            "schema_version": 1,
            "status": str(preview.get("status") or "blocked"),
            "stop_reason": str(preview.get("stop_reason") or "preview_failed"),
            "message": "Could not create mission preview.",
            "preview": preview,
            "goal": goal,
        }
    mission_id = str((preview.get("mission") or {}).get("mission_id") or "")
    if not mission_id:
        return {
            "schema_version": 1,
            "status": "blocked",
            "stop_reason": "missing_mission",
            "message": "Preview did not allocate a mission id.",
            "preview": preview,
            "goal": goal,
        }
    started = start_background_chief_run(
        workspace_root=ws,
        mission_id=mission_id,
        agents=(agent_name,),
        allow_dirty=allow_dirty,
        test_command=test_cmd,
        allow_test_edits=allow_test_edits,
        merge=merge,
        skip_liveness_probe=False,
    )
    return {
        "schema_version": 1,
        "status": str(started.get("status") or ""),
        "stop_reason": str(started.get("stop_reason") or ""),
        "message": str(started.get("message") or ""),
        "mission_id": mission_id,
        "goal": goal,
        "agent": agent_name,
        "reasoning_effort": str(reasoning_effort or ""),
        "model_policy": dict(model_policy) if isinstance(model_policy, dict) else {},
        "execution_policy": dict(execution_policy) if isinstance(execution_policy, dict) else {},
        "background": started.get("background"),
        "preview": preview,
        "started": started,
    }


def run_host_session(
    *,
    workspace_root: str | Path,
    repo_root: str | Path,
    goals: list[str],
    hours: float = 2.0,
    agent: str = "codex",
    test_command: str | None = None,
    poll_seconds: float | None = None,
    allow_dirty: bool = True,
    allow_test_edits: bool = False,
    merge: bool = False,
    max_active: int | None = None,
    stagger_seconds: float = 3.0,
    python: str | None = None,
    unleash: bool = False,
    race: bool = False,
    mode: str | None = None,
    wake_on_quota: bool | None = None,
    self_heal_pytest: bool | None = None,
    auto_split_goals: bool | None = None,
    reasoning_effort: str | None = None,
    model_policy: dict[str, Any] | None = None,
    execution_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Long-host loop: launch goals, reconcile, respect stop/liveness/hours.

    Default mode is **economy** (save tokens). Pass mode=unleash/race/yolo
    or flags for expensive paths.
    """
    ws = Path(workspace_root).expanduser().resolve()
    repo = Path(repo_root).expanduser().resolve()
    resolved_mode = normalize_host_mode(mode, unleash_flag=unleash, race_flag=race)
    policy = load_host_policy(ws, mode=resolved_mode, unleash=unleash, race=race)
    agent_name = normalize_agent_name(agent or policy.get("agent") or "codex")
    poll = float(poll_seconds if poll_seconds is not None else policy.get("poll_seconds") or 90)
    hours = max(0.1, float(hours))
    deadline = time.time() + hours * 3600
    clear_host_stop(ws)

    do_wake = policy.get("wake_on_quota") if wake_on_quota is None else wake_on_quota
    do_heal = policy.get("self_heal_pytest") if self_heal_pytest is None else self_heal_pytest
    do_split = policy.get("auto_split_goals") if auto_split_goals is None else auto_split_goals
    do_race = bool(race or policy.get("race") or resolved_mode == "race")
    allow_dirty = bool(allow_dirty if allow_dirty is not None else policy.get("allow_dirty"))
    # Do not force expensive flags from unleash when user chose economy/standard
    allow_test_edits = bool(allow_test_edits if allow_test_edits else policy.get("allow_test_edits"))
    merge = bool(merge if merge else policy.get("merge"))
    max_active = int(max_active or policy.get("max_active") or 1)
    effective_reasoning_effort = str(reasoning_effort or policy.get("reasoning_effort") or "inherit").strip() or "inherit"
    effective_model_policy = (
        dict(model_policy)
        if isinstance(model_policy, dict)
        else dict(policy.get("model_policy") or {})
        if isinstance(policy.get("model_policy"), dict)
        else None
    )
    effective_execution_policy = (
        dict(execution_policy)
        if isinstance(execution_policy, dict)
        else dict(policy.get("execution_policy") or {})
        if isinstance(policy.get("execution_policy"), dict)
        else None
    )
    max_heal_attempts = max(0, int(policy.get("max_self_heal_attempts") or 0))
    if self_heal_pytest is True and max_heal_attempts == 0:
        max_heal_attempts = 1
    heal_probe_interval = max(0.0, float(policy.get("self_heal_probe_interval_seconds") or 0.0))
    unleash = resolved_mode in {"unleash", "race", "yolo"}

    # Expand goals (wild split)
    expanded: list[str] = []
    for g in goals:
        expanded.extend(maybe_split_goal(str(g), enabled=bool(do_split)))
    if not expanded:
        expanded = [str(g) for g in goals if str(g).strip()]

    session = {
        "schema_version": 1,
        "session_id": f"host-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}",
        "status": "running",
        "mode": resolved_mode,
        "token_cost": policy.get("token_cost"),
        "token_cost_label": policy.get("token_cost_label"),
        "started_at": utc_now(),
        "hours": hours,
        "agent": agent_name,
        "goals": list(expanded),
        "goal_index": 0,
        "launched_count": 0,
        "launched_mission_ids": [],
        "events": [],
        "wild": {
            "wake_on_quota": bool(do_wake),
            "self_heal_pytest": bool(do_heal),
            "max_self_heal_attempts": max_heal_attempts,
            "auto_split_goals": bool(do_split),
            "race": bool(do_race),
            "merge": bool(merge),
            "max_active": int(max_active),
        },
        "routing": {
            "reasoning_effort": effective_reasoning_effort,
            "model_policy": effective_model_policy or {},
            "execution_policy": effective_execution_policy or {},
        },
        "self_heal_attempts": 0,
        "pending_races": [],
        "race_outcomes": [],
        # Recorded so a second host on the same workspace can be refused rather
        # than silently doubling the spend and interleaving writes into this file.
        "pid": os.getpid(),
    }
    existing_owner = _live_host_owner(ws)
    if existing_owner:
        append_host_log(ws, f"HOST_REFUSED another host already owns this workspace pid={existing_owner}")
        return {
            "status": "blocked",
            "stop_reason": "host_already_running",
            "existing_pid": existing_owner,
            "workspace_root": str(ws),
            "message": (
                f"这个工作区已经有一个托管进程在跑（PID {existing_owner}）。"
                "两个 host 会各自派工、重复烧额度，并且互相覆盖 session.json。"
                "先 `pacer host stop`，或者等它结束。"
            ),
        }
    save_host_session(ws, session)
    append_host_log(
        ws,
        f"HOST_START mode={session['mode']} cost={session.get('token_cost')} "
        f"hours={hours} goals={len(expanded)} agent={agent_name} "
        f"active={max_active} wake={do_wake} race={do_race} heal={do_heal}",
    )

    # Apply policy auto-resume max into env for maybe_auto_resume consumers
    os.environ["PACER_AUTO_RESUME_MAX"] = str(int(policy.get("max_auto_resumes_per_mission") or 2))

    launches: list[dict[str, Any]] = []
    pending_races: list[dict[str, Any]] = []
    heal_attempts = 0
    last_heal_probe_at: float | None = None
    while time.time() < deadline:
        if host_stop_requested(ws):
            append_host_log(ws, "HOST_STOP flag seen")
            break

        # Dashboard tick = reconcile + liveness
        dash = build_host_dashboard(
            workspace_root=ws,
            repo_root=repo,
            agent=agent_name,
            run_pytest=False,
            python=python,
            auto_resume=True,
        )
        live = dash.get("provider_liveness") if isinstance(dash.get("provider_liveness"), dict) else {}
        running = int((dash.get("missions") or {}).get("running") or 0)
        remaining_races: list[dict[str, Any]] = []
        for race_state in pending_races:
            outcome = poll_race(
                workspace_root=ws,
                legs=list(race_state.get("legs") or []),
                abort_losers=bool(race_state.get("abort_losers", True)),
                log=lambda m: append_host_log(ws, m),
            )
            if outcome.get("status") == "race_running":
                remaining_races.append(race_state)
                continue
            outcome = {**outcome, "race_id": race_state.get("race_id"), "goal": race_state.get("goal")}
            session.setdefault("race_outcomes", []).append(outcome)
            append_host_log(
                ws,
                f"RACE_SETTLED id={race_state.get('race_id')} status={outcome.get('status')} "
                f"winner={((outcome.get('winner') or {}).get('mission_id'))}",
            )
        if len(remaining_races) != len(pending_races):
            running = int(_mission_counts(ws).get("running") or 0)
        pending_races = remaining_races
        session["pending_races"] = list(pending_races)
        append_host_log(
            ws,
            f"TICK ready={dash.get('ready_for_host')} running={running} "
            f"verified={(dash.get('missions') or {}).get('verified')} "
            f"liveness={live.get('ok')} recon={((dash.get('reconcile') or {}).get('count'))}",
        )

        # Wild: if supply is dead but time remains, sleep until wake instead of exiting.
        if not live.get("ok") and do_wake and time.time() < deadline:
            reason = str(live.get("stop_reason") or "")
            if reason in {"quota_exhausted", "not_authenticated", "agent_unavailable", "provider_5xx"}:
                append_host_log(ws, f"WAKE_MODE waiting reason={reason}")
                wake = wait_for_agent_liveness(
                    agent_name,
                    timeout_seconds=min(deadline - time.time(), 6 * 3600),
                    poll_seconds=float(policy.get("wake_poll_seconds") or 90),
                    stop_check=lambda: host_stop_requested(ws),
                    log=lambda m: append_host_log(ws, m),
                )
                live = wake
                if not wake.get("ok"):
                    append_host_log(ws, f"WAKE_GAVE_UP reason={wake.get('stop_reason')}")
                    # Keep looping until hours end if stop not requested — another chance
                    if host_stop_requested(ws):
                        break
                    time.sleep(min(poll, 60))
                    continue

        # Wild self-heal: pytest red → priority interrupt (can preempt others)
        heal_active = priority_heal_is_active(ws)
        if (
            do_heal
            and live.get("ok")
            and not heal_active
            and self_heal_probe_allowed(
                attempts=heal_attempts,
                max_attempts=max_heal_attempts,
                last_probe_at=last_heal_probe_at,
                interval_seconds=heal_probe_interval,
            )
        ):
            last_heal_probe_at = time.monotonic()
            preempt = bool(policy.get("self_heal_preempt") or unleash or policy.get("unleash"))
            heal = maybe_self_heal_pytest(
                workspace_root=ws,
                repo_root=repo,
                agent=agent_name,
                test_command=test_command,
                python=python,
                allow_dirty=allow_dirty,
                priority=100,
                preempt_non_priority=preempt,
            )
            if heal is not None and str(heal.get("status") or "") not in {"skipped", ""}:
                heal_attempts += 1
                session["self_heal_attempts"] = heal_attempts
                launches.append({**heal, "self_heal": True})
                session["launched_count"] = int(session.get("launched_count") or 0) + 1
                mid = str(heal.get("mission_id") or "")
                if mid:
                    ids = list(session.get("launched_mission_ids") or [])
                    ids.append(mid)
                    session["launched_mission_ids"] = ids
                append_host_log(
                    ws,
                    f"SELF_HEAL_PRIORITY status={heal.get('status')} mission={mid} preempt={preempt}",
                )
                running += 1
                heal_active = True

        # Launch next goals while under concurrency cap and liveness ok.
        # If a priority heal is active, hold the queue (插队).
        idx = int(session.get("goal_index") or 0)
        while (
            idx < len(expanded)
            and running < max(1, int(max_active))
            and live.get("ok")
            and time.time() < deadline
            and not host_stop_requested(ws)
            and not heal_active
        ):
            goal = str(expanded[idx] or "").strip()
            idx += 1
            session["goal_index"] = idx
            if not goal:
                continue
            if do_race:
                result = race_hosted_goal(
                    workspace_root=ws,
                    repo_root=repo,
                    goal=goal,
                    agents=list(policy.get("race_agents") or ["codex", "claude-code"]),
                    test_command=test_command,
                    allow_dirty=allow_dirty,
                    allow_test_edits=allow_test_edits,
                    merge=merge,
                    abort_losers=bool(policy.get("race_abort_losers", True)),
                    settle=False,
                    log=lambda m: append_host_log(ws, m),
                    max_rounds=int(policy.get("max_rounds") or 3),
                    max_repair_rounds=int(policy.get("max_repair_rounds") or 1),
                    max_wall_minutes=int(policy.get("max_wall_minutes") or 50),
                    max_worker_minutes=int(policy.get("max_worker_minutes") or 35),
                    reasoning_effort=effective_reasoning_effort,
                    model_policy=effective_model_policy,
                    execution_policy=effective_execution_policy,
                )
                # Count each race leg as launch
                for leg in result.get("results") or []:
                    if isinstance(leg, dict):
                        launches.append(leg)
                        mid = str(leg.get("mission_id") or "")
                        if mid:
                            ids = list(session.get("launched_mission_ids") or [])
                            ids.append(mid)
                            session["launched_mission_ids"] = ids
                session["launched_count"] = int(session.get("launched_count") or 0) + int(
                    result.get("started_count") or 0
                )
                started_legs = [
                    {
                        "mission_id": leg.get("mission_id"),
                        "status": leg.get("status"),
                        "agent": leg.get("agent"),
                        "race_id": leg.get("race_id"),
                    }
                    for leg in result.get("results") or []
                    if isinstance(leg, dict)
                    and str(leg.get("status") or "") in {"background_started", "running"}
                ]
                if started_legs:
                    pending_races.append(
                        {
                            "race_id": result.get("race_id"),
                            "goal": goal,
                            "legs": started_legs,
                            "abort_losers": bool(policy.get("race_abort_losers", True)),
                        }
                    )
                    session["pending_races"] = list(pending_races)
                running = int(_mission_counts(ws).get("running") or 0)
                append_host_log(
                    ws,
                    f"RACE_ASYNC status={result.get('status')} started={result.get('started_count')} "
                    f"race={result.get('race_id')} goal={goal[:80]}",
                )
            else:
                result = start_hosted_goal(
                    workspace_root=ws,
                    repo_root=repo,
                    goal=goal,
                    agent=agent_name,
                    test_command=test_command,
                    allow_dirty=allow_dirty,
                    allow_test_edits=allow_test_edits,
                    merge=merge,
                    max_rounds=int(policy.get("max_rounds") or 3),
                    max_repair_rounds=int(policy.get("max_repair_rounds") or 1),
                    max_wall_minutes=int(policy.get("max_wall_minutes") or 50),
                    max_worker_minutes=int(policy.get("max_worker_minutes") or 35),
                    reasoning_effort=effective_reasoning_effort,
                    model_policy=effective_model_policy,
                    execution_policy=effective_execution_policy,
                    require_liveness=True,
                )
                launches.append(result)
                session["launched_count"] = int(session.get("launched_count") or 0) + 1
                mid = str(result.get("mission_id") or "")
                if mid:
                    ids = list(session.get("launched_mission_ids") or [])
                    ids.append(mid)
                    session["launched_mission_ids"] = ids
                append_host_log(
                    ws,
                    f"LAUNCH status={result.get('status')} stop={result.get('stop_reason')} "
                    f"mission={mid} goal={goal[:80]}",
                )
                if str(result.get("status") or "") not in {"background_started", "running"}:
                    if str(result.get("stop_reason") or "") in {
                        "quota_exhausted",
                        "not_authenticated",
                        "agent_unavailable",
                    }:
                        append_host_log(ws, "LAUNCH_BLOCKED supply issue")
                        live = {"ok": False, "stop_reason": result.get("stop_reason")}
                        break
                else:
                    running += 1
            save_host_session(ws, session)
            time.sleep(max(0.5, float(stagger_seconds)))

        session["goal_index"] = idx
        session["last_tick_at"] = utc_now()
        save_host_session(ws, session)

        # Exit early if all goals launched and nothing running
        if idx >= len(expanded) and running == 0 and not pending_races:
            # One more self-heal chance at drain
            if (
                do_heal
                and live.get("ok")
                and self_heal_probe_allowed(
                    attempts=heal_attempts,
                    max_attempts=max_heal_attempts,
                    last_probe_at=last_heal_probe_at,
                    interval_seconds=heal_probe_interval,
                )
            ):
                last_heal_probe_at = time.monotonic()
                heal = maybe_self_heal_pytest(
                    workspace_root=ws,
                    repo_root=repo,
                    agent=agent_name,
                    test_command=test_command,
                    python=python,
                    allow_dirty=allow_dirty,
                )
                if heal is not None and str(heal.get("status") or "") in {"background_started", "running"}:
                    heal_attempts += 1
                    session["self_heal_attempts"] = heal_attempts
                    launches.append({**heal, "self_heal": True})
                    append_host_log(ws, f"SELF_HEAL_DRAIN mission={heal.get('mission_id')}")
                    save_host_session(ws, session)
                    time.sleep(min(poll, 60))
                    continue
            append_host_log(ws, "HOST_DRAINED all goals finished")
            # Scan launched missions to build an outcome summary for the caller.
            _launched_ids = session.get("launched_mission_ids") or []
            if _launched_ids:
                _all_rows = {str(r.get("mission_id") or ""): r for r in list_missions(ws)}
                _mission_outcomes: dict[str, list[str]] = {"verified": [], "stopped": [], "other": []}
                for _mid in _launched_ids:
                    _row = _all_rows.get(str(_mid) or "")
                    if _row is None:
                        continue
                    _s = str(_row.get("status") or "")
                    if _s in {"verified"}:
                        _mission_outcomes["verified"].append(str(_mid))
                    elif _s == "stopped":
                        _mission_outcomes["stopped"].append(str(_mid))
                    else:
                        _mission_outcomes["other"].append(str(_mid))
                session["mission_outcomes"] = _mission_outcomes
            break
        if not live.get("ok") and idx >= len(expanded) and not do_wake:
            append_host_log(ws, "HOST_IDLE supply down and no more goals")
            break

        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(poll, max(15.0, remaining)))

    remaining_races = []
    for race_state in pending_races:
        outcome = poll_race(
            workspace_root=ws,
            legs=list(race_state.get("legs") or []),
            abort_losers=bool(race_state.get("abort_losers", True)),
            log=lambda m: append_host_log(ws, m),
        )
        if outcome.get("status") == "race_running":
            remaining_races.append(race_state)
        else:
            session.setdefault("race_outcomes", []).append(
                {**outcome, "race_id": race_state.get("race_id"), "goal": race_state.get("goal")}
            )
    pending_races = remaining_races
    session["pending_races"] = list(pending_races)

    # Final dashboard with pytest
    final_dash = build_host_dashboard(
        workspace_root=ws,
        repo_root=repo,
        agent=agent_name,
        run_pytest=True,
        python=python,
        auto_resume=False,
    )
    session["status"] = "stopped" if host_stop_requested(ws) else "completed"
    session["ended_at"] = utc_now()
    final_missions = final_dash.get("missions") or {}
    verification_gap = max(0, int(final_missions.get("stopped", 0)) + int(final_missions.get("other", 0)))
    session["verification_gap"] = verification_gap
    if verification_gap > 0 and session.get("status") == "completed":
        session["status"] = "completed_with_gaps"
    session["final_dashboard"] = {
        "missions": final_dash.get("missions"),
        "ready_for_host": final_dash.get("ready_for_host"),
        "blockers": final_dash.get("blockers"),
        "pytest_ok": (final_dash.get("pytest") or {}).get("ok"),
    }
    save_host_session(ws, session)
    append_host_log(ws, f"HOST_END status={session['status']} launched={session.get('launched_count')}")
    clear_host_stop(ws)

    return {
        "schema_version": 1,
        "status": session["status"],
        "session": session,
        "launches": launches,
        "dashboard": final_dash,
    }


def host_run_to_markdown(result: dict[str, Any]) -> str:
    session = result.get("session") if isinstance(result.get("session"), dict) else {}
    dash = result.get("dashboard") if isinstance(result.get("dashboard"), dict) else {}
    lines = [
        "# Pacer host 结果",
        "",
        f"- 会话：`{session.get('session_id')}`",
        f"- 状态：**{result.get('status')}**",
        f"- 时长目标：{session.get('hours')}h · 助手：`{session.get('agent')}`",
        f"- 启动任务数：{session.get('launched_count')}",
        f"- 目标进度：{session.get('goal_index')}/{len(session.get('goals') or [])}",
        "",
        host_dashboard_to_markdown(dash) if dash else "",
    ]
    launches = result.get("launches") if isinstance(result.get("launches"), list) else []
    if launches:
        lines.extend(["", "## 本轮启动", ""])
        for item in launches[:30]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- `{item.get('mission_id') or '-'}` **{item.get('status')}/{item.get('stop_reason') or '-'}** "
                f"— {str(item.get('goal') or '')[:80]}"
            )
    return "\n".join(lines)
