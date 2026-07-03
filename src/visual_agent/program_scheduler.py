from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .chief_queue import mission_queue_item_to_dict, submit_mission_queue_item
from .chief_run import run_chief_mission
from .milestone_checkpoint import generate_milestone_checkpoint
from .models import to_jsonable
from .programs import (
    append_program_event,
    build_program_hourly_plan,
    load_program,
    refresh_daily_plan,
    save_program,
)
from .reference_research import build_reference_pack, save_reference_pack


MissionRunner = Callable[..., dict[str, Any]]


def start_program(
    *,
    workspace_root: str | Path,
    program_id: str,
    hours: float = 5.0,
    queue: bool = True,
    mission_runner: MissionRunner | None = None,
) -> dict[str, Any]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    program = load_program(workspace_path, program_id)
    if program is None:
        raise FileNotFoundError(f"No saved program found: {program_id}")
    hourly = refresh_daily_plan(workspace_path, program_id, hours=hours)
    scheduled = hourly.get("scheduled") if isinstance(hourly.get("scheduled"), list) else []
    tasks = program.get("tasks") if isinstance(program.get("tasks"), list) else []
    by_id = {str(task.get("task_id")): task for task in tasks if isinstance(task, dict)}
    runner = mission_runner or run_chief_mission
    created: list[dict[str, Any]] = []
    queued: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for slot in scheduled:
        task = by_id.get(str(slot.get("task_id") or ""))
        if not task:
            continue
        mode = str(slot.get("mode") or "")
        pack = build_reference_pack(objective=str(task.get("objective") or ""), repo_root=program.get("repo_root") or ".", task_id=str(task.get("task_id") or ""))
        saved_pack = save_reference_pack(workspace_root=workspace_path, program_id=program_id, pack=pack)
        task["reference_pack_path"] = saved_pack["path"]
        if mode == "research_or_doc":
            task["status"] = "needs_review"
            task["block_reason"] = "research/doc only in current quota window"
            blocked.append({"task_id": task.get("task_id"), "reason": task["block_reason"], "reference_pack_path": saved_pack["path"]})
            continue
        task_goal = _dispatch_goal_for_task(task)
        result = runner(
            goal=task_goal,
            workspace_root=workspace_path,
            repo_root=program.get("repo_root") or ".",
            agents=((str(task.get("agent") or "codex"),) if task.get("agent") else ()),
            execute=False,
            dry_run=True,
            test_command=str(task.get("test_command") or "") or None,
            max_wall_minutes=max(30, int(task.get("estimated_minutes") or 45)),
            max_worker_minutes=max(15, int(task.get("estimated_minutes") or 45)),
        )
        mission = result.get("mission") if isinstance(result.get("mission"), dict) else {}
        mission_id = str(mission.get("mission_id") or "")
        task["mission_id"] = mission_id
        task["status"] = "preview_created"
        created.append({"task_id": task.get("task_id"), "mission_id": mission_id, "status": result.get("status"), "stop_reason": result.get("stop_reason")})
        if queue and mission_id and str(result.get("status") or "") == "preview":
            item = submit_mission_queue_item(
                workspace_root=workspace_path,
                mission_id=mission_id,
                agent=str(task.get("agent") or "codex"),
                test_command=str(task.get("test_command") or "") or None,
                merge_policy="manual",
                priority=_priority_for_mode(mode),
            )
            task["queue_id"] = item.queue_id
            task["status"] = "queued"
            queued.append({"task_id": task.get("task_id"), **mission_queue_item_to_dict(item)})

    for item in hourly.get("blocked") or []:
        task = by_id.get(str(item.get("task_id") or ""))
        if task:
            task["status"] = "blocked"
            task["block_reason"] = item.get("reason")
            blocked.append({"task_id": task.get("task_id"), "reason": item.get("reason")})
    program["tasks"] = tasks
    program["status"] = "running" if queued else ("paused" if blocked or hourly.get("deferred") else "planning")
    program["next_action"] = _next_action(program, hourly, queued, blocked)
    save_program(workspace_path, program)
    append_program_event(
        workspace_path,
        program_id,
        {"event": "started", "created": len(created), "queued": len(queued), "blocked": len(blocked), "hours": hours},
    )

    # Layer 3: if any tasks are already verified (re-run of a program with
    # partial completion), emit a milestone checkpoint so the user gets a
    # consolidated review sheet without having to ask.
    milestone_result: dict[str, Any] | None = None
    verified_so_far = [t for t in tasks if str(t.get("status") or "") == "verified"]
    if verified_so_far:
        try:
            label = str(program.get("objective") or program_id)[:60]
            program_ws = workspace_path / "programs" / program_id
            milestone_result = generate_milestone_checkpoint(
                milestone_label=label,
                completed_tasks=verified_so_far,
                workspace_root=program_ws,
                repo_root=program.get("repo_root"),
            )
        except Exception:  # noqa: BLE001 — checkpoint failure must never block dispatch
            milestone_result = None

    result: dict[str, Any] = {
        "schema_version": 1,
        "product": "DevPacer",
        "kind": "program_start",
        "program_id": program_id,
        "status": program["status"],
        "hourly_plan": hourly,
        "created_missions": created,
        "queued_items": queued,
        "blocked_tasks": blocked,
        "program": program,
    }
    if milestone_result:
        result["milestone_checkpoint"] = {
            "label": milestone_result.get("milestone_label"),
            "task_count": milestone_result.get("task_count"),
            "saved_path": milestone_result.get("saved_path"),
            "markdown": milestone_result.get("markdown"),
        }
    return result


