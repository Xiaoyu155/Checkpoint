from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from .agent_backends import looks_like_quota_exhaustion, redact_backend, resolve_backend_for_tier, resolve_failover_backend
from .agent_capabilities import canonical_agent_name, load_agent_profile, recommend_worker_config
from .command_verification import changed_test_files, command_repair_brief, run_command_verification, tamper_repair_brief
from .chief_plans_store import append_worker_record, load_plan, plan_dir, save_verification
from .codex_check import codex_check_to_markdown, run_codex_check
from .diff_summary import build_diff_summary
from .git_diff import changed_files
from .models import to_jsonable
from .repo_map import build_repo_map, render_repo_map, repo_map_cache_path
from .workspace import open_workspace


# Checkpoint writes its own run artifacts (workspace outputs, status file, browser
# cache) into the repo it verifies. Those are tool outputs, not code under test,
# and must never be counted as changed files — otherwise a second verification
# round sees them as uncovered changes and reports a phantom coverage gap.
_ARTIFACT_BASENAMES = {".visual-agent-status.md", "强制测试记录.md"}
_ARTIFACT_DIR_PREFIXES = (".pw-browsers/",)

# Coding agents that have a real executable worker adapter (headless run + output
# capture). Codex remains the default; Claude Code breaks the single-point
# dependency so a Codex outage does not stop all delivery.
EXECUTABLE_CODING_AGENTS = {"codex", "claude-code"}


def _coverage_changed_files(*, repo_root: Path, workspace_root: Path) -> list[str]:
    raw = changed_files(base="HEAD", cwd=repo_root)
    workspace_prefix = ""
    try:
        workspace_prefix = workspace_root.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        workspace_prefix = ""
    kept: list[str] = []
    for path in raw:
        normalized = str(path).replace("\\", "/").strip()
        if not normalized:
            continue
        basename = normalized.rsplit("/", 1)[-1]
        if basename in _ARTIFACT_BASENAMES:
            continue
        if workspace_prefix and (normalized == workspace_prefix or normalized.startswith(workspace_prefix + "/")):
            continue
        if any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in _ARTIFACT_DIR_PREFIXES):
            continue
        kept.append(normalized)
    return kept


CommandRunner = Callable[..., dict[str, Any]]


