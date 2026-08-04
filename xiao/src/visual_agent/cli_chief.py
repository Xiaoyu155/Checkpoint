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
    "host",
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
        "host":                    _handle_host,
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
        base_probe_enabled=not getattr(args, "no_base_probe", False),
        dependency_bootstrap_enabled=not getattr(args, "no_dep_bootstrap", False),
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
        base_probe_enabled=not getattr(args, "no_base_probe", False),
        dependency_bootstrap_enabled=not getattr(args, "no_dep_bootstrap", False),
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

def _handle_host(args: Any) -> int:
    """Official long-host product path: status / doctor / run / stop."""
    from .models import to_jsonable
    from .pacer_host import (
        build_host_dashboard,
        host_dashboard_to_markdown,
        host_run_to_markdown,
        request_host_stop,
        run_host_session,
        save_host_policy,
        load_host_policy,
        start_hosted_goal,
    )
    from .provider_liveness import clear_worker_agent_quota_cache, normalize_agent_name

    action = str(getattr(args, "host_action", None) or getattr(args, "action", "") or "status")
    workspace = getattr(args, "workspace_root", ".agent-workspace")
    repo = getattr(args, "repo_root", ".")
    requested_mode = str(getattr(args, "mode", "") or "").strip().lower()
    explicit_yolo = action == "yolo" or bool(getattr(args, "yolo", False)) or requested_mode == "yolo"
    agent = normalize_agent_name(getattr(args, "agent", None) or ("claude-code" if explicit_yolo else "codex"))
    fmt = str(getattr(args, "format", "markdown") or "markdown")
    quota_cache_clear = None
    if bool(getattr(args, "clear_quota_cache", False)):
        quota_cache_clear = clear_worker_agent_quota_cache(agent)

    if action in {"status", "dashboard"}:
        dash = build_host_dashboard(
            workspace_root=workspace,
            repo_root=repo,
            agent=agent,
            run_pytest=bool(getattr(args, "pytest", False)),
            auto_resume=False,
        )
        if quota_cache_clear is not None:
            dash["quota_cache_clear"] = quota_cache_clear
        if fmt == "json":
            print(json.dumps(to_jsonable(dash), ensure_ascii=False, indent=2))
        else:
            print(host_dashboard_to_markdown(dash))
        return 0 if dash.get("ready_for_host") else 1

    if action == "doctor":
        run_pytest = bool(getattr(args, "pytest", False))
        dash = build_host_dashboard(
            workspace_root=workspace,
            repo_root=repo,
            agent=agent,
            run_pytest=run_pytest,
            auto_resume=False,
        )
        if quota_cache_clear is not None:
            dash["quota_cache_clear"] = quota_cache_clear
        if fmt == "json":
            print(json.dumps(to_jsonable(dash), ensure_ascii=False, indent=2))
        else:
            print(host_dashboard_to_markdown(dash))
            print("\n## Doctor 结论")
            if dash.get("ready_for_host"):
                print("- 可以开始托管：`pacer host run --goal \"...\" --hours 2 --execute`")
            else:
                print("- 先处理阻塞项（登录/额度/STOP），再托管。")
            if not run_pytest:
                print("- 未运行全仓测试；需要时加 `--pytest`。")
            if quota_cache_clear is not None:
                print(f"- 已清除额度失败缓存：{', '.join(quota_cache_clear['cleared_keys'])}")
        return 0 if dash.get("ready_for_host") else 1

    if action == "stop":
        path = request_host_stop(workspace)
        msg = {"status": "stop_requested", "path": str(path)}
        print(json.dumps(msg, ensure_ascii=False, indent=2) if fmt == "json" else f"已请求停止托管：`{path}`")
        return 0

    if action == "policy":
        policy = load_host_policy(workspace)
        if getattr(args, "max_auto_resumes", None) is not None:
            policy["max_auto_resumes_per_mission"] = int(args.max_auto_resumes)
        if getattr(args, "poll_seconds", None) is not None:
            policy["poll_seconds"] = float(args.poll_seconds)
        if getattr(args, "agent", None):
            policy["agent"] = agent
        path = save_host_policy(workspace, policy)
        if fmt == "json":
            print(json.dumps({"path": str(path), "policy": policy}, ensure_ascii=False, indent=2))
        else:
            print(f"已写入托管策略：`{path}`\n```json\n{json.dumps(policy, ensure_ascii=False, indent=2)}\n```")
        return 0

    if action in {"run", "start", "unleash", "yolo"}:
        from .pacer_host import HOST_MODE_PROFILES, normalize_host_mode

        if explicit_yolo:
            mode = "yolo"
        else:
            mode = normalize_host_mode(
                getattr(args, "mode", None),
                unleash_flag=bool(getattr(args, "unleash", False) or action == "unleash"),
                race_flag=bool(getattr(args, "race", False)),
            )
        profile = HOST_MODE_PROFILES[mode]
        race = bool(getattr(args, "race", False) or mode == "race")
        execution_policy = (
            dict(profile.get("execution_policy"))
            if isinstance(profile.get("execution_policy"), dict)
            else None
        )
        # Only force expensive options when mode asks for them — not by default.
        wake = getattr(args, "wake_on_quota", False)
        heal = getattr(args, "self_heal", False)
        if wake is False and profile.get("wake_on_quota"):
            wake = True
        if heal is False and profile.get("self_heal_pytest"):
            heal = True
        # Explicit CLI False flags not available; use mode as source of truth.
        wake = bool(profile.get("wake_on_quota") or getattr(args, "wake_on_quota", False))
        heal = bool(profile.get("self_heal_pytest") or getattr(args, "self_heal", False))

        if not bool(getattr(args, "execute", False)):
            goals = _host_collect_goals(args)
            dash = build_host_dashboard(
                workspace_root=workspace,
                repo_root=repo,
                agent=agent,
                run_pytest=False,
                mode=mode,
                auto_resume=False,
            )
            payload = {
                "status": "preview",
                "message": "Add --execute to actually host. Showing dashboard + planned goals.",
                "goals": goals,
                "mode": mode,
                "token_cost": profile.get("token_cost"),
                "token_cost_label": profile.get("token_cost_label"),
                "wild": {"race": race, "wake_on_quota": wake, "self_heal": heal},
                "execution_policy": execution_policy or {},
                "dashboard": dash,
            }
            if fmt == "json":
                print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
            else:
                print(host_dashboard_to_markdown(dash))
                print(f"\n## 计划目标（未执行 · 模式 `{mode}` · {profile.get('token_cost_label')}）\n")
                for i, g in enumerate(goals, 1):
                    print(f"{i}. {g}")
                print(
                    f"\n额度：`{profile.get('token_cost')}` · 并发={profile.get('max_active')} · "
                    f"resume={profile.get('max_auto_resumes_per_mission')} · race={race}"
                )
                if mode == "economy":
                    print("默认 **economy 省额度**：单路执行、少重试。要更快用 `--mode standard` 或 `unleash`。")
                elif mode == "unleash":
                    print("Unleash：**吃额度换效率**（回血续跑 / 多 resume / 自愈插队 / 可 merge）。")
                elif mode == "race":
                    print("Race：**很吃额度**（双助手竞速，败者杀掉）。")
                elif mode == "yolo":
                    print("YOLO：Claude Code 使用 `--permission-mode bypassPermissions`，不再等待权限确认。")
                print("\n加上 `--execute` 才会真正后台托管。")
            return 0 if dash.get("ready_for_host") else 1

        goals = _host_collect_goals(args)
        if not goals:
            print("host run/unleash/yolo 需要 --goal 和/或 --goals-file。")
            return 1
        hours = float(getattr(args, "hours", 0) or 0)
        expensive = mode in {"unleash", "race", "yolo"}
        # Single-shot only for economy/standard, non-race, non-watch
        if (
            hours <= 0
            and len(goals) == 1
            and not getattr(args, "watch", False)
            and not expensive
            and not race
        ):
            result = start_hosted_goal(
                workspace_root=workspace,
                repo_root=repo,
                goal=goals[0],
                agent=agent,
                test_command=getattr(args, "test_command", None),
                allow_dirty=bool(getattr(args, "allow_dirty", True)),
                allow_test_edits=bool(getattr(args, "allow_test_edits", False) or profile.get("allow_test_edits")),
                merge=bool(getattr(args, "merge", False) or profile.get("merge")),
                max_rounds=int(profile.get("max_rounds") or 2),
                max_repair_rounds=int(profile.get("max_repair_rounds") or 1),
                max_wall_minutes=int(profile.get("max_wall_minutes") or 40),
                max_worker_minutes=int(profile.get("max_worker_minutes") or 30),
                reasoning_effort=str(profile.get("reasoning_effort") or "inherit"),
                model_policy=profile.get("model_policy") if isinstance(profile.get("model_policy"), dict) else None,
                execution_policy=execution_policy,
            )
            if fmt == "json":
                print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
            else:
                print(
                    f"## 托管启动（`{mode}` · {profile.get('token_cost_label')}）\n\n"
                    f"- status: **{result.get('status')}**\n"
                    f"- stop: `{result.get('stop_reason') or '-'}`\n"
                    f"- mission: `{result.get('mission_id') or '-'}`\n"
                    f"- goal: {result.get('goal')}\n\n"
                    f"{result.get('message') or ''}\n"
                )
            return 0 if str(result.get("status") or "") in {"background_started", "running"} else 1

        max_active = getattr(args, "max_active", None)
        if max_active is None:
            max_active = int(profile.get("max_active") or 1)
        result = run_host_session(
            workspace_root=workspace,
            repo_root=repo,
            goals=goals,
            hours=max(0.25, hours or (3.0 if expensive else 2.0)),
            agent=agent,
            test_command=getattr(args, "test_command", None),
            poll_seconds=getattr(args, "poll_seconds", None),
            allow_dirty=bool(getattr(args, "allow_dirty", True)),
            allow_test_edits=bool(getattr(args, "allow_test_edits", False) or profile.get("allow_test_edits")),
            merge=bool(getattr(args, "merge", False) or profile.get("merge")),
            max_active=int(max_active),
            mode=mode,
            unleash=expensive,
            race=race,
            wake_on_quota=wake,
            self_heal_pytest=heal,
            reasoning_effort=str(profile.get("reasoning_effort") or "inherit"),
            model_policy=profile.get("model_policy") if isinstance(profile.get("model_policy"), dict) else None,
            execution_policy=execution_policy,
        )
        if fmt == "json":
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        else:
            print(host_run_to_markdown(result))
        return 0 if str(result.get("status") or "") in {"completed", "stopped"} else 1

    print(f"Unknown host action: {action}. Use status|doctor|run|unleash|yolo|stop|policy.")
    return 2