def emit_milestone_checkpoint(
    *,
    workspace_root: str | Path,
    program_id: str,
    milestone_label: str | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Generate a Layer-3 human review sheet for the program's verified tasks.

    Finds all tasks in the program with status "verified", groups them under
    the given milestone_label (or defaults to the program objective), and
    writes a Markdown checkpoint file to
    ``<workspace>/programs/<program_id>/milestones/``.

    Call this:
    - Manually after a batch of queue tasks finishes ("checkpoint program milestone <id>")
    - Automatically from chief_queue when it detects a milestone boundary
    """
    ws = Path(workspace_root).expanduser().resolve()
    program = load_program(ws, program_id)
    if program is None:
        return {"status": "error", "reason": f"Program not found: {program_id}"}

    tasks = program.get("tasks") if isinstance(program.get("tasks"), list) else []
    verified_tasks = [t for t in tasks if isinstance(t, dict) and str(t.get("status") or "") == "verified"]
    if not verified_tasks:
        return {"status": "skipped", "reason": "No verified tasks in program yet."}

    label = (milestone_label or str(program.get("objective") or program_id))[:60]
    program_ws = ws / "programs" / program_id

    checkpoint = generate_milestone_checkpoint(
        milestone_label=label,
        completed_tasks=verified_tasks,
        workspace_root=program_ws,
        repo_root=repo_root,
    )
    append_program_event(ws, program_id, {"event": "milestone_checkpoint", "label": label, "task_count": len(verified_tasks), "saved_path": checkpoint.get("saved_path")})
    return {"status": "generated", "label": label, "task_count": len(verified_tasks), **checkpoint}


def program_start_to_markdown(payload: dict[str, Any]) -> str:
    lines = ["## DevPacer Program Start", ""]
    lines.append(f"Program: `{payload.get('program_id')}`")
    lines.append(f"Status: `{payload.get('status')}`")
    created = payload.get("created_missions") if isinstance(payload.get("created_missions"), list) else []
    queued = payload.get("queued_items") if isinstance(payload.get("queued_items"), list) else []
    blocked = payload.get("blocked_tasks") if isinstance(payload.get("blocked_tasks"), list) else []
    lines.append(f"Created missions: `{len(created)}`")
    lines.append(f"Queued items: `{len(queued)}`")
    lines.append(f"Blocked/research tasks: `{len(blocked)}`")
    if queued:
        lines.extend(["", "### Queued", ""])
        for item in queued:
            lines.append(f"- `{item.get('task_id')}` -> `{item.get('queue_id')}` agent={item.get('agent')}")
    if blocked:
        lines.extend(["", "### Blocked / Research", ""])
        for item in blocked:
            lines.append(f"- `{item.get('task_id')}` {item.get('reason')}")
    return "\n".join(lines).rstrip()


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2)


def _priority_for_mode(mode: str) -> int:
    if mode == "strong_worker":
        return 10
    if mode == "cheap_worker":
        return 5
    return 0


def _dispatch_goal_for_task(task: dict[str, Any]) -> str:
    objective = str(task.get("objective") or "").strip()
    test_command = str(task.get("test_command") or "").strip()
    parts = [objective]
    parts.append("Complete this imported program task as a concrete code change, keeping scope limited to the objective.")
    if test_command:
        parts.append(f"Done when `{test_command}` passes without weakening tests.")
    else:
        parts.append("Done when the smallest relevant project check passes and the change is documented in the final report.")
    return "\n".join(part for part in parts if part)


def _next_action(program: dict[str, Any], hourly: dict[str, Any], queued: list[dict[str, Any]], blocked: list[dict[str, Any]]) -> str:
    if queued:
        return "Run `checkpoint mission worker --watch` to execute queued program tasks."
    if hourly.get("deferred"):
        return "Wait for 5h quota reset or run cheap/research tasks only."
    if blocked:
        return "Review blocked tasks and provide credentials, external access, or decisions."
    return "No ready tasks were scheduled."
