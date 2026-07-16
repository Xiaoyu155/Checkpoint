from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .chief_queue import mission_queue_item_to_dict, submit_mission_queue_item
from .chief_run import run_chief_mission
from .milestone_checkpoint import generate_milestone_checkpoint
from .models import to_jsonable
from .programs import (
    append_program_event,
    load_program,
    ready_program_tasks,
    refresh_daily_plan,
    save_program,
)
from .reference_research import build_reference_pack, save_reference_pack


MissionRunner = Callable[..., dict[str, Any]]

# Mission outcomes that map to a terminal program-task status.
_MISSION_VERIFIED = {"verified"}
_MISSION_FAILED = {"stopped", "failed", "worker_failed", "verification_failed"}
_MISSION_TERMINAL_BLOCKED = {"verified_blocked"}
# Task statuses the sync may update. "failed" stays syncable because a retried
# mission can verify later (V5: task-001 failed four times, then verified);
# only verified/done are final.
_TASK_IN_FLIGHT = {"queued", "preview_created", "running"}
_TASK_SYNCABLE = _TASK_IN_FLIGHT | {"failed", "blocked"}


def sync_program_tasks(*, workspace_root: str | Path, program_id: str) -> dict[str, Any]:
    """Pull mission outcomes back into program task statuses.

    Without this sync a sequential program can never advance: task N stays
    "queued" forever even after its mission verified, so task N+1 never
    becomes ready (found by V5 cold-start validation).
    """
    from .missions import load_mission

    workspace_path = Path(workspace_root).expanduser().resolve()
    program = load_program(workspace_path, program_id)
    if program is None:
        raise FileNotFoundError(f"No saved program found: {program_id}")
    tasks = program.get("tasks") if isinstance(program.get("tasks"), list) else []
    updated: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_status = str(task.get("status") or "")
        if task_status not in _TASK_SYNCABLE:
            continue
        mission_id = str(task.get("mission_id") or "")
        if not mission_id:
            continue
        mission = load_mission(workspace_path, mission_id)
        if not mission:
            continue
        mission_status = str(mission.get("status") or "")
        stop_reason = str(mission.get("stop_reason") or "")
        if mission_status in _MISSION_VERIFIED or stop_reason == "verified":
            task["status"] = "verified"
            task.pop("block_reason", None)
            task.pop("terminal_outcome", None)
            updated.append({"task_id": task.get("task_id"), "status": "verified"})
        elif mission_status in _MISSION_TERMINAL_BLOCKED:
            task["status"] = "blocked"
            task["block_reason"] = stop_reason or "verified_blocked"
            task["terminal_outcome"] = "verified_blocked"
            updated.append(
                {
                    "task_id": task.get("task_id"),
                    "status": "blocked",
                    "reason": task["block_reason"],
                }
            )
        elif (
            task_status in _TASK_IN_FLIGHT
            and mission_status in _MISSION_FAILED
            and stop_reason not in {"", "verified"}
        ):
            task["status"] = "failed"
            task["block_reason"] = stop_reason
            updated.append({"task_id": task.get("task_id"), "status": "failed", "reason": stop_reason})
    previous_program_status = str(program.get("status") or "")
    terminal_blocked = any(
        isinstance(task, dict) and str(task.get("terminal_outcome") or "") == "verified_blocked"
        for task in tasks
    )
    completed = bool(tasks) and all(
        isinstance(task, dict) and str(task.get("status") or "") in {"verified", "done"}
        for task in tasks
    )
    if terminal_blocked:
        program["status"] = "blocked"
        program["next_action"] = "Review the verified_blocked mission before resuming this program."
    elif completed:
        program["status"] = "completed"
        program["next_action"] = "All program tasks are verified."
    elif previous_program_status in {"blocked", "completed"}:
        program["status"] = "running" if any(
            isinstance(task, dict) and str(task.get("status") or "") in _TASK_IN_FLIGHT for task in tasks
        ) else "planning"
    if updated or str(program.get("status") or "") != previous_program_status:
        program["tasks"] = tasks
        save_program(workspace_path, program)
        append_program_event(
            workspace_path,
            program_id,
            {"event": "tasks_synced", "updated": updated, "program_status": program.get("status")},
        )
    return {"program_id": program_id, "updated": updated, "program_status": program.get("status")}