def dispatch_chief_plan(
    *,
    workspace_root: str | Path,
    plan_id: str,
    execute: bool = False,
    dry_run: bool = True,
    track_id: str | None = None,
    run_profile: str = "dry-run",
    include_slow: bool = False,
    max_workflows: int = 10,
    timeout_seconds: float = 1800.0,
    allow_dirty: bool = False,
    allow_coverage_gap: bool = False,
    auto_repair_once: bool = False,
    model_policy: dict[str, Any] | None = None,
    test_command: str | None = None,
    allow_test_edits: bool = False,
    merge: bool = False,
    command_runner: CommandRunner | None = None,
    codex_runner: Any = None,
    failure_evidence_builder: Any = None,
) -> dict[str, Any]:
    """Preview or execute one chief-engineer worker dispatch.

    Execution is intentionally narrow: only the Codex implementation adapter is
    supported. Other profiles can appear as inspection lanes in the plan, but
    dispatch will not run them as coding workers.
    """
    workspace_path = Path(workspace_root).expanduser().resolve()
    plan = load_plan(workspace_path, plan_id)
    if plan is None:
        return _blocked(plan_id=plan_id, reason=f"No saved plan found: {plan_id}")

    selected_track = _select_track(plan, track_id=track_id)
    if selected_track is None:
        return _blocked(plan_id=plan_id, reason="No matching worker track found in the saved plan.", plan=plan)

    repo_root = Path(str(plan.get("repo_root") or ".")).expanduser().resolve()
    track_token = str(selected_track.get("id") or "track_1_codex")
    worktree = default_worktree_path(repo_root=repo_root, plan_id=plan_id, track_id=track_token)
    branch = default_branch_name(plan_id=plan_id, track_id=track_token)
    verification_workspace = default_worktree_workspace_path(
        source_workspace=workspace_path,
        repo_root=repo_root,
        worktree=worktree,
    )
    verification_command = build_verification_command(
        workspace_root=verification_workspace,
        repo_root=worktree,
        run_profile=run_profile,
        include_slow=include_slow,
    )
    # Zero-token architecture memory: refresh the local index (only files that
    # changed since the last dispatch are re-parsed) and hand every worker
    # attempt a budgeted excerpt. The memory layer must never block dispatch.
    repo_map_text = ""
    repo_map_stats: dict[str, Any] = {}
    try:
        repo_map_payload = build_repo_map(repo_root=repo_root, cache_path=repo_map_cache_path(workspace_path))
        changed_focus = [str(item) for item in (plan.get("changed_files") or []) if isinstance(item, str)]
        repo_map_text = render_repo_map(
            repo_map_payload,
            goal=str(plan.get("objective") or ""),
            focus_files=changed_focus,
        )
        repo_map_stats = {
            "file_count": repo_map_payload.get("file_count"),
            "parsed": repo_map_payload.get("parsed"),
            "reused": repo_map_payload.get("reused"),
        }
    except Exception as exc:  # noqa: BLE001 - memory failure degrades to no map, never a blocked dispatch
        repo_map_stats = {"error": str(exc)[:200]}

    worker_command = build_worker_command(
        plan=plan,
        track=selected_track,
        worktree=worktree,
        verification_command=verification_command,
        phase="implementation",
        model_policy=model_policy,
        repo_map_text=repo_map_text,
    )
    preview = {
        "schema_version": 1,
        "status": "preview",
        "plan_id": plan_id,
        "objective": str(plan.get("objective") or ""),
        "plan_status": str(plan.get("status") or ""),
        "dry_run": bool(dry_run or not execute),
        "execute": bool(execute and not dry_run),
        "worker": {
            "track_id": track_token,
            "agent": str(selected_track.get("agent") or ""),
            "track_kind": str(selected_track.get("track_kind") or "implementation"),
            "command": worker_command["display"],
            "argv": worker_command["argv"],
        },
        "worktree": {
            "path": str(worktree),
            "branch": branch,
            "created": False,
        },
        "verification": {
            "command": verification_command,
            "workspace_root": str(verification_workspace),
            "records_workspace_root": str(workspace_path),
            "run_profile": run_profile,
            "include_slow": bool(include_slow),
            "max_workflows": int(max_workflows),
        },
        "repo_map": repo_map_stats,
        "subscription_quota": _quota_preview(),
        "warnings": _dispatch_warnings(plan, selected_track),
        "records": {
            "plan_dir": str(plan_dir(workspace_path, plan_id)),
            "workers_jsonl": str(plan_dir(workspace_path, plan_id) / "workers.jsonl"),
            "verification_json": str(plan_dir(workspace_path, plan_id) / "verification.json"),
        },
    }

    blocked_reason = _dispatch_block_reason(
        plan=plan,
        track=selected_track,
        allow_coverage_gap=allow_coverage_gap,
        execute=execute and not dry_run,
        has_test_command=bool(str(test_command or "").strip()),
    )
    if blocked_reason:
        return {**preview, "status": "blocked", "reason": blocked_reason}
    if dry_run or not execute:
        return preview

    repo_check = _check_repo(repo_root)
    if repo_check.get("status") != "ok":
        return {**preview, "status": "blocked", "reason": repo_check.get("reason")}
    dirty = git_dirty_files(
        repo_root,
        ignored_prefixes=workspace_record_dirty_prefixes(repo_root=repo_root, workspace_root=workspace_path),
    )
    if dirty and not allow_dirty:
        return {
            **preview,
            "status": "blocked",
            "reason": "Repository has uncommitted changes; pass --allow-dirty only when you intentionally want to branch from this state.",
            "dirty_files": dirty[:20],
        }

    worker_agent = canonical_agent_name(str(selected_track.get("agent") or "codex"))
    worker_profile = load_agent_profile(worker_agent) or {}
    exe_name = str(worker_profile.get("executable") or worker_agent)
    executable = shutil.which(exe_name)
    if not executable and command_runner is None:
        return {**preview, "status": "blocked", "reason": f"{exe_name} executable was not found on PATH."}
    worker_command["argv"][0] = executable or exe_name

    setup = create_worktree(repo_root=repo_root, worktree=worktree, branch=branch)
    if setup.get("status") not in {"created", "reused"}:
        return {**preview, "status": "blocked", "reason": setup.get("reason"), "worktree_setup": setup}
    preview["worktree"]["created"] = True
    if setup.get("status") == "reused":
        preview["worktree"]["reused"] = True
    # The commit the worker branched from: the tamper guard diffs against it, so
    # test edits are caught even when the worker commits inside the worktree.
    worktree_base = _git_head(worktree)
    preview["worktree"]["base_commit"] = worktree_base
    workspace_setup = prepare_worktree_workspace(
        source_workspace=workspace_path,
        target_workspace=verification_workspace,
    )
    preview["verification"]["workspace_prepared"] = workspace_setup["status"]
    if workspace_setup.get("status") == "failed":
        return {**preview, "status": "blocked", "reason": workspace_setup.get("reason"), "workspace_setup": workspace_setup}

    logs_dir = plan_dir(workspace_path, plan_id) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    runner = command_runner or run_process_capture

    # Route a cheap-tier task to an external backend (e.g. MiMo) when configured:
    # burns that provider's credits, not the Claude subscription. Repair escalates
    # the tier, so it falls back to the subscription automatically.
    worker_agent_norm = canonical_agent_name(str(selected_track.get("agent") or ""))
    worker_backend = resolve_backend_for_tier(str(selected_track.get("tier") or "")) if worker_agent_norm == "claude-code" else None
    initial_argv = list(worker_command["argv"])
    initial_env: dict[str, str] | None = None
    if worker_backend:
        initial_argv = _override_model_flag(initial_argv, worker_backend["model"])
        initial_env = worker_backend["env"]
        preview["worker"]["backend"] = redact_backend(worker_backend)

    worker_record = _run_worker_attempt(
        workspace_root=workspace_path,
        plan_id=plan_id,
        attempt="initial",
        track=selected_track,
        argv=initial_argv,
        stdin_text=worker_command.get("stdin"),
        cwd=worktree,
        timeout_seconds=timeout_seconds,
        log_path=logs_dir / f"{track_token}-initial.log",
        runner=runner,
        env=initial_env,
        backend=worker_backend,
    )

    # Quota failover: if the subscription worker was blocked by a usage/rate
    # limit, keep going instead of stopping. Claude Code falls over to a cheap
    # backend (MiMo); Codex falls over to the Claude Code worker first (which
    # may then itself take the backend hop). Only when we were not already on
    # a backend.
    failover_record: dict[str, Any] | None = None
    # Repair must run on whichever worker actually did the work; when a
    # failover hop takes over, these follow it.
    repair_track = selected_track
    repair_executable = executable
    repair_env: dict[str, str] | None = None
    repair_backend: dict[str, Any] | None = None
    quota_hit = (
        worker_backend is None
        and worker_record.get("status") != "completed"
        and looks_like_quota_exhaustion(worker_record.get("stdout_tail"), worker_record.get("stderr_tail"))
    )
    if quota_hit and worker_agent_norm == "claude-code":
        failover_backend = resolve_failover_backend()
        if failover_backend:
            failover_argv = _override_model_flag(list(worker_command["argv"]), failover_backend["model"])
            preview["worker"]["failover_backend"] = redact_backend(failover_backend)
            failover_record = _run_worker_attempt(
                workspace_root=workspace_path,
                plan_id=plan_id,
                attempt="quota_failover",
                track=selected_track,
                argv=failover_argv,
                stdin_text=worker_command.get("stdin"),
                cwd=worktree,
                timeout_seconds=timeout_seconds,
                log_path=logs_dir / f"{track_token}-quota-failover.log",
                runner=runner,
                env=failover_backend["env"],
                backend=failover_backend,
            )
            # The failover attempt is what verification should judge.
            worker_record = failover_record
            repair_env = failover_backend["env"]
            repair_backend = failover_backend
    elif quota_hit and worker_agent_norm == "codex":
        # Codex subscription ran out (found live by V5): rebuild the same task
        # for the Claude Code worker and keep going in the same worktree.
        claude_track = {**selected_track, "agent": "claude-code"}
        claude_command = build_worker_command(
            plan=plan,
            track=claude_track,
            worktree=worktree,
            verification_command=verification_command,
            phase="implementation",
            model_policy=model_policy,
            repo_map_text=repo_map_text,
        )
        claude_profile = load_agent_profile("claude-code") or {}
        claude_exe = shutil.which(str(claude_profile.get("executable") or "claude"))
        if claude_exe:
            claude_argv = list(claude_command["argv"])
            claude_argv[0] = claude_exe
            preview["worker"]["failover_agent"] = "claude-code"
            failover_record = _run_worker_attempt(
                workspace_root=workspace_path,
                plan_id=plan_id,
                attempt="agent_failover",
                track=claude_track,
                argv=claude_argv,
                stdin_text=claude_command.get("stdin"),
                cwd=worktree,
                timeout_seconds=timeout_seconds,
                log_path=logs_dir / f"{track_token}-agent-failover.log",
                runner=runner,
            )
            repair_track = claude_track
            repair_executable = claude_exe
            if (
                failover_record.get("status") != "completed"
                and looks_like_quota_exhaustion(failover_record.get("stdout_tail"), failover_record.get("stderr_tail"))
            ):
                failover_backend = resolve_failover_backend()
                if failover_backend:
                    backend_argv = _override_model_flag(list(claude_argv), failover_backend["model"])
                    preview["worker"]["failover_backend"] = redact_backend(failover_backend)
                    failover_record = _run_worker_attempt(
                        workspace_root=workspace_path,
                        plan_id=plan_id,
                        attempt="quota_failover",
                        track=claude_track,
                        argv=backend_argv,
                        stdin_text=claude_command.get("stdin"),
                        cwd=worktree,
                        timeout_seconds=timeout_seconds,
                        log_path=logs_dir / f"{track_token}-quota-failover.log",
                        runner=runner,
                        env=failover_backend["env"],
                        backend=failover_backend,
                    )
                    repair_env = failover_backend["env"]
                    repair_backend = failover_backend
            worker_record = failover_record

    verification_attempts: list[dict[str, Any]] = []
    verification = run_dispatch_verification(
        workspace_root=workspace_path,
        verification_workspace_root=verification_workspace,
        plan_id=plan_id,
        repo_root=worktree,
        run_profile=run_profile,
        include_slow=include_slow,
        max_workflows=max_workflows,
        codex_runner=codex_runner,
        failure_evidence_builder=failure_evidence_builder,
        test_command=test_command,
        worktree_base=worktree_base,
        allow_test_edits=allow_test_edits,
    )
    verification_attempts.append(verification)

    repair_worker_record: dict[str, Any] | None = None
    if auto_repair_once and verification.get("verdict") == "fail":
        repair_prompt = str((verification.get("repair_brief") or {}).get("repair_prompt") or "").strip()
        if repair_prompt:
            repair_command = build_worker_command(
                plan=plan,
                track=repair_track,
                worktree=worktree,
                verification_command=verification_command,
                prompt_override=repair_prompt + "\n\nAfter repairing, run: " + verification_command,
                phase="repair",
                model_policy=model_policy,
                repo_map_text=repo_map_text,
            )
            repair_argv = list(repair_command["argv"])
            if repair_executable:
                repair_argv[0] = repair_executable
            if repair_backend:
                repair_argv = _override_model_flag(repair_argv, repair_backend["model"])
            repair_worker_record = _run_worker_attempt(
                workspace_root=workspace_path,
                plan_id=plan_id,
                attempt="auto_repair_once",
                track=repair_track,
                argv=repair_argv,
                stdin_text=repair_command.get("stdin"),
                cwd=worktree,
                timeout_seconds=timeout_seconds,
                log_path=logs_dir / f"{track_token}-auto-repair-once.log",
                runner=runner,
                env=repair_env,
                backend=repair_backend,
            )
            verification_attempts.append(
                run_dispatch_verification(
                    workspace_root=workspace_path,
                    verification_workspace_root=verification_workspace,
                    plan_id=plan_id,
                    repo_root=worktree,
                    run_profile=run_profile,
                    include_slow=include_slow,
                    max_workflows=max_workflows,
                    codex_runner=codex_runner,
                    failure_evidence_builder=failure_evidence_builder,
                    test_command=test_command,
                    worktree_base=worktree_base,
                    allow_test_edits=allow_test_edits,
                )
            )

    latest_verification = verification_attempts[-1]
    worker_completed = worker_record.get("status") == "completed"
    # Every failover hop is exhausted and the worker still hit a quota wall:
    # surface that honestly instead of letting the gate failure masquerade as
    # a code problem ("same_failure_repeated" on an untouched worktree).
    quota_exhausted = (
        not worker_completed
        and looks_like_quota_exhaustion(worker_record.get("stdout_tail"), worker_record.get("stderr_tail"))
    )
    status = "completed" if worker_completed else "worker_failed"
    if latest_verification.get("verdict") == "fail":
        status = "verification_failed"
    elif latest_verification.get("verdict") == "pass" and worker_completed:
        status = "verified"
    elif latest_verification.get("verdict") == "pass":
        status = "worker_failed"
    elif latest_verification.get("verdict") == "inspection_only":
        # Inspection-only proves the page rendered, not that the product works.
        # It must never surface as verified (principle: dry-run cannot pose as
        # real acceptance).
        status = "inspection_only"

    # Close the loop: merge the worker's isolated branch back only when the
    # change is genuinely verified. Verification is the sole gate — never merge
    # anything that did not pass, and abort on any conflict rather than force it.
    merge_result: dict[str, Any] | None = None
    if merge:
        if status == "verified":
            merge_result = merge_worktree_branch(
                repo_root=repo_root,
                worktree=worktree,
                branch=branch,
                message=f"{plan.get('objective') or 'DevPacer change'} [plan {plan_id}]",
            )
        else:
            merge_result = {"status": "skipped", "reason": f"not merged: status is {status}, not verified."}

    payload = {
        **preview,
        "status": status,
        "dry_run": False,
        "quota_exhausted": quota_exhausted,
        "worker_record": worker_record,
        "failover_worker_record": failover_record,
        "repair_worker_record": repair_worker_record,
        "verification_attempts": verification_attempts,
        "latest_verification": latest_verification,
        "usage_summary": summarize_worker_usage(
            [worker_record, failover_record, repair_worker_record]
        ),
    }
    if quota_exhausted:
        payload.setdefault("warnings", []).append(
            "Worker hit a subscription quota/usage limit and no failover completed."
            " The task itself was not attempted; retry after the quota resets."
        )
    if merge_result is not None:
        payload["merge"] = merge_result
    return payload


