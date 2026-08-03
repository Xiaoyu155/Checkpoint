"""Durable mission state for the autonomous development loop.

Checkpoint remains the verification engine. A mission is the product-level unit
for Pacer: objective, plan, budget, rounds, and final report.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import to_jsonable


MISSIONS_DIRNAME = "missions"
_SAFE_MISSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


def missions_dir(workspace_root: str | Path) -> Path:
    return Path(workspace_root).expanduser().resolve() / MISSIONS_DIRNAME


def validate_mission_id(mission_id: str) -> str:
    """Return a safe mission id or raise before it can affect a path."""
    value = str(mission_id or "")
    if not value or value != value.strip():
        raise ValueError("mission_id must be a non-empty, trimmed identifier")
    if value in {".", ".."} or "/" in value or "\\" in value or Path(value).is_absolute():
        raise ValueError(f"Unsafe mission_id: {value!r}")
    if not _SAFE_MISSION_ID.fullmatch(value):
        raise ValueError(f"Unsafe mission_id: {value!r}")
    return value


def mission_dir(workspace_root: str | Path, mission_id: str) -> Path:
    return missions_dir(workspace_root) / validate_mission_id(mission_id)


def make_mission_id(objective: str, *, now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%d-%H%M%S-%f")
    digest = hashlib.sha1(str(objective).strip().encode("utf-8")).hexdigest()[:6]
    return f"{stamp}-{digest}-{uuid4().hex[:8]}"


def default_budget_policy(
    *,
    max_rounds: int = 3,
    max_wall_minutes: int = 60,
    max_worker_minutes: int = 45,
    max_repair_rounds: int | None = None,
    max_total_tokens: int = 120_000,
    max_same_failure_count: int = 2,
) -> dict[str, Any]:
    repair_rounds = max(0, min(max_rounds - 1, 2)) if max_repair_rounds is None else max(0, max_repair_rounds)
    return {
        "max_rounds": max(1, int(max_rounds)),
        "max_wall_minutes": max(1, int(max_wall_minutes)),
        "max_worker_minutes": max(1, int(max_worker_minutes)),
        "max_repair_rounds": repair_rounds,
        "max_total_tokens": max(1, int(max_total_tokens)),
        "max_same_failure_count": max(1, int(max_same_failure_count)),
        "model_policy": {
            "implementation": "strong",
            "repair": "strong",
            "classification": "fast",
            "visual_review": "multimodal",
        },
        "approval_policy": {
            "allow_file_edits": True,
            "allow_network": False,
            "allow_destructive_commands": False,
            "require_human_for_merge": True,
        },
    }


def create_mission(
    *,
    workspace_root: str | Path,
    objective: str,
    repo_root: str | Path,
    plan_id: str,
    budget_policy: dict[str, Any],
    mission_id: str | None = None,
    status: str = "created",
    requirement_contract: dict[str, Any] | None = None,
    verification_env: list[dict[str, Any]] | None = None,
    test_command: str | None = None,
    allow_dirty: bool = False,
    allow_test_edits: bool = False,
    merge: bool = False,
    reasoning_effort: str = "inherit",
    dispatch_mode: str = "tracked",
    prompt_style: str = "expanded",
    repair_strategy: str = "resume",
) -> dict[str, Any]:
    mid, directory = _reserve_mission_directory(
        workspace_root,
        objective=objective,
        requested_id=mission_id,
    )
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "schema_version": 1,
        "mission_id": mid,
        "product": "Pacer",
        "verification_engine": "Checkpoint",
        "objective": str(objective),
        "status": status,
        "stop_reason": "",
        "created_at": now,
        "updated_at": now,
        "workspace_root": str(Path(workspace_root).expanduser().resolve()),
        "repo_root": str(Path(repo_root).expanduser().resolve()),
        "plan_id": str(plan_id),
        "current_round": 0,
        "budget_policy": dict(budget_policy),
        "test_command": str(test_command or "").strip(),
        "allow_dirty": bool(allow_dirty),
        "allow_test_edits": bool(allow_test_edits),
        "merge": bool(merge),
        "reasoning_effort": str(reasoning_effort or "inherit"),
        "dispatch_mode": str(dispatch_mode or "tracked"),
        "prompt_style": str(prompt_style or "expanded"),
        "repair_strategy": str(repair_strategy or "resume"),
    }
    if isinstance(requirement_contract, dict) and requirement_contract:
        record["requirement_contract"] = dict(requirement_contract)
    if isinstance(verification_env, list) and verification_env:
        record["verification_env"] = list(verification_env)
    path = directory / "mission.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(to_jsonable(record), handle, ensure_ascii=False, indent=2)
    save_budget(workspace_root, mid, budget_policy)
    return record


def save_mission(workspace_root: str | Path, mission: dict[str, Any]) -> dict[str, Any]:
    mid = validate_mission_id(str(mission.get("mission_id") or ""))
    directory = mission_dir(workspace_root, mid)
    directory.mkdir(parents=True, exist_ok=True)
    record = dict(mission)
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = directory / "mission.json"
    _atomic_write_json(path, record)
    return {"path": str(path), "mission": record}


def load_mission(workspace_root: str | Path, mission_id: str) -> dict[str, Any] | None:
    path = mission_dir(workspace_root, mission_id) / "mission.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def save_budget(workspace_root: str | Path, mission_id: str, budget_policy: dict[str, Any]) -> dict[str, Any]:
    directory = mission_dir(workspace_root, mission_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "budget.json"
    _atomic_write_json(path, budget_policy)
    return {"path": str(path), "budget_policy": dict(budget_policy)}


def append_round(workspace_root: str | Path, mission_id: str, record: dict[str, Any]) -> dict[str, Any]:
    directory = mission_dir(workspace_root, mission_id)
    directory.mkdir(parents=True, exist_ok=True)
    entry = dict(record)
    entry.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    path = directory / "rounds.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(to_jsonable(entry), ensure_ascii=False) + "\n")
    return {"path": str(path), "record": entry}


def load_rounds(workspace_root: str | Path, mission_id: str) -> list[dict[str, Any]]:
    path = mission_dir(workspace_root, mission_id) / "rounds.jsonl"
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def write_final_report(workspace_root: str | Path, mission_id: str, text: str) -> dict[str, Any]:
    directory = mission_dir(workspace_root, mission_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "final_report.md"
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return {"path": str(path)}


def list_missions(workspace_root: str | Path) -> list[dict[str, Any]]:
    directory = missions_dir(workspace_root)
    if not directory.exists():
        return []
    # Opportunistic *read-side* reconcile only marks dead/PID-reused workers.
    # Do not spend tokens as a side effect of listing missions.
    try:
        from .chief_background import reconcile_workspace_backgrounds

        reconcile_workspace_backgrounds(
            workspace_root, update=True, limit=30, auto_resume=False
        )
    except Exception:
        pass
    summaries: list[dict[str, Any]] = []
    for mission_path in sorted(directory.glob("*/mission.json")):
        try:
            payload = json.loads(mission_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if bool(payload.get("hidden")) or str(payload.get("status") or "").strip().lower() in {"archived", "deleted"}:
            continue
        journey: dict[str, Any] = {}
        journey_path = mission_path.parent / "journey.json"
        if journey_path.exists():
            try:
                loaded_journey = json.loads(journey_path.read_text(encoding="utf-8"))
                journey = loaded_journey if isinstance(loaded_journey, dict) else {}
            except (OSError, json.JSONDecodeError):
                journey = {}
        summaries.append(
            {
                "mission_id": str(payload.get("mission_id") or mission_path.parent.name),
                "status": str(payload.get("status") or ""),
                "stop_reason": str(payload.get("stop_reason") or ""),
                "objective": str(payload.get("objective") or ""),
                "plan_id": str(payload.get("plan_id") or ""),
                "created_at": str(payload.get("created_at") or ""),
                "updated_at": str(payload.get("updated_at") or ""),
                "repo_root": str(payload.get("repo_root") or ""),
                "test_command": str(payload.get("test_command") or ""),
                "agent": str(payload.get("agent") or ""),
                "merge_policy": str(payload.get("merge_policy") or ""),
                "requirement_contract": payload.get("requirement_contract") if isinstance(payload.get("requirement_contract"), dict) else {},
                "journey_status": str(journey.get("status") or ""),
                "journey_summary": str(journey.get("summary") or ""),
            }
        )
    summaries.sort(key=lambda item: item["mission_id"], reverse=True)
    return summaries


def load_any_mission(workspace_root: str | Path, mission_id: str) -> dict[str, Any] | None:
    """Load an active mission; if missing, fall back to the archived copy."""
    mission = load_mission(workspace_root, mission_id)
    if mission is not None:
        return mission
    archived = (
        Path(workspace_root).expanduser().resolve()
        / "archived_missions"
        / validate_mission_id(mission_id)
        / "mission.json"
    )
    if not archived.exists():
        return None
    try:
        payload = json.loads(archived.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def list_missions_to_markdown(summaries: list[dict[str, Any]]) -> str:
    if not summaries:
        return "No saved missions yet. Run `chief-run --goal ...` to create one."
    lines = ["## Pacer Missions", ""]
    for item in summaries:
        stop = f" / {item['stop_reason']}" if item.get("stop_reason") else ""
        lines.append(f"- `{item['mission_id']}` [{item['status']}{stop}] {item['objective']}")
        lines.append(f"  plan: `{item.get('plan_id', '')}` · updated: {item.get('updated_at', '')}")
    return "\n".join(lines)


def _reserve_mission_directory(
    workspace_root: str | Path,
    *,
    objective: str,
    requested_id: str | None,
) -> tuple[str, Path]:
    root = missions_dir(workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    if requested_id is not None:
        mission_id = validate_mission_id(requested_id)
        directory = root / mission_id
        try:
            directory.mkdir()
        except FileExistsError as exc:
            raise FileExistsError(f"Mission already exists: {mission_id}") from exc
        return mission_id, directory
    for _ in range(20):
        mission_id = make_mission_id(objective)
        directory = root / mission_id
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        return mission_id, directory
    raise RuntimeError("Unable to allocate a unique mission id")


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Atomic JSON write with Windows-friendly replace retries.

    Concurrent mission processes can briefly lock the destination file and
    raise WinError 5/32 on ``os.replace``. Retrying avoids false hard failures
    during multi-mission stress.
    """
    import time

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    text = json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2)
    last_error: OSError | None = None
    try:
        temporary.write_text(text, encoding="utf-8")
        for attempt in range(12):
            try:
                os.replace(temporary, path)
                last_error = None
                break
            except OSError as exc:  # noqa: PERF203 - explicit retry path
                last_error = exc
                # 5 = access denied, 32 = sharing violation on Windows
                winerr = getattr(exc, "winerror", None)
                if winerr not in {5, 32} and not isinstance(exc, PermissionError):
                    raise
                time.sleep(0.05 * (attempt + 1))
        if last_error is not None:
            raise last_error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