def advance_program_for_mission(*, workspace_root: str | Path, mission_id: str) -> dict[str, Any] | None:
    """After a queue item finishes, sync its program and queue newly-ready tasks.

    Called by the mission queue worker so a sequential program keeps moving
    without the user re-running `program start` by hand. Returns None when the
    mission does not belong to any program.
    """
    from .programs import list_programs

    workspace_path = Path(workspace_root).expanduser().resolve()
    target = str(mission_id or "")
    if not target:
        return None
    for summary in list_programs(workspace_path):
        program_id = str(summary.get("program_id") or "")
        program = load_program(workspace_path, program_id)
        if program is None:
            continue
        tasks = program.get("tasks") if isinstance(program.get("tasks"), list) else []
        if not any(str(t.get("mission_id") or "") == target for t in tasks if isinstance(t, dict)):
            continue
        synced = sync_program_tasks(workspace_root=workspace_path, program_id=program_id)
        result: dict[str, Any] = {"program_id": program_id, "synced": synced.get("updated") or []}
        refreshed = load_program(workspace_path, program_id) or program
        result["status"] = str(refreshed.get("status") or "")
        if result["status"] not in {"blocked", "completed"} and ready_program_tasks(refreshed):
            started = start_program(workspace_root=workspace_path, program_id=program_id)
            result["queued_items"] = started.get("queued_items") or []
        else:
            result["queued_items"] = []
        return result
    return None


