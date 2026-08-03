from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .hourly_budget import build_hourly_plan, hourly_plan_to_markdown
from .mission_plan_import import parse_development_plan
from .models import to_jsonable
from .subscription_quota import load_quota_snapshot
from .verification_profiles import choose_verification_command


PROGRAMS_DIRNAME = "programs"
DEFAULT_AUTONOMOUS_MODEL_ROUTING = {
    "strong_worker": "inherit",
    "cheap_worker": "gpt-5.6-luna",
    "research_or_doc": "gpt-5.6-luna",
    "delegated_worker": "gpt-5.6-terra",
}


def programs_dir(workspace_root: str | Path) -> Path:
    return Path(workspace_root).expanduser().resolve() / PROGRAMS_DIRNAME


def program_dir(workspace_root: str | Path, program_id: str) -> Path:
    return programs_dir(workspace_root) / str(program_id)


def make_program_id(objective: str, *, now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    digest = hashlib.sha1(str(objective).encode("utf-8")).hexdigest()[:6]
    return f"{moment.strftime('%Y%m%d-%H%M%S')}-{digest}"


def create_program_from_plan(
    *,
    source_file: str | Path,
    workspace_root: str | Path,
    repo_root: str | Path,
    objective: str | None = None,
    hours: float = 5.0,
    agent: str = "codex",
    test_command: str | None = "auto",
    sequential: bool = True,
    limit: int | None = 12,
    autonomous: bool = False,
    allow_dirty: bool = False,
    model: str | None = None,
    strong_model: str | None = None,
    cheap_model: str | None = None,
    research_model: str | None = None,
    codex_provider: str | None = None,
    codex_failover_provider: str | None = None,
    memory_mode: str = "enabled",
    acceptance_policy: str | None = None,
) -> dict[str, Any]:
    source_path = Path(source_file).expanduser().resolve()
    text = source_path.read_text(encoding="utf-8")
    parsed = parse_development_plan(text, source_name=str(source_path), limit=None if autonomous else limit)
    title = str(objective or parsed.get("title") or source_path.stem)
    workspace_path = Path(workspace_root).expanduser().resolve()
    repo_path = Path(repo_root).expanduser().resolve()
    resolved_test_command = choose_verification_command(repo_path) if str(test_command or "").strip().lower() == "auto" else str(test_command or "").strip()
    drafts = [item for item in (parsed.get("drafts") or []) if isinstance(item, dict)]
    fallback_warning = ""
    if not drafts and title.strip():
        drafts = [
            {
                "index": 1,
                "objective": title.strip(),
                "source_line": 1,
                "source_type": "objective_fallback",
                "section": "",
                "raw": title.strip(),
            }
        ]
        fallback_warning = "No actionable plan items were parsed; used the top-level objective as task-001."
    tasks = [
        _task_from_draft(
            draft,
            previous_task_id=(f"task-{index:03d}" if sequential and index > 0 else ""),
            agent=agent,
            test_command=resolved_test_command or str(test_command or ""),
        )
        for index, draft in enumerate(drafts)
    ]
    now = datetime.now(timezone.utc).isoformat()
    source_plan_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    model_routing = dict(DEFAULT_AUTONOMOUS_MODEL_ROUTING)
    model_override = str(model or "").strip()
    if model_override:
        model_routing = {key: model_override for key in model_routing}
    else:
        for key, value in (
            ("strong_worker", strong_model),
            ("cheap_worker", cheap_model),
            ("research_or_doc", research_model),
        ):
            if str(value or "").strip():
                model_routing[key] = str(value).strip()
    program = {
        "schema_version": 1,
        "product": "DevPacer",
        "kind": "program",
        "program_id": make_program_id(title),
        "objective": title,
        "source_plan": str(source_path),
        "source_plan_sha256": source_plan_sha256,
        "workspace_root": str(workspace_path),
        "repo_root": str(repo_path),
        "status": "planning",
        "created_at": now,
        "updated_at": now,
        "quota_policy": {
            "window_minutes": int(max(1.0, float(hours)) * 60),
            "strong_worker_budget_minutes": max(30, int(float(hours) * 60) - 90),
            "reserve_minutes": 45,
            "pause_at_used_percentage": 82,
            "resume_after_reset": True,
            "quota_mode": "unrestricted" if autonomous else "conservative",
        },
        "autonomy_policy": {
            "mode": "autonomous" if autonomous else "supervised",
            "dispatch_mode": "delegated" if autonomous else "tracked",
            "reasoning_effort": "inherit",
            "max_rounds": 8 if autonomous else 3,
            "max_repair_rounds": 7 if autonomous else 2,
            "max_wall_minutes": 480 if autonomous else 60,
            "max_worker_minutes": 240 if autonomous else 45,
            "allow_coverage_gap": bool(autonomous),
            "run_profile": "supervised" if autonomous else "dry-run",
            "allow_dirty": bool(allow_dirty),
            "model_routing": model_routing,
            "closed_loop": {
                "codex_provider": str(codex_provider or "inherit").strip() or "inherit",
                "codex_failover_provider": str(codex_failover_provider or "").strip(),
                "memory_mode": str(memory_mode or "enabled").strip().lower(),
                "acceptance_policy": str(
                    acceptance_policy or ("strict" if autonomous else "standard")
                ).strip().lower(),
                "roadmap_mode": "locked" if autonomous else "advisory",
                "source_plan_sha256": source_plan_sha256,
            },
        },
        "tasks": tasks,
        "current_focus": tasks[0]["objective"] if tasks else "",
        "next_action": "Run program plan, then program start." if tasks else "No actionable tasks found.",
    }
    if fallback_warning:
        program["warnings"] = [fallback_warning]
    save_program(workspace_path, program)
    append_program_event(workspace_path, program["program_id"], {"event": "created", "task_count": len(tasks)})
    refresh_daily_plan(workspace_path, program["program_id"], hours=hours)
    return load_program(workspace_path, program["program_id"]) or program


def save_program(workspace_root: str | Path, program: dict[str, Any]) -> dict[str, Any]:
    pid = str(program.get("program_id") or "")
    if not pid:
        raise ValueError("program_id is required")
    directory = program_dir(workspace_root, pid)
    directory.mkdir(parents=True, exist_ok=True)
    record = dict(program)
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = directory / "program.json"
    path.write_text(json.dumps(to_jsonable(record), ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(path), "program": record}


def load_program(workspace_root: str | Path, program_id: str) -> dict[str, Any] | None:
    path = program_dir(workspace_root, program_id) / "program.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def list_programs(workspace_root: str | Path) -> list[dict[str, Any]]:
    root = programs_dir(workspace_root)
    if not root.exists():
        return []
    items = []
    for path in sorted(root.glob("*/program.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
        items.append(
            {
                "program_id": str(payload.get("program_id") or path.parent.name),
                "objective": str(payload.get("objective") or ""),
                "status": str(payload.get("status") or ""),
                "task_count": len(tasks),
                "updated_at": str(payload.get("updated_at") or ""),
            }
        )
    return sorted(items, key=lambda item: item["program_id"], reverse=True)


def update_program_task(workspace_root: str | Path, program_id: str, task_id: str, **changes: Any) -> dict[str, Any]:
    program = load_program(workspace_root, program_id)
    if program is None:
        raise FileNotFoundError(f"No saved program found: {program_id}")
    tasks = program.get("tasks") if isinstance(program.get("tasks"), list) else []
    for task in tasks:
        if isinstance(task, dict) and str(task.get("task_id") or "") == str(task_id):
            task.update(changes)
            save_program(workspace_root, program)
            append_program_event(workspace_root, program_id, {"event": "task_updated", "task_id": task_id, "changes": changes})
            return task
    raise FileNotFoundError(f"No task {task_id} in program {program_id}")


def ready_program_tasks(program: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = [item for item in (program.get("tasks") or []) if isinstance(item, dict)]
    done = {str(item.get("task_id")) for item in tasks if str(item.get("status") or "") in {"verified", "done"}}
    ready = []
    for task in tasks:
        if str(task.get("status") or "pending") != "pending":
            continue
        deps = [str(item) for item in (task.get("depends_on") or [])]
        if all(dep in done for dep in deps):
            ready.append({**task, "status": "ready"})
    return ready


def build_program_hourly_plan(*, workspace_root: str | Path, program_id: str, hours: float = 5.0) -> dict[str, Any]:
    program = load_program(workspace_root, program_id)
    if program is None:
        raise FileNotFoundError(f"No saved program found: {program_id}")
    policy = program.get("quota_policy") if isinstance(program.get("quota_policy"), dict) else {}
    return build_hourly_plan(
        tasks=ready_program_tasks(program),
        quota_snapshot=load_quota_snapshot(),
        hours=hours,
        reserve_minutes=int(policy.get("reserve_minutes") or 45),
        pause_at_used_percentage=float(policy.get("pause_at_used_percentage") or 82),
        quota_mode=str(policy.get("quota_mode") or "conservative"),
    )


def refresh_daily_plan(workspace_root: str | Path, program_id: str, *, hours: float = 5.0) -> dict[str, Any]:
    plan = build_program_hourly_plan(workspace_root=workspace_root, program_id=program_id, hours=hours)
    directory = program_dir(workspace_root, program_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "daily_plan.md").write_text(hourly_plan_to_markdown(plan) + "\n", encoding="utf-8")
    (directory / "daily_plan.json").write_text(json.dumps(to_jsonable(plan), ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


def append_program_event(workspace_root: str | Path, program_id: str, event: dict[str, Any]) -> dict[str, Any]:
    directory = program_dir(workspace_root, program_id)
    directory.mkdir(parents=True, exist_ok=True)
    record = dict(event)
    record.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    path = directory / "timeline.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(to_jsonable(record), ensure_ascii=False) + "\n")
    return {"path": str(path), "record": record}


def program_to_markdown(program: dict[str, Any]) -> str:
    tasks = program.get("tasks") if isinstance(program.get("tasks"), list) else []
    lines = ["## DevPacer Program", ""]
    lines.append(f"Program: `{program.get('program_id')}`")
    lines.append(f"Status: `{program.get('status')}`")
    lines.append(f"Objective: {program.get('objective')}")
    lines.append(f"Tasks: `{len(tasks)}`")
    lines.append(f"Autonomy: `{((program.get('autonomy_policy') or {}).get('mode') or 'supervised')}`")
    lines.append(f"Next action: {program.get('next_action') or ''}")
    if tasks:
        lines.extend(["", "### Tasks", ""])
        for task in tasks:
            deps = ", ".join(task.get("depends_on") or []) or "none"
            lines.append(
                f"- `{task.get('task_id')}` [{task.get('status')}] {task.get('objective')} "
                f"tier={task.get('worker_tier')} estimate={task.get('estimated_minutes')}m deps={deps}"
            )
    return "\n".join(lines).rstrip()


def programs_to_markdown(items: list[dict[str, Any]]) -> str:
    if not items:
        return "No DevPacer programs yet."
    lines = ["## DevPacer Programs", ""]
    for item in items:
        lines.append(f"- `{item.get('program_id')}` [{item.get('status')}] tasks={item.get('task_count')} {item.get('objective')}")
    return "\n".join(lines)


def payload_to_json(payload: dict[str, Any] | list[dict[str, Any]]) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2)


def _task_from_draft(draft: dict[str, Any], *, previous_task_id: str, agent: str, test_command: str) -> dict[str, Any]:
    objective = str(draft.get("objective") or "")
    risk = _risk_for_objective(objective)
    tier = _tier_for_objective(objective, risk)
    minutes = _estimate_minutes(objective, risk, tier)
    task_id = f"task-{int(draft.get('index') or 0):03d}"
    return {
        "task_id": task_id,
        "objective": objective,
        "source_line": draft.get("source_line"),
        "source_type": draft.get("source_type"),
        "depends_on": [previous_task_id] if previous_task_id else [],
        "risk": risk,
        "worker_tier": tier,
        "agent": agent,
        "estimated_minutes": minutes,
        "estimated_strong_minutes": minutes if tier == "strong" else 0,
        "estimated_rounds": 1 if risk in {"low", "external"} else 2,
        "test_command": test_command,
        "acceptance_mode": "manual_review" if risk == "external" else ("command" if test_command else "best_effort"),
        "status": "pending",
        "mission_id": "",
        "queue_id": "",
    }


def _risk_for_objective(objective: str) -> str:
    text = objective.lower()
    external_markers = ("生产", "容器", "数据库", "db", "secret", "密钥", "部署", "支付", "真实账号", "production")
    if any(marker in text for marker in external_markers):
        return "external"
    high_markers = ("架构", "重构", "并发", "权限", "认证", "auth", "migration", "支付")
    if any(marker in text for marker in high_markers):
        return "high"
    low_markers = ("文案", "样式", "readme", "docs", "日志", "配置", "rename", "copy")
    if any(marker in text for marker in low_markers):
        return "low"
    return "medium"


def _tier_for_objective(objective: str, risk: str) -> str:
    if risk == "external":
        return "research"
    if risk == "low":
        return "cheap"
    if re.search(r"(架构|重构|核心|语音|支付|auth|认证|并发)", objective, re.IGNORECASE):
        return "strong"
    return "strong"


def _estimate_minutes(objective: str, risk: str, tier: str) -> int:
    if risk == "external":
        return 20
    if tier == "cheap":
        return 20
    length_bonus = min(30, max(0, len(objective) // 8))
    if risk == "high":
        return 75 + length_bonus
    return 45 + length_bonus
