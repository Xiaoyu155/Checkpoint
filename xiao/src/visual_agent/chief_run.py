"""Bounded autonomous development missions.

``chief-run`` is the first Pacer loop: build or load a plan, persist mission
state, preview the dispatch, optionally execute Codex, verify with Checkpoint,
and stop with a durable reason.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from .agent_capabilities import canonical_agent_name, load_agent_profile, recommend_worker_config
from .chief_dispatch import dispatch_chief_plan, preflight_to_markdown
from .chief_engineer import build_chief_plan, chief_plan_to_dict
from .chief_plans_store import load_plan, save_plan
from .command_verification import (
    NON_REPAIRABLE_COMMAND_FAILURE_KINDS,
    normalize_verification_env,
    verification_env_from_required_names,
)
from .goal_grounding import goal_references_plan, ground_goal, grounding_to_markdown
from .mission_intake import is_manual_verification_goal, is_review_plan_goal
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
from .mission_pipeline import mission_result_to_pipeline_state, write_mission_state
from .mission_contract import normalize_requirement_contract, requirement_contract_planning_answers
from .mission_progress import build_mission_progress, save_mission_progress
from .managed_state import managed_task_idempotency_key
from .model_router import tier_task_kind
from .notifications import build_event_notification, load_notification_config, send_email_notification
from .verification_profiles import resolve_test_command
from .workspace import init_workspace


DispatchRunner = Callable[..., dict[str, Any]]


def _planning_answers(answers: tuple[str, ...] | list[str], contract: dict[str, Any]) -> tuple[str, ...]:
    merged: list[str] = []
    for item in answers:
        text = str(item).strip()
        if text and text not in merged:
            merged.append(text)
    for item in requirement_contract_planning_answers(contract):
        if item and item not in merged:
            merged.append(item)
    return tuple(merged)


def _merge_verification_env(*groups: list[dict[str, Any]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for item in normalize_verification_env(group):
            if item.get("kind") == "env_var":
                key = ("env_var", str(item.get("name") or ""))
            else:
                key = ("marker", str(item.get("pattern") or ""))
            if key[1] and key not in seen:
                seen.add(key)
                merged.append(item)
    return merged


def _managed_execution_policy(
    budget: dict[str, Any],
    *,
    idempotency_key: str,
) -> dict[str, Any]:
    existing = (
        budget.get("execution_policy")
        if isinstance(budget.get("execution_policy"), dict)
        else {}
    )
    return {
        **existing,
        "idempotency_key": str(idempotency_key),
        "managed_budget": {
            "max_wall_seconds": float(budget.get("max_wall_minutes") or 1) * 60.0,
            "max_total_tokens": int(budget.get("max_total_tokens") or 120_000),
            "max_attempts": int(budget.get("max_rounds") or 1),
            "max_repair_rounds": int(budget.get("max_repair_rounds") or 0),
            "max_same_failure_count": int(budget.get("max_same_failure_count") or 2),
        },
    }


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
    max_rounds: int = 3,
    max_repair_rounds: int | None = 2,
    max_wall_minutes: int = 60,
    max_worker_minutes: int = 45,
    max_total_tokens: int = 120_000,
    max_same_failure_count: int = 2,
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
    base_probe_enabled: bool = True,
    merge: bool = False,
    require_env: tuple[str, ...] = (),
    verification_env: list[dict[str, Any]] | None = None,
    dispatch_runner: DispatchRunner | None = None,
    ground_vague_goals: bool = True,
    grounding_runner: Callable[..., dict[str, Any]] | None = None,
    requirement_contract: dict[str, Any] | None = None,
    reasoning_effort: str | None = None,
    dispatch_mode: str | None = None,
    prompt_style: str | None = None,
    repair_strategy: str | None = None,
    model_policy: dict[str, Any] | None = None,
    execution_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = monotonic()
    run_started_at = datetime.now(timezone.utc).isoformat()
    workspace_path = Path(workspace_root).expanduser().resolve()
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
    repo_path = Path(
        str((mission or {}).get("repo_root") or repo_root)
    ).expanduser().resolve()
    if mission is not None:
        plan_id = str(mission.get("plan_id") or plan_id or "")
        goal = str(mission.get("objective") or goal or "")
    original_goal = str(goal or "")
    requested_test_command = str(test_command or "").strip()
    if not requested_test_command and mission is not None:
        requested_test_command = str(mission.get("test_command") or "").strip()
    resolved_test_command, verification_profile = resolve_test_command(
        requested_test_command or None,
        repo_root=repo_path,
    )
    test_command_unresolved = bool(requested_test_command) and not str(resolved_test_command or "").strip()
    test_command = requested_test_command if test_command_unresolved else resolved_test_command
    if test_command and not workspace_path.exists():
        init_workspace(workspace_path, with_demo=False)
    agent_for_contract = str(agents[0]) if agents else ""
    contract_source = requirement_contract
    if contract_source is None and isinstance((mission or {}).get("requirement_contract"), dict):
        contract_source = mission.get("requirement_contract")
    contract_payload = normalize_requirement_contract(
        contract_source,
        goal=original_goal,
        answers=answers,
        repo_root=repo_path,
        test_command=test_command,
        agent=agent_for_contract,
    )
    contract_goal = str(contract_payload.get("final_goal") or "").strip()
    if contract_goal:
        goal = contract_goal
    planning_answers = _planning_answers(answers, contract_payload)
    budget = default_budget_policy(
        max_rounds=max_rounds,
        max_wall_minutes=max_wall_minutes,
        max_worker_minutes=max_worker_minutes,
        max_repair_rounds=max_repair_rounds,
        max_total_tokens=max_total_tokens,
        max_same_failure_count=max_same_failure_count,
    )
    if isinstance(model_policy, dict):
        budget["model_policy"] = {
            **budget["model_policy"],
            **{str(key): str(value) for key, value in model_policy.items() if str(value).strip()},
        }
    if isinstance(execution_policy, dict):
        budget["execution_policy"] = {
            str(key): value for key, value in execution_policy.items() if value not in {None, ""}
        }
    requested_verification_env = _merge_verification_env(
        normalize_verification_env((mission or {}).get("verification_env")),
        normalize_verification_env(verification_env),
        verification_env_from_required_names(require_env),
    )
    runner = dispatch_runner or dispatch_chief_plan
    grounding_payload: dict[str, Any] | None = None
    if mission is not None:
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
            answers=planning_answers,
        )
        plan_payload = chief_plan_to_dict(plan)
        if contract_payload:
            plan_payload["requirement_contract"] = contract_payload
        # A vague goal is not always noise: it often points at a plan document
        # in the repo. Review those documents (cheap model) before refusing —
        # resolve into the plan's next concrete task, or come back with a
        # proposed plan to discuss instead of a bare error. A goal that cites a
        # plan ("按照开发计划推进") is grounded even when it passes the clarity
        # gate: dispatching it raw would burn a worker run on a goal whose
        # definition of done lives in a file the worker never read.
        review_request_goal = is_review_plan_goal(str(goal))
        if ground_vague_goals and not review_request_goal and (
            str(plan_payload.get("status") or "") == "needs_clarification" or goal_references_plan(str(goal))
        ):
            grounder = grounding_runner or ground_goal
            grounding_payload = grounder(goal=str(goal), repo_root=repo_path)
            if not isinstance(grounding_payload, dict):
                grounding_payload = None
            if grounding_payload and grounding_payload.get("resolved") and str(grounding_payload.get("grounded_goal") or "").strip():
                goal = str(grounding_payload["grounded_goal"]).strip()
                grounded_answers = tuple(planning_answers) + tuple(
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
                if contract_payload:
                    plan_payload["requirement_contract"] = contract_payload
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
    verification_mode = "command" if str(test_command or "").strip() else "workflow"
    if str(plan_payload.get("verification_mode") or "workflow") != verification_mode:
        plan_payload["verification_mode"] = verification_mode
        if plan_id:
            save_plan(plan_payload, workspace_root=workspace_path, plan_id=str(plan_id))
    if agents:
        changed_agent = _override_plan_agent(plan_payload, str(agents[0]))
        if changed_agent and plan_id:
            save_plan(plan_payload, workspace_root=workspace_path, plan_id=str(plan_id))
    if verification_profile:
        plan_payload["verification_profile"] = verification_profile
        if plan_id:
            save_plan(plan_payload, workspace_root=workspace_path, plan_id=str(plan_id))
    effective_allow_dirty = bool(allow_dirty or (mission or {}).get("allow_dirty"))
    effective_allow_test_edits = bool((mission or {}).get("allow_test_edits")) or allow_test_edits or _plan_requests_test_edits(
        original_goal=original_goal,
        goal=str(goal or ""),
        plan=plan_payload,
        grounding=grounding_payload,
    )
    effective_merge = bool(merge or (mission or {}).get("merge"))
    saved_reasoning_effort = str((mission or {}).get("reasoning_effort") or "").strip()
    dispatch_reasoning_effort: str | None
    if reasoning_effort is not None:
        dispatch_reasoning_effort = str(reasoning_effort).strip() or "inherit"
    elif saved_reasoning_effort and saved_reasoning_effort.lower() != "inherit":
        dispatch_reasoning_effort = saved_reasoning_effort
    else:
        dispatch_reasoning_effort = None
    effective_reasoning_effort = dispatch_reasoning_effort or "inherit"
    effective_dispatch_mode = str(dispatch_mode or (mission or {}).get("dispatch_mode") or "tracked")
    effective_prompt_style = str(prompt_style or (mission or {}).get("prompt_style") or "expanded")
    effective_repair_strategy = str(repair_strategy or (mission or {}).get("repair_strategy") or "resume")
    if effective_allow_test_edits and not allow_test_edits:
        plan_payload["test_edit_policy"] = {
            "allow_test_edits": True,
            "source": "objective",
            "reason": "The objective explicitly asks to add or update tests.",
        }
        if plan_id:
            save_plan(plan_payload, workspace_root=workspace_path, plan_id=str(plan_id))

    task_idempotency_key = str((mission or {}).get("idempotency_key") or "").strip()
    if not task_idempotency_key:
        task_idempotency_key = managed_task_idempotency_key(
            goal=str(goal or plan_payload.get("objective") or ""),
            repo_root=str(repo_path),
            test_command=str(test_command or ""),
            requirement_contract=contract_payload,
        )

    if mission is None:
        mission = create_mission(
            workspace_root=workspace_path,
            objective=str(goal or plan_payload.get("objective") or ""),
            repo_root=repo_path,
            plan_id=str(plan_id),
            budget_policy=budget,
            mission_id=mission_id,
            status="created",
            requirement_contract=contract_payload,
            verification_env=requested_verification_env,
            test_command=test_command,
            allow_dirty=effective_allow_dirty,
            allow_test_edits=effective_allow_test_edits,
            merge=effective_merge,
            reasoning_effort=effective_reasoning_effort,
            dispatch_mode=effective_dispatch_mode,
            prompt_style=effective_prompt_style,
            repair_strategy=effective_repair_strategy,
        )
        mission["idempotency_key"] = task_idempotency_key
        mission = save_mission(workspace_path, mission)["mission"]
        if execute and not dry_run:
            mission["budget_started_at"] = run_started_at
            mission["status"] = "running"
            mission = save_mission(workspace_path, mission)["mission"]
        write_mission_state(
            workspace_path,
            str(mission["mission_id"]),
            current_state="DRAFT",
            event="chief_run_mission_created",
            goal=str(goal or plan_payload.get("objective") or ""),
            plan_id=str(plan_id),
            idempotency_key=task_idempotency_key,
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
        if contract_payload:
            append_round(
                workspace_path,
                mission["mission_id"],
                {
                    "round": _next_round_number(workspace_path, mission["mission_id"]),
                    "type": "requirement_contract",
                    "status": "recorded",
                    "payload": _compact_requirement_contract(contract_payload),
                },
            )
        save_mission_progress(
            workspace_path,
            str(mission["mission_id"]),
            stage="planning",
            stage_label="Planning",
            status=str(mission.get("status") or "created"),
            plan_id=str(plan_id or ""),
            last_activity_at=str(mission.get("updated_at") or ""),
        )
    elif execute and not dry_run:
        if requested_verification_env:
            mission["verification_env"] = requested_verification_env
        if test_command:
            mission["test_command"] = str(test_command)
        mission["allow_dirty"] = bool(effective_allow_dirty)
        mission["allow_test_edits"] = bool(effective_allow_test_edits)
        mission["merge"] = effective_merge
        mission["reasoning_effort"] = effective_reasoning_effort
        mission["dispatch_mode"] = effective_dispatch_mode
        mission["prompt_style"] = effective_prompt_style
        mission["repair_strategy"] = effective_repair_strategy
        mission.setdefault("budget_started_at", run_started_at)
        mission["status"] = "running"
        mission["stop_reason"] = ""
        save_mission(workspace_path, mission)
        save_mission_progress(
            workspace_path,
            str(mission["mission_id"]),
            stage="resuming",
            stage_label="Resuming mission",
            status="running",
            plan_id=str(plan_id or ""),
        )
        write_mission_state(
            workspace_path,
            str(mission["mission_id"]),
            current_state="EXECUTING",
            event="chief_run_resume_executing",
            plan_id=str(plan_id or ""),
            idempotency_key=task_idempotency_key,
        )
    if _mission_budget_elapsed_minutes(mission, started) > float(budget["max_wall_minutes"]):
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
    _mission_objective = str((mission or {}).get("objective") or goal or plan_payload.get("objective") or "")
    manual_verification_goal = is_manual_verification_goal(_mission_objective)
    review_plan_goal = is_review_plan_goal(_mission_objective)
    # A plan-referencing goal that grounding could not resolve stops here even
    # if the plan looks "ready": dispatching it would waste the worker run.
    grounding_unresolved = bool(grounding_payload) and not bool(grounding_payload.get("resolved"))
    if review_plan_goal and not test_command and (plan_status == "needs_clarification" or grounding_unresolved):
        # Review/plan goals are themselves actionable report missions. They do
        # not need a pre-existing plan document before the worker can inspect
        # the repository and produce 审查与开发计划.md.
        plan_status = "needs_workflow_coverage"
        plan_payload["status"] = plan_status
        grounding_unresolved = False
    if (
        (plan_status in {"needs_clarification", "needs_workflow_coverage"} or grounding_unresolved)
        and not test_command
        and manual_verification_goal
    ):
        append_round(
            workspace_path,
            mission["mission_id"],
            {
                "round": _next_round_number(workspace_path, mission["mission_id"]),
                "type": "planning",
                "status": "manual_acceptance_required",
                "plan_id": plan_id,
            },
        )
        return _finish(
            workspace_root=workspace_path,
            mission=mission,
            plan=plan_payload,
            status="preview",
            stop_reason="manual_verification_required",
            message="任务需要人工/现场验收方案，请在工作台主对话框确认设备、场景、指标和结论模板。",
            started=started,
        )
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
    # Diagnosis missions deliver a root-cause report, not a code change, so
    # workflow coverage does not apply to them.
    from .chief_engineer import is_diagnosis_goal as _is_diag

    if (
        plan_status == "needs_workflow_coverage"
        and not allow_coverage_gap
        and not test_command
        and not _is_diag(_mission_objective)
        and not manual_verification_goal
        and not review_plan_goal
    ):
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
    if plan_status == "needs_workflow_coverage" and not test_command and manual_verification_goal:
        append_round(
            workspace_path,
            mission["mission_id"],
            {
                "round": _next_round_number(workspace_path, mission["mission_id"]),
                "type": "planning",
                "status": "manual_acceptance_required",
                "plan_id": plan_id,
            },
        )
        return _finish(
            workspace_root=workspace_path,
            mission=mission,
            plan=plan_payload,
            status="preview",
            stop_reason="manual_verification_required",
            message="任务需要人工/现场验收方案，请在工作台主对话框确认设备、场景、指标和结论模板。",
            started=started,
        )

    managed_execution_policy = _managed_execution_policy(
        budget,
        idempotency_key=task_idempotency_key,
    )
    preview = runner(
        workspace_root=workspace_path,
        plan_id=str(plan_id),
        mission_id=str(mission["mission_id"]),
        execute=False,
        dry_run=True,
        run_profile=run_profile,
        include_slow=include_slow,
        max_workflows=max_workflows,
        timeout_seconds=min(float(timeout_seconds), float(budget["max_worker_minutes"]) * 60.0),
        delegated_timeout_seconds=min(float(timeout_seconds) * 2.0, float(budget["max_worker_minutes"]) * 60.0),
        allow_dirty=effective_allow_dirty,
        allow_coverage_gap=allow_coverage_gap,
        auto_repair_once=False,
        max_repair_rounds=0,
        model_policy=budget.get("model_policy") if isinstance(budget, dict) else None,
        execution_policy=managed_execution_policy,
        reasoning_effort=dispatch_reasoning_effort,
        dispatch_mode=effective_dispatch_mode,
        prompt_style=effective_prompt_style,
        repair_strategy=effective_repair_strategy,
        test_command=test_command,
        allow_test_edits=effective_allow_test_edits,
        base_probe_enabled=base_probe_enabled,
        verification_env=requested_verification_env,
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
    preview_worktree = preview.get("worktree") if isinstance(preview.get("worktree"), dict) else {}
    save_mission_progress(
        workspace_path,
        str(mission["mission_id"]),
        stage="dispatch_ready",
        stage_label="Dispatch ready",
        status=str(preview.get("status") or ""),
        plan_id=str(plan_id or ""),
        worktree=str(preview_worktree.get("path") or ""),
    )
    if str(preview.get("status") or "") in {"blocked", "preflight_blocked"}:
        return _finish(
            workspace_root=workspace_path,
            mission=mission,
            plan=plan_payload,
            status="stopped",
            stop_reason=_dispatch_stop_reason(preview),
            message=str(preview.get("message") or preview.get("reason") or "Dispatch preview was blocked."),
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

    used_execution_rounds = _used_execution_rounds(workspace_path, str(mission["mission_id"]))
    remaining_rounds = max(0, int(budget.get("max_rounds") or 0) - used_execution_rounds)
    if remaining_rounds < 1:
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

    repair_rounds = min(
        max(0, remaining_rounds - 1),
        max(0, int(budget.get("max_repair_rounds") or 0)),
    )
    auto_repair_once = bool(repair_rounds > 0)
    save_mission_progress(
        workspace_path,
        str(mission["mission_id"]),
        stage="worker_starting",
        stage_label="Worker starting",
        status="running",
        plan_id=str(plan_id or ""),
    )
    worker_timeout_seconds = min(float(timeout_seconds), float(budget["max_worker_minutes"]) * 60.0)
    delegated_timeout_seconds = min(float(timeout_seconds) * 2.0, float(budget["max_worker_minutes"]) * 60.0)
    dispatch = runner(
        workspace_root=workspace_path,
        plan_id=str(plan_id),
        mission_id=str(mission["mission_id"]),
        execute=True,
        dry_run=False,
        run_profile=run_profile,
        include_slow=include_slow,
        max_workflows=max_workflows,
        timeout_seconds=worker_timeout_seconds,
        delegated_timeout_seconds=delegated_timeout_seconds,
        allow_dirty=effective_allow_dirty,
        allow_coverage_gap=allow_coverage_gap,
        auto_repair_once=auto_repair_once,
        max_repair_rounds=repair_rounds,
        model_policy=budget.get("model_policy") if isinstance(budget, dict) else None,
        execution_policy=managed_execution_policy,
        reasoning_effort=dispatch_reasoning_effort,
        dispatch_mode=effective_dispatch_mode,
        prompt_style=effective_prompt_style,
        repair_strategy=effective_repair_strategy,
        test_command=test_command,
        allow_test_edits=effective_allow_test_edits,
        base_probe_enabled=base_probe_enabled,
        merge=effective_merge,
        verification_env=requested_verification_env,
        allow_prior_verified_evidence=_allow_prior_verified_evidence_for_resume(
            mission,
            resume_mission_id=resume_mission_id,
        ),
    )
    _record_dispatch_attempts(workspace_path, mission["mission_id"], dispatch, start_round=_next_round_number(workspace_path, mission["mission_id"]))
    remaining_budget = {**budget, "max_rounds": remaining_rounds}
    stop_reason = _stop_reason_from_dispatch(dispatch, remaining_budget)
    dispatch_status = str(dispatch.get("status") or "")
    status = (
        "verified"
        if stop_reason == "verified"
        else "verified_blocked"
        if dispatch_status == "verified_blocked"
        else "stopped"
    )
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
    re.compile(r"(写|加|补|改|更新|添加|新增|创建|编写|补齐)[^。！？!?；;\n]{0,24}(验收|评测|回归|eval)(样本|用例|脚本|数据|测试|fixture|cases?)"),
    re.compile(r"测试用例"),
    re.compile(r"(验收|评测|回归)(样本|用例)"),
    re.compile(r"\b(add|write|update|create)\b[^.!?\n]{0,40}\btests?\b"),
    re.compile(r"\b(add|write|update|create)\b[^.!?\n]{0,40}\b(eval|acceptance|regression)\b"),
    re.compile(r"\b(add|write|update|create|extend|improve|increase)\b[^.!?\n]{0,80}\b(?:test\s+)?coverage\b"),
    re.compile(r"(补|增加|新增|提高|完善)[^。！？!?；;\n]{0,24}(覆盖|覆盖率)"),
    re.compile(r"\btest cases?\b"),
)
_NEGATED_TEST_EDIT_PATTERNS = (
    re.compile(r"(不要|不许|禁止|不能|勿|不得)[^。！？!?；;\n]{0,20}(改|修改|更新|添加|新增|创建|编写|写|补|补齐)[^。！？!?；;\n]{0,20}(测试|验收|评测|回归|eval)"),
    re.compile(r"保留[^。！？!?；;\n]{0,20}(测试|验收|评测|回归|eval)[^。！？!?；;\n]{0,20}不变"),
    re.compile(r"\b(do not|don't|never)\b[^.!?\n]{0,40}\b(modify|change|edit|update|add|write|create)\b[^.!?\n]{0,40}\b(tests?|eval|acceptance|regression)\b"),
)


def _objective_requests_test_edits(objective: str) -> bool:
    text = str(objective or "").lower()
    for pattern in _NEGATED_TEST_EDIT_PATTERNS:
        text = pattern.sub(" ", text)
    compact = "".join(text.split())
    return any(p.search(compact) or p.search(text) for p in _TEST_EDIT_REQUEST_PATTERNS)


def _plan_requests_test_edits(
    *,
    original_goal: str,
    goal: str,
    plan: dict[str, Any] | None,
    grounding: dict[str, Any] | None,
) -> bool:
    snippets = [original_goal, goal]
    payload = plan if isinstance(plan, dict) else {}
    snippets.append(str(payload.get("objective") or ""))
    criteria = payload.get("acceptance_criteria") if isinstance(payload.get("acceptance_criteria"), list) else []
    snippets.extend(str(item) for item in criteria)
    if isinstance(grounding, dict):
        snippets.extend(
            str(grounding.get(key) or "")
            for key in ("input_goal", "grounded_goal", "acceptance_hint", "evidence")
        )
    return _objective_requests_test_edits("\n".join(item for item in snippets if item))


def _compact_requirement_contract(contract: dict[str, Any]) -> dict[str, Any]:
    answers = contract.get("answers") if isinstance(contract.get("answers"), list) else []
    questions = contract.get("clarifying_questions") if isinstance(contract.get("clarifying_questions"), list) else []
    compact: dict[str, Any] = {
        "source": str(contract.get("source") or ""),
        "input_goal": str(contract.get("input_goal") or "")[:500],
        "final_goal": str(contract.get("final_goal") or "")[:500],
        "acceptance_hint": str(contract.get("acceptance_hint") or "")[:500],
        "answers": [str(item)[:500] for item in answers[:8]],
        "clarifying_questions": [str(item)[:300] for item in questions[:8]],
    }
    if "already_clear" in contract:
        compact["already_clear"] = bool(contract.get("already_clear"))
    if contract.get("model_id"):
        compact["model_id"] = str(contract.get("model_id"))
    if contract.get("intake_policy"):
        compact["intake_policy"] = str(contract.get("intake_policy"))
    if contract.get("model_unavailable") is not None:
        compact["model_unavailable"] = bool(contract.get("model_unavailable"))
    return compact


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
    requested = canonical_agent_name(str(agent or "").strip())
    if not requested:
        return False
    tracks = plan.get("worker_tracks") if isinstance(plan.get("worker_tracks"), list) else []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        if str(track.get("track_kind") or "implementation") == "inspection":
            continue
        before = json.dumps(to_jsonable(track), ensure_ascii=False, sort_keys=True)
        if canonical_agent_name(str(track.get("agent") or "")) == requested:
            _apply_agent_override_to_track(track, requested)
            after = json.dumps(to_jsonable(track), ensure_ascii=False, sort_keys=True)
            return before != after
        _apply_agent_override_to_track(track, requested)
        return True
    if tracks:
        return False
    plan["worker_tracks"] = [
        {
            "agent": requested,
            "id": f"track_1_{requested.replace('-', '_')}",
            "track_kind": "implementation",
            "tier": "strong",
        }
    ]
    _apply_agent_override_to_track(plan["worker_tracks"][0], requested)
    return True


def _apply_agent_override_to_track(track: dict[str, Any], agent: str) -> None:
    profile = load_agent_profile(agent) or {}
    task_kind = tier_task_kind(str(track.get("tier") or "strong"))
    config = recommend_worker_config(profile, task_kind=task_kind) if profile else {}
    track["agent"] = agent
    track["id"] = f"track_1_{agent.replace('-', '_')}"
    # Model and permission flags are agent-specific. When switching a saved
    # mission from Claude/MiMo to Codex, carrying `opus`, `sonnet`, or
    # `--permission-mode` makes the Codex worker fail before it can start.
    track["model"] = str(config.get("model") or "")
    track["sandbox"] = config.get("sandbox") or {}
    track["approval"] = config.get("approval") or {}
    track["reasoning_effort"] = str(config.get("reasoning_effort") or "inherit")
    track["parallelism_hint"] = str(config.get("parallelism_hint") or "")
    track.pop("command", None)
    track.pop("command_template", None)


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


def _managed_retry_failure_kind(dispatch: dict[str, Any]) -> str:
    """Surface the managed retry classifier when dispatch already computed one.

    Dispatch records provider 5xx / rate-limit / network timeouts under
    managed_runtime.retry.failure_kind. Without this bridge, those precise
    kinds collapse to a generic worker_error stop reason and the user is told
    to re-run agents doctor even when the local CLI is healthy.
    """
    runtime = dispatch.get("managed_runtime") if isinstance(dispatch.get("managed_runtime"), dict) else {}
    retry = runtime.get("retry") if isinstance(runtime.get("retry"), dict) else {}
    kind = str(retry.get("failure_kind") or "").strip().lower()
    if kind in {
        "provider_5xx",
        "provider_rate_limit",
        "network_timeout",
        "process_crash",
        "evidence_rejected",
    }:
        return kind
    return ""


def _stop_reason_from_dispatch(dispatch: dict[str, Any], budget: dict[str, Any]) -> str:
    status = str(dispatch.get("status") or "")
    if status in {"blocked", "preflight_blocked"}:
        reason = str(dispatch.get("reason") or "")
        if reason == "pytest_not_importable":
            return "pytest_not_importable"
        return _dispatch_stop_reason(dispatch)
    if dispatch.get("quota_exhausted"):
        # The worker never got to do the task; a gate failure here is noise.
        return "quota_exhausted"
    latest = dispatch.get("latest_verification") if isinstance(dispatch.get("latest_verification"), dict) else {}
    verdict = str(latest.get("verdict") or "")
    if status in {"verified", "merged"} and verdict == "pass":
        return "verified"
    # Defensive: if verification passed with a completed worker, never call it budget_exhausted.
    if verdict == "pass" and status in {"verified", "merged", "no_product_changes"}:
        return "verified" if status != "no_product_changes" else "no_product_changes"
    if status == "verified_blocked":
        return "worker_toolchain_violation"
    if status == "no_product_changes":
        return "no_product_changes"
    if status == "worker_toolchain_violation":
        return "worker_toolchain_violation"
    if status == "worker_failed_tests_pass":
        return "worker_failed_tests_pass"
    if status == "worker_failed":
        classified = _managed_retry_failure_kind(dispatch)
        return classified or "worker_error"
    if status == "merged_verification_failed":
        return "merged_verification_failed"
    if status == "managed_usage_unknown":
        return "usage_unknown"
    if status == "managed_budget_exhausted":
        # Only when work did not already succeed.
        if verdict == "pass":
            worker = dispatch.get("worker_record") if isinstance(dispatch.get("worker_record"), dict) else {}
            if str(worker.get("status") or "") == "completed":
                return "verified"
        return "budget_exhausted"
    if verdict == "coverage_gap":
        return "coverage_gap"
    if verdict == "inspection_only":
        return "inspection_only"
    command_result = latest.get("command_verification") if isinstance(latest.get("command_verification"), dict) else {}
    failure_kind = str(command_result.get("failure_kind") or "")
    if failure_kind in NON_REPAIRABLE_COMMAND_FAILURE_KINDS:
        return failure_kind
    attempts = dispatch.get("verification_attempts") if isinstance(dispatch.get("verification_attempts"), list) else []
    failed = [_failed_signature(item) for item in attempts if _failed_signature(item)]
    if len(failed) >= 2 and failed[-1] == failed[-2]:
        dispatch["same_failure_signature"] = failed[-1]
        return "same_failure_repeated"
    # Attempt count only exhausts budget when the last verification did not pass.
    if verdict != "pass" and len(attempts) >= int(budget.get("max_rounds") or 1):
        return "budget_exhausted"
    if verdict == "fail" or status == "verification_failed":
        return "verification_failed"
    return "worker_error"


def _dispatch_stop_reason(dispatch: dict[str, Any]) -> str:
    status = str(dispatch.get("status") or "")
    reason_raw = str(dispatch.get("reason") or "")
    if status == "preflight_blocked" and reason_raw in {"test_command_unresolved", "verification_environment_missing"}:
        return reason_raw
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


def _allow_prior_verified_evidence_for_resume(
    mission: dict[str, Any] | None,
    *,
    resume_mission_id: str | None,
) -> bool:
    if not resume_mission_id or not isinstance(mission, dict):
        return False
    return str(mission.get("status") or "") == "verified" and str(mission.get("stop_reason") or "") == "verified"


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
        "message": payload.get("message"),
        "preflight": payload.get("preflight"),
        "diagnosis_report": payload.get("diagnosis_report"),
        "review_plan_report": payload.get("review_plan_report"),
        "worker": payload.get("worker"),
        "worktree": payload.get("worktree"),
        "verification": payload.get("verification"),
        "toolchain_policy": payload.get("toolchain_policy"),
        "toolchain_preflight": payload.get("toolchain_preflight"),
        "toolchain_violation": payload.get("toolchain_violation"),
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
    """User-facing stop text: friend/family voice first (pacer_voice)."""
    from .pacer_voice import user_message_for_stop

    legacy = {
        "manual_verification_required": "这件事更适合你现场看一眼再定，我不会假装已经做完。",
        "review_plan_required": "你更像是要一份审查/计划，而不是直接改代码。我们先把范围说清楚。",
        "inspection_only": "这轮只做了检查性观察，还不能当成产品已经验收通过。",
        "test_command_invalid": "测试命令本身没跑起来（写错或找不到）。先在本机把命令跑通再试。",
        "command_launch_error": "测试命令启动失败，多半是环境或路径问题。",
        "command_timeout": "测试跑太久超时了。我们可以加长时间，或先缩小范围。",
        "merged_verification_failed": "隔离区里曾经过了，合并回主项目后又挂了。需要你看一眼主项目。",
        "same_failure_repeated": "修了几轮还是同一个失败（见下方「反复失败的步骤」）。建议把目标说得更窄一点，或者告诉我你希望先跳过哪个验证步骤。",
        "worker_toolchain_violation": "测试过了，但用的工具链和约定不一致，我不敢自动合并。",
        "process_crash": "编程助手进程自己崩了。可以再试，或换一个助手。",
        "usage_unknown": "编程助手没有返回可审计的用量，托管预算无法判断；这不是额度已经耗尽。",
    }
    try:
        text = user_message_for_stop(stop_reason)
        # user_story unknown reasons still return a generic; prefer specific legacy when present.
        if stop_reason in legacy and str(text).startswith("这轮先告一段落"):
            return legacy[stop_reason]
        return text
    except Exception:
        return legacy.get(stop_reason, "这轮先告一段落。有问题我们再用白话对一下。")


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
        write_mission_state(
            workspace_root,
            str(mission["mission_id"]),
            current_state=mission_result_to_pipeline_state(status, stop_reason),
            event="chief_run_finished",
            status=status,
            stop_reason=stop_reason,
            idempotency_key=str(mission.get("idempotency_key") or ""),
            managed_runtime=(
                dispatch_payload.get("managed_runtime")
                if isinstance(dispatch_payload, dict)
                and isinstance(dispatch_payload.get("managed_runtime"), dict)
                else {}
            ),
        )

    # Add failover information to the message if applicable
    if dispatch_payload and status == "verified":
        failover = dispatch_payload.get("failover_worker_record")
        if isinstance(failover, dict) and failover.get("status") == "completed":
            backend = failover.get("backend", {})
            backend_name = backend.get("name", "unknown")
            message += f"（使用 {backend_name} 后端完成，节省了主 agent 配额）"

    payload = {
        "schema_version": 1,
        "product": "Pacer",
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
        progress = build_mission_progress(workspace_root=workspace_root, mission=mission, rounds=rounds)
        progress_fields = {key: value for key, value in progress.items() if key != "mission_id"}
        progress = save_mission_progress(workspace_root, str(mission["mission_id"]), **progress_fields)
        payload["progress"] = progress
        try:
            from .mission_journey import build_mission_journey, save_mission_journey

            journey = build_mission_journey(
                workspace_root=workspace_root,
                mission_id=str(mission["mission_id"]),
                mission=mission,
                plan=plan,
                dispatch=dispatch_payload,
                progress=progress,
            )
            saved_journey = save_mission_journey(workspace_root, str(mission["mission_id"]), journey)
            payload["journey"] = journey
            payload["journey_path"] = saved_journey["path"]
        except Exception as exc:  # noqa: BLE001 - observability must not overturn a mission result.
            payload["journey_error"] = f"{type(exc).__name__}: {exc}"
        report_text = chief_run_to_markdown(payload)
        saved_report = write_final_report(workspace_root, str(mission["mission_id"]), report_text)
        payload["final_report_path"] = saved_report["path"]
        if _should_append_mandatory_record(payload):
            record = _append_mandatory_record(payload, workspace_root=workspace_root)
            if record.get("path"):
                payload["mandatory_record_path"] = record["path"]
            if record.get("error"):
                payload["mandatory_record_error"] = record["error"]
        payload["notification"] = _send_terminal_notification(payload)
    return payload


def _send_terminal_notification(payload: dict[str, Any]) -> dict[str, Any]:
    event = _notification_event_for_terminal_payload(payload)
    if not event:
        return {"status": "skipped", "reason": "non_terminal_event"}
    if load_notification_config() is None:
        return {"status": "skipped", "reason": "notification_config_missing", "event": event}
    mission = payload.get("mission") if isinstance(payload.get("mission"), dict) else {}
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    track = _first_implementation_track(plan)
    selected = track.get("model_selection", {}).get("selected") if isinstance(track.get("model_selection"), dict) else {}
    notification_payload = {
        "project": mission.get("product") or payload.get("product") or "Pacer",
        "mission_id": mission.get("mission_id"),
        "objective": mission.get("objective"),
        "status": payload.get("status"),
        "stop_reason": payload.get("stop_reason"),
        "message": payload.get("message"),
        "report_path": payload.get("final_report_path"),
        "agent": track.get("agent"),
        "model": selected.get("id") if isinstance(selected, dict) else track.get("model"),
    }
    notification = build_event_notification(event, notification_payload)
    try:
        return send_email_notification(notification, dry_run=False)
    except Exception as exc:  # pragma: no cover - network failures depend on local SMTP.
        return {"status": "failed", "event": event, "error": str(exc)}


def _notification_event_for_terminal_payload(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "")
    stop_reason = str(payload.get("stop_reason") or "")
    if status == "verified" or stop_reason == "verified":
        return "mission_verified"
    if stop_reason == "quota_exhausted":
        return "quota_exhausted"
    if stop_reason == "worker_error":
        return "worker_error"
    if stop_reason == "needs_clarification":
        return "needs_user_input"
    if status == "verified_blocked":
        return "mission_stopped"
    if status in {"blocked", "failed", "merged_verification_failed"}:
        return "mission_failed"
    if status in {"stopped", "preview"}:
        return "mission_stopped"
    return ""


def _first_implementation_track(plan: dict[str, Any]) -> dict[str, Any]:
    tracks = plan.get("worker_tracks") if isinstance(plan.get("worker_tracks"), list) else []
    for item in tracks:
        if isinstance(item, dict) and item.get("track_kind") == "implementation":
            return item
    return tracks[0] if tracks and isinstance(tracks[0], dict) else {}


def _should_append_mandatory_record(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "")
    stop_reason = str(payload.get("stop_reason") or "")
    if status in {"preview", "background_started"} or stop_reason == "preview_only":
        return False
    return status in {"verified", "verified_blocked", "stopped", "blocked", "failed"} or bool(stop_reason)


def _append_mandatory_record(payload: dict[str, Any], *, workspace_root: Path) -> dict[str, Any]:
    mission = payload.get("mission") if isinstance(payload.get("mission"), dict) else {}
    if not mission:
        return {}
    mission_id = str(mission.get("mission_id") or "").strip()
    if not mission_id:
        return {}
    try:
        path = Path(workspace_root).expanduser().resolve() / "missions" / mission_id / "强制测试记录.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        dispatch = payload.get("dispatch") if isinstance(payload.get("dispatch"), dict) else {}
        latest = dispatch.get("latest_verification") if isinstance(dispatch.get("latest_verification"), dict) else {}
        command_verification = latest.get("command_verification") if isinstance(latest.get("command_verification"), dict) else {}
        merge = dispatch.get("merge") if isinstance(dispatch.get("merge"), dict) else {}
        usage = dispatch.get("usage_summary") if isinstance(dispatch.get("usage_summary"), dict) else {}
        if not path.exists():
            path.write_text(
                "# 强制测试记录\n\n"
                "本文件由 DevPacer / Checkpoint / Codex 自动追加，保存在 mission 工作区内，避免污染产品仓库。\n",
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


def _mission_budget_elapsed_minutes(mission: dict[str, Any], current_run_started: float) -> float:
    value = str(mission.get("budget_started_at") or "").strip()
    if not value:
        return _elapsed_minutes(current_run_started)
    try:
        started_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return _elapsed_minutes(current_run_started)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - started_at.astimezone(timezone.utc)).total_seconds() / 60.0)


def _used_execution_rounds(workspace_root: Path, mission_id: str) -> int:
    return sum(
        1
        for item in load_rounds(workspace_root, mission_id)
        if str(item.get("type") or "") in {"verification", "dispatch"}
    )


def chief_run_to_markdown(payload: dict[str, Any]) -> str:
    mission = payload.get("mission") if isinstance(payload.get("mission"), dict) else {}
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    rounds = payload.get("rounds") if isinstance(payload.get("rounds"), list) else []
    dispatch_payload = payload.get("dispatch") if isinstance(payload.get("dispatch"), dict) else {}
    latest_verification = (
        dispatch_payload.get("latest_verification")
        if isinstance(dispatch_payload.get("latest_verification"), dict)
        else {}
    )
    command_verification = (
        latest_verification.get("command_verification")
        if isinstance(latest_verification.get("command_verification"), dict)
        else {}
    )
    return _chief_run_three_section_markdown(
        payload=payload,
        mission=mission,
        plan=plan,
        rounds=rounds,
        dispatch_payload=dispatch_payload,
        latest_verification=latest_verification,
        command_verification=command_verification,
    )


def _chief_run_three_section_markdown(
    *,
    payload: dict[str, Any],
    mission: dict[str, Any],
    plan: dict[str, Any],
    rounds: list[dict[str, Any]],
    dispatch_payload: dict[str, Any],
    latest_verification: dict[str, Any],
    command_verification: dict[str, Any],
) -> str:
    status = str(payload.get("status") or "")
    stop_reason = str(payload.get("stop_reason") or "")
    progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
    merge = dispatch_payload.get("merge") if isinstance(dispatch_payload.get("merge"), dict) else {}
    worktree = dispatch_payload.get("worktree") if isinstance(dispatch_payload.get("worktree"), dict) else {}
    changed_product = progress.get("changed_product_files") if isinstance(progress.get("changed_product_files"), list) else []
    product_count = int(progress.get("changed_product_file_count") or len(changed_product))
    verified = status in {"verified", "merged"} and stop_reason == "verified"
    merged = str(merge.get("status") or "") in {"merged", "nothing_to_merge"} or str(dispatch_payload.get("status") or "") == "merged"

    from .pacer_voice import user_markdown_section, user_story

    goal = str(mission.get("objective") or plan.get("objective") or "")
    cmd = str(command_verification.get("command") or mission.get("test_command") or "")
    wt = str(worktree.get("path") or "")
    story = user_story(
        stop_reason=stop_reason,
        status=status,
        goal=goal,
        product_change_count=product_count,
        verification_command=cmd,
        worktree=wt,
        message_fallback=str(payload.get("message") or ""),
        acceptance_tier=str(
            (latest_verification.get("acceptance") or {}).get("tier") or ""
            if isinstance(latest_verification.get("acceptance"), dict)
            else ""
        ),
    )
    lines = list(user_markdown_section(story))
    lines.extend(
        [
            "",
            "## 结论（简表）",
            "",
            f"- 一句话：{story.get('headline') or payload.get('message') or _message_for_stop(stop_reason)}",
            f"- status / stop_reason：`{status}` / `{stop_reason}`",
            f"- Verified: `{str(verified).lower()}`",
            f"- Merged: `{str(merged).lower()}`",
            f"- 产品改动文件数：`{product_count}`",
        ]
    )
    if stop_reason == "same_failure_repeated":
        sig = str(dispatch_payload.get("same_failure_signature") or "")
        if sig:
            lines.extend(["", "### 反复失败的步骤", "", f"```\n{sig}\n```"])

    journey = payload.get("journey") if isinstance(payload.get("journey"), dict) else {}
    if journey:
        from .mission_journey import mission_journey_to_markdown

        lines.extend(["", "## 闭环", "", mission_journey_to_markdown(journey)])

    lines.extend(["", "## 证据", ""])
    _append_plan_evidence(lines, plan=plan, command_verification=command_verification)
    _append_verification_evidence(lines, latest_verification=latest_verification, command_verification=command_verification)
    _append_product_change_evidence(lines, changed_product=changed_product, product_count=product_count)
    _append_worktree_evidence(lines, mission=mission, worktree=worktree, merge=merge)
    _append_preflight_evidence(lines, dispatch_payload=dispatch_payload)
    _append_policy_evidence(lines, payload=payload, dispatch_payload=dispatch_payload)
    _append_context_evidence(lines, mission=mission, plan=plan, dispatch_payload=dispatch_payload, rounds=rounds)

    lines.extend(["", "## 下一步", ""])
    next_commands = _next_step_commands(
        payload=payload,
        mission=mission,
        dispatch_payload=dispatch_payload,
        command_verification=command_verification,
    )
    for label, command in next_commands:
        lines.append(f"- {label}: `{command}`")
    return "\n".join(lines).rstrip()


def _append_plan_evidence(lines: list[str], *, plan: dict[str, Any], command_verification: dict[str, Any]) -> None:
    if not plan:
        return
    if command_verification:
        lines.append("- Status: `command_gate`")
        lines.append("- Verification mode: `command`")
        lines.append("- Workflow coverage: workflow coverage 由显式测试命令接管")
        lines.append("- Selected workflows: not used (explicit test command)")
        return
    workflows = ", ".join(plan.get("selected_workflows") or []) or "none"
    lines.append(f"- Plan status: `{plan.get('status')}`")
    lines.append(f"- Selected workflows: {workflows}")


def _append_verification_evidence(
    lines: list[str],
    *,
    latest_verification: dict[str, Any],
    command_verification: dict[str, Any],
) -> None:
    if not command_verification:
        verdict = str(latest_verification.get("verdict") or "")
        if verdict:
            lines.append(f"- Verification verdict: `{verdict}`")
        return
    lines.append("- Mode: `command`")
    lines.append(f"- Command: `{command_verification.get('command') or ''}`")
    lines.append(f"- Verdict: `{latest_verification.get('verdict') or command_verification.get('verdict') or ''}`")
    if command_verification.get("exit_code") is not None:
        lines.append(f"- Exit code: `{command_verification.get('exit_code')}`")
    if command_verification.get("elapsed_seconds") is not None:
        lines.append(f"- Elapsed seconds: `{command_verification.get('elapsed_seconds')}`")
    if command_verification.get("failure_kind"):
        lines.append(f"- Failure kind: `{command_verification.get('failure_kind')}`")
    confidence = str(command_verification.get("classification_confidence") or "")
    if confidence:
        lines.append(f"- Classification confidence: `{confidence}`")
        if confidence == "heuristic":
            lines.append("- Classification note: heuristic判定，建议人工确认。")


def _append_product_change_evidence(lines: list[str], *, changed_product: list[Any], product_count: int) -> None:
    lines.append(f"- Changed product file count: `{product_count}`")
    if changed_product:
        lines.append("- Changed product files: " + ", ".join(str(item) for item in changed_product[:12]))
    else:
        lines.append("- Changed product files: none recorded")


def _append_worktree_evidence(
    lines: list[str],
    *,
    mission: dict[str, Any],
    worktree: dict[str, Any],
    merge: dict[str, Any],
) -> None:
    repo_root = str(mission.get("repo_root") or "")
    if repo_root:
        lines.append(f"- Repo root: `{repo_root}`")
    if worktree.get("path"):
        lines.append(f"- Worktree path: `{worktree.get('path')}`")
    if worktree.get("branch"):
        lines.append(f"- Worktree branch: `{worktree.get('branch')}`")
    if merge:
        lines.append(f"- Merge status: `{merge.get('status') or ''}`")
        if merge.get("target"):
            lines.append(f"- Merge target: `{merge.get('target')}`")
        if merge.get("commit"):
            lines.append(f"- Merge commit: `{merge.get('commit')}`")


def _append_preflight_evidence(lines: list[str], *, dispatch_payload: dict[str, Any]) -> None:
    preflight_text = preflight_to_markdown(dispatch_payload.get("preflight"), heading="Preflight")
    if preflight_text:
        lines.extend(["", preflight_text])


def _append_policy_evidence(lines: list[str], *, payload: dict[str, Any], dispatch_payload: dict[str, Any]) -> None:
    toolchain_violation = (
        dispatch_payload.get("toolchain_violation")
        if isinstance(dispatch_payload.get("toolchain_violation"), dict)
        else {}
    )
    if toolchain_violation:
        lines.append("- Gate Decision: worker used a forbidden sibling SDK wrapper/path.")
        lines.append("- Verified: `false`")
        if toolchain_violation.get("expected_executable"):
            lines.append(f"- Expected executable: `{toolchain_violation.get('expected_executable')}`")
        if toolchain_violation.get("forbidden_path"):
            lines.append(f"- Forbidden path: `{toolchain_violation.get('forbidden_path')}`")
        if toolchain_violation.get("log_path"):
            lines.append(f"- Worker log: `{toolchain_violation.get('log_path')}`")
    merge = dispatch_payload.get("merge") if isinstance(dispatch_payload.get("merge"), dict) else {}
    if (
        str(payload.get("stop_reason") or "") == "merged_verification_failed"
        or str(dispatch_payload.get("status") or "") == "merged_verification_failed"
    ) and merge.get("commit"):
        lines.append(f"- Revert command: `git revert -m 1 {merge.get('commit')}`")


def _append_context_evidence(
    lines: list[str],
    *,
    mission: dict[str, Any],
    plan: dict[str, Any],
    dispatch_payload: dict[str, Any],
    rounds: list[dict[str, Any]],
) -> None:
    diagnosis_report = str(dispatch_payload.get("diagnosis_report") or "").strip()
    if diagnosis_report:
        _append_text_block(lines, "诊断报告", diagnosis_report)
    review_plan_report = str(dispatch_payload.get("review_plan_report") or "").strip()
    if review_plan_report:
        _append_text_block(lines, "审查与开发计划", review_plan_report)
    contract = mission.get("requirement_contract") if isinstance(mission.get("requirement_contract"), dict) else {}
    if contract:
        lines.append("- Requirement Contract:")
        audit_parts = []
        if contract.get("source"):
            audit_parts.append(f"source={contract.get('source')}")
        if contract.get("intake_policy"):
            audit_parts.append(f"policy={contract.get('intake_policy')}")
        if contract.get("model_id"):
            audit_parts.append(f"model={contract.get('model_id')}")
        if audit_parts:
            lines.append(f"  - 收口来源：`{', '.join(audit_parts)}`")
        if contract.get("model_unavailable"):
            lines.append("  - 收口降级：选中的 Agent 不可用，未静默切换到其他模型。")
        if contract.get("input_goal") and contract.get("input_goal") != contract.get("final_goal"):
            lines.append(f"  - 原始目标：{contract.get('input_goal')}")
        if contract.get("final_goal"):
            lines.append(f"  - 收口目标：{contract.get('final_goal')}")
        if contract.get("acceptance_hint"):
            lines.append(f"  - 验收提示：{contract.get('acceptance_hint')}")
        answers = contract.get("answers") if isinstance(contract.get("answers"), list) else []
        for item in answers[:8]:
            lines.append(f"  - 用户补充：{item}")
    grounding = plan.get("grounding") if isinstance(plan.get("grounding"), dict) else None
    if grounding:
        _append_text_block(lines, "计划审查", grounding_to_markdown(grounding))
    if rounds:
        summary = []
        for item in rounds[-8:]:
            summary.append(f"{item.get('round')}: {item.get('type')} -> {item.get('status')}")
        lines.append("- Rounds: " + "; ".join(summary))


def _append_text_block(lines: list[str], label: str, text: str) -> None:
    if not text:
        return
    lines.extend([f"- {label}:", "```markdown", text.strip(), "```"])


def _next_step_commands(
    *,
    payload: dict[str, Any],
    mission: dict[str, Any],
    dispatch_payload: dict[str, Any],
    command_verification: dict[str, Any],
) -> list[tuple[str, str]]:
    mission_id = str((mission or {}).get("mission_id") or "")
    repo_root = str((mission or {}).get("repo_root") or "")
    stop_reason = str(payload.get("stop_reason") or "")
    merge = dispatch_payload.get("merge") if isinstance(dispatch_payload.get("merge"), dict) else {}
    worktree = dispatch_payload.get("worktree") if isinstance(dispatch_payload.get("worktree"), dict) else {}
    commands: list[tuple[str, str]] = []
    if stop_reason == "merged_verification_failed" and merge.get("commit"):
        commands.append(("回滚合并", f"git -C {_ps_quote(repo_root or '.')} revert -m 1 {merge.get('commit')}"))
    elif stop_reason == "verification_environment_missing":
        for name in _missing_env_names(dispatch_payload, command_verification) or ["QWEN_API_KEY"]:
            commands.append((f"设置环境变量 {name}", f"$env:{name} = \"<value>\""))
        if mission_id:
            commands.append(("重试任务", f"checkpoint mission resume --mission {_ps_quote(mission_id)} --execute"))
    elif stop_reason == "test_command_unresolved":
        if mission_id:
            commands.append(("带明确验收命令重试", f"checkpoint mission resume --mission {_ps_quote(mission_id)} --execute --test-command \"<your test command>\""))
    elif stop_reason == "command_timeout":
        suggested = command_verification.get("suggested_timeout_seconds") or 1800
        if mission_id:
            commands.append(("增加超时后重试", f"checkpoint mission resume --mission {_ps_quote(mission_id)} --execute --timeout-seconds {suggested:g}"))
    elif stop_reason == "same_failure_repeated":
        if mission_id:
            commands.append(("查看任务状态", f"checkpoint mission status --mission {_ps_quote(mission_id)}"))
            commands.append(("拆小目标后重试", f"checkpoint mission resume --mission {_ps_quote(mission_id)} --execute --max-repair-rounds 0"))
    elif stop_reason == "quota_exhausted":
        commands.append(("下次启动自动等额度恢复", "pacer host run --wake-on-quota --goal \"<goal>\" --execute"))
        if mission_id:
            commands.append(("额度恢复后 resume", f"checkpoint mission resume --mission {_ps_quote(mission_id)} --execute"))
    elif stop_reason == "budget_exhausted":
        if mission_id:
            commands.append(("加大轮数后重试", f"checkpoint mission resume --mission {_ps_quote(mission_id)} --execute --max-rounds 5"))
            commands.append(("拆小目标后重试", f"checkpoint mission resume --mission {_ps_quote(mission_id)} --execute --max-repair-rounds 0"))
    elif stop_reason == "verified":
        branch = str(merge.get("branch") or worktree.get("branch") or "")
        if str(merge.get("status") or "") in {"merged", "nothing_to_merge"}:
            commands.append(("查看合并结果", f"git -C {_ps_quote(repo_root or '.')} log -1 --oneline"))
        elif branch:
            commands.append(("合并 worktree 分支", f"git -C {_ps_quote(repo_root or '.')} merge --no-ff {branch}"))
        command = str(command_verification.get("command") or "").strip()
        if command:
            commands.append(("复跑验收命令", f"Set-Location {_ps_quote(repo_root or '.')}; {command}"))
    else:
        if mission_id:
            commands.append(("查看任务状态", f"checkpoint mission status --mission {_ps_quote(mission_id)}"))
            commands.append(("按原任务重试", f"checkpoint mission resume --mission {_ps_quote(mission_id)} --execute"))
    if not commands:
        commands.append(("列出任务", "checkpoint mission list"))
    return commands


def _missing_env_names(dispatch_payload: dict[str, Any], command_verification: dict[str, Any]) -> list[str]:
    preflight = dispatch_payload.get("preflight") if isinstance(dispatch_payload.get("preflight"), dict) else {}
    verification_env = preflight.get("verification_env") if isinstance(preflight.get("verification_env"), dict) else {}
    missing = verification_env.get("missing_env_vars") if isinstance(verification_env.get("missing_env_vars"), list) else []
    if missing:
        return [str(item) for item in missing if str(item)]
    values = command_verification.get("missing_env_vars") if isinstance(command_verification.get("missing_env_vars"), list) else []
    return [str(item) for item in values if str(item)]


def _ps_quote(value: str) -> str:
    text = str(value or "")
    return "'" + text.replace("'", "''") + "'"


def mission_status_payload(*, workspace_root: str | Path, mission_id: str) -> dict[str, Any]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    mission = load_mission(workspace_path, mission_id)
    if mission is None:
        return {
            "schema_version": 1,
            "product": "Pacer",
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
        "product": mission.get("product", "Pacer"),
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
    payload["progress"] = build_mission_progress(
        workspace_root=workspace_path,
        mission=payload["mission"] if isinstance(payload.get("mission"), dict) else mission,
        rounds=payload["rounds"] if isinstance(payload.get("rounds"), list) else rounds,
        background=payload.get("background") if isinstance(payload.get("background"), dict) else None,
    )
    progress_message = _progress_next_action(payload["progress"])
    if progress_message:
        payload["message"] = progress_message
    try:
        from .mission_journey import build_mission_journey

        payload["journey"] = build_mission_journey(
            workspace_root=workspace_path,
            mission_id=mission_id,
            mission=payload["mission"] if isinstance(payload.get("mission"), dict) else mission,
            progress=payload["progress"],
        )
    except Exception as exc:  # noqa: BLE001 - status still returns durable mission facts.
        payload["journey_error"] = f"{type(exc).__name__}: {exc}"
    return payload


def _status_next_action(mission: dict[str, Any]) -> str:
    status = str(mission.get("status") or "")
    stop_reason = str(mission.get("stop_reason") or "")
    if status == "background_running":
        return "Background run is active. Check the progress field for stage, activity, product changes, and verification state."
    if status == "preview" and stop_reason == "preview_only":
        return "Run `checkpoint mission resume --mission <mission_id> --execute` to execute this previewed mission."
    if status == "verified":
        return "Mission verified. Run `checkpoint mission list` to review, or `git log` to see the merged commit."
    if status == "verified_blocked":
        return "Verification passed, but a policy gate blocked verified/merge. Open final_report.md and retry with the required toolchain."
    if stop_reason == "coverage_gap":
        return "Add --test-command \"<your test command>\" to your next mission start, or add --allow-coverage-gap."
    if stop_reason == "manual_verification_required":
        return "Confirm the manual/field acceptance plan in the workbench chat, then continue from that plan."
    if stop_reason == "review_plan_required":
        return "Confirm review scope and plan format in the workbench chat, then generate the development plan."
    if stop_reason == "needs_clarification":
        return "Refine the goal with a concrete acceptance criterion and start a new mission."
    if stop_reason == "permission_required":
        return "Run `git status` to see dirty files. Commit or stash them, then retry."
    if stop_reason in ("same_failure_repeated", "verification_failed"):
        return "Open final_report.md to read the failing test output, then start a new mission with a more specific goal."
    if stop_reason == "merged_verification_failed":
        return "Open final_report.md for the post-merge failure, then fix or revert the target branch before continuing."
    if stop_reason == "quota_exhausted":
        return "Wait for quota reset, or explicitly retry with another agent such as --agent claude-code or --agent mimo."
    if stop_reason == "worker_toolchain_violation":
        return "Open final_report.md for the forbidden tool path, then retry with the exact SDK executable named by the verification command."
    if stop_reason == "worker_error":
        return "Run `checkpoint agents doctor` to confirm the agent is installed, then resume the mission."
    if stop_reason == "verification_environment_missing":
        return "Configure the required verification environment or API key, then resume the mission without changing tests or eval scripts."
    if stop_reason == "test_command_unresolved":
        return "Pass an explicit --test-command, or add a detectable project test configuration, then start the mission again."
    if stop_reason in ("budget_exhausted", "command_timeout"):
        return "Resume with a larger --max-rounds or narrower --goal to reduce scope."
    return "Run `checkpoint mission list` and open final_report.md for the next action."


def _progress_next_action(progress: dict[str, Any]) -> str:
    stage = str(progress.get("stage") or "")
    if stage == "worker_activity_stale":
        return (
            "Mission is marked running, but no live background worker or worker record was found. "
            "Resume the mission or start a new one; current worktree changes are listed in progress.changed_files."
        )
    if stage in {"worker_running", "worker_started", "background_started"}:
        changed = int(progress.get("changed_product_file_count") or 0)
        return f"Worker is active; product files changed so far: {changed}. Check progress.latest_log_path for live output."
    if stage == "verification_running":
        return "Worker finished or is finishing; Pacer is running the acceptance gate."
    if stage == "verification_pending":
        return "Worker completed; run or wait for verification before trusting the result."
    return ""


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2)