def start_program(
    *,
    workspace_root: str | Path,
    program_id: str,
    hours: float = 5.0,
    queue: bool = True,
    mission_runner: MissionRunner | None = None,
    autonomous: bool | None = None,
) -> dict[str, Any]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    if load_program(workspace_path, program_id) is None:
        raise FileNotFoundError(f"No saved program found: {program_id}")
    # Pull mission outcomes into task statuses first, so re-running
    # `program start` advances a sequential program past verified tasks.
    sync_program_tasks(workspace_root=workspace_path, program_id=program_id)
    program = load_program(workspace_path, program_id)
    autonomy_policy = program.get("autonomy_policy") if isinstance(program.get("autonomy_policy"), dict) else {}
    autonomous_mode = bool(autonomous) if autonomous is not None else str(autonomy_policy.get("mode") or "") == "autonomous"
    if autonomous_mode:
        model_routing = (
            autonomy_policy.get("model_routing")
            if isinstance(autonomy_policy.get("model_routing"), dict)
            else {}
        )
        closed_loop = (
            autonomy_policy.get("closed_loop")
            if isinstance(autonomy_policy.get("closed_loop"), dict)
            else {}
        )
        autonomy_policy = {
            "mode": "autonomous",
            "dispatch_mode": "delegated",
            "reasoning_effort": str(autonomy_policy.get("reasoning_effort") or "inherit"),
            "max_rounds": max(3, int(autonomy_policy.get("max_rounds") or 8)),
            "max_repair_rounds": max(2, int(autonomy_policy.get("max_repair_rounds") or 7)),
            "max_wall_minutes": max(60, int(autonomy_policy.get("max_wall_minutes") or 480)),
            "max_worker_minutes": max(45, int(autonomy_policy.get("max_worker_minutes") or 240)),
            "max_total_tokens": max(
                1,
                int(autonomy_policy.get("max_total_tokens") or 120_000),
            ),
            "max_same_failure_count": max(
                1,
                int(autonomy_policy.get("max_same_failure_count") or 2),
            ),
            "allow_coverage_gap": True,
            "run_profile": "supervised",
            "allow_dirty": bool(autonomy_policy.get("allow_dirty", False)),
            "merge_policy": str(autonomy_policy.get("merge_policy") or "manual"),
            "model_routing": dict(model_routing),
            "closed_loop": dict(closed_loop),
        }
        program["autonomy_policy"] = autonomy_policy
        roadmap_block = _roadmap_integrity_block(program, autonomy_policy)
        if roadmap_block:
            program["status"] = "blocked"
            program["next_action"] = roadmap_block
            save_program(workspace_path, program)
            append_program_event(
                workspace_path,
                program_id,
                {"event": "roadmap_integrity_blocked", "reason": roadmap_block},
            )
            return {
                "schema_version": 1,
                "product": "DevPacer",
                "kind": "program_start",
                "program_id": program_id,
                "status": "blocked",
                "hourly_plan": {},
                "created_missions": [],
                "queued_items": [],
                "blocked_tasks": [{"task_id": "", "reason": roadmap_block}],
                "program": program,
            }
        quota_policy = program.get("quota_policy") if isinstance(program.get("quota_policy"), dict) else {}
        quota_policy["quota_mode"] = "unrestricted"
        quota_policy["reserve_minutes"] = 0
        quota_policy["pause_at_used_percentage"] = 101
        program["quota_policy"] = quota_policy
        save_program(workspace_path, program)
    if str(program.get("status") or "") in {"blocked", "completed"}:
        return {
            "schema_version": 1,
            "product": "DevPacer",
            "kind": "program_start",
            "program_id": program_id,
            "status": program.get("status"),
            "hourly_plan": {},
            "created_missions": [],
            "queued_items": [],
            "blocked_tasks": [
                {"task_id": task.get("task_id"), "reason": task.get("block_reason")}
                for task in (program.get("tasks") or [])
                if isinstance(task, dict) and str(task.get("status") or "") == "blocked"
            ],
            "program": program,
        }
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
        if mode == "research_or_doc" and not autonomous_mode:
            task["status"] = "needs_review"
            task["block_reason"] = "research/doc only in current quota window"
            blocked.append({"task_id": task.get("task_id"), "reason": task["block_reason"], "reference_pack_path": saved_pack["path"]})
            continue
        upstream_mission_ids = [
            str(by_id.get(str(dependency), {}).get("mission_id") or "")
            for dependency in task.get("depends_on") or []
            if str(by_id.get(str(dependency), {}).get("mission_id") or "")
        ]
        task_goal = _dispatch_goal_for_task(
            task,
            autonomous=autonomous_mode,
            program_id=str(program.get("program_id") or ""),
            upstream_mission_ids=upstream_mission_ids,
        )
        mission_options: dict[str, Any] = {}
        if autonomous_mode:
            route_mode = mode
            if mode == "delegated_worker":
                route_mode = {
                    "strong": "strong_worker",
                    "cheap": "cheap_worker",
                    "research": "research_or_doc",
                }.get(str(task.get("worker_tier") or ""), mode)
            selected_model = str(
                (autonomy_policy.get("model_routing") or {}).get(route_mode) or "inherit"
            ).strip()
            repair_model = str(
                (autonomy_policy.get("model_routing") or {}).get("strong_worker")
                or (autonomy_policy.get("model_routing") or {}).get("delegated_worker")
                or "strong"
            ).strip()
            mission_options = {
                "max_rounds": int(autonomy_policy["max_rounds"]),
                "max_repair_rounds": int(autonomy_policy["max_repair_rounds"]),
                "max_total_tokens": int(autonomy_policy["max_total_tokens"]),
                "max_same_failure_count": int(autonomy_policy["max_same_failure_count"]),
                "max_wall_minutes": max(
                    int(autonomy_policy["max_wall_minutes"]),
                    int(task.get("estimated_minutes") or 45) * 4,
                ),
                "max_worker_minutes": max(
                    int(autonomy_policy["max_worker_minutes"]),
                    int(task.get("estimated_minutes") or 45) * 2,
                ),
                "dispatch_mode": "delegated",
                "prompt_style": "expanded",
                "repair_strategy": "resume",
                "reasoning_effort": str(autonomy_policy["reasoning_effort"]),
                "model_policy": {
                    "implementation": selected_model,
                    "repair": repair_model,
                    "classification": "fast",
                    "visual_review": "multimodal",
                },
                "execution_policy": {
                    **dict(autonomy_policy.get("closed_loop") or {}),
                    "program_id": str(program_id),
                    "task_id": str(task.get("task_id") or ""),
                    "source_plan": str(program.get("source_plan") or ""),
                    "source_plan_sha256": str(program.get("source_plan_sha256") or ""),
                },
            }
        result = runner(
            goal=task_goal,
            workspace_root=workspace_path,
            repo_root=program.get("repo_root") or ".",
            agents=((str(task.get("agent") or "codex"),) if task.get("agent") else ()),
            execute=False,
            dry_run=True,
            # Imported Program tasks are already grounded by the locked source
            # plan. Re-grounding on words such as "roadmap" can turn a concrete
            # autonomous task back into an unnecessary clarification request.
            ground_vague_goals=False,
            test_command=str(task.get("test_command") or "") or None,
            max_wall_minutes=mission_options.pop("max_wall_minutes", max(30, int(task.get("estimated_minutes") or 45))),
            max_worker_minutes=mission_options.pop("max_worker_minutes", max(15, int(task.get("estimated_minutes") or 45))),
            **mission_options,
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
                merge_policy=str(autonomy_policy.get("merge_policy") or "manual"),
                priority=_priority_for_mode(mode),
                run_profile=str(autonomy_policy.get("run_profile") or "dry-run") if autonomous_mode else "dry-run",
                max_workflows=30 if autonomous_mode else 10,
                timeout_seconds=float(autonomy_policy.get("max_worker_minutes") or 30) * 60 if autonomous_mode else 1800.0,
                allow_dirty=bool(autonomy_policy.get("allow_dirty", False)) if autonomous_mode else False,
                allow_coverage_gap=bool(autonomy_policy.get("allow_coverage_gap")) if autonomous_mode else False,
                reasoning_effort=str(autonomy_policy.get("reasoning_effort") or "inherit") if autonomous_mode else None,
                dispatch_mode="delegated" if autonomous_mode else None,
                prompt_style="expanded" if autonomous_mode else None,
                repair_strategy="resume" if autonomous_mode else None,
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
        {
            "event": "started",
            "created": len(created),
            "queued": len(queued),
            "blocked": len(blocked),
            "hours": hours,
            "autonomy_mode": "autonomous" if autonomous_mode else "supervised",
        },
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
    if mode == "delegated_worker":
        return 15
    if mode == "strong_worker":
        return 10
    if mode == "cheap_worker":
        return 5
    return 0


def _roadmap_integrity_block(program: dict[str, Any], autonomy_policy: dict[str, Any]) -> str:
    closed_loop = autonomy_policy.get("closed_loop") if isinstance(autonomy_policy.get("closed_loop"), dict) else {}
    if str(closed_loop.get("roadmap_mode") or "") != "locked":
        return ""
    source = Path(str(program.get("source_plan") or "")).expanduser()
    expected = str(program.get("source_plan_sha256") or closed_loop.get("source_plan_sha256") or "")
    if not source.is_file():
        return f"Locked roadmap source is missing: {source}"
    actual = hashlib.sha256(source.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    if expected and actual != expected:
        return "Locked roadmap source changed after program creation; create a new program or restore the plan."
    return ""


def _dispatch_goal_for_task(
    task: dict[str, Any],
    *,
    autonomous: bool = False,
    program_id: str = "",
    upstream_mission_ids: list[str] | None = None,
) -> str:
    objective = str(task.get("objective") or "").strip()
    test_command = str(task.get("test_command") or "").strip()
    parts = [objective]
    context = [f"program_id={program_id}" if program_id else "", f"task_id={task.get('task_id') or ''}"]
    if upstream_mission_ids:
        context.append("upstream_mission_ids=" + ",".join(upstream_mission_ids))
    parts.append("Closed-loop context: " + "; ".join(item for item in context if item))
    if autonomous:
        parts.append(
            "Own this task end to end. Explore the repository, choose the implementation path, and delegate or reorganize work as needed."
        )
    else:
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
