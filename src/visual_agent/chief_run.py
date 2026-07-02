"""Bounded autonomous development missions.

``chief-run`` is the first DevPacer loop: build or load a plan, persist mission
state, preview the dispatch, optionally execute Codex, verify with Checkpoint,
and stop with a durable reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from .chief_dispatch import dispatch_chief_plan
from .chief_engineer import build_chief_plan, chief_plan_to_dict
from .chief_plans_store import load_plan, save_plan
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
    dispatch_runner: DispatchRunner | None = None,
) -> dict[str, Any]:
    started = monotonic()
    workspace_path = Path(workspace_root).expanduser().resolve()
    repo_path = Path(repo_root).expanduser().resolve()
    budget = default_budget_policy(
        max_rounds=max_rounds,
        max_wall_minutes=max_wall_minutes,
        max_worker_minutes=max_worker_minutes,
    )
    runner = dispatch_runner or dispatch_chief_plan
    mission: dict[str, Any] | None = None
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
        saved = save_plan(plan_payload, workspace_root=workspace_path)
        plan_payload["plan_id"] = saved["plan_id"]
        plan_payload["saved_path"] = saved["path"]
        plan_id = saved["plan_id"]
    else:
        plan_id = str(plan_payload.get("plan_id") or plan_id)
        if not goal:
            goal = str(plan_payload.get("objective") or "")

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
    if plan_status in {"blocked", "needs_clarification"}:
        stop_reason = "needs_clarification" if plan_status == "needs_clarification" else "blocked_plan"
        append_round(
            workspace_path,
            mission["mission_id"],
            {
                "round": _next_round_number(workspace_path, mission["mission_id"]),
                "type": "planning",
                "status": plan_status,
                "stop_reason": stop_reason,
                "plan_id": plan_id,
            },
        )
        return _finish(
            workspace_root=workspace_path,
            mission=mission,
            plan=plan_payload,
            status="stopped",
            stop_reason=stop_reason,
            message="Mission stopped before dispatch because the plan is not actionable.",
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
            message="Dry-run complete. Re-run with --execute to launch the Codex worker.",
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


def _next_round_number(workspace_root: Path, mission_id: str) -> int:
    rounds = load_rounds(workspace_root, mission_id)
    if not rounds:
        return 0
    return max((int(item.get("round") or 0) for item in rounds), default=-1) + 1


def _stop_reason_from_dispatch(dispatch: dict[str, Any], budget: dict[str, Any]) -> str:
    status = str(dispatch.get("status") or "")
    if status == "blocked":
        return _dispatch_stop_reason(dispatch)
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
        "coverage_gap": "Mission stopped because verification coverage is missing or weak.",
        "inspection_only": "Mission produced inspection-only evidence; rerun with a real interaction profile when needed.",
        "same_failure_repeated": "Mission stopped because the same verification failure repeated after repair.",
        "budget_exhausted": "Mission stopped because the configured round budget was exhausted.",
        "worker_error": "Mission stopped because the worker process failed.",
        "verification_failed": "Mission stopped because verification still fails.",
        "permission_required": "Mission stopped because human approval or a clean repository is required.",
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
    return payload


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
    if rounds:
        lines.extend(["", "### Rounds", ""])
        for item in rounds:
            detail = str(item.get("failed_signature") or item.get("stop_reason") or "").strip()
            suffix = f" - {detail}" if detail else ""
            lines.append(f"- Round {item.get('round')}: `{item.get('type')}` -> `{item.get('status')}`{suffix}")
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
        return "Run `chief-run --resume <mission_id> --execute` to execute this previewed mission."
    if stop_reason == "coverage_gap":
        return "Add or select workflow coverage, then rerun or resume with an explicit coverage override only for experiments."
    if stop_reason == "needs_clarification":
        return "Answer the clarifying questions and create a new mission."
    if status == "verified":
        return "Mission is verified; review the final report and worktree before merge."
    return "Review the rounds and final report for the next action."


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2)
