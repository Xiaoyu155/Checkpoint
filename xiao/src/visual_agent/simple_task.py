from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def run_simple_managed_task(
    goal: str,
    *,
    repo_root: str | Path = ".",
    workspace_root: str | Path = ".agent-workspace",
    codex_provider: str | None = None,
    worker_runner: Callable[..., dict[str, Any]] | None = None,
    progress_func: Callable[[str], Any] | None = None,
    progress_interval_seconds: float = 2.0,
    poll_seconds: float = 1.0,
    max_wait_seconds: float = 8 * 60 * 60,
) -> dict[str, Any]:
    """Run one natural-language request through the full autonomous Program path."""
    from .chief_queue import run_mission_queue_worker
    from .mission_intake import is_review_plan_goal
    from .program_scheduler import start_program, sync_program_tasks
    from .programs import create_program_from_plan, load_program, save_program
    from .workspace import init_workspace

    objective = " ".join(str(goal or "").split()).strip()
    if not objective:
        return {"status": "blocked", "reason": "Task is empty."}
    repo = Path(repo_root).expanduser().resolve()
    workspace = Path(workspace_root).expanduser().resolve()
    review_task = is_review_plan_goal(objective)
    if not review_task:
        from .verification_profiles import choose_verification_command

        verification_command = choose_verification_command(repo)
        if not verification_command:
            return {
                "schema_version": 1,
                "product": "Pacer",
                "status": "needs_input",
                "reason": "project_verification_unresolved",
                "message": _project_selection_message(repo),
                "program_id": "",
                "tasks": [],
            }
    else:
        verification_command = ""
    if not workspace.exists():
        init_workspace(workspace, with_demo=False)
    intake = workspace / "intake"
    intake.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(objective.encode("utf-8")).hexdigest()[:8]
    plan_path = intake / f"task-{stamp}-{digest}.md"
    plan_path.write_text(f"- [ ] {objective}\n", encoding="utf-8")

    program = create_program_from_plan(
        source_file=plan_path,
        workspace_root=workspace,
        repo_root=repo,
        objective=objective,
        agent="codex",
        test_command=None if review_task else verification_command,
        sequential=True,
        limit=None,
        autonomous=True,
        allow_dirty=True,
        codex_provider=codex_provider,
        memory_mode="enabled",
        acceptance_policy="strict",
    )
    autonomy = program.get("autonomy_policy") if isinstance(program.get("autonomy_policy"), dict) else {}
    autonomy["merge_policy"] = "auto"
    program["autonomy_policy"] = autonomy
    save_program(workspace, program)
    start = start_program(workspace_root=workspace, program_id=str(program["program_id"]), hours=8, autonomous=True)
    if start.get("blocked_tasks"):
        return {"status": "blocked", "program": start.get("program"), "blocked_tasks": start["blocked_tasks"]}

    started_message = f"Pacer Program {program['program_id']} 已启动，正在托管开发与验收..."
    if progress_func:
        progress_func(started_message)
    else:
        print(started_message, file=sys.stderr)
    runner = worker_runner or run_mission_queue_worker
    deadline = time.monotonic() + max(1.0, float(max_wait_seconds))
    worker_runs: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        current = load_program(workspace, str(program["program_id"])) or program
        tasks = [item for item in current.get("tasks") or [] if isinstance(item, dict)]
        statuses = {str(item.get("status") or "") for item in tasks}
        if tasks and statuses <= {"verified", "done"}:
            sync_program_tasks(workspace_root=workspace, program_id=str(program["program_id"]))
            current = load_program(workspace, str(program["program_id"])) or current
            return _simple_result(current, worker_runs, plan_path)
        if statuses & {"failed", "blocked"}:
            return _simple_result(current, worker_runs, plan_path)
        worker_payload = _run_worker_with_progress(
            runner,
            workspace=workspace,
            program_id=str(program["program_id"]),
            progress_func=progress_func,
            interval_seconds=progress_interval_seconds,
        )
        worker_runs.append(worker_payload)
        sync_program_tasks(workspace_root=workspace, program_id=str(program["program_id"]))
        if not worker_payload.get("ran") and str(worker_payload.get("status") or "") == "idle":
            time.sleep(max(0.1, float(poll_seconds)))
    current = load_program(workspace, str(program["program_id"])) or program
    return {**_simple_result(current, worker_runs, plan_path), "status": "timeout"}


def _run_worker_with_progress(
    runner: Callable[..., dict[str, Any]],
    *,
    workspace: Path,
    program_id: str,
    progress_func: Callable[[str], Any] | None,
    interval_seconds: float,
) -> dict[str, Any]:
    if progress_func is None:
        return runner(workspace_root=workspace, run_once=True, watch=False)
    stopped = threading.Event()
    monitor = threading.Thread(
        target=_monitor_worker_progress,
        kwargs={
            "workspace": workspace,
            "program_id": program_id,
            "progress_func": progress_func,
            "stopped": stopped,
            "interval_seconds": interval_seconds,
        },
        daemon=True,
    )
    monitor.start()
    try:
        return runner(workspace_root=workspace, run_once=True, watch=False)
    finally:
        stopped.set()
        monitor.join(timeout=max(1.0, float(interval_seconds) * 2))


