"""Durable storage for chief-engineer plans.

A plan is only useful if it survives the terminal or chat that created it. Each
saved plan lives at ``<workspace>/chief_plans/<plan_id>/plan.json`` so it can be
listed, shown, and (later) resumed by dispatch. This module owns the on-disk
layout; callers pass plain dicts.
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


PLANS_DIRNAME = "chief_plans"
_SAFE_PLAN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


def plans_dir(workspace_root: str | Path) -> Path:
    return Path(workspace_root).expanduser().resolve() / PLANS_DIRNAME


def validate_plan_id(plan_id: str) -> str:
    value = str(plan_id or "")
    if not value or value != value.strip():
        raise ValueError("plan_id must be a non-empty, trimmed identifier")
    if value in {".", ".."} or "/" in value or "\\" in value or Path(value).is_absolute():
        raise ValueError(f"Unsafe plan_id: {value!r}")
    if not _SAFE_PLAN_ID.fullmatch(value):
        raise ValueError(f"Unsafe plan_id: {value!r}")
    return value


def plan_dir(workspace_root: str | Path, plan_id: str) -> Path:
    return plans_dir(workspace_root) / validate_plan_id(plan_id)


def make_plan_id(objective: str, *, now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%d-%H%M%S-%f")
    digest = hashlib.sha1(str(objective).strip().encode("utf-8")).hexdigest()[:6]
    return f"{stamp}-{digest}-{uuid4().hex[:8]}"


def save_plan(plan: dict[str, Any], *, workspace_root: str | Path, plan_id: str | None = None) -> dict[str, Any]:
    """Persist a plan dict. Returns {plan_id, path}."""
    objective = str(plan.get("objective") or "")
    if plan_id is None:
        pid, directory = _reserve_plan_directory(workspace_root, objective=objective)
    else:
        pid = validate_plan_id(plan_id)
        directory = plan_dir(workspace_root, pid)
        directory.mkdir(parents=True, exist_ok=True)
    record = dict(plan)
    record["plan_id"] = pid
    record.setdefault("saved_at", datetime.now(timezone.utc).isoformat())
    path = directory / "plan.json"
    _atomic_write_json(path, record)
    return {"plan_id": pid, "path": str(path)}


def load_plan(workspace_root: str | Path, plan_id: str) -> dict[str, Any] | None:
    path = plan_dir(workspace_root, plan_id) / "plan.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_plans(workspace_root: str | Path) -> list[dict[str, Any]]:
    directory = plans_dir(workspace_root)
    if not directory.exists():
        return []
    summaries: list[dict[str, Any]] = []
    for plan_path in sorted(directory.glob("*/plan.json")):
        try:
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        summaries.append(
            {
                "plan_id": str(payload.get("plan_id") or plan_path.parent.name),
                "status": str(payload.get("status") or ""),
                "objective": str(payload.get("objective") or ""),
                "saved_at": str(payload.get("saved_at") or ""),
                "selected_workflows": list(payload.get("selected_workflows") or []),
            }
        )
    # Newest first (plan ids are timestamp-prefixed).
    summaries.sort(key=lambda item: item["plan_id"], reverse=True)
    return summaries


def append_worker_record(workspace_root: str | Path, plan_id: str, record: dict[str, Any]) -> dict[str, Any]:
    directory = plan_dir(workspace_root, plan_id)
    directory.mkdir(parents=True, exist_ok=True)
    entry = dict(record)
    entry.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    path = directory / "workers.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"path": str(path), "record": entry}


def append_dispatch_record(workspace_root: str | Path, plan_id: str, record: dict[str, Any]) -> dict[str, Any]:
    directory = plan_dir(workspace_root, plan_id)
    directory.mkdir(parents=True, exist_ok=True)
    entry = dict(record)
    entry.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    path = directory / "dispatches.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(to_jsonable(entry), ensure_ascii=False) + "\n")
    return {"path": str(path), "record": entry}


def load_worker_records(workspace_root: str | Path, plan_id: str) -> list[dict[str, Any]]:
    path = plan_dir(workspace_root, plan_id) / "workers.jsonl"
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


def save_verification(workspace_root: str | Path, plan_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    directory = plan_dir(workspace_root, plan_id)
    directory.mkdir(parents=True, exist_ok=True)
    record = dict(payload)
    record.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    path = directory / "verification.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(path), "record": record}


def load_verification(workspace_root: str | Path, plan_id: str) -> dict[str, Any] | None:
    path = plan_dir(workspace_root, plan_id) / "verification.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def list_plans_to_markdown(summaries: list[dict[str, Any]]) -> str:
    if not summaries:
        return "No saved chief plans yet. Run `chief-plan --save` to create one."
    lines = ["## Saved Chief Plans", ""]
    for item in summaries:
        workflows = ", ".join(item.get("selected_workflows") or []) or "none"
        lines.append(f"- `{item['plan_id']}` [{item['status']}] {item['objective']}")
        lines.append(f"  saved: {item.get('saved_at', '')} · workflows: {workflows}")
    return "\n".join(lines)


def _reserve_plan_directory(workspace_root: str | Path, *, objective: str) -> tuple[str, Path]:
    root = plans_dir(workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(20):
        plan_id = make_plan_id(objective)
        directory = root / plan_id
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        return plan_id, directory
    raise RuntimeError("Unable to allocate a unique plan id")


def _atomic_write_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
