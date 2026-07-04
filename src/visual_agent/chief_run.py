"""Bounded autonomous development missions.

``chief-run`` is the first DevPacer loop: build or load a plan, persist mission
state, preview the dispatch, optionally execute Codex, verify with Checkpoint,
and stop with a durable reason.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from .chief_dispatch import dispatch_chief_plan
from .chief_engineer import build_chief_plan, chief_plan_to_dict
from .chief_plans_store import load_plan, save_plan
from .goal_grounding import goal_references_plan, ground_goal, grounding_to_markdown
from .missions import (
    append_round,
    create_mission,
    default_budget_policy,
    load_mission,
    load_rounds,
    save_mission,
    write_final_report,
)
from .models import to_jsonable
from .verification_profiles import resolve_test_command
from .workspace import init_workspace


DispatchRunner = Callable[..., dict[str, Any]]


def run_chief_mission(
    *,
    goal: str | None = None,
    workspace_root: str | Path,
    repo_root: str | Path = ".",
    base: str = "HEAD",
    plan_id: str | None = None,
    mission_id: str | None = None,
    resume_mission_id: str | None = None,
    agents: tuple[str, ...] = (),
    answers: tuple[str, ...] = (),
    interview: bool = False,
    max_rounds: int = 2,
    max_wall_minutes: int = 60,
    max_worker_minutes: int = 45,
    execute: bool = False,
    dry_run: bool = True,
    run_profile: str = "dry-run",
    include_slow: bool = False,
    max_workflows: int = 10,
    timeout_seconds: float = 1800.0,
    allow_dirty: bool = False,
    allow_coverage_gap: bool = False,
    test_command: str | None = None,
    allow_test_edits: bool = False,
    merge: bool = False,
    dispatch_runner: DispatchRunner | None = None,
    ground_vague_goals: bool = True,
    grounding_runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = monotonic()
    workspace_path = Path(workspace_root).expanduser().resolve()
    repo_path = Path(repo_root).expanduser().resolve()
    resolved_test_command, verification_profile = resolve_test_command(test_command, repo_root=repo_path)
    test_command = resolved_test_command
    if test_command and not workspace_path.exists():
        init_workspace(workspace_path, with_demo=False)
    budget = default_budget_policy(
        max_rounds=max_rounds,
        max_wall_minutes=max_wall_minutes,
        max_worker_minutes=max_worker_minutes,
    )
    runner = dispatch_runner or dispatch_chief_plan
    mission: dict[str, Any] | None = None
    grounding_payload: dict[str, Any] | None = None
    if resume_mission_id:
        mission = load_mission(workspace_path, resume_mission_id)
        if mission is None:
            return _terminal_payload(
                workspace_root=workspace_path,
                mission=None,
                plan=None,
                status="blocked",
                stop_reason="missing_mission",
                message=f"No saved mission found: {resume_mission_id}",
                started=started,
            )
        plan_id = str(mission.get("plan_id") or plan_id or "")
        goal = str(mission.get("objective") or goal or "")
        repo_path = Path(str(mission.get("repo_root") or repo_path)).expanduser().resolve()
        if isinstance(mission.get("budget_policy"), dict):
            budget = dict(mission["budget_policy"])

    plan_payload = load_plan(workspace_path, plan_id) if plan_id else None
    if resume_mission_id and (not plan_id or plan_payload is None):
        return _terminal_payload(
            workspace_root=workspace_path,
            mission=mission,
            plan=None,
            status="blocked",
            stop_reason="missing_plan",
            message=f"Saved mission {resume_mission_id} does not have a loadable chief plan.",
            started=started,
        )
    if plan_id and plan_payload is None:
        return _terminal_payload(
            workspace_root=workspace_path,
            mission=None,
            plan=None,
            status="blocked",
            stop_reason="missing_plan",
            message=f"No saved chief plan found: {plan_id}",
            started=started,
        )

    if plan_payload is None:
        if not str(goal or "").strip():
            return _terminal_payload(
                workspace_root=workspace_path,
                mission=None,
                plan=None,
                status="blocked",
                stop_reason="needs_goal",
                message="chief-run requires --goal or --plan.",
                started=started,
            )
        plan = build_chief_plan(
            goal=str(goal),
            workspace_root=workspace_path,
            repo_root=repo_path,
            base=base,
            agents=agents,
            include_slow=include_slow,
            max_workflows=max_workflows,
            run_profile=run_profile,
            interview=interview,
            answers=answers,
        )
        plan_payload = chief_plan_to_dict(plan)
        # A vague goal is not always noise: it often points at a plan document
        # in the repo. Review those documents (cheap model) before refusing —
        # resolve into the plan's next concrete task, or come back with a
        # proposed plan to discuss instead of a bare error. A goal that cites a
        # plan ("按照开发计划推进") is grounded even when it passes the clarity
        # gate: dispatching it raw would burn a worker run on a goal whose
        # definition of done lives in a file the worker never read.
        if ground_vague_goals and (
            str(plan_payload.get("status") or "") == "needs_clarification" or goal_references_plan(str(goal))
        ):
            grounder = grounding_runner or ground_goal
            grounding_payload = grounder(goal=str(goal), repo_root=repo_path)
            if not isinstance(grounding_payload, dict):
                grounding_payload = None
            if grounding_payload and grounding_payload.get("resolved") and str(grounding_payload.get("grounded_goal") or "").strip():
                goal = str(grounding_payload["grounded_goal"]).strip()
                grounded_answers = tuple(answers) + tuple(
                    part
                    for part in (
                        f"依据项目计划文档 {grounding_payload.get('plan_document') or ''}：{grounding_payload.get('evidence') or ''}".strip("："),
                        str(grounding_payload.get("acceptance_hint") or "").strip(),
                    )
                    if part
                )
                plan = build_chief_plan(
                    goal=goal,
                    workspace_root=workspace_path,
                    repo_root=repo_path,
                    base=base,
                    agents=agents,
                    include_slow=include_slow,
                    max_workflows=max_workflows,
                    run_profile=run_profile,
                    interview=interview,
                    answers=grounded_answers,
                )
                plan_payload = chief_plan_to_dict(plan)
            if grounding_payload:
                plan_payload["grounding"] = grounding_payload
        saved = save_plan(plan_payload, workspace_root=workspace_path)
        plan_payload["plan_id"] = saved["plan_id"]
        plan_payload["saved_path"] = saved["path"]
        plan_id = saved["plan_id"]
    else:
        plan_id = str(plan_payload.get("plan_id") or plan_id)
        if not goal:
            goal = str(plan_payload.get("objective") or "")
    if agents:
        changed_agent = _override_plan_agent(plan_payload, str(agents[0]))
        if changed_agent and plan_id:
            save_plan(plan_payload, workspace_root=workspace_path, plan_id=str(plan_id))
    if verification_profile:
        plan_payload["verification_profile"] = verification_profile
        if plan_id:
            save_plan(plan_payload, workspace_root=workspace_path, plan_id=str(plan_id))
    effective_allow_test_edits = allow_test_edits or _objective_requests_test_edits(
        str(goal or plan_payload.get("objective") or "")
    )
    if effective_allow_test_edits and not allow_test_edits:
        plan_payload["test_edit_policy"] = {
            "allow_test_edits": True,
            "source": "objective",
            "reason": "The objective explicitly asks to add or update tests.",
        }
        if plan_id:
            save_plan(plan_payload, workspace_root=workspace_path, plan_id=str(plan_id))

    if mission is None:
        mission = create_mission(
            workspace_root=workspace_path,
            objective=str(goal or plan_payload.get("objective") or ""),
            repo_root=repo_path,
            plan_id=str(plan_id),
            budget_policy=budget,
            mission_id=mission_id,
            status="created",
        )
        if grounding_payload:
            append_round(
                workspace_path,
                mission["mission_id"],
                {
                    "round": _next_round_number(workspace_path, mission["mission_id"]),
                    "type": "grounding",
                    "status": "resolved" if grounding_payload.get("resolved") else "unresolved",
                    "source": str(grounding_payload.get("source") or ""),
                    "plan_document": str(grounding_payload.get("plan_document") or ""),
                    "grounded_goal": str(grounding_payload.get("grounded_goal") or ""),
                },
            )
    elif execute and not dry_run:
        mission["status"] = "running"
        mission["stop_reason"] = ""
        save_mission(workspace_path, mission)
    if _elapsed_minutes(started) > budget["max_wall_minutes"]:
        return _finish(
            workspace_root=workspace_path,
            mission=mission,
            plan=plan_payload,
            status="stopped",
            stop_reason="budget_exhausted",
            message="Mission exceeded wall-clock budget before dispatch.",
            started=started,
        )

    plan_status = str(plan_payload.get("status") or "")
    # A plan-referencing goal that grounding could not resolve stops here even
    # if the plan looks "ready": dispatching it would waste the worker run.
    grounding_unresolved = bool(grounding_payload) and not bool(grounding_payload.get("resolved"))
    if plan_status in {"blocked", "needs_clarification"} or grounding_unresolved:
        stop_reason = "blocked_plan" if plan_status == "blocked" else "needs_clarification"
        append_round(
            workspace_path,
            mission["mission_id"],
            {
                "round": _next_round_number(workspace_path, mission["mission_id"]),
                "type": "planning",
                "status": plan_status if plan_status in {"blocked", "needs_clarification"} else "needs_clarification",
                "stop_reason": stop_reason,
                "plan_id": plan_id,
            },
        )
        clarification_message = "Mission stopped before dispatch because the plan is not actionable."
        if stop_reason == "needs_clarification" and grounding_payload:
            clarification_message = (
                "目标还不够具体，已先审查了项目里的计划文档（见报告的“计划审查”），"
                "请按报告里的建议确认下一步，再重新发起。"
            )
        return _finish(
            workspace_root=workspace_path,
            mission=mission,
            plan=plan_payload,
            status="stopped",
            stop_reason=stop_reason,
            message=clarification_message,
            started=started,
        )
    if plan_status == "needs_workflow_coverage" and not allow_coverage_gap and not test_command:
        append_round(
            workspace_path,
            mission["mission_id"],
            {
                "round": _next_round_number(workspace_path, mission["mission_id"]),
                "type": "planning",
                "status": plan_status,
                "stop_reason": "coverage_gap",
                "plan_id": plan_id,
            },
        )
        return _finish(
            workspace_root=workspace_path,
            mission=mission,
            plan=plan_payload,
            status="stopped",
            stop_reason="coverage_gap",
            message="Mission stopped because workflow coverage is missing or weak.",
            started=started,
        )

    preview = runner(
        workspace_root=workspace_path,
        plan_id=str(plan_id),
        execute=False,
        dry_run=True,
        run_profile=run_profile,
        include_slow=include_slow,
        max_workflows=max_workflows,
        timeout_seconds=min(float(timeout_seconds), float(budget["max_worker_minutes"]) * 60.0),
        allow_dirty=allow_dirty,
        allow_coverage_gap=allow_coverage_gap,
        auto_repair_once=False,
        model_policy=budget.get("model_policy") if isinstance(budget, dict) else None,
        test_command=test_command,
        allow_test_edits=effective_allow_test_edits,
    )
    append_round(
        workspace_path,
        mission["mission_id"],
        {
            "round": _next_round_number(workspace_path, mission["mission_id"]),
            "type": "dispatch_preview",
            "status": str(preview.get("status") or ""),
            "stop_reason": str(preview.get("reason") or ""),
            "plan_id": plan_id,
            "payload": _compact_dispatch(preview),
        },
    )
    if str(preview.get("status") or "") == "blocked":
        return _finish(
            workspace_root=workspace_path,
            mission=mission,
            plan=plan_payload,
            status="stopped",
            stop_reason=_dispatch_stop_reason(preview),
            message=str(preview.get("reason") or "Dispatch preview was blocked."),
            started=started,
            dispatch_payload=preview,
        )
    if dry_run or not execute:
        return _finish(
            workspace_root=workspace_path,
            mission=mission,
            plan=plan_payload,
            status="preview",
            stop_reason="preview_only",
            message="Dry-run complete. Re-run with --execute to launch the selected coding worker.",
            started=started,
            dispatch_payload=preview,
        )

    if budget["max_rounds"] < 1:
        return _finish(
            workspace_root=workspace_path,
            mission=mission,
            plan=plan_payload,
            status="stopped",
            stop_reason="budget_exhausted",
            message="Mission budget allows zero execution rounds.",
            started=started,
            dispatch_payload=preview,
        )

    auto_repair_once = bool(budget["max_rounds"] > 1 and budget["max_repair_rounds"] > 0)
    dispatch = runner(
        workspace_root=workspace_path,
        plan_id=str(plan_id),
        execute=True,
        dry_run=False,
        run_profile=run_profile,
        include_slow=include_slow,
        max_workflows=max_workflows,
        timeout_seconds=min(float(timeout_seconds), float(budget["max_worker_minutes"]) * 60.0),
        allow_dirty=allow_dirty,
        allow_coverage_gap=allow_coverage_gap,
        auto_repair_once=auto_repair_once,
        model_policy=budget.get("model_policy") if isinstance(budget, dict) else None,
        test_command=test_command,
        allow_test_edits=effective_allow_test_edits,
        merge=merge,
    )
    _record_dispatch_attempts(workspace_path, mission["mission_id"], dispatch, start_round=_next_round_number(workspace_path, mission["mission_id"]))
    stop_reason = _stop_reason_from_dispatch(dispatch, budget)
    status = "verified" if stop_reason == "verified" else "stopped"
    return _finish(
        workspace_root=workspace_path,
        mission=mission,
        plan=plan_payload,
        status=status,
        stop_reason=stop_reason,
        message=_message_for_stop(stop_reason),
        started=started,
        dispatch_payload=dispatch,
    )


_TEST_EDIT_REQUEST_PATTERNS = (
    # Verb + up to a clause of qualifiers + 测试, so "写 pytest 测试" or
    # "添加对应的单元测试" match, not only the literal "写测试" (V5 finding:
    # the cold-start objective said "写 pytest 测试" and the guard then
    # deleted the tests the task explicitly asked for).
    re.compile(r"(写|加|补|改|更新|添加|新增|创建|编写)[^。！？!?；;\n]{0,16}测试"),
    re.compile(r"测试用例"),
    re.compile(r"\b(add|write|update|create)\b[^.!?\n]{0,40}\btests?\b"),
    re.compile(r"\btest cases?\b"),
)


def _objective_requests_test_edits(objective: str) -> bool:
    text = str(objective or "").lower()
    compact = "".join(text.split())
    return any(p.search(compact) or p.search(text) for p in _TEST_EDIT_REQUEST_PATTERNS)


def _record_dispatch_attempts(workspace_root: Path, mission_id: str, dispatch: dict[str, Any], *, start_round: int = 1) -> None:
    attempts = dispatch.get("verification_attempts") if isinstance(dispatch.get("verification_attempts"), list) else []
    if attempts:
        for index, attempt in enumerate(attempts, start=1):
            append_round(
                workspace_root,
                mission_id,
                {
                    "round": start_round + index - 1,
                    "type": "verification",
                    "status": str(attempt.get("verdict") or ""),
                    "run_profile": str(attempt.get("run_profile") or ""),
                    "workflow_count": len(attempt.get("results") or []),
                    "failed_signature": _failed_signature(attempt),
                    "payload": _compact_verification(attempt),
                },
            )
        next_round = start_round + len(attempts)
        _record_usage_round(workspace_root, mission_id, dispatch, next_round)
        _record_merge_round(workspace_root, mission_id, dispatch, next_round + 1)
        return
    append_round(
        workspace_root,
        mission_id,
        {
            "round": start_round,
            "type": "dispatch",
            "status": str(dispatch.get("status") or ""),
            "stop_reason": str(dispatch.get("reason") or ""),
            "payload": _compact_dispatch(dispatch),
        },
    )


def _record_merge_round(workspace_root: Path, mission_id: str, dispatch: dict[str, Any], round_no: int) -> None:
    """Persist the merge result so users can see whether their branch was merged."""
    merge = dispatch.get("merge") if isinstance(dispatch.get("merge"), dict) else None
    if not merge:
        return
    append_round(
        workspace_root,
        mission_id,
        {
            "round": round_no,
            "type": "merge",
            "status": str(merge.get("status") or ""),
            "branch": merge.get("branch"),
            "target": merge.get("target"),
            "commit": merge.get("commit"),
            "reason": merge.get("reason"),
        },
    )


def _override_plan_agent(plan: dict[str, Any], agent: str) -> bool:
    requested = str(agent or "").strip()
    if not requested:
        return False
    tracks = plan.get("worker_tracks") if isinstance(plan.get("worker_tracks"), list) else []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        if str(track.get("track_kind") or "implementation") == "inspection":
            continue
        if str(track.get("agent") or "") == requested:
            return False
        track["agent"] = requested
        track["id"] = f"track_1_{requested.replace('-', '_')}"
        return True
    if tracks:
        return False
    plan["worker_tracks"] = [
        {
            "id": f"track_1_{requested.replace('-', '_')}",
            "agent": requested,
            "track_kind": "implementation",
            "tier": "strong",
        }
    ]
    return True


def _record_usage_round(workspace_root: Path, mission_id: str, dispatch: dict[str, Any], round_no: int) -> None:
    """Persist a token/cost round so the user can see what the task actually used."""
    usage = dispatch.get("usage_summary") if isinstance(dispatch.get("usage_summary"), dict) else {}
    if not usage or int(usage.get("total_tokens") or 0) <= 0:
        return
    failover = dispatch.get("failover_worker_record") if isinstance(dispatch.get("failover_worker_record"), dict) else None
    append_round(
        workspace_root,
        mission_id,
        {
            "round": round_no,
            "type": "usage",
            "status": "recorded",
            "usage_summary": usage,
            "failover_backend": (failover or {}).get("backend"),
        },
    )


def _next_round_number(workspace_root: Path, mission_id: str) -> int:
    rounds = load_rounds(workspace_root, mission_id)
    if not rounds:
        return 0
    return max((int(item.get("round") or 0) for item in rounds), default=-1) + 1


def _stop_reason_from_dispatch(dispatch: dict[str, Any], budget: dict[str, Any]) -> str:
    status = str(dispatch.get("status") or "")
    if status == "blocked":
        return _dispatch_stop_reason(dispatch)
    if dispatch.get("quota_exhausted"):
        # The worker never got to do the task; a gate failure here is noise.
        return "quota_exhausted"
    if status == "worker_failed":
        return "worker_error"
    latest = dispatch.get("latest_verification") if isinstance(dispatch.get("latest_verification"), dict) else {}
    verdict = str(latest.get("verdict") or "")
    if verdict == "pass":
        return "verified"
    if verdict == "coverage_gap":
        return "coverage_gap"
    if verdict == "inspection_only":
        return "inspection_only"
    command_result = latest.get("command_verification") if isinstance(latest.get("command_verification"), dict) else {}
    failure_kind = str(command_result.get("failure_kind") or "")
    if failure_kind in {"test_command_invalid", "command_launch_error", "command_timeout"}:
        return failure_kind
    attempts = dispatch.get("verification_attempts") if isinstance(dispatch.get("verification_attempts"), list) else []
    failed = [_failed_signature(item) for item in attempts if _failed_signature(item)]
    if len(failed) >= 2 and failed[-1] == failed[-2]:
        return "same_failure_repeated"
    if len(attempts) >= int(budget.get("max_rounds") or 1):
        return "budget_exhausted"
    if verdict == "fail" or status == "verification_failed":
        return "verification_failed"
    return "worker_error"


def _dispatch_stop_reason(dispatch: dict[str, Any]) -> str:
    reason = str(dispatch.get("reason") or "").lower()
    if "coverage" in reason:
        return "coverage_gap"
    if "clarification" in reason:
        return "needs_clarification"
    if "permission" in reason or "approval" in reason:
        return "permission_required"
    if "dirty" in reason:
        return "permission_required"
    return "blocked"


def _failed_signature(verification: dict[str, Any]) -> str:
    results = verification.get("results") if isinstance(verification.get("results"), list) else []
    for item in results:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "") == "failed":
            return "|".join(
                [
                    str(item.get("name") or ""),
                    str(item.get("failed_step") or ""),
                    str(item.get("message") or ""),
                ]
            )
    repair = verification.get("repair_brief") if isinstance(verification.get("repair_brief"), dict) else {}
    failed_step = repair.get("failed_step") if isinstance(repair.get("failed_step"), dict) else {}
    if repair:
        return "|".join([str(repair.get("workflow") or ""), str(failed_step.get("id") or ""), str(repair.get("message") or "")])
    return ""


def _compact_dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "worker": payload.get("worker"),
        "worktree": payload.get("worktree"),
        "verification": payload.get("verification"),
        "usage_summary": payload.get("usage_summary"),
        "merge": payload.get("merge"),
        "failover_worker_record": _compact_worker(payload.get("failover_worker_record")),
    }


def _compact_worker(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    return {
        "attempt": record.get("attempt"),
        "status": record.get("status"),
        "backend": record.get("backend"),
        "usage": record.get("usage"),
    }


def _compact_verification(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict": payload.get("verdict"),
        "passed": payload.get("passed"),
        "inspection_only": payload.get("inspection_only"),
        "failed": payload.get("failed"),
        "total": payload.get("total"),
        "saved_path": payload.get("saved_path"),
        "repair_brief": payload.get("repair_brief"),
    }


def _message_for_stop(stop_reason: str) -> str:
    return {
        "verified": "Mission verified by Checkpoint.",
        "coverage_gap": (
            "Mission stopped: no test command or workflow coverage found. "
            "Fix: add --test-command \"pytest -q\" (or your project's test command), "
            "or add --allow-coverage-gap to skip this gate."
        ),
        "inspection_only": "Mission produced inspection-only evidence; rerun with a real interaction profile when needed.",
        "test_command_invalid": (
            "Mission stopped: the verification command exited with an error before running any tests. "
            "Fix: run the test command manually to confirm it works, then retry."
        ),
        "command_launch_error": (
            "Mission stopped: the verification command could not be launched. "
            "Fix: check that the command is on PATH and the test dependencies are installed."
        ),
        "command_timeout": (
            "Mission stopped: the verification command timed out. "
            "Fix: increase --timeout-seconds or make the test suite faster."
        ),
        "same_failure_repeated": (
            "Mission stopped: the same test failure repeated after the auto-repair round. "
            "Fix: open final_report.md, read the failing test output, then start a new mission with a more specific goal."
        ),
        "budget_exhausted": (
            "Mission stopped: the configured round budget was exhausted. "
            "Fix: resume the mission with --max-rounds increased, or start a new mission with a narrower goal."
        ),
        "worker_error": (
            "Mission stopped: the AI worker process failed or was killed. "
            "Fix: run `checkpoint agents doctor` to confirm the agent is available, then resume."
        ),
        "verification_failed": (
            "Mission stopped: verification still fails after all repair rounds. "
            "Fix: open final_report.md to see what the worker tried, then refine the goal."
        ),
        "permission_required": (
            "Mission stopped: the repository has uncommitted changes (dirty working tree). "
            "Fix: commit or stash your changes first, then retry. "
            "Or add --allow-dirty to skip this safety check."
        ),
        "quota_exhausted": (
            "Mission stopped: all available AI backends hit their quota limit. "
            "Fix: wait for quota to reset (codex: weekly, claude: 5-hour window) or switch to a different --agent."
        ),
        "needs_clarification": (
            "Mission stopped: the goal is too vague to plan safely. "
            "Fix: refine the --goal to include a specific acceptance criterion, e.g. 'add X so that test Y passes'."
        ),
    }.get(stop_reason, "Mission stopped.")


def _finish(
    *,
    workspace_root: Path,
    mission: dict[str, Any] | None,
    plan: dict[str, Any] | None,
    status: str,
    stop_reason: str,
    message: str,
    started: float,
    dispatch_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rounds: list[dict[str, Any]] = []
    if mission:
        rounds = load_rounds(workspace_root, str(mission["mission_id"]))
        mission.update(
            {
                "status": status,
                "stop_reason": stop_reason,
                "current_round": max((int(item.get("round") or 0) for item in rounds), default=0),
            }
        )
        saved = save_mission(workspace_root, mission)
        mission = saved["mission"]
    payload = {
        "schema_version": 1,
        "product": "DevPacer",
        "verification_engine": "Checkpoint",
        "status": status,
        "stop_reason": stop_reason,
        "message": message,
        "elapsed_seconds": round(monotonic() - started, 6),
        "mission": mission,
        "plan": plan,
        "rounds": rounds,
        "dispatch": dispatch_payload,
    }
    if mission:
        report_text = chief_run_to_markdown(payload)
        saved_report = write_final_report(workspace_root, str(mission["mission_id"]), report_text)
        payload["final_report_path"] = saved_report["path"]
        record = _append_mandatory_record(payload)
        if record.get("path"):
            payload["mandatory_record_path"] = record["path"]
        if record.get("error"):
            payload["mandatory_record_error"] = record["error"]
    return payload


def _append_mandatory_record(payload: dict[str, Any]) -> dict[str, Any]:
    mission = payload.get("mission") if isinstance(payload.get("mission"), dict) else {}
    if not mission:
        return {}
    repo_root_text = str(mission.get("repo_root") or "").strip()
    if not repo_root_text:
        return {}
    try:
        repo_root = Path(repo_root_text).expanduser().resolve()
        repo_root.mkdir(parents=True, exist_ok=True)
        path = repo_root / "强制测试记录.md"
        dispatch = payload.get("dispatch") if isinstance(payload.get("dispatch"), dict) else {}
        latest = dispatch.get("latest_verification") if isinstance(dispatch.get("latest_verification"), dict) else {}
        command_verification = latest.get("command_verification") if isinstance(latest.get("command_verification"), dict) else {}
        merge = dispatch.get("merge") if isinstance(dispatch.get("merge"), dict) else {}
        usage = dispatch.get("usage_summary") if isinstance(dispatch.get("usage_summary"), dict) else {}
        if not path.exists():
            path.write_text(
                "# 强制测试记录\n\n"
                "本文件由 DevPacer / Checkpoint / Codex 自动追加。每次托管开发、验收、合并或失败都必须记录到这里，方便审查与回滚。\n",
                encoding="utf-8",
            )
        lines = [
            "",
            f"## {mission.get('mission_id')} - {payload.get('status')} / {payload.get('stop_reason')}",
            "",
            f"- Time: {mission.get('updated_at') or ''}",
            f"- Objective: {mission.get('objective') or ''}",
            f"- Final report: {payload.get('final_report_path') or ''}",
            f"- Verification command: {command_verification.get('command') or ''}",
            f"- Verification verdict: {latest.get('verdict') or ''}",
            f"- Merge: {merge.get('status') or 'not_requested'}",
            f"- Tokens: {usage.get('total_tokens', 0)}",
            f"- Message: {payload.get('message') or ''}",
        ]
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
        return {"path": str(path)}
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _terminal_payload(
    *,
    workspace_root: Path,
    mission: dict[str, Any] | None,
    plan: dict[str, Any] | None,
    status: str,
    stop_reason: str,
    message: str,
    started: float,
) -> dict[str, Any]:
    return _finish(
        workspace_root=workspace_root,
        mission=mission,
        plan=plan,
        status=status,
        stop_reason=stop_reason,
        message=message,
        started=started,
    )


def _elapsed_minutes(started: float) -> float:
    return (monotonic() - started) / 60.0


def chief_run_to_markdown(payload: dict[str, Any]) -> str:
    mission = payload.get("mission") if isinstance(payload.get("mission"), dict) else {}
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    rounds = payload.get("rounds") if isinstance(payload.get("rounds"), list) else []
    lines = ["## DevPacer Mission", ""]
    lines.append(f"Status: `{payload.get('status')}`")
    lines.append(f"Stop reason: `{payload.get('stop_reason')}`")
    if payload.get("message"):
        lines.append(f"Message: {payload['message']}")
    if mission:
        lines.extend(
            [
                "",
                "### Mission",
                "",
                f"- Mission id: `{mission.get('mission_id')}`",
                f"- Objective: {mission.get('objective')}",
                f"- Plan id: `{mission.get('plan_id')}`",
                f"- Product: `{mission.get('product')}`",
                f"- Verification engine: `{mission.get('verification_engine')}`",
            ]
        )
        budget = mission.get("budget_policy") if isinstance(mission.get("budget_policy"), dict) else {}
        if budget:
            lines.append(
                f"- Budget: max_rounds={budget.get('max_rounds')}, "
                f"max_wall_minutes={budget.get('max_wall_minutes')}, "
                f"max_worker_minutes={budget.get('max_worker_minutes')}"
            )
    if plan:
        lines.extend(["", "### Plan", ""])
        lines.append(f"- Status: `{plan.get('status')}`")
        workflows = ", ".join(plan.get("selected_workflows") or []) or "none"
        lines.append(f"- Selected workflows: {workflows}")
    grounding = plan.get("grounding") if isinstance(plan.get("grounding"), dict) else None
    if grounding:
        lines.extend(["", grounding_to_markdown(grounding)])
    if rounds:
        lines.extend(["", "### Rounds", ""])
        for item in rounds:
            if item.get("type") == "merge":
                merge_detail = str(item.get("reason") or item.get("commit") or "").strip()[:80]
                suffix = f" - {merge_detail}" if merge_detail else ""
            else:
                detail = str(item.get("failed_signature") or item.get("stop_reason") or "").strip()
                suffix = f" - {detail}" if detail else ""
            lines.append(f"- Round {item.get('round')}: `{item.get('type')}` -> `{item.get('status')}`{suffix}")
    usage_round = next((item for item in reversed(rounds) if str(item.get("type")) == "usage"), None)
    if usage_round:
        usage = usage_round.get("usage_summary") if isinstance(usage_round.get("usage_summary"), dict) else {}
        lines.extend(["", "### 用量 Tokens & Cost", ""])
        cache_read = int(usage.get("cache_read_tokens") or 0)
        cache_create = int(usage.get("cache_creation_tokens") or 0)
        token_detail = f"新输入 {usage.get('input_tokens', 0)} / 输出 {usage.get('output_tokens', 0)}"
        if cache_read or cache_create:
            token_detail += f" / 缓存读 {cache_read} / 缓存写 {cache_create}"
        lines.append(f"- Tokens: {usage.get('total_tokens', 0)} ({token_detail})")
        lines.append(f"- 订阅真实花费: ${usage.get('spent_usd', 0)}")
        if float(usage.get("saved_usd") or 0) > 0:
            lines.append(f"- 走便宜后端省下(按 Claude 价估): ${usage.get('saved_usd', 0)}")
        if usage_round.get("failover_backend"):
            lines.append(f"- ⚡ 额度失败转移到便宜后端: `{usage_round.get('failover_backend')}`")
    background = payload.get("background") if isinstance(payload.get("background"), dict) else {}
    if background:
        lines.extend(["", "### Background", ""])
        if background.get("pid"):
            lines.append(f"- PID: `{background.get('pid')}`")
        if background.get("stdout_log"):
            lines.append(f"- Stdout: `{background.get('stdout_log')}`")
        if background.get("stderr_log"):
            lines.append(f"- Stderr: `{background.get('stderr_log')}`")
    if payload.get("final_report_path"):
        lines.extend(["", "### Records", "", f"- Final report: `{payload['final_report_path']}`"])
    return "\n".join(lines).rstrip()


def mission_status_payload(*, workspace_root: str | Path, mission_id: str) -> dict[str, Any]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    mission = load_mission(workspace_path, mission_id)
    if mission is None:
        return {
            "schema_version": 1,
            "product": "DevPacer",
            "verification_engine": "Checkpoint",
            "status": "blocked",
            "stop_reason": "missing_mission",
            "message": f"No saved mission found: {mission_id}",
            "mission": None,
            "rounds": [],
        }
    rounds = load_rounds(workspace_path, mission_id)
    payload = {
        "schema_version": 1,
        "product": mission.get("product", "DevPacer"),
        "verification_engine": mission.get("verification_engine", "Checkpoint"),
        "status": mission.get("status", ""),
        "stop_reason": mission.get("stop_reason", ""),
        "message": _status_next_action(mission),
        "mission": mission,
        "plan": {},
        "rounds": rounds,
    }
    report_path = Path(workspace_path) / "missions" / mission_id / "final_report.md"
    if report_path.exists():
        payload["final_report_path"] = str(report_path)
    background_path = Path(workspace_path) / "missions" / mission_id / "background.json"
    if background_path.exists():
        try:
            from .chief_background import inspect_background_state

            payload["background"] = inspect_background_state(workspace_root=workspace_path, mission_id=mission_id, update=True)
            if payload["background"].get("status") in {"completed", "failed", "timeout", "orphaned"}:
                refreshed = load_mission(workspace_path, mission_id)
                if refreshed is not None:
                    payload["mission"] = refreshed
                    payload["status"] = refreshed.get("status", payload["status"])
                    payload["stop_reason"] = refreshed.get("stop_reason", payload["stop_reason"])
                    payload["rounds"] = load_rounds(workspace_path, mission_id)
                    payload["message"] = _status_next_action(refreshed)
                if payload["background"].get("status") in {"timeout", "orphaned"}:
                    saved_report = write_final_report(workspace_path, mission_id, chief_run_to_markdown(payload))
                    payload["final_report_path"] = saved_report["path"]
        except (OSError, json.JSONDecodeError):
            payload["background"] = {"status": "unreadable", "path": str(background_path)}
    return payload


def _status_next_action(mission: dict[str, Any]) -> str:
    status = str(mission.get("status") or "")
    stop_reason = str(mission.get("stop_reason") or "")
    if status == "background_running":
        return "Background run is active or recently started. Inspect logs if it does not update."
    if status == "preview" and stop_reason == "preview_only":
        return "Run `mission start --resume <mission_id> --execute` to execute this previewed mission."
    if status == "verified":
        return "Mission verified. Run `checkpoint mission list` to review, or `git log` to see the merged commit."
    if stop_reason == "coverage_gap":
        return "Add --test-command \"<your test command>\" to your next mission start, or add --allow-coverage-gap."
    if stop_reason == "needs_clarification":
        return "Refine the goal with a concrete acceptance criterion and start a new mission."
    if stop_reason == "permission_required":
        return "Run `git status` to see dirty files. Commit or stash them, then retry."
    if stop_reason in ("same_failure_repeated", "verification_failed"):
        return "Open final_report.md to read the failing test output, then start a new mission with a more specific goal."
    if stop_reason == "quota_exhausted":
        return "Wait for quota reset or switch agent with --agent claude-code (or --agent mimo)."
    if stop_reason == "worker_error":
        return "Run `checkpoint agents doctor` to confirm the agent is installed, then resume the mission."
    if stop_reason in ("budget_exhausted", "command_timeout"):
        return "Resume with a larger --max-rounds or narrower --goal to reduce scope."
    return "Run `checkpoint mission list` and open final_report.md for the next action."


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2)