def summarize_worker_usage(records: list[dict[str, Any] | None]) -> dict[str, Any]:
    """Aggregate token/cost across a dispatch's worker attempts so the user can
    see exactly what a task cost. Cheap-backend spend is kept separate (it burns
    cheap credits, not the Claude subscription).

    Cache tokens (cache_read_input_tokens, cache_creation_input_tokens) are
    included in the total so the number reflects real model consumption, not
    just the tiny uncached-new-input slice."""
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0
    spent_usd = 0.0
    saved_usd = 0.0
    attempts = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else None
        if not usage:
            continue
        attempts += 1
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
        cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
        cache_creation_tokens += int(usage.get("cache_creation_input_tokens") or 0)
        cost = float(usage.get("cost_usd") or 0.0)
        if usage.get("cost_is_savings"):
            saved_usd += cost
        else:
            spent_usd += cost
    return {
        "attempts_with_usage": attempts,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "total_tokens": input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens,
        "spent_usd": round(spent_usd, 4),
        "saved_usd": round(saved_usd, 4),
    }


def _select_track(plan: dict[str, Any], *, track_id: str | None) -> dict[str, Any] | None:
    tracks = plan.get("worker_tracks") if isinstance(plan.get("worker_tracks"), list) else []
    normalized = str(track_id or "").strip()
    if normalized:
        return next((track for track in tracks if isinstance(track, dict) and str(track.get("id") or "") == normalized), None)
    for track in tracks:
        if not isinstance(track, dict):
            continue
        agent = canonical_agent_name(str(track.get("agent") or ""))
        if agent in EXECUTABLE_CODING_AGENTS and str(track.get("track_kind") or "implementation") != "inspection":
            return track
    return next((track for track in tracks if isinstance(track, dict)), None)