def _monitor_worker_progress(
    *,
    workspace: Path,
    program_id: str,
    progress_func: Callable[[str], Any],
    stopped: threading.Event,
    interval_seconds: float,
) -> None:
    from .programs import load_program

    started = time.monotonic()
    last_heartbeat = -30.0
    seen_messages: set[str] = set()
    interval = max(0.05, float(interval_seconds))
    while not stopped.wait(interval):
        program = load_program(workspace, program_id) or {}
        tasks = [item for item in program.get("tasks") or [] if isinstance(item, dict)]
        mission_id = str(tasks[0].get("mission_id") or "") if tasks else ""
        progress = _read_json(workspace / "missions" / mission_id / "progress.json") if mission_id else {}
        elapsed = time.monotonic() - started
        if elapsed - last_heartbeat >= 15.0:
            stage = str(progress.get("stage_label") or progress.get("stage") or "准备 worker")
            progress_func(f"[{_format_elapsed(elapsed)}] {stage}")
            last_heartbeat = elapsed
        log_path = Path(str(progress.get("latest_log_path") or ""))
        if not log_path.is_file():
            continue
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if not isinstance(item, dict) or item.get("type") != "agent_message":
                continue
            text = str(item.get("text") or "").strip()
            message_id = str(item.get("id") or hashlib.sha256(text.encode("utf-8")).hexdigest())
            if text and message_id not in seen_messages:
                seen_messages.add(message_id)
                progress_func(f"Codex: {text}")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def _project_selection_message(repo: Path) -> str:
    from .verification_profiles import choose_verification_command

    candidates: list[str] = []
    try:
        children = sorted((path for path in repo.iterdir() if path.is_dir()), key=lambda path: path.name.lower())
    except OSError:
        children = []
    for child in children:
        if child.name.startswith((".", "_")) or ".checkpoint-worktrees" in child.name:
            continue
        command = choose_verification_command(child)
        if command:
            candidates.append(f"- {child}（验收：{command}）")
        if len(candidates) >= 5:
            break
    lines = [
        f"当前目录 {repo} 不是可直接验收的项目根目录，因此没有创建 Program，也没有调用 Codex。",
        "请先进入具体项目目录再启动 pacer。",
    ]
    if candidates:
        lines.extend(["检测到这些候选项目：", *candidates])
    return "\n".join(lines)


def _simple_result(program: dict[str, Any], worker_runs: list[dict[str, Any]], plan_path: Path) -> dict[str, Any]:
    tasks = [item for item in program.get("tasks") or [] if isinstance(item, dict)]
    statuses = [str(item.get("status") or "") for item in tasks]
    if tasks and all(status in {"verified", "done"} for status in statuses):
        status = "completed"
    elif any(status in {"failed", "blocked"} for status in statuses):
        status = "failed"
    else:
        status = str(program.get("status") or "running")
    payload = {
        "schema_version": 1,
        "product": "Pacer",
        "status": status,
        "program_id": str(program.get("program_id") or ""),
        "plan_path": str(plan_path),
        "tasks": [
            {
                "task_id": str(item.get("task_id") or ""),
                "mission_id": str(item.get("mission_id") or ""),
                "status": str(item.get("status") or ""),
                "reason": str(item.get("block_reason") or ""),
            }
            for item in tasks
        ],
        "worker_runs": worker_runs,
    }
    from .mission_intake import is_review_plan_goal

    if is_review_plan_goal(str(program.get("objective") or "")) and tasks:
        mission_id = str(tasks[-1].get("mission_id") or "")
        report_path = Path(str(program.get("workspace_root") or "")) / "missions" / mission_id / "final_report.md"
        if mission_id and report_path.is_file():
            payload["review_report_path"] = str(report_path)
            payload["review_report"] = report_path.read_text(encoding="utf-8")
    return payload


def simple_result_to_markdown(payload: dict[str, Any]) -> str:
    if payload.get("status") == "needs_input":
        return str(payload.get("message") or "任务信息不足，请补充项目路径和验收标准。")
    lines = [
        f"Pacer: {payload.get('status')}",
        f"Program: {payload.get('program_id')}",
    ]
    for task in payload.get("tasks") or []:
        suffix = f" - {task.get('reason')}" if task.get("reason") else ""
        lines.append(f"- {task.get('task_id')}: {task.get('status')} ({task.get('mission_id')}){suffix}")
    if payload.get("review_report_path"):
        lines.append(f"审查报告：{payload['review_report_path']}")
    if payload.get("review_report"):
        lines.extend(["", str(payload["review_report"]).strip()])
    return "\n".join(lines)