def _host_collect_goals(args: Any) -> list[str]:
    goals: list[str] = []
    goal = str(getattr(args, "goal", None) or "").strip()
    if goal:
        goals.append(goal)
    for extra in getattr(args, "goals", None) or []:
        text = str(extra or "").strip()
        if text:
            goals.append(text)
    goals_file = getattr(args, "goals_file", None)
    if goals_file:
        path = Path(str(goals_file)).expanduser()
        if path.is_file():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                goals.append(text)
    return goals


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
    from .chief_run import chief_run_to_markdown, mission_status_payload
    from .missions import list_missions, list_missions_to_markdown, load_mission
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
    payload = mission_status_payload(workspace_root=args.workspace_root, mission_id=args.mission)
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
    import json as _json
    from .chief_background import run_background_worker
    from .chief_run import chief_run_to_markdown, payload_to_json

    raw_policy = getattr(args, "execution_policy_json", None)
    execution_policy: dict | None = None
    if raw_policy:
        try:
            parsed = _json.loads(raw_policy)
            if isinstance(parsed, dict):
                execution_policy = parsed
        except (ValueError, TypeError):
            pass

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
        base_probe_enabled=not getattr(args, "no_base_probe", False),
        dependency_bootstrap_enabled=not getattr(args, "no_dep_bootstrap", False),
        merge=getattr(args, "merge", False),
        execution_policy=execution_policy,
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