def _dispatch_block_reason(
    *,
    plan: dict[str, Any],
    track: dict[str, Any],
    allow_coverage_gap: bool,
    execute: bool,
    has_test_command: bool = False,
) -> str:
    plan_status = str(plan.get("status") or "")
    if plan_status in {"blocked", "needs_clarification"}:
        return f"Plan status is {plan_status}; resolve it before dispatch."
    # A test/build command IS the acceptance gate, so authored-workflow coverage
    # is irrelevant — otherwise every fresh project (which has no workflows) is
    # blocked by a coverage gap it can never satisfy. This mirrors the same
    # exemption in run_chief_mission.
    if plan_status == "needs_workflow_coverage" and not allow_coverage_gap and not has_test_command:
        return "Plan has weak workflow coverage; rerun chief-plan after adding coverage or pass --allow-coverage-gap for a dry experiment."
    agent = canonical_agent_name(str(track.get("agent") or ""))
    if str(track.get("track_kind") or "implementation") == "inspection":
        return "Selected track is an inspection lane, not a coding worker."
    if execute and agent not in EXECUTABLE_CODING_AGENTS:
        return f"Agent '{agent}' has no executable worker adapter (supported: {', '.join(sorted(EXECUTABLE_CODING_AGENTS))})."
    return ""


def _quota_preview() -> dict[str, Any]:
    """Latest subscription-window snapshot (5h/7d), so every preview shows how
    much headroom a dispatch has. Warn-only by design: snapshots can be stale,
    and blocking on stale data would strand missions."""
    try:
        from .subscription_quota import load_quota_snapshot, quota_status

        snapshot = load_quota_snapshot()
        status = quota_status(snapshot)
        summary: dict[str, Any] = {"level": status["level"], "messages": status["messages"]}
        if snapshot:
            summary["rate_limits"] = snapshot.get("rate_limits")
            summary["age_minutes"] = snapshot.get("age_minutes")
        return summary
    except OSError:
        return {"level": "unknown", "messages": []}


