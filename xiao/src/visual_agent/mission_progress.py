from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .chief_plans_store import load_verification, load_worker_records, plan_dir
from .codex_check import is_runtime_changed_file
from .command_verification import is_test_path
from .missions import load_rounds, mission_dir
from .models import to_jsonable

_PROGRESS_LOCK = threading.RLock()
_TERMINAL_STAGES = {
    "verified",
    "verified_blocked",
    "blocked",
    "worker_failed_tests_pass",
    "post_merge_verification_passed",
    "post_merge_verification_failed",
}
_LIVE_ACTIVITY_STAGES = {
    "background_started",
    "worker_started",
    "worker_starting",
    "worker_running",
    "verification_running",
}


def progress_path(workspace_root: str | Path, mission_id: str) -> Path:
    return mission_dir(workspace_root, mission_id) / "progress.json"


def load_mission_progress(workspace_root: str | Path, mission_id: str) -> dict[str, Any]:
    path = progress_path(workspace_root, mission_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_mission_progress(workspace_root: str | Path, mission_id: str, **fields: Any) -> dict[str, Any]:
    mid = str(mission_id or "").strip()
    if not mid:
        return {}
    path = progress_path(workspace_root, mid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _PROGRESS_LOCK:
        payload = load_mission_progress(workspace_root, mid)
        payload.update({key: value for key, value in fields.items() if value is not None})
        payload["schema_version"] = 1
        payload["mission_id"] = mid
        payload["updated_at"] = _utc_now()
        text = json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return payload


def record_worker_output(
    workspace_root: str | Path,
    mission_id: str,
    *,
    stream: str,
    chunk: str,
    log_path: str | Path | None = None,
) -> dict[str, Any]:
    text = str(chunk or "")
    with _PROGRESS_LOCK:
        current = load_mission_progress(workspace_root, mission_id)
        if str(current.get("stage") or "") in _TERMINAL_STAGES:
            if not text.strip():
                return current
            return save_mission_progress(
                workspace_root,
                mission_id,
                last_output_at=_utc_now(),
                last_output_stream=str(stream or ""),
                last_output_tail=text[-500:],
            )
    fields: dict[str, Any] = {
        "stage": "worker_running",
        "stage_label": "Worker running",
        "status": "running",
        "stop_reason": "",
        "needs_attention": False,
        "blocker": "",
        "last_activity_at": _utc_now(),
        "log_path": str(log_path or ""),
    }
    if text.strip():
        fields.update(
            {
                "last_output_at": _utc_now(),
                "last_output_stream": str(stream or ""),
                "last_output_tail": text[-500:],
            }
        )
    return save_mission_progress(workspace_root, mission_id, **fields)


def build_mission_progress(
    *,
    workspace_root: str | Path,
    mission: dict[str, Any],
    rounds: list[dict[str, Any]] | None = None,
    background: dict[str, Any] | None = None,
    worker_records: list[dict[str, Any]] | None = None,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(workspace_root).expanduser().resolve()
    mission_id = str(mission.get("mission_id") or "").strip()
    plan_id = str(mission.get("plan_id") or mission_id).strip()
    saved = load_mission_progress(root, mission_id) if mission_id else {}
    rounds = rounds if rounds is not None else (load_rounds(root, mission_id) if mission_id else [])
    worker_records = worker_records if worker_records is not None else (load_worker_records(root, plan_id) if plan_id else [])
    verification = verification if verification is not None else (load_verification(root, plan_id) if plan_id else {})
    verification = verification if isinstance(verification, dict) else {}
    current_verification = verification if _verification_round_is_current(rounds) else {}
    background = background if isinstance(background, dict) else _load_background(root, mission_id)

    latest_round = rounds[-1] if rounds else {}
    latest_worker = worker_records[-1] if worker_records else {}
    worktree = _first_non_empty(
        saved.get("worktree"),
        latest_worker.get("cwd") if isinstance(latest_worker, dict) else "",
        _worktree_from_rounds(rounds),
    )
    changed_files = _git_changed_files(Path(worktree)) if worktree else []
    product_changed_files = [path for path in changed_files if _is_product_change(path, repo_root=Path(worktree) if worktree else None)]
    command = _verification_command(verification)
    stage = _derive_stage(
        mission=mission,
        background=background,
        latest_round=latest_round,
        latest_worker=latest_worker,
        verification=current_verification,
        saved_stage=str(saved.get("stage") or ""),
    )
    latest_log = _latest_log_metadata(root=root, plan_id=plan_id, mission_id=mission_id)
    activity = _infer_activity(stage=stage, saved=saved, latest_worker=latest_worker, latest_log=latest_log)
    reported_activity_active = activity == str(saved.get("activity") or "") and _reported_activity_is_fresh(saved)
    activity_started_at = str(saved.get("activity_started_at") or "") if reported_activity_active else ""
    last_activity_at = _latest_timestamp(
        saved.get("last_activity_at"),
        saved.get("updated_at"),
        saved.get("last_output_at"),
        background.get("worker_started_at") if isinstance(background, dict) else "",
        background.get("completed_at") if isinstance(background, dict) else "",
        background.get("started_at") if isinstance(background, dict) else "",
        latest_log.get("modified_at"),
        mission.get("updated_at"),
    )

    progress = {
        "schema_version": 1,
        "mission_id": mission_id,
        "plan_id": plan_id,
        "status": str(mission.get("status") or ""),
        "stop_reason": str(mission.get("stop_reason") or ""),
        "stage": stage,
        "stage_label": _stage_label(stage),
        "activity": activity,
        "activity_label": _activity_label(activity),
        "activity_command": str(saved.get("activity_command") or "") if reported_activity_active else "",
        "activity_started_at": activity_started_at,
        "activity_elapsed_seconds": _activity_elapsed_seconds(activity_started_at) if activity_started_at else None,
        "message": _stage_message(stage),
        "current_round": mission.get("current_round"),
        "latest_round_type": str(latest_round.get("type") or ""),
        "latest_round_status": str(latest_round.get("status") or ""),
        "background_status": str((background or {}).get("status") or ""),
        "background_alive": bool((background or {}).get("alive")) if background else False,
        "worker_status": str(latest_worker.get("status") or ""),
        "worker_exit_code": latest_worker.get("exit_code"),
        "worker_records": len(worker_records),
        "agent": str(latest_worker.get("agent") or saved.get("agent") or ""),
        "worktree": worktree,
        "changed_files": changed_files,
        "changed_file_count": len(changed_files),
        "changed_product_files": product_changed_files,
        "changed_product_file_count": len(product_changed_files),
        "verification_verdict": str(current_verification.get("verdict") or ""),
        "verification_command": command,
        "verification_path": str(plan_dir(root, plan_id) / "verification.json") if plan_id else "",
        "last_activity_at": last_activity_at,
        "last_output_at": str(saved.get("last_output_at") or ""),
        "last_output_stream": str(saved.get("last_output_stream") or ""),
        "last_output_tail": str(saved.get("last_output_tail") or ""),
        "latest_log_path": latest_log.get("path", ""),
        "latest_log_modified_at": latest_log.get("modified_at", ""),
        "needs_attention": _needs_attention(stage, mission=mission, background=background),
        "blocker": _blocker(stage, mission=mission, background=background, verification=current_verification, latest_worker=latest_worker),
        "updated_at": _utc_now(),
    }
    if saved:
        for key in ("started_at", "heartbeat_at", "log_path"):
            if saved.get(key) and not progress.get(key):
                progress[key] = saved[key]
    return progress


def _load_background(root: Path, mission_id: str) -> dict[str, Any]:
    if not mission_id:
        return {}
    path = mission_dir(root, mission_id) / "background.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _verification_round_is_current(rounds: list[dict[str, Any]]) -> bool:
    last_verification_index = -1
    for index, item in enumerate(rounds):
        if str(item.get("type") or "") == "verification":
            last_verification_index = index
    return last_verification_index >= len(rounds) - 1 if rounds else False


def _worktree_from_rounds(rounds: list[dict[str, Any]]) -> str:
    for item in reversed(rounds):
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        worktree = payload.get("worktree") if isinstance(payload.get("worktree"), dict) else {}
        path = str(worktree.get("path") or "").strip()
        if path:
            return path
    return ""


def _git_changed_files(repo_root: Path) -> list[str]:
    if not repo_root.exists():
        return []
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "-c", "core.quotePath=false", "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    paths: list[str] = []
    for raw in completed.stdout.splitlines():
        if len(raw) < 4:
            continue
        path = raw[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip().strip('"')
        if path:
            paths.append(path.replace("\\", "/"))
    return sorted(dict.fromkeys(paths))


def _is_product_change(path: str, *, repo_root: Path | None) -> bool:
    normalized = str(path).replace("\\", "/").strip().lstrip("/")
    if not normalized:
        return False
    if is_runtime_changed_file(normalized, repo_root=repo_root):
        return False
    if is_test_path(normalized):
        return False
    basename = normalized.rsplit("/", 1)[-1]
    if basename in {".visual-agent-status.md", "强制测试记录.md"}:
        return False
    ignored_prefixes = (
        ".agent-workspace/",
        ".pytest_cache/",
        "__pycache__/",
        "node_modules/",
        ".npm-cache/",
        ".dart_tool/",
        ".dart-home/",
        "artifacts/",
    )
    if any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in ignored_prefixes):
        return False
    if normalized.endswith((".pyc", ".pyo", ".pyd")):
        return False
    return True


def _verification_command(verification: dict[str, Any]) -> str:
    command_verification = verification.get("command_verification") if isinstance(verification.get("command_verification"), dict) else {}
    command = str(command_verification.get("command") or "").strip()
    if command:
        return command
    return str(verification.get("command") or "").strip()


def _derive_stage(
    *,
    mission: dict[str, Any],
    background: dict[str, Any],
    latest_round: dict[str, Any],
    latest_worker: dict[str, Any],
    verification: dict[str, Any],
    saved_stage: str,
) -> str:
    status = str(mission.get("status") or "")
    stop_reason = str(mission.get("stop_reason") or "")
    verdict = str(verification.get("verdict") or "")
    worker_status = str(latest_worker.get("status") or "")
    background_status = str(background.get("status") or "")
    latest_type = str(latest_round.get("type") or "")
    if status == "verified":
        return "verified"
    if status == "verified_blocked":
        return "verified_blocked"
    if stop_reason == "worker_failed_tests_pass":
        return "worker_failed_tests_pass"
    if status == "preview" and (stop_reason == "preview_only" or latest_type == "dispatch_preview"):
        return "dispatch_ready"
    if stop_reason:
        return "blocked" if status == "stopped" else status or "stopped"
    if verdict:
        return "verification_passed" if verdict == "pass" else "verification_failed" if verdict == "fail" else "verification_recorded"
    if (
        background_status == "running"
        and worker_status == "completed"
        and _worker_record_is_current_for_background(latest_worker, background)
    ):
        return "verification_running"
    if background_status == "running" and saved_stage in {
        "background_started",
        "worker_started",
        "worker_starting",
        "worker_running",
        "verification_running",
    }:
        return saved_stage
    if worker_status:
        if worker_status == "completed":
            return "verification_pending"
        if worker_status in {"failed", "blocked"}:
            return "worker_blocked"
        return f"worker_{worker_status}"
    if latest_type == "dispatch_preview" and not background_status and status in {"running", "background_running"}:
        return "worker_activity_stale"
    if background_status == "running" or status in {"running", "background_running"}:
        if saved_stage:
            return saved_stage
        return "worker_running"
    if latest_type == "dispatch_preview":
        return "dispatch_ready"
    if status == "preview":
        return "preview"
    return status or "unknown"


def _worker_record_is_current_for_background(worker: dict[str, Any], background: dict[str, Any]) -> bool:
    worker_time = _parse_time(worker.get("recorded_at"))
    background_time = _parse_time(background.get("worker_started_at") or background.get("started_at"))
    if worker_time is None or background_time is None:
        return False
    return worker_time >= background_time


def _stage_label(stage: str) -> str:
    labels = {
        "preview": "Preview ready",
        "dispatch_ready": "Dispatch ready",
        "background_started": "Background started",
        "worker_started": "Worker started",
        "worker_running": "Worker running",
        "verification_pending": "Verification pending",
        "verification_running": "Verification running",
        "verification_passed": "Verification passed",
        "verification_failed": "Verification failed",
        "verified": "Verified",
        "verified_blocked": "Verified but blocked",
        "worker_failed_tests_pass": "Worker failed; tests passed",
        "worker_orphaned": "Background worker lost",
        "worker_blocked": "Worker blocked",
        "worker_activity_stale": "Worker activity stale",
        "blocked": "Blocked",
    }
    return labels.get(stage, stage.replace("_", " ").title())


def _infer_activity(*, stage: str, saved: dict[str, Any], latest_worker: dict[str, Any], latest_log: dict[str, str]) -> str:
    if stage not in _LIVE_ACTIVITY_STAGES:
        return ""
    reported_activity = str(saved.get("activity") or "")
    if reported_activity and _reported_activity_is_fresh(saved):
        return reported_activity
    worker_status = str(latest_worker.get("status") or "")
    if stage == "verification_running":
        return "verification"
    if worker_status == "completed":
        return "worker_completed"
    if worker_status in {"failed", "blocked"}:
        return "worker_blocked"
    text = " ".join(
        str(value or "")
        for value in (
            saved.get("last_output_tail"),
            latest_worker.get("stdout_tail"),
            latest_worker.get("stderr_tail"),
        )
    ).lower()
    if not text.strip() and latest_log.get("path"):
        text = _read_text_tail(Path(str(latest_log["path"])), max_chars=4000).lower()
    if not text:
        return "waiting_for_output" if stage in {"worker_running", "worker_started", "background_started"} else ""
    if any(marker in text for marker in ("node --import", "npm test", "pytest", "vitest", "tap", "--test")):
        return "tests_running"
    if any(
        re.search(pattern, text, re.MULTILINE)
        for pattern in (
            r"^\s*npm\s+ci\b",
            r"^\s*npm\s+install\b",
            r"^\s*\$?\s*npm\s+ci\b",
            r"^\s*\$?\s*npm\s+install\b",
            r"silly tarball",
            r"extracting by manifest",
        )
    ):
        return "dependency_install"
    if any(marker in text for marker in ("apply_patch", "update file", "new file", "write_text", "set-content")):
        return "editing_files"
    if any(marker in text for marker in ("get-content", "rg ", "ripgrep", "read file", "git status", "git diff")):
        return "inspecting_files"
    return "worker_output"


def _read_text_tail(path: Path, *, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


def _activity_label(activity: str) -> str:
    labels = {
        "waiting_for_output": "Waiting for worker output",
        "inspecting_files": "Inspecting files",
        "editing_files": "Editing files",
        "dependency_install": "Installing dependencies",
        "tests_running": "Running tests",
        "verification": "Running verification",
        "worker_executing": "Worker executing",
        "worker_completed": "Worker completed",
        "worker_blocked": "Worker blocked",
        "worker_output": "Worker output",
    }
    return labels.get(activity, "")


def _reported_activity_is_fresh(saved: dict[str, Any], *, max_seconds: int = 600) -> bool:
    started = _parse_time(saved.get("activity_started_at"))
    if started is None:
        return False
    now = datetime.now(timezone.utc)
    return 0 <= (now - started).total_seconds() <= max_seconds


def _activity_elapsed_seconds(started_at: str) -> int:
    started = _parse_time(started_at)
    if started is None:
        return 0
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def _stage_message(stage: str) -> str:
    messages = {
        "worker_running": "Worker is active; progress is tracked from heartbeat, worker records, and worktree diff.",
        "verification_running": "Worker finished or is finishing; Pacer is running the acceptance gate.",
        "verification_pending": "Worker completed; waiting for verification evidence.",
        "verification_passed": "Acceptance gate passed; checking whether product changes are valid.",
        "verification_failed": "Acceptance gate failed; repair evidence is available.",
        "verified": "Mission reached verified.",
        "verified_blocked": "Verification passed but a policy gate blocked merge/verified acceptance.",
        "worker_failed_tests_pass": (
            "Worker did not complete normally, but current worktree changes passed the test command. "
            "Manual inspection is required before merge."
        ),
        "worker_activity_stale": "Mission is marked running, but no live background worker or worker record was found.",
        "blocked": "Mission stopped and needs attention.",
    }
    return messages.get(stage, "Mission progress snapshot is available.")


def _needs_attention(stage: str, *, mission: dict[str, Any], background: dict[str, Any]) -> bool:
    stop_reason = str(mission.get("stop_reason") or "")
    if stop_reason and stop_reason != "verified":
        return True
    if stage in {
        "verification_failed",
        "worker_blocked",
        "worker_activity_stale",
        "blocked",
        "verified_blocked",
        "worker_failed_tests_pass",
    }:
        return True
    if str(background.get("status") or "") in {"failed", "timeout", "orphaned"}:
        return True
    return False


def _blocker(
    stage: str,
    *,
    mission: dict[str, Any],
    background: dict[str, Any],
    verification: dict[str, Any],
    latest_worker: dict[str, Any],
) -> str:
    stop_reason = str(mission.get("stop_reason") or "")
    if stop_reason and stop_reason != "verified":
        return stop_reason
    if str(background.get("status") or "") in {"failed", "timeout", "orphaned"}:
        return str(background.get("status") or "")
    if str(latest_worker.get("blocked_reason") or ""):
        return str(latest_worker.get("blocked_reason") or "")
    if str(verification.get("verdict") or "") == "fail":
        repair = verification.get("repair_brief") if isinstance(verification.get("repair_brief"), dict) else {}
        return str(repair.get("failure_kind") or repair.get("source") or "verification_failed")
    if stage == "worker_activity_stale":
        return "worker_activity_stale"
    if stage in {"verified_blocked", "worker_blocked", "worker_failed_tests_pass"}:
        return stage
    return ""


def _latest_log_metadata(*, root: Path, plan_id: str, mission_id: str) -> dict[str, str]:
    candidates: list[Path] = []
    if plan_id:
        candidates.extend((plan_dir(root, plan_id) / "logs").glob("*.log"))
    if mission_id:
        candidates.extend((mission_dir(root, mission_id) / "logs").glob("*.log"))
    latest_path = None
    latest_mtime = 0.0
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > latest_mtime:
            latest_path = path
            latest_mtime = mtime
    if latest_path is None:
        return {}
    return {"path": str(latest_path), "modified_at": datetime.fromtimestamp(latest_mtime, timezone.utc).isoformat()}


def _latest_timestamp(*values: Any) -> str:
    latest: datetime | None = None
    for value in values:
        parsed = _parse_time(value)
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    return latest.isoformat() if latest is not None else ""


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
