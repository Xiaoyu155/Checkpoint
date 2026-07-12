"""CLI handlers for all DevPacer chief-engineer / mission / program commands.

Extracted from cli.py to keep main() as a thin dispatcher.
Each function takes the parsed argparse Namespace and returns an exit code.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any


def _fmt_err(exc: Exception, *, command: str = "") -> str:
    """Minimal error formatter — avoids importing from cli.py (circular)."""
    msg = str(exc).strip()
    hints = {
        "workspace does not exist": "visual-agent init --root .agent-workspace",
        "no such file or directory": "Check the path from the project root, or rerun with an absolute path.",
        "filenotfounderror": "Check the path from the project root, or rerun with an absolute path.",
    }
    for key, suggestion in hints.items():
        if key in msg.lower():
            return f"Error: {msg}\nTry: {suggestion}"
    return f"Error: {msg}"


# ── constants ─────────────────────────────────────────────────────────────────

CHIEF_COMMANDS = {
    "chief-plan", "chief-plans", "chief-dispatch", "chief-run", "chief-run-demo",
    "chief-missions", "chief-memory", "repo-map", "refine-goal",
    "quota", "quota-statusline",
    "program", "autopilot",
    "mission",
    "chief-status", "chief-queue", "chief-worker", "chief-background-worker",
    "dashboard", "app", "agents",
}


def handle_chief_command(args: Any) -> int:
    """Dispatch to the appropriate handler based on args.command."""
    cmd = args.command
    handlers = {
        "chief-plan":              _handle_chief_plan,
        "chief-plans":             _handle_chief_plans,
        "chief-dispatch":          _handle_chief_dispatch,
        "chief-run":               _handle_chief_run,
        "chief-run-demo":          _handle_chief_run_demo,
        "chief-missions":          _handle_chief_missions,
        "chief-memory":            _handle_chief_memory,
        "repo-map":                _handle_repo_map,
        "refine-goal":             _handle_refine_goal,
        "quota":                   _handle_quota,
        "quota-statusline":        _handle_quota_statusline,
        "program":                 _handle_program,
        "autopilot":               _handle_autopilot,
        "mission":                 _handle_mission,
        "chief-status":            _handle_chief_status,
        "chief-queue":             _handle_chief_queue,
        "chief-worker":            _handle_chief_worker,
        "chief-background-worker": _handle_chief_background_worker,
        "dashboard":               _handle_dashboard,
        "app":                     _handle_app,
        "agents":                  _handle_agents,
    }
    handler = handlers.get(cmd)
    if handler is None:
        return 2
    return handler(args)


# ── chief-plan ────────────────────────────────────────────────────────────────

def _handle_chief_plan(args: Any) -> int:
    from .chief_engineer import build_chief_plan, chief_plan_to_dict, chief_plan_to_markdown

    plan = build_chief_plan(
        goal=args.goal,
        workspace_root=args.workspace_root,
        repo_root=args.repo_root,
        base=args.base,
        agents=tuple(args.agent or ()),
        include_slow=args.include_slow,
        max_workflows=args.max_workflows,
        run_profile=args.run_profile,
        interview=getattr(args, "interview", False),
        answers=tuple(getattr(args, "answer", []) or ()),
    )
    plan_dict = chief_plan_to_dict(plan)
    if getattr(args, "save", False):
        from .chief_plans_store import save_plan
        saved = save_plan(plan_dict, workspace_root=args.workspace_root)
        plan_dict["plan_id"] = saved["plan_id"]
        plan_dict["saved_path"] = saved["path"]
    if args.format == "json":
        rendered = json.dumps(plan_dict, ensure_ascii=False, indent=2)
    else:
        rendered = chief_plan_to_markdown(plan)
        if plan_dict.get("saved_path"):
            rendered += f"\n\nSaved plan: `{plan_dict['plan_id']}` -> {plan_dict['saved_path']}"
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if plan.status not in {"blocked", "needs_clarification"} else 1


def _handle_chief_plans(args: Any) -> int:
    from .chief_plans_store import list_plans, list_plans_to_markdown, load_plan

    if args.action == "list":
        summaries = list_plans(args.workspace_root)
        if args.format == "json":
            print(json.dumps(summaries, ensure_ascii=False, indent=2))
        else:
            print(list_plans_to_markdown(summaries))
        return 0
    plan_id = getattr(args, "plan_id", None)
    if not plan_id:
        print("chief-plans show requires a plan id. Run `chief-plans list` to find one.")
        return 1
    payload = load_plan(args.workspace_root, plan_id)
    if payload is None:
        print(f"No saved plan found with id: {plan_id}")
        return 1
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        from .chief_engineer import ChiefPlan, chief_plan_to_markdown as _plan_md
        known = {f.name for f in dataclasses.fields(ChiefPlan)}
        plan = ChiefPlan(**{k: v for k, v in payload.items() if k in known})
        print(_plan_md(plan))
    return 0


# ── chief-dispatch ────────────────────────────────────────────────────────────

def _handle_chief_dispatch(args: Any) -> int:
    from .chief_dispatch import chief_dispatch_to_markdown, dispatch_chief_plan, payload_to_json

    configured_repair_rounds = getattr(args, "max_repair_rounds", None)
    if configured_repair_rounds is None:
        configured_repair_rounds = 1 if args.auto_repair_once else 2
    payload = dispatch_chief_plan(
        test_command=getattr(args, "test_command", None),
        allow_test_edits=getattr(args, "allow_test_edits", False),
        merge=getattr(args, "merge", False),
        workspace_root=args.workspace_root,
        plan_id=args.plan,
        execute=bool(args.execute),
        dry_run=bool(args.dry_run or not args.execute),
        track_id=args.track_id,
        run_profile=args.run_profile,
        include_slow=args.include_slow,
        max_workflows=args.max_workflows,
        timeout_seconds=args.timeout_seconds,
        allow_dirty=args.allow_dirty,
        allow_coverage_gap=args.allow_coverage_gap,
        auto_repair_once=args.auto_repair_once,
        max_repair_rounds=configured_repair_rounds,
        reasoning_effort=getattr(args, "reasoning_effort", None),
        dispatch_mode=getattr(args, "dispatch_mode", "tracked"),
        prompt_style=getattr(args, "prompt_style", "expanded"),
        repair_strategy=getattr(args, "repair_strategy", "resume"),
    )
    if args.format == "json":
        print(payload_to_json(payload))
    else:
        print(chief_dispatch_to_markdown(payload))
    return 1 if str(payload.get("status") or "") in {"blocked", "worker_failed", "worker_toolchain_violation", "verification_failed"} else 0


# ── chief-run ─────────────────────────────────────────────────────────────────

def _run_chief_mission_args(args: Any, *, execute: bool, dry_run: bool, resume_mission_id: str | None = None) -> dict[str, Any]:
    from .chief_run import run_chief_mission
    return run_chief_mission(
        test_command=getattr(args, "test_command", None),
        allow_test_edits=getattr(args, "allow_test_edits", False),
        merge=getattr(args, "merge", False),
        require_env=tuple(getattr(args, "require_env", []) or ()),
        goal=args.goal,
        workspace_root=args.workspace_root,
        repo_root=args.repo_root,
        base=args.base,
        plan_id=args.plan,
        mission_id=getattr(args, "mission_id", None),
        resume_mission_id=resume_mission_id,
        agents=tuple(args.agent or ()),
        answers=tuple(getattr(args, "answer", []) or ()),
        interview=args.interview,
        max_rounds=args.max_rounds,
        max_repair_rounds=getattr(args, "max_repair_rounds", 2),
        max_wall_minutes=args.max_wall_minutes,
        max_worker_minutes=args.max_worker_minutes,
        execute=execute,
        dry_run=dry_run,
        run_profile=args.run_profile,
        include_slow=args.include_slow,
        max_workflows=args.max_workflows,
        timeout_seconds=args.timeout_seconds,
        allow_dirty=args.allow_dirty,
        allow_coverage_gap=args.allow_coverage_gap,
        reasoning_effort=getattr(args, "reasoning_effort", None),
        dispatch_mode=getattr(args, "dispatch_mode", None),
        prompt_style=getattr(args, "prompt_style", None),
        repair_strategy=getattr(args, "repair_strategy", None),
    )


def _handle_chief_run(args: Any) -> int:
    from .chief_run import chief_run_to_markdown, payload_to_json

    if args.background:
        from .chief_background import start_background_chief_run

        if args.resume:
            mission_id = args.resume
            preview = None
        else:
            preview = _run_chief_mission_args(args, execute=False, dry_run=True)
            if str(preview.get("status") or "") != "preview":
                print(payload_to_json(preview) if args.format == "json" else chief_run_to_markdown(preview))
                return 1
            mission_id = str((preview.get("mission") or {}).get("mission_id") or "")
        payload = start_background_chief_run(
            workspace_root=args.workspace_root,
            mission_id=mission_id,
            agents=tuple(args.agent or ()),
            run_profile=args.run_profile,
            include_slow=args.include_slow,
            max_workflows=args.max_workflows,
            timeout_seconds=args.timeout_seconds,
            allow_dirty=args.allow_dirty,
            allow_coverage_gap=args.allow_coverage_gap,
            test_command=getattr(args, "test_command", None),
            allow_test_edits=getattr(args, "allow_test_edits", False),
            merge=getattr(args, "merge", False),
        )
        if preview is not None:
            payload["preview"] = preview
    else:
        payload = _run_chief_mission_args(
            args,
            execute=bool(args.execute),
            dry_run=bool(args.dry_run or not args.execute),
            resume_mission_id=args.resume,
        )
    print(payload_to_json(payload) if args.format == "json" else chief_run_to_markdown(payload))
    return 0 if str(payload.get("status") or "") in {"preview", "verified", "background_started"} else 1


def _handle_chief_run_demo(args: Any) -> int:
    from .chief_run_demo import chief_demo_to_markdown, payload_to_json, run_chief_demo

    payload = run_chief_demo(workspace_root=args.workspace_root, demo_root=args.demo_root)
    print(payload_to_json(payload) if args.format == "json" else chief_demo_to_markdown(payload))
    return 0 if str(payload.get("status") or "") == "verified" else 1


# ── chief-missions ────────────────────────────────────────────────────────────

def _handle_chief_missions(args: Any) -> int:
    from .missions import list_missions, list_missions_to_markdown

    if args.action == "list":
        summaries = list_missions(args.workspace_root)
        limit = getattr(args, "limit", None)
        if limit is not None:
            summaries = summaries[: max(0, int(limit))]
        print(json.dumps(summaries, ensure_ascii=False, indent=2) if args.format == "json" else list_missions_to_markdown(summaries))
        return 0
    mission_id = getattr(args, "mission_id", None)
    if not mission_id:
        print("chief-missions show requires a mission id. Run `chief-missions list` to find one.")
        return 1
    from .chief_run import chief_run_to_markdown
    from .missions import load_mission, load_rounds
    from .models import to_jsonable

    mission = load_mission(args.workspace_root, mission_id)
    if mission is None:
        print(f"No saved mission found with id: {mission_id}")
        return 1
    payload = {
        "schema_version": 1,
        "product": mission.get("product", "DevPacer"),
        "verification_engine": mission.get("verification_engine", "Checkpoint"),
        "status": mission.get("status", ""),
        "stop_reason": mission.get("stop_reason", ""),
        "message": "",
        "mission": mission,
        "plan": {},
        "rounds": load_rounds(args.workspace_root, mission_id),
    }
    print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2) if args.format == "json" else chief_run_to_markdown(payload))
    return 0


def _handle_chief_memory(args: Any) -> int:
    from .project_memory import build_project_memory, payload_to_json, project_memory_to_markdown

    payload = build_project_memory(workspace_root=args.workspace_root, repo_root=getattr(args, "repo_root", "."), goal=args.goal, limit=args.limit)
    print(payload_to_json(payload) if args.format == "json" else project_memory_to_markdown(payload))
    return 0


# ── repo-map / refine-goal / quota ───────────────────────────────────────────

def _handle_repo_map(args: Any) -> int:
    from .repo_map import build_repo_map, payload_to_json, repo_map_cache_path, repo_map_to_markdown

    payload = build_repo_map(repo_root=args.repo_root, cache_path=repo_map_cache_path(args.workspace_root))
    print(payload_to_json(payload) if args.format == "json" else repo_map_to_markdown(payload, goal=args.goal, max_lines=args.max_lines))
    return 0


def _handle_refine_goal(args: Any) -> int:
    from .goal_intake import intake_to_markdown, payload_to_json, refine_goal

    payload = refine_goal(
        args.goal,
        answers=list(args.answer or []),
        model_id=args.model,
        base_url=args.base_url,
        endpoint=args.endpoint,
        enable_model=not args.no_model,
    )
    print(payload_to_json(payload) if args.format == "json" else intake_to_markdown(payload))
    return 0


def _handle_quota(args: Any) -> int:
    from .subscription_quota import (
        load_quota_snapshot,
        payload_to_json,
        quota_status,
        quota_to_markdown,
        refresh_codex_quota_snapshot,
    )

    refresh = None
    if bool(getattr(args, "refresh_codex", False)):
        refresh = refresh_codex_quota_snapshot(command=getattr(args, "codex_command", None))
    snapshot = load_quota_snapshot()
    payload = {"snapshot": snapshot, "status": quota_status(snapshot)}
    if refresh is not None:
        payload["refresh"] = refresh
    print(payload_to_json(payload) if args.format == "json" else quota_to_markdown(snapshot))
    return 0


def _handle_quota_statusline(_args: Any) -> int:
    import sys
    from .subscription_quota import format_statusline, record_statusline_snapshot

    try:
        stdin_payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        stdin_payload = {}
    if not isinstance(stdin_payload, dict):
        stdin_payload = {}
    snapshot = record_statusline_snapshot(stdin_payload)
    print(format_statusline(stdin_payload, snapshot))
    return 0


# ── program ───────────────────────────────────────────────────────────────────

def _handle_program(args: Any) -> int:
    from .hourly_budget import hourly_plan_to_markdown, payload_to_json as hourly_json
    from .program_scheduler import payload_to_json as start_json, program_start_to_markdown, start_program
    from .programs import (
        create_program_from_plan,
        list_programs,
        load_program,
        payload_to_json as program_json,
        program_dir,
        program_to_markdown,
        programs_to_markdown,
        refresh_daily_plan,
    )

    if args.action == "create":
        if not args.file:
            print("program create requires --file <development_plan.md>.")
            return 1
        payload = create_program_from_plan(
            source_file=args.file,
            workspace_root=args.workspace_root,
            repo_root=args.repo_root,
            objective=args.objective,
            hours=args.hours,
            agent=args.agent,
            test_command=args.test_command,
            sequential=not bool(args.parallel),
            limit=None if args.autonomous else args.limit,
            autonomous=bool(args.autonomous),
            allow_dirty=bool(args.allow_dirty),
            model=getattr(args, "model", None),
            strong_model=getattr(args, "strong_model", None),
            cheap_model=getattr(args, "cheap_model", None),
            research_model=getattr(args, "research_model", None),
            codex_provider=getattr(args, "codex_provider", None),
            codex_failover_provider=getattr(args, "codex_failover_provider", None),
            memory_mode=getattr(args, "memory_mode", "enabled"),
            acceptance_policy=getattr(args, "acceptance_policy", None),
        )
        print(program_json(payload) if args.format == "json" else program_to_markdown(payload))
        return 0
    if args.action == "list":
        payload = list_programs(args.workspace_root)
        print(program_json(payload) if args.format == "json" else programs_to_markdown(payload))
        return 0
    if not args.program:
        print(f"program {args.action} requires --program <program_id>.")
        return 1
    if args.action == "plan":
        payload = refresh_daily_plan(args.workspace_root, args.program, hours=args.hours)
        print(hourly_json(payload) if args.format == "json" else hourly_plan_to_markdown(payload))
        return 0
    if args.action == "start":
        payload = start_program(
            workspace_root=args.workspace_root,
            program_id=args.program,
            hours=args.hours,
            autonomous=True if args.autonomous else None,
        )
        print(start_json(payload) if args.format == "json" else program_start_to_markdown(payload))
        return 0
    program = load_program(args.workspace_root, args.program)
    if program is None:
        print(f"No saved program found: {args.program}")
        return 1
    if args.action == "report":
        report_path = program_dir(args.workspace_root, args.program) / "daily_plan.md"
        print(report_path.read_text(encoding="utf-8") if report_path.exists() else program_to_markdown(program))
        return 0
    print(program_json(program) if args.format == "json" else program_to_markdown(program))
    return 0


def _handle_autopilot(args: Any) -> int:
    from .program_scheduler import payload_to_json as start_json, program_start_to_markdown, start_program
    from .programs import create_program_from_plan

    program = create_program_from_plan(
        source_file=args.file,
        workspace_root=args.workspace_root,
        repo_root=args.repo_root,
        objective=args.objective,
        hours=args.hours,
        agent=args.agent,
        test_command=args.test_command,
        sequential=not bool(args.parallel),
        limit=None if args.autonomous else args.limit,
        autonomous=bool(args.autonomous),
        allow_dirty=bool(args.allow_dirty),
        model=getattr(args, "model", None),
        strong_model=getattr(args, "strong_model", None),
        cheap_model=getattr(args, "cheap_model", None),
        research_model=getattr(args, "research_model", None),
        codex_provider=getattr(args, "codex_provider", None),
        codex_failover_provider=getattr(args, "codex_failover_provider", None),
        memory_mode=getattr(args, "memory_mode", "enabled"),
        acceptance_policy=getattr(args, "acceptance_policy", None),
    )
    payload = start_program(
        workspace_root=args.workspace_root,
        program_id=str(program["program_id"]),
        hours=args.hours,
        autonomous=True if args.autonomous else None,
    )
    print(start_json(payload) if args.format == "json" else program_start_to_markdown(payload))
    return 0


# ── mission ───────────────────────────────────────────────────────────────────

def _handle_mission(args: Any) -> int:
    action = str(args.action)
    if action in {"start", "resume"}:
        return _mission_start_or_resume(args, action)
    if action == "status":
        return _mission_status(args)
    if action in {"list", "show"}:
        return _mission_list_or_show(args, action)
    if action == "queue":
        return _mission_queue(args)
    if action == "worker":
        return _mission_worker(args)
    if action == "memory":
        return _mission_memory(args)
    if action == "import":
        return _mission_import(args)
    return 2


def _mission_start_or_resume(args: Any, action: str) -> int:
    from .chief_run import chief_run_to_markdown, payload_to_json

    resume_id = args.mission if action == "resume" else None
    if action == "resume" and not resume_id:
        print("mission resume requires --mission <mission_id>.")
        return 1
    if action == "start" and not (args.goal or args.plan):
        print("mission start requires --goal or --plan.")
        return 1

    if args.background:
        from .chief_background import start_background_chief_run

        if resume_id:
            mission_id = resume_id
            preview = None
        else:
            preview = _run_chief_mission_args(args, execute=False, dry_run=True)
            if str(preview.get("status") or "") != "preview":
                print(payload_to_json(preview) if args.format == "json" else chief_run_to_markdown(preview))
                return 1
            mission_id = str((preview.get("mission") or {}).get("mission_id") or "")
        payload = start_background_chief_run(
            workspace_root=args.workspace_root,
            mission_id=mission_id,
            agents=tuple(args.agent or ()),
            run_profile=args.run_profile,
            include_slow=args.include_slow,
            max_workflows=args.max_workflows,
            timeout_seconds=args.timeout_seconds,
            allow_dirty=args.allow_dirty,
            allow_coverage_gap=args.allow_coverage_gap,
            test_command=getattr(args, "test_command", None),
            allow_test_edits=getattr(args, "allow_test_edits", False),
            merge=getattr(args, "merge", False),
        )
        if preview is not None:
            payload["preview"] = preview
    else:
        payload = _run_chief_mission_args(
            args,
            execute=bool(args.execute),
            dry_run=not bool(args.execute),
            resume_mission_id=resume_id,
        )
    print(payload_to_json(payload) if args.format == "json" else chief_run_to_markdown(payload))
    return 0 if str(payload.get("status") or "") in {"preview", "verified", "background_started"} else 1


def _mission_status(args: Any) -> int:
    from .chief_run import chief_run_to_markdown, mission_status_payload, payload_to_json

    if not args.mission:
        print("mission status requires --mission <mission_id>.")
        return 1
    payload = mission_status_payload(workspace_root=args.workspace_root, mission_id=args.mission)
    print(payload_to_json(payload) if args.format == "json" else chief_run_to_markdown(payload))
    return 0 if str(payload.get("status") or "") != "blocked" else 1


def _mission_list_or_show(args: Any, action: str) -> int:
    from .chief_run import chief_run_to_markdown
    from .missions import list_missions, list_missions_to_markdown, load_mission, load_rounds
    from .models import to_jsonable

    if action == "list":
        summaries = list_missions(args.workspace_root)
        print(json.dumps(summaries, ensure_ascii=False, indent=2) if args.format == "json" else list_missions_to_markdown(summaries))
        return 0
    if not args.mission:
        print("mission show requires --mission <mission_id>.")
        return 1
    mission = load_mission(args.workspace_root, args.mission)
    if mission is None:
        print(f"No saved mission found with id: {args.mission}")
        return 1
    payload: dict[str, Any] = {
        "schema_version": 1,
        "product": mission.get("product", "DevPacer"),
        "verification_engine": mission.get("verification_engine", "Checkpoint"),
        "status": mission.get("status", ""),
        "stop_reason": mission.get("stop_reason", ""),
        "message": "",
        "mission": mission,
        "plan": {},
        "rounds": load_rounds(args.workspace_root, args.mission),
    }
    print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2) if args.format == "json" else chief_run_to_markdown(payload))
    return 0


def _mission_queue(args: Any) -> int:
    from .chief_queue import mission_queue_item_to_dict, mission_queue_submit_to_markdown, payload_to_json, submit_mission_queue_item

    if not args.mission:
        print("mission queue requires --mission <mission_id>.")
        return 1
    try:
        item = submit_mission_queue_item(
            workspace_root=args.workspace_root,
            mission_id=args.mission,
            priority=args.priority,
            run_profile=args.run_profile,
            include_slow=args.include_slow,
            max_workflows=args.max_workflows,
            timeout_seconds=args.timeout_seconds,
            allow_dirty=args.allow_dirty,
            allow_coverage_gap=args.allow_coverage_gap,
            agent=getattr(args, "agent", None),
            test_command=getattr(args, "test_command", None),
            allow_test_edits=getattr(args, "allow_test_edits", False),
            merge_policy=getattr(args, "merge_policy", "manual"),
            reasoning_effort=getattr(args, "reasoning_effort", None),
            dispatch_mode=getattr(args, "dispatch_mode", None),
            prompt_style=getattr(args, "prompt_style", None),
            repair_strategy=getattr(args, "repair_strategy", None),
            force=args.force,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(_fmt_err(exc, command="mission queue"))
        return 1
    print(payload_to_json(mission_queue_item_to_dict(item)) if args.format == "json" else mission_queue_submit_to_markdown(item))
    return 0


def _mission_worker(args: Any) -> int:
    from .chief_queue import mission_queue_worker_to_markdown, payload_to_json, run_mission_queue_worker

    run_once = bool(args.run_once or not args.watch)
    payload = run_mission_queue_worker(
        workspace_root=args.workspace_root,
        run_once=run_once,
        watch=bool(args.watch),
        poll_seconds=args.poll_seconds,
        max_items=args.max_items,
        max_seconds=args.max_seconds,
    )
    print(payload_to_json(payload) if args.format == "json" else mission_queue_worker_to_markdown(payload))
    return 0 if str(payload.get("status") or "") in {"run_once_completed", "idle", "max_items_reached", "max_seconds_reached"} else 1


def _mission_memory(args: Any) -> int:
    from .project_memory import build_project_memory, payload_to_json, project_memory_to_markdown

    payload = build_project_memory(workspace_root=args.workspace_root, repo_root=getattr(args, "repo_root", "."), goal=args.goal, limit=args.limit)
    print(payload_to_json(payload) if args.format == "json" else project_memory_to_markdown(payload))
    return 0


def _mission_import(args: Any) -> int:
    from .mission_plan_import import import_development_plan, mission_plan_import_to_markdown, payload_to_json

    if not args.file:
        print("mission import requires --file <development_plan.md>.")
        return 1
    if args.execute or args.background:
        print("mission import never executes Codex directly. Use --create/--queue, then run mission worker explicitly.")
        return 1
    try:
        payload = import_development_plan(
            source_file=args.file,
            workspace_root=args.workspace_root,
            repo_root=args.repo_root,
            base=args.base,
            create=bool(args.create or args.queue),
            queue=bool(args.queue),
            limit=args.limit,
            agents=tuple(args.agent or ()),
            answers=tuple(getattr(args, "answer", []) or ()),
            interview=args.interview,
            max_rounds=args.max_rounds,
            max_wall_minutes=args.max_wall_minutes,
            max_worker_minutes=args.max_worker_minutes,
            run_profile=args.run_profile,
            include_slow=args.include_slow,
            max_workflows=args.max_workflows,
            timeout_seconds=args.timeout_seconds,
            allow_dirty=args.allow_dirty,
            allow_coverage_gap=args.allow_coverage_gap,
            test_command=getattr(args, "test_command", None),
            allow_test_edits=getattr(args, "allow_test_edits", False),
            merge_policy=("auto" if getattr(args, "merge", False) else getattr(args, "merge_policy", "manual")),
            priority=args.priority,
            force=args.force,
        )
    except OSError as exc:
        print(_fmt_err(exc, command="mission import"))
        return 1
    print(payload_to_json(payload) if args.format == "json" else mission_plan_import_to_markdown(payload))
    return 0 if str(payload.get("status") or "") != "empty" else 1


# ── chief-status / chief-queue / chief-worker ─────────────────────────────────

def _handle_chief_status(args: Any) -> int:
    from .chief_run import chief_run_to_markdown, mission_status_payload, payload_to_json

    payload = mission_status_payload(workspace_root=args.workspace_root, mission_id=args.mission)
    print(payload_to_json(payload) if args.format == "json" else chief_run_to_markdown(payload))
    return 0 if str(payload.get("status") or "") != "blocked" else 1


def _handle_chief_queue(args: Any) -> int:
    from .chief_queue import (
        list_mission_queue_items,
        load_mission_queue_item,
        mission_queue_item_to_dict,
        mission_queue_submit_to_markdown,
        mission_queue_to_markdown,
        payload_to_json,
        submit_mission_queue_item,
    )

    if args.action == "submit":
        if not args.mission:
            print("chief-queue submit requires --mission <mission_id>.")
            return 1
        try:
            item = submit_mission_queue_item(
                workspace_root=args.workspace_root,
                mission_id=args.mission,
                priority=args.priority,
                run_profile=args.run_profile,
                include_slow=args.include_slow,
                max_workflows=args.max_workflows,
                timeout_seconds=args.timeout_seconds,
                allow_dirty=args.allow_dirty,
                allow_coverage_gap=args.allow_coverage_gap,
                agent=getattr(args, "agent", None),
                test_command=getattr(args, "test_command", None),
                allow_test_edits=getattr(args, "allow_test_edits", False),
                merge_policy=getattr(args, "merge_policy", "manual"),
                reasoning_effort=getattr(args, "reasoning_effort", None),
                dispatch_mode=getattr(args, "dispatch_mode", None),
                prompt_style=getattr(args, "prompt_style", None),
                repair_strategy=getattr(args, "repair_strategy", None),
                force=args.force,
            )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            print(_fmt_err(exc, command="chief-queue submit"))
            return 1
        print(payload_to_json(mission_queue_item_to_dict(item)) if args.format == "json" else mission_queue_submit_to_markdown(item))
        return 0
    if args.action == "list":
        payload = list_mission_queue_items(args.workspace_root, status=args.status)
        print(payload_to_json(payload) if args.format == "json" else mission_queue_to_markdown(payload))
        return 0
    queue_id = getattr(args, "queue_id", None)
    if not queue_id:
        print("chief-queue show requires --queue-id <queue_id>.")
        return 1
    item = load_mission_queue_item(args.workspace_root, queue_id)
    if item is None:
        print(f"No queued mission found with id: {queue_id}")
        return 1
    print(payload_to_json(mission_queue_item_to_dict(item)) if args.format == "json" else mission_queue_to_markdown({"entries": [mission_queue_item_to_dict(item)]}))
    return 0


def _handle_chief_worker(args: Any) -> int:
    from .chief_queue import mission_queue_worker_to_markdown, payload_to_json, run_mission_queue_worker

    payload = run_mission_queue_worker(
        workspace_root=args.workspace_root,
        run_once=bool(args.run_once or not args.watch),
        watch=bool(args.watch),
        poll_seconds=args.poll_seconds,
        max_items=args.max_items,
        max_seconds=args.max_seconds,
    )
    print(payload_to_json(payload) if args.format == "json" else mission_queue_worker_to_markdown(payload))
    return 0 if str(payload.get("status") or "") in {"run_once_completed", "idle", "max_items_reached", "max_seconds_reached"} else 1


def _handle_chief_background_worker(args: Any) -> int:
    from .chief_background import run_background_worker
    from .chief_run import chief_run_to_markdown, payload_to_json

    payload = run_background_worker(
        workspace_root=args.workspace_root,
        mission_id=args.mission,
        agents=tuple(getattr(args, "agent", []) or ()),
        run_profile=args.run_profile,
        include_slow=args.include_slow,
        max_workflows=args.max_workflows,
        timeout_seconds=args.timeout_seconds,
        allow_dirty=args.allow_dirty,
        allow_coverage_gap=args.allow_coverage_gap,
        test_command=getattr(args, "test_command", None),
        allow_test_edits=getattr(args, "allow_test_edits", False),
        merge=getattr(args, "merge", False),
    )
    print(payload_to_json(payload) if args.format == "json" else chief_run_to_markdown(payload))
    return 0 if str(payload.get("status") or "") == "verified" else 1


# ── dashboard / app / agents ──────────────────────────────────────────────────

def _handle_dashboard(args: Any) -> int:
    from .dashboard import serve_dashboard

    serve_dashboard(
        workspace_root=args.workspace_root,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )
    return 0


def _handle_app(args: Any) -> int:
    try:
        from .workbench_app import launch_desktop_app
    except Exception as exc:  # noqa: BLE001
        print(f"Could not start the desktop app: {exc}")
        print("Fallback: run `checkpoint dashboard` for the web workbench.")
        return 1
    try:
        return launch_desktop_app(project_dir=args.project or None)
    except Exception as exc:  # noqa: BLE001
        print(f"Desktop app error: {exc}")
        print("Fallback: run `checkpoint dashboard` for the web workbench.")
        return 1


def _handle_agents(args: Any) -> int:
    from .agent_capabilities import (
        agent_profile_to_markdown,
        agents_doctor,
        agents_doctor_to_markdown,
        list_agent_profiles,
        load_agent_profile,
    )

    if args.action == "list":
        profiles = list_agent_profiles()
        names = [{"agent": p.get("agent"), "display_name": p.get("display_name")} for p in profiles]
        print(json.dumps(names, ensure_ascii=False, indent=2) if args.format == "json" else "\n".join(f"- {e['agent']}: {e['display_name']}" for e in names))
        return 0
    if args.action == "show":
        if not args.agent:
            print("agents show requires an agent name (codex or claude-code).")
            return 1
        profile = load_agent_profile(args.agent)
        if profile is None:
            print(f"No capability profile for agent: {args.agent}")
            return 1
        print(json.dumps(profile, ensure_ascii=False, indent=2) if args.format == "json" else agent_profile_to_markdown(profile))
        return 0
    report = agents_doctor(agents=(args.agent,) if args.agent else ())
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else agents_doctor_to_markdown(report))
    return 0