def _dispatch_warnings(plan: dict[str, Any], track: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if str(plan.get("status") or "") == "needs_workflow_coverage":
        warnings.append("Workflow coverage is weak; dispatch should be blocked unless this is an explicit experiment.")
    if str(track.get("track_kind") or "implementation") == "inspection":
        warnings.append("This track is read-only inspection; it should not create a code diff.")
    warnings.append(
        "Worktree verification assumes the workflow points at the app instance or fixture produced by that worktree."
    )
    return warnings


# Map a model_policy tier to a capability-profile task_kind (which resolves to a
# concrete model id via the agent profile's model roles).
_POLICY_TIER_TASK_KIND = {"strong": "implementation", "fast": "fast", "multimodal": "implementation"}


def _task_kind_for_phase(phase: str, model_policy: dict[str, Any] | None) -> str:
    policy = model_policy if isinstance(model_policy, dict) else {}
    tier = str(policy.get(phase) or ("strong" if phase != "classification" else "fast")).strip().lower()
    return _POLICY_TIER_TASK_KIND.get(tier, "implementation")


def build_worker_command(
    *,
    plan: dict[str, Any],
    track: dict[str, Any],
    worktree: Path,
    verification_command: str,
    prompt_override: str | None = None,
    phase: str = "implementation",
    model_policy: dict[str, Any] | None = None,
    repo_map_text: str | None = None,
) -> dict[str, Any]:
    agent = canonical_agent_name(str(track.get("agent") or "codex"))
    profile = load_agent_profile(agent) or {}
    task_kind = _task_kind_for_phase(phase, model_policy)
    config = recommend_worker_config(profile, task_kind=task_kind) if profile else {}
    prompt = prompt_override or build_worker_prompt(plan=plan, track=track, worktree=worktree, verification_command=verification_command)
    # Every attempt (including repair) is a fresh model session; the map is how
    # the worker orients without re-reading the repository each round.
    if repo_map_text:
        prompt = (
            prompt
            + "\n\nRepository map (DevPacer local index, current at dispatch; trust it for"
            " orientation instead of scanning the repo):\n"
            + repo_map_text
        )
    if agent != "codex":
        # Profile-driven headless command (e.g. Claude Code: `claude -p ...`).
        # Any agent with a headless profile becomes an executable worker.
        headless = profile.get("headless") if isinstance(profile.get("headless"), dict) else {}
        if headless.get("command"):
            argv = _build_headless_argv(headless=headless, config=config, track=track, phase=phase, prompt=prompt)
            return {"argv": argv, "display": format_argv(argv)}
        return {"argv": [agent, prompt], "display": format_argv([agent, prompt])}
    argv = ["codex", "exec"]
    # The implementation phase honors any model already chosen for the track; other
    # phases (e.g. repair) follow the model_policy tier so a cheaper model can be
    # used when the policy asks for it.
    if phase == "implementation":
        model = str(track.get("model") or config.get("model") or "")
    else:
        model = str(config.get("model") or track.get("model") or "")
    if model:
        argv.extend(["--model", model])
    for source in (track.get("sandbox"), track.get("approval")):
        flag = str((source or {}).get("flag") or "").strip() if isinstance(source, dict) else ""
        if flag:
            argv.extend(shlex.split(flag, posix=False))
    # Codex can read the initial prompt from stdin when PROMPT is "-".  Keeping
    # large repo maps out of argv avoids Windows' command-line length limit.
    argv.append("-")
    return {"argv": argv, "display": format_argv(argv) + " < prompt via stdin", "stdin": prompt}


def _override_model_flag(argv: list[str], model: str) -> list[str]:
    argv = list(argv)
    for i, token in enumerate(argv):
        if token == "--model" and i + 1 < len(argv):
            argv[i + 1] = model
            return argv
    return argv


def _build_headless_argv(*, headless: dict[str, Any], config: dict[str, Any], track: dict[str, Any], phase: str, prompt: str) -> list[str]:
    base_template = str(headless.get("command") or "").strip()
    base = base_template.replace('"<prompt>"', "").replace("<prompt>", "").strip()
    argv = shlex.split(base, posix=False) if base else []
    if phase == "implementation":
        model = str(track.get("model") or config.get("model") or "")
    else:
        model = str(config.get("model") or track.get("model") or "")
    if model:
        argv.extend(["--model", model])
    # Headless-safe execution flags (permission mode, output format) come from the
    # profile so a worker runs unattended without hanging on interactive prompts.
    for key in ("permission_flag", "extra_flags"):
        flag = str(headless.get(key) or "").strip()
        if flag:
            argv.extend(shlex.split(flag, posix=False))
    argv.append(prompt)
    return argv


def build_worker_prompt(
    *,
    plan: dict[str, Any],
    track: dict[str, Any],
    worktree: Path,
    verification_command: str,
) -> str:
    criteria = plan.get("acceptance_criteria") if isinstance(plan.get("acceptance_criteria"), list) else []
    selected = plan.get("selected_workflows") if isinstance(plan.get("selected_workflows"), list) else []
    lines = [
        f"Objective: {plan.get('objective') or ''}",
        f"Plan id: {plan.get('plan_id') or ''}",
        f"Worker track: {track.get('id') or ''} ({track.get('agent') or ''})",
        f"Worktree: {worktree}",
        "Keep changes scoped to the objective. Do not edit Checkpoint plan records.",
    ]
    if selected:
        lines.append("Selected Checkpoint workflows: " + ", ".join(str(item) for item in selected))
    if criteria:
        lines.append("Acceptance criteria:")
        lines.extend(f"- {item}" for item in criteria)
    changed = plan.get("changed_files") if isinstance(plan.get("changed_files"), list) else []
    scoped = [str(item) for item in changed if not str(item).startswith(".agent-workspace")][:12]
    if scoped:
        lines.append("Touch only the files this task requires; the changed files in scope are:")
        lines.extend(f"- {item}" for item in scoped)
    lines.extend(
        [
            "Work efficiently to conserve model budget: read only the files you need for this objective;"
            " do not scan or grep the whole repository, and do not refactor unrelated code.",
            "Before claiming done, run this verification command and inspect the evidence if it fails:",
            verification_command,
        ]
    )
    return "\n".join(lines)


def build_verification_command(
    *,
    workspace_root: str | Path,
    repo_root: str | Path,
    run_profile: str,
    include_slow: bool,
) -> str:
    slow = " --include-slow" if include_slow else ""
    return (
        "checkpoint codex-check"
        f" --workspace-root {quote_cli(workspace_root)}"
        f" --repo-root {quote_cli(repo_root)}"
        " --base HEAD"
        f" --run-profile {run_profile}"
        f"{slow}"
        " --strict"
        " --format markdown"
    )


def run_dispatch_verification(
    *,
    workspace_root: Path,
    verification_workspace_root: Path | None = None,
    plan_id: str,
    repo_root: Path,
    run_profile: str,
    include_slow: bool,
    max_workflows: int,
    codex_runner: Any = None,
    failure_evidence_builder: Any = None,
    test_command: str | None = None,
    worktree_base: str | None = None,
    allow_test_edits: bool = False,
) -> dict[str, Any]:
    verification_workspace_path = (verification_workspace_root or workspace_root).expanduser().resolve()
    now = datetime.now(timezone.utc).isoformat()

    # Command gate: when a test/build command is given, it IS the acceptance
    # (works on any project, no authored workflows). It is deterministic and
    # cannot be gamed by the model, so it fully replaces workflow verification —
    # otherwise a workspace's seeded demo workflows would fail on an unrelated
    # project.
    if test_command:
        # A gate over edited tests proves nothing: refuse before running it.
        tampered = changed_test_files(repo_root=Path(repo_root), base_ref=worktree_base)
        if tampered and not allow_test_edits:
            payload = {
                "plan_id": plan_id,
                "repo_root": str(repo_root),
                "workspace_root": str(verification_workspace_path),
                "records_workspace_root": str(workspace_root),
                "run_profile": run_profile,
                "passed": 0,
                "inspection_only": 0,
                "failed": 1,
                "total": 1,
                "verdict": "fail",
                "tampered_test_files": tampered,
                "repair_brief": tamper_repair_brief(tampered, base_ref=worktree_base),
                "markdown": (
                    f"Verification refused: worker modified test file(s) {', '.join(tampered[:5])}."
                    " Tests are the acceptance contract; pass --allow-test-edits only when the task"
                    " is explicitly about changing tests."
                ),
                "recorded_at": now,
            }
            saved = save_verification(workspace_root, plan_id, payload)
            payload["saved_path"] = saved["path"]
            return payload
        command_result = run_command_verification(command=test_command, repo_root=Path(repo_root))
        verdict = "pass" if command_result.get("verdict") == "pass" else "fail"
        passed = 1 if verdict == "pass" else 0
        payload = {
            "plan_id": plan_id,
            "repo_root": str(repo_root),
            "workspace_root": str(verification_workspace_path),
            "records_workspace_root": str(workspace_root),
            "run_profile": run_profile,
            "passed": passed,
            "inspection_only": 0,
            "failed": 0 if verdict == "pass" else 1,
            "total": 1,
            "verdict": verdict,
            "test_files_changed": tampered,
            "command_verification": command_result,
            "markdown": (
                f"Test command `{test_command}` passed (exit 0)."
                if verdict == "pass"
                else f"Test command failed: `{test_command}` (exit {command_result.get('exit_code')})."
            ),
            "recorded_at": now,
        }
        if verdict == "fail":
            payload["repair_brief"] = command_repair_brief(command_result)
        payload["diff_summary"] = _safe_diff_summary(repo_root=Path(repo_root), base_ref=worktree_base)
        if payload["diff_summary"].get("large_diff"):
            payload.setdefault("warnings", []).append(
                f"Diff volume unusually large ({payload['diff_summary']['file_count']} files, "
                f"+{payload['diff_summary']['lines_added']}/-{payload['diff_summary']['lines_removed']} lines). "
                "Review the diff carefully to confirm the worker did not touch unrelated code."
            )
        saved = save_verification(workspace_root, plan_id, payload)
        payload["saved_path"] = saved["path"]
        return payload

    command_result = None
    # Workflow verification (product UI), when no test command was given.
    workspace = open_workspace(verification_workspace_path)
    runner = codex_runner or run_codex_check
    changed = _coverage_changed_files(repo_root=Path(repo_root), workspace_root=verification_workspace_path)
    result = runner(
        workspace,
        base="HEAD",
        repo_root=repo_root,
        include_slow=include_slow,
        max_workflows=max_workflows,
        run_profile=run_profile,
        changed=changed,
    )
    verdict = result.verdict
    # If the command gate passed and there are no product workflows to run, the
    # test command IS the acceptance evidence -> pass.
    if command_result and command_result.get("verdict") == "pass" and verdict in {"no_workflows", "coverage_gap", "no_changed_files"}:
        verdict = "pass"
    payload = to_jsonable(result)
    payload.update(
        {
            "plan_id": plan_id,
            "repo_root": str(repo_root),
            "workspace_root": str(verification_workspace_path),
            "records_workspace_root": str(workspace_root),
            "run_profile": run_profile,
            "passed": result.passed,
            "inspection_only": result.inspection_only,
            "failed": result.failed,
            "total": result.total,
            "verdict": verdict,
            "markdown": codex_check_to_markdown(result),
            "recorded_at": now,
        }
    )
    if command_result:
        payload["command_verification"] = command_result
    if verdict == "fail":
        payload["repair_brief"] = build_repair_brief(
            workspace_root=verification_workspace_path,
            failure_evidence_builder=failure_evidence_builder,
        )
    # Layer 2: always attach a diff summary so the dashboard can show what
    # changed regardless of pass/fail.  Runs after verification to avoid adding
    # latency to the hot path.
    payload["diff_summary"] = _safe_diff_summary(repo_root=Path(repo_root), base_ref=None)
    if payload["diff_summary"].get("large_diff"):
        payload.setdefault("warnings", []).append(
            f"Diff volume unusually large ({payload['diff_summary']['file_count']} files). "
            "Review carefully."
        )
    saved = save_verification(workspace_root, plan_id, payload)
    payload["saved_path"] = saved["path"]
    return payload


def build_repair_brief(*, workspace_root: Path, failure_evidence_builder: Any = None) -> dict[str, Any]:
    try:
        builder = failure_evidence_builder
        if builder is None:
            from .repair import build_failure_evidence_pack

            builder = build_failure_evidence_pack
        evidence = builder(workspace_root, max_chars=12000)
    except Exception as exc:
        return {
            "status": "unavailable",
            "source": "diagnose-latest-failure",
            "message": f"{type(exc).__name__}: {exc}",
        }
    failed_step = evidence.get("failed_step") if isinstance(evidence.get("failed_step"), dict) else {}
    return {
        "status": str(evidence.get("status") or ""),
        "source": "diagnose-latest-failure",
        "run_id": str(evidence.get("run_id") or ""),
        "workflow": str(evidence.get("workflow") or ""),
        "failed_step": failed_step,
        "repair_prompt": str(evidence.get("repair_prompt") or ""),
        "message": str(evidence.get("message") or ""),
    }


def _run_worker_attempt(
    *,
    workspace_root: Path,
    plan_id: str,
    attempt: str,
    track: dict[str, Any],
    argv: list[str],
    cwd: Path,
    timeout_seconds: float,
    log_path: Path,
    runner: CommandRunner,
    stdin_text: str | None = None,
    env: dict[str, str] | None = None,
    backend: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = monotonic()
    if stdin_text is not None:
        try:
            result = runner(argv, cwd, timeout_seconds, log_path, env=env, stdin_text=stdin_text)
        except TypeError:
            try:
                result = runner(argv, cwd, timeout_seconds, log_path, stdin_text=stdin_text)
            except TypeError:
                try:
                    result = runner(argv, cwd, timeout_seconds, log_path, env=env)
                except TypeError:
                    result = runner(argv, cwd, timeout_seconds, log_path)
    else:
        try:
            result = runner(argv, cwd, timeout_seconds, log_path, env=env)
        except TypeError:
            result = runner(argv, cwd, timeout_seconds, log_path)
    record = {
        "schema_version": 1,
        "plan_id": plan_id,
        "attempt": attempt,
        "track_id": str(track.get("id") or ""),
        "agent": str(track.get("agent") or ""),
        "status": "completed" if int(result.get("exit_code") or 0) == 0 else "failed",
        "exit_code": int(result.get("exit_code") or 0),
        "elapsed_seconds": round(monotonic() - started, 6),
        "cwd": str(cwd),
        "command": format_argv(argv),
        "log_path": str(log_path),
        "stdout_tail": str(result.get("stdout_tail") or ""),
        "stderr_tail": str(result.get("stderr_tail") or ""),
    }
    usage = _extract_worker_usage(str(track.get("agent") or ""), log_path, str(result.get("stdout_tail") or ""))
    if usage:
        # On a cheap external backend, the CLI-reported cost is priced at Claude
        # rates but the real spend is the backend's credits, so it counts as
        # savings, not spend.
        if backend and backend.get("cost_is_savings"):
            usage["cost_is_savings"] = True
            usage["backend"] = backend.get("name")
        record["usage"] = usage
    if backend:
        record["backend"] = {"name": backend.get("name"), "model": backend.get("model")}
    append_worker_record(workspace_root, plan_id, record)
    return record


def _extract_worker_usage(agent: str, log_path: Path, stdout_tail: str) -> dict[str, Any] | None:
    """Pull real cost/token usage from a worker run.

    Claude Code's ``-p --output-format json`` emits a result object with
    ``total_cost_usd`` and a ``usage`` block. Recording it gives DevPacer a real
    budget ledger instead of an estimate.
    """
    if canonical_agent_name(agent) != "claude-code":
        return None
    text = ""
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        text = (stdout_tail or "").strip()
    if not text:
        return None
    payload: dict[str, Any] | None = None
    candidates = [text]
    # Claude Code prints one JSON result object; a trailing stderr warning can be
    # appended, so also try the span from the first "{" to the last "}".
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{") : text.rfind("}") + 1])
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and ("total_cost_usd" in obj or "usage" in obj):
            payload = obj
            break
    if payload is None:
        return None
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    ledger = {
        "cost_usd": payload.get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "num_turns": payload.get("num_turns"),
        "session_id": payload.get("session_id"),
    }
    return {key: value for key, value in ledger.items() if value is not None}


def run_process_capture(
    argv: list[str],
    cwd: Path,
    timeout_seconds: float,
    log_path: Path,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    run_env = None
    if env:
        run_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}}
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=run_env,
            input=stdin_text,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        log_path.write_text(stdout + ("\n" if stdout and stderr else "") + stderr, encoding="utf-8")
        return {
            "exit_code": completed.returncode,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
        }
    except subprocess.TimeoutExpired as exc:
        stdout = _decode_timeout_text(exc.stdout)
        stderr = _decode_timeout_text(exc.stderr)
        log_path.write_text(stdout + ("\n" if stdout and stderr else "") + stderr, encoding="utf-8")
        return {
            "exit_code": 124,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": (stderr or f"Timed out after {timeout_seconds:.0f}s")[-2000:],
        }


def merge_worktree_branch(*, repo_root: Path, worktree: Path, branch: str, message: str) -> dict[str, Any]:
    """Merge a verified worker branch back into the main repo's current branch.

    Rigorous by design: commit the worker's edits on its branch, refuse to merge
    into a dirty or detached target, and on any conflict abort immediately (never
    leave the repo in a conflicted state, never force). The caller must only call
    this after verification passed.
    """
    def g(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)

    # 1) Commit the worker's (uncommitted) edits onto its own branch.
    g(worktree, "add", "-A")
    wt_status = g(worktree, "status", "--porcelain")
    if wt_status.stdout.strip():
        commit = g(
            worktree,
            "-c", "user.email=devpacer@local", "-c", "user.name=DevPacer",
            "commit", "-m", f"DevPacer verified change: {message}",
        )
        if commit.returncode != 0:
            return {"status": "failed", "reason": "could not commit worker changes: " + (commit.stderr or commit.stdout).strip()[:300]}

    # Nothing new on the branch => nothing to merge.
    ahead = g(repo_root, "rev-list", "--count", f"HEAD..{branch}")
    if ahead.returncode == 0 and ahead.stdout.strip() == "0":
        return {"status": "nothing_to_merge", "branch": branch}

    # 2) Target must be a real branch and clean (ignoring Checkpoint artifacts).
    current = g(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    target = current.stdout.strip()
    if current.returncode != 0 or not target or target == "HEAD":
        return {"status": "blocked", "reason": "main repo is in detached HEAD; not merging."}
    dirty_lines = git_dirty_files(
        repo_root,
        ignored_prefixes=workspace_record_dirty_prefixes(repo_root=repo_root, workspace_root=repo_root / ".agent-workspace"),
    )
    if dirty_lines:
        return {"status": "blocked", "reason": "target branch has uncommitted changes; not merging.", "dirty": dirty_lines[:10]}

    # 3) Merge (no fast-forward). On conflict, abort — never leave a mess.
    merge = g(repo_root, "merge", "--no-ff", "-m", f"DevPacer merge {branch}: {message}", branch)
    if merge.returncode != 0:
        g(repo_root, "merge", "--abort")
        return {"status": "conflict", "branch": branch, "target": target, "reason": (merge.stderr or merge.stdout).strip()[:400]}
    head = g(repo_root, "rev-parse", "HEAD")
    return {"status": "merged", "branch": branch, "target": target, "commit": head.stdout.strip()}


def _git_head(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def create_worktree(*, repo_root: Path, worktree: Path, branch: str) -> dict[str, Any]:
    if worktree.exists():
        check = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode != 0 or "true" not in check.stdout.lower():
            return {"status": "blocked", "reason": f"Worktree path already exists and is not a git worktree: {worktree}"}
        dirty = subprocess.run(
            ["git", "-C", str(worktree), "-c", "core.quotePath=false", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if dirty.returncode != 0:
            return {"status": "blocked", "reason": f"Could not inspect existing worktree: {worktree}"}
        dirty_lines = [line for line in dirty.stdout.splitlines() if line.strip()]
        if dirty_lines:
            return {
                "status": "blocked",
                "reason": f"Worktree path already exists with uncommitted changes: {worktree}",
                "dirty": dirty_lines[:10],
            }
        return {"status": "reused", "path": str(worktree), "branch": branch}
    worktree.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree), "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return {
            "status": "failed",
            "reason": (completed.stderr or completed.stdout or "git worktree add failed").strip(),
        }
    return {"status": "created", "path": str(worktree), "branch": branch}


def default_worktree_workspace_path(*, source_workspace: Path, repo_root: Path, worktree: Path) -> Path:
    """Return the workspace path that should be used while verifying a worktree.

    If the planning workspace lives under the repo root (the common
    ``<repo>/.agent-workspace`` case), use the same relative workspace path
    inside the worktree. Local workflow URLs then resolve against worktree files,
    not the original checkout. External workspaces keep using their original
    location.
    """
    try:
        relative = source_workspace.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return source_workspace.resolve()
    return (worktree.resolve() / relative).resolve()


def prepare_worktree_workspace(*, source_workspace: Path, target_workspace: Path) -> dict[str, Any]:
    source = source_workspace.resolve()
    target = target_workspace.resolve()
    if source == target:
        return {"status": "shared", "path": str(target)}
    try:
        target.mkdir(parents=True, exist_ok=True)
        manifest = source / "workspace.json"
        if manifest.exists():
            shutil.copyfile(manifest, target / "workspace.json")
        for dirname in ("workflows", "inputs", "fixtures"):
            source_dir = source / dirname
            if not source_dir.exists():
                continue
            shutil.copytree(source_dir, target / dirname, dirs_exist_ok=True)
    except OSError as exc:
        return {
            "status": "failed",
            "path": str(target),
            "reason": f"Could not prepare worktree workspace: {type(exc).__name__}: {exc}",
        }
    return {"status": "prepared", "path": str(target)}


def git_dirty_files(repo_root: Path, *, ignored_prefixes: tuple[str, ...] = ()) -> list[str]:
    completed = subprocess.run(
        ["git", "-c", "core.quotePath=false", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    return [line.strip() for line in lines if not _dirty_path_ignored(line, ignored_prefixes, repo_root=repo_root)]


def workspace_record_dirty_prefixes(*, repo_root: Path, workspace_root: Path) -> tuple[str, ...]:
    try:
        workspace_relative = workspace_root.resolve().relative_to(repo_root.resolve()).as_posix().rstrip("/")
    except ValueError:
        return ()
    if not workspace_relative:
        return ()
    # The whole workspace is DevPacer's runtime dir (plans, missions, runs,
    # repo_map.json, ...), never user source. Ignoring the root prefix keeps the
    # dirty-tree gate from tripping on the tool's own files — including a fresh
    # project where git collapses the entire untracked ".agent-workspace/" into
    # one porcelain entry.
    return (f"{workspace_relative}/", "强制测试记录.md")


def _dirty_path_ignored(line: str, ignored_prefixes: tuple[str, ...], *, repo_root: Path | None = None) -> bool:
    if not ignored_prefixes:
        return False
    path = _porcelain_path(line)
    if path == ".gitignore" and repo_root is not None:
        return _gitignore_change_is_only_checkpoint_runtime(repo_root, ignored_prefixes=ignored_prefixes)
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in ignored_prefixes)


def _porcelain_path(line: str) -> str:
    raw = line[3:] if len(line) > 3 else line
    if " -> " in raw:
        raw = raw.split(" -> ", 1)[1]
    return raw.strip().replace("\\", "/")


def _gitignore_change_is_only_checkpoint_runtime(repo_root: Path, *, ignored_prefixes: tuple[str, ...]) -> bool:
    gitignore = repo_root / ".gitignore"
    allowed = {"# DevPacer / Checkpoint generated runtime files"}
    allowed.update(prefix.rstrip("/") + "/" for prefix in ignored_prefixes if prefix.strip())
    if not gitignore.exists():
        return False

    diff = subprocess.run(
        ["git", "diff", "--", ".gitignore"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if diff.returncode == 0 and diff.stdout.strip():
        saw_generated_addition = False
        for raw in diff.stdout.splitlines():
            if raw.startswith(("+++", "---", "@@")):
                continue
            if raw.startswith("-"):
                return False
            if raw.startswith("+"):
                value = raw[1:].strip()
                if not value:
                    continue
                if value not in allowed:
                    return False
                saw_generated_addition = True
        return saw_generated_addition

    try:
        lines = [line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return False
    return bool(lines) and all(line in allowed for line in lines)


def _check_repo(repo_root: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or "true" not in completed.stdout.lower():
        return {"status": "blocked", "reason": f"Not a git work tree: {repo_root}"}
    return {"status": "ok"}


def default_worktree_path(*, repo_root: Path, plan_id: str, track_id: str) -> Path:
    token = _safe_token(track_id)
    root = repo_root.parent / f"{repo_root.name}.checkpoint-worktrees"
    return (root / _safe_token(plan_id) / token).resolve()


def default_branch_name(*, plan_id: str, track_id: str) -> str:
    return f"checkpoint/{_safe_token(plan_id)}/{_safe_token(track_id)}"


def chief_dispatch_to_markdown(payload: dict[str, Any]) -> str:
    lines = ["## Chief Dispatch", ""]
    lines.append(f"Status: `{payload.get('status')}`")
    if payload.get("reason"):
        lines.append(f"Reason: {payload['reason']}")
    lines.append(f"Plan: `{payload.get('plan_id')}`")
    if payload.get("objective"):
        lines.append(f"Objective: {payload['objective']}")
    worker = payload.get("worker") if isinstance(payload.get("worker"), dict) else {}
    if worker:
        lines.extend(
            [
                "",
                "### Worker",
                "",
                f"- Track: `{worker.get('track_id')}`",
                f"- Agent: `{worker.get('agent')}`",
                f"- Kind: `{worker.get('track_kind')}`",
                f"- Command: `{worker.get('command')}`",
            ]
        )
    worktree = payload.get("worktree") if isinstance(payload.get("worktree"), dict) else {}
    if worktree:
        lines.extend(
            [
                "",
                "### Worktree",
                "",
                f"- Path: `{worktree.get('path')}`",
                f"- Branch: `{worktree.get('branch')}`",
                f"- Created: `{worktree.get('created')}`",
            ]
        )
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    if verification:
        lines.extend(["", "### Verification", "", f"```powershell\n{verification.get('command')}\n```"])
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    if warnings:
        lines.extend(["", "### Warnings", ""])
        lines.extend(f"- {item}" for item in warnings)
    if payload.get("worker_record"):
        record = payload["worker_record"]
        lines.extend(["", "### Worker Result", ""])
        lines.append(f"- Status: `{record.get('status')}`")
        lines.append(f"- Exit code: `{record.get('exit_code')}`")
        lines.append(f"- Log: `{record.get('log_path')}`")
    latest = payload.get("latest_verification") if isinstance(payload.get("latest_verification"), dict) else None
    if latest:
        lines.extend(["", "### Latest Verification", ""])
        lines.append(f"- Verdict: `{latest.get('verdict')}`")
        lines.append(f"- Passed: `{latest.get('passed')}` · Inspection-only: `{latest.get('inspection_only')}` · Failed: `{latest.get('failed')}`")
        if latest.get("saved_path"):
            lines.append(f"- Saved: `{latest['saved_path']}`")
        repair = latest.get("repair_brief") if isinstance(latest.get("repair_brief"), dict) else {}
        if repair:
            lines.extend(["", "### Repair Brief", ""])
            lines.append(f"- Source: `{repair.get('source')}`")
            lines.append(f"- Workflow: `{repair.get('workflow')}`")
            lines.append(f"- Run: `{repair.get('run_id')}`")
            if repair.get("repair_prompt"):
                lines.extend(["", "```text", str(repair.get("repair_prompt")).strip(), "```"])
    records = payload.get("records") if isinstance(payload.get("records"), dict) else {}
    if records:
        lines.extend(["", "### Records", ""])
        for key, value in records.items():
            lines.append(f"- {key}: `{value}`")
    return "\n".join(lines).rstrip()


def format_argv(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def quote_cli(value: str | Path) -> str:
    text = str(value)
    if not text:
        return '""'
    if os.name == "nt":
        return subprocess.list2cmdline([text])
    return shlex.quote(text)


def _safe_diff_summary(*, repo_root: Path, base_ref: str | None) -> dict[str, Any]:
    """Build a diff summary; degrade gracefully on any git / filesystem error."""
    try:
        return build_diff_summary(repo_root=repo_root, base_ref=base_ref)
    except Exception:  # noqa: BLE001
        return {"file_count": 0, "lines_added": 0, "lines_removed": 0, "large_diff": False,
                "changed_files": [], "functions_touched": [], "user_checklist": [], "summary_text": ""}


def _decode_timeout_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _blocked(*, plan_id: str, reason: str, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "blocked",
        "plan_id": plan_id,
        "objective": str((plan or {}).get("objective") or ""),
        "reason": reason,
    }


def _safe_token(value: str) -> str:
    token = "".join(char.lower() if char.isalnum() else "-" for char in str(value).strip())
    while "--" in token:
        token = token.replace("--", "-")
    return token.strip("-") or "item"


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2)
