"""Bounded autonomous development missions.

``chief-run`` is the first Pacer loop: build or load a plan, persist mission
state, preview the dispatch, optionally execute Codex, verify with Checkpoint,
and stop with a durable reason.
"""

from __future__ import annotations

import json
import re
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
    require_env: tuple[str, ...] = (),
    verification_env: list[dict[str, Any]] | None = None,
    dispatch_runner: DispatchRunner | None = None,
    ground_vague_goals: bool = True,
    grounding_runner: Callable[..., dict[str, Any]] | None = None,
    requirement_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = monotonic()
    original_goal = str(goal or "")
    workspace_path = Path(workspace_root).expanduser().resolve()
    repo_path = Path(repo_root).expanduser().resolve()
    agent_for_contract = str(agents[0]) if agents else ""
    contract_payload = normalize_requirement_contract(
        requirement_contract,
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
    raw_test_command = str(test_command or "").strip()
    resolved_test_command, verification_profile = resolve_test_command(test_command, repo_root=repo_path)
    test_command_unresolved = bool(raw_test_command) and not str(resolved_test_command or "").strip()
    test_command = raw_test_command if test_command_unresolved else resolved_test_command
    if test_command and not workspace_path.exists():
        init_workspace(workspace_path, with_demo=False)
    budget = default_budget_policy(
        max_rounds=max_rounds,
        max_wall_minutes=max_wall_minutes,
        max_worker_minutes=max_worker_minutes,
    )
    requested_verification_env = _merge_verification_env(
        normalize_verification_env(verification_env),
        verification_env_from_required_names(require_env),
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
        if not contract_payload and isinstance(mission.get("requirement_contract"), dict):
            contract_payload = normalize_requirement_contract(
                mission.get("requirement_contract"),
                goal=str(goal or ""),
                answers=answers,
                repo_root=repo_path,
                test_command=test_command,
                agent=agent_for_contract,
            )
            planning_answers = _planning_answers(answers, contract_payload)
        if isinstance(mission.get("budget_policy"), dict):
            budget = dict(mission["budget_policy"])
        requested_verification_env = _merge_verification_env(
            normalize_verification_env(mission.get("verification_env")),
            requested_verification_env,
        )

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
    if agents:
        changed_agent = _override_plan_agent(plan_payload, str(agents[0]))
        if changed_agent and plan_id:
            save_plan(plan_payload, workspace_root=workspace_path, plan_id=str(plan_id))
    if verification_profile:
        plan_payload["verification_profile"] = verification_profile
        if plan_id:
            save_plan(plan_payload, workspace_root=workspace_path, plan_id=str(plan_id))
    effective_allow_test_edits = allow_test_edits or _plan_requests_test_edits(
        original_goal=original_goal,
        goal=str(goal or ""),
        plan=plan_payload,
        grounding=grounding_payload,
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
            requirement_contract=contract_payload,
            verification_env=requested_verification_env,
        )
        write_mission_state(
            workspace_path,
            str(mission["mission_id"]),
            current_state="DRAFT",
            event="chief_run_mission_created",
            goal=str(goal or plan_payload.get("objective") or ""),
            plan_id=str(plan_id),
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
        )
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
        allow_dirty=allow_dirty,
        allow_coverage_gap=allow_coverage_gap,
        auto_repair_once=False,
        model_policy=budget.get("model_policy") if isinstance(budget, dict) else None,
        test_command=test_command,
        allow_test_edits=effective_allow_test_edits,
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
    save_mission_progress(
        workspace_path,
        str(mission["mission_id"]),
        stage="worker_starting",
        stage_label="Worker starting",
        status="running",
        plan_id=str(plan_id or ""),
    )
    dispatch = runner(
        workspace_root=workspace_path,
        plan_id=str(plan_id),
        mission_id=str(mission["mission_id"]),
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
        verification_env=requested_verification_env,
        allow_prior_verified_evidence=_allow_prior_verified_evidence_for_resume(
            mission,
            resume_mission_id=resume_mission_id,
        ),
    )
    _record_dispatch_attempts(workspace_path, mission["mission_id"], dispatch, start_round=_next_round_number(workspace_path, mission["mission_id"]))
    stop_reason = _stop_reason_from_dispatch(dispatch, budget)
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
    re.compile(r"(写|加|补|改|更新|添加|新增|创建|编写|补齐)[^。！？!?；;\n]{0,24}(验收|评测|回归|eval)"),
    re.compile(r"测试用例"),
    re.compile(r"(验收|评测|回归)(样本|用例)"),
    re.compile(r"\b(add|write|update|create)\b[^.!?\n]{0,40}\btests?\b"),
    re.compile(r"\b(add|write|update|create)\b[^.!?\n]{0,40}\b(eval|acceptance|regression)\b"),
    re.compile(r"\btest cases?\b"),
)


def _objective_requests_test_edits(objective: str) -> bool:
    text = str(objective or "").lower()
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
    config = recommend_worker_config(profile, task_kind="implementation") if profile else {}
    track["agent"] = agent
    track["id"] = f"track_1_{agent.replace('-', '_')}"
    # Model and permission flags are agent-specific. When switching a saved
    # mission from Claude/MiMo to Codex, carrying `opus`, `sonnet`, or
    # `--permission-mode` makes the Codex worker fail before it can start.
    track["model"] = str(config.get("model") or "")
    track["sandbox"] = config.get("sandbox") or {}
    track["approval"] = config.get("approval") or {}
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


def _stop_reason_from_dispatch(dispatch: dict[str, Any], budget: dict[str, Any]) -> str:
    status = str(dispatch.get("status") or "")
    if status in {"blocked", "preflight_blocked"}:
        return _dispatch_stop_reason(dispatch)
    if dispatch.get("quota_exhausted"):
        # The worker never got to do the task; a gate failure here is noise.
        return "quota_exhausted"
    latest = dispatch.get("latest_verification") if isinstance(dispatch.get("latest_verification"), dict) else {}
    verdict = str(latest.get("verdict") or "")
    if status in {"verified", "merged"} and verdict == "pass":
        return "verified"
    if status == "verified_blocked":
        return "worker_toolchain_violation"
    if status == "no_product_changes":
        return "no_product_changes"
    if status == "worker_toolchain_violation":
        return "worker_toolchain_violation"
    if status == "worker_failed_tests_pass":
        return "worker_failed_tests_pass"
    if status == "worker_failed":
        return "worker_error"
    if status == "merged_verification_failed":
        return "merged_verification_failed"
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
        return "same_failure_repeated"
    if len(attempts) >= int(budget.get("max_rounds") or 1):
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
    return {
        "verified": "任务已通过验收。",
        "coverage_gap": (
            "任务停止：未找到测试命令或工作流覆盖。"
            "修复：添加 --test-command \"pytest -q\"（或你项目的测试命令），"
            "或添加 --allow-coverage-gap 跳过此检查。"
        ),
        "manual_verification_required": "任务需要人工/现场验收方案；请在工作台主对话框确认设备、场景、指标和结论模板。",
        "review_plan_required": "任务需要审查/开发计划输出；请在工作台主对话框确认审查范围和交付格式。",
        "inspection_only": "任务仅产生了检查性证据；如需真实交互，请重新运行。",
        "test_command_invalid": (
            "任务停止：验证命令在运行测试前就出错了。"
            "修复：手动运行测试命令确认可用，然后重试。"
        ),
        "command_launch_error": (
            "任务停止：无法启动验证命令。"
            "修复：检查命令是否在 PATH 中，以及测试依赖是否已安装。"
        ),
        "command_timeout": (
            "任务停止：验证命令超时。"
            "修复：增加 --timeout-seconds 或加快测试速度。"
        ),
        "verification_environment_missing": (
            "任务停止：验证环境缺少必需的外部服务或密钥。"
            "修复：配置验收所需环境变量/API key 后重试；不要让 worker 修改产品代码、测试或 eval 脚本来绕过。"
        ),
        "test_command_unresolved": (
            "任务停止：无法解析验收命令。"
            "修复：传入明确的 --test-command，或添加 package.json/pytest/go/cargo 等可检测的测试配置后重试。"
        ),
        "merged_verification_failed": (
            "任务停止：worktree 验收通过并已合并，但目标分支合并后复验失败。"
            "修复：打开 final_report.md 查看 post-merge 输出，先修复或回滚目标分支，再继续托管。"
        ),
        "same_failure_repeated": (
            "任务停止：自动修复后同样的测试失败仍然出现。"
            "修复：打开 final_report.md 查看失败输出，然后用更具体的目标重新开始。"
        ),
        "budget_exhausted": (
            "任务停止：已用完配置的轮次预算。"
            "修复：用 --max-rounds 增加轮次后恢复任务，或用更窄的目标重新开始。"
        ),
        "worker_error": (
            "任务停止：AI worker 进程失败或被终止。"
            "修复：运行 `checkpoint agents doctor` 确认可用，然后恢复。"
            "如果你明确接受其他后端，可以手动重试：--agent claude-code 或 --agent mimo。"
        ),
        "worker_failed_tests_pass": (
            "任务停止：worker 未正常完成，但现有改动通过了测试命令。"
            "请人工检查 worktree 后再用 chief-merge 手动合并。"
        ),
        "worker_toolchain_violation": (
            "任务验收命令通过，但 AI worker 使用了任务禁止的工具链路径。"
            "当前状态为 verified_blocked，不能合并。"
            "修复：打开 final_report.md 查看 forbidden_path，要求 worker 只使用验收命令中的精确 SDK 可执行文件后重试。"
        ),
        "no_product_changes": (
            "任务停止：验证命令通过，但 worker 没有产生真实产品代码改动。"
            "修复：缩小目标或更换 agent 后重试；不要把缓存、报告或 .gitignore 当作产品改动。"
        ),
        "verification_failed": (
            "任务停止：所有修复轮次后验证仍然失败。"
            "修复：打开 final_report.md 查看 worker 的尝试，然后细化目标。"
        ),
        "permission_required": (
            "任务停止：仓库有未提交的更改（工作树不干净）。"
            "修复：先提交或暂存更改，然后重试。"
            "或添加 --allow-dirty 跳过此安全检查。"
        ),
        "quota_exhausted": (
            "任务停止：当前指定的 AI agent 达到了配额限制。"
            "修复：等待配额重置；如果你明确接受其他后端，"
            "可以手动重试：--agent claude-code 或 --agent mimo。"
        ),
        "needs_clarification": (
            "任务停止：目标太模糊，无法安全规划。"
            "修复：细化 --goal，包含具体的验收标准，例如 '添加 X 使测试 Y 通过'。"
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
        write_mission_state(
            workspace_root,
            str(mission["mission_id"]),
            current_state=mission_result_to_pipeline_state(status, stop_reason),
            event="chief_run_finished",
            status=status,
            stop_reason=stop_reason,
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
    lines = ["## Pacer Mission", ""]
    lines.append(f"Status: `{payload.get('status')}`")
    lines.append(f"Stop reason: `{payload.get('stop_reason')}`")
    if payload.get("message"):
        lines.append(f"Message: {payload['message']}")
    progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
    if progress:
        lines.extend(["", "### Progress", ""])
        lines.append(f"- Stage: `{progress.get('stage') or ''}`")
        if progress.get("stage_label"):
            lines.append(f"- Stage label: {progress.get('stage_label')}")
        if progress.get("last_activity_at"):
            lines.append(f"- Last activity: `{progress.get('last_activity_at')}`")
        lines.append(f"- Product files changed: `{progress.get('changed_product_file_count', 0)}`")
        changed_product = progress.get("changed_product_files") if isinstance(progress.get("changed_product_files"), list) else []
        if changed_product:
            lines.append("- Changed product files: " + ", ".join(f"`{item}`" for item in changed_product[:8]))
        if progress.get("verification_verdict"):
            lines.append(f"- Verification verdict: `{progress.get('verification_verdict')}`")
        if progress.get("blocker"):
            lines.append(f"- Blocker: `{progress.get('blocker')}`")
    preflight_markdown = preflight_to_markdown(dispatch_payload.get("preflight"), heading="## Preflight")
    if preflight_markdown:
        lines.extend(["", preflight_markdown])
    diagnosis_report = str(dispatch_payload.get("diagnosis_report") or "")
    if diagnosis_report:
        lines.extend(["", "### 诊断报告", "", diagnosis_report])
    review_plan_report = str(dispatch_payload.get("review_plan_report") or "")
    if review_plan_report:
        lines.extend(["", "### 审查与开发计划", "", review_plan_report])
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
        contract = mission.get("requirement_contract") if isinstance(mission.get("requirement_contract"), dict) else {}
        if contract:
            lines.extend(["", "### Requirement Contract", ""])
            audit_parts = []
            if contract.get("source"):
                audit_parts.append(f"source={contract.get('source')}")
            if contract.get("intake_policy"):
                audit_parts.append(f"policy={contract.get('intake_policy')}")
            if contract.get("model_id"):
                audit_parts.append(f"model={contract.get('model_id')}")
            if audit_parts:
                lines.append(f"- 收口来源：`{', '.join(audit_parts)}`")
            if contract.get("model_unavailable"):
                lines.append("- 收口降级：选中的 Agent 不可用，未静默切换到其他模型。")
            if contract.get("input_goal") and contract.get("input_goal") != contract.get("final_goal"):
                lines.append(f"- 原始目标：{contract.get('input_goal')}")
            if contract.get("final_goal"):
                lines.append(f"- 收口目标：{contract.get('final_goal')}")
            if contract.get("acceptance_hint"):
                lines.append(f"- 验收提示：{contract.get('acceptance_hint')}")
            answers = contract.get("answers") if isinstance(contract.get("answers"), list) else []
            for item in answers[:8]:
                lines.append(f"- 用户补充：{item}")
    if plan:
        lines.extend(["", "### Plan", ""])
        if command_verification:
            original_status = str(plan.get("status") or "")
            lines.append("- Status: `command_gate`")
            if original_status and original_status != "ready":
                lines.append(f"- Workflow coverage status: `{original_status}` (superseded by explicit test command)")
            lines.append("- Verification mode: `command`")
            lines.append("- Selected workflows: not used (explicit test command)")
        else:
            lines.append(f"- Status: `{plan.get('status')}`")
            workflows = ", ".join(plan.get("selected_workflows") or []) or "none"
            lines.append(f"- Selected workflows: {workflows}")
    if command_verification:
        lines.extend(["", "### Verification", ""])
        lines.append("- Mode: `command`")
        lines.append(f"- Command: `{command_verification.get('command') or ''}`")
        lines.append(f"- Verdict: `{latest_verification.get('verdict') or command_verification.get('verdict') or ''}`")
        if command_verification.get("exit_code") is not None:
            lines.append(f"- Exit code: `{command_verification.get('exit_code')}`")
        if command_verification.get("failure_kind"):
            lines.append(f"- Failure kind: `{command_verification.get('failure_kind')}`")
        confidence = str(command_verification.get("classification_confidence") or "")
        if confidence:
            lines.append(f"- Classification confidence: `{confidence}`")
            if confidence == "heuristic":
                lines.append("- Classification note: heuristic判定，建议人工确认。")
    merge = dispatch_payload.get("merge") if isinstance(dispatch_payload.get("merge"), dict) else {}
    if merge:
        lines.extend(["", "### Merge", ""])
        lines.append(f"- Status: `{merge.get('status') or ''}`")
        if merge.get("target"):
            lines.append(f"- Target: `{merge.get('target')}`")
        if merge.get("branch"):
            lines.append(f"- Branch: `{merge.get('branch')}`")
        if merge.get("commit"):
            lines.append(f"- Commit: `{merge.get('commit')}`")
        if merge.get("reason"):
            lines.append(f"- Reason: {merge.get('reason')}")
        if (
            str(payload.get("stop_reason") or "") == "merged_verification_failed"
            or str(dispatch_payload.get("status") or "") == "merged_verification_failed"
        ) and merge.get("commit"):
            lines.append(f"- Revert hint: `git revert -m 1 {merge.get('commit')}`")
    toolchain_violation = (
        dispatch_payload.get("toolchain_violation")
        if isinstance(dispatch_payload.get("toolchain_violation"), dict)
        else {}
    )
    if toolchain_violation:
        lines.extend(["", "### Gate Decision", ""])
        lines.append("- Verified: `false`")
        lines.append("- Reason: worker used a forbidden sibling SDK wrapper/path.")
        if toolchain_violation.get("expected_executable"):
            lines.append(f"- Expected executable: `{toolchain_violation.get('expected_executable')}`")
        if toolchain_violation.get("forbidden_path"):
            lines.append(f"- Forbidden path: `{toolchain_violation.get('forbidden_path')}`")
        if toolchain_violation.get("log_path"):
            lines.append(f"- Worker log: `{toolchain_violation.get('log_path')}`")
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
    return payload


def _status_next_action(mission: dict[str, Any]) -> str:
    status = str(mission.get("status") or "")
    stop_reason = str(mission.get("stop_reason") or "")
    if status == "background_running":
        return "Background run is active. Check the progress field for stage, activity, product changes, and verification state."
    if status == "preview" and stop_reason == "preview_only":
        return "Run `mission start --resume <mission_id> --execute` to execute this previewed mission."
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
