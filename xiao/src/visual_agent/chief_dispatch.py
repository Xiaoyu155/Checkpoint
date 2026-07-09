from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from .agent_backends import (
    canonical_backend_name,
    looks_like_quota_exhaustion,
    redact_backend,
    resolve_backend_by_name,
    resolve_failover_backend,
    resolve_backend_for_tier,
)
from .agent_capabilities import canonical_agent_name, load_agent_profile, recommend_worker_config
from .command_verification import (
    NON_REPAIRABLE_COMMAND_FAILURE_KINDS,
    acceptance_chain_repair_brief,
    changed_test_files,
    changed_acceptance_chain_files,
    command_repair_brief,
    is_test_path,
    run_command_verification,
    tamper_repair_brief,
)
from .chief_plans_store import append_worker_record, load_plan, plan_dir, save_verification
from .codex_check import codex_check_to_markdown, is_runtime_changed_file, run_codex_check
from .diff_summary import build_diff_summary
from .mission_progress import record_worker_output, save_mission_progress
from .subprocess_window import hidden_subprocess_kwargs
from .git_diff import changed_files
from .llm_providers import LLMBackend, run_llm_completion
from .models import to_jsonable
from .repo_map import build_repo_map, render_repo_map, repo_map_cache_path
from .workspace import open_workspace


# Checkpoint writes its own run artifacts (workspace outputs, status file, browser
# cache) into the repo it verifies. Those are tool outputs, not code under test,
# and must never be counted as changed files — otherwise a second verification
# round sees them as uncovered changes and reports a phantom coverage gap.
_ARTIFACT_BASENAMES = {".visual-agent-status.md", "强制测试记录.md"}
_ARTIFACT_DIR_PREFIXES = (".pw-browsers/", ".npm-cache/", ".dart-home/", ".dart_tool/", "artifacts/")
_NON_PRODUCT_DIR_PREFIXES = ("_graveyard_", "graveyard/", "archive/", "archives/", "_open_source_cases/")
# Python cache and compiled files should never be counted as product changes.
_CACHE_DIR_PREFIXES = ("__pycache__/", ".pytest_cache/")
_CACHE_EXTENSIONS = {".pyc", ".pyo", ".pyd"}
_GENERATED_NOISE_PATHS = {
    "linux/flutter/generated_plugin_registrant.cc",
    "linux/flutter/generated_plugin_registrant.h",
    "linux/flutter/generated_plugins.cmake",
    "macos/Flutter/GeneratedPluginRegistrant.swift",
    "windows/flutter/generated_plugin_registrant.cc",
    "windows/flutter/generated_plugin_registrant.h",
    "windows/flutter/generated_plugins.cmake",
}

# Coding agents that have a real unattended worker adapter. Claude Code was
# re-enabled 2026-07-05 by product decision: the free tier runs on the user's
# own codex/claude subscriptions, so both must be dispatchable. Note that
# unattended claude-code missions consume the 5-hour subscription window.
EXECUTABLE_CODING_AGENTS = {"codex", "claude-code", "mimo"}


def _allow_automatic_low_cost_failover(worker_agent_norm: str) -> bool:
    return worker_agent_norm != "codex"


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
        if is_runtime_changed_file(normalized, repo_root=repo_root):
            continue
        if any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in _ARTIFACT_DIR_PREFIXES):
            continue
        if _is_tool_generated_change(repo_root, normalized):
            continue
        if _is_non_product_path(normalized):
            continue
        # Filter out Python cache and compiled files
        if any(normalized.startswith(prefix) for prefix in _CACHE_DIR_PREFIXES):
            continue
        if any(basename.endswith(ext) for ext in _CACHE_EXTENSIONS):
            continue
        kept.append(normalized)
    return kept


def _is_non_product_path(path: str) -> bool:
    normalized = str(path).replace("\\", "/").strip().lstrip("/")
    if not normalized:
        return True
    parts = normalized.split("/")
    if any(part.endswith(".checkpoint-worktrees") or ".checkpoint-worktrees" in part for part in parts):
        return True
    return any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in _NON_PRODUCT_DIR_PREFIXES)


CommandRunner = Callable[..., dict[str, Any]]


def dispatch_chief_plan(
    *,
    workspace_root: str | Path,
    plan_id: str,
    mission_id: str | None = None,
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
    allow_prior_verified_evidence: bool = False,
    verification_env: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Preview or execute one chief-engineer worker dispatch.

    Execution is intentionally narrow: only the Codex implementation adapter is
    supported. Other profiles can appear as inspection lanes in the plan, but
    dispatch will not run them as coding workers.
    """
    workspace_path = Path(workspace_root).expanduser().resolve()
    progress_mission_id = str(mission_id or plan_id)
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
    effective_verification_command = str(test_command or "").strip() or verification_command
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

    # Report missions deliver a markdown artifact, not a code change: the
    # worker investigates and writes a report file, and that file is the
    # acceptance gate instead of workflows.
    from .chief_engineer import is_diagnosis_goal as _is_diagnosis_goal
    from .mission_intake import is_review_plan_goal

    objective_text = str(plan.get("objective") or "")
    diagnosis_mode = _is_diagnosis_goal(objective_text) and not str(test_command or "").strip()
    review_plan_mode = is_review_plan_goal(objective_text) and not str(test_command or "").strip()
    dirty_context = _dirty_context_summary(
        repo_root=repo_root,
        allow_dirty=allow_dirty,
        ignored_prefixes=workspace_record_dirty_prefixes(repo_root=repo_root, workspace_root=workspace_path),
    )
    dirty_context_text = _dirty_context_prompt(dirty_context)
    report_prompt = None
    if diagnosis_mode:
        report_prompt = (
            objective_text.strip()
            + "\n\n这是诊断任务：不要修改任何现有代码文件。"
            "请调查仓库（脚本、配置、README、日志与定时任务线索），找出最可能的根因，"
            "然后创建一个新文件 诊断报告.md（放在仓库根目录），用中文写三节：## 根因、## 证据、## 修复建议。"
        )
    elif review_plan_mode:
        report_prompt = (
            objective_text.strip()
            + "\n\n这是审查/开发计划任务：不要修改任何现有代码文件，不要运行破坏性命令。"
            "请调查目标目录中的产品结构、README、配置、测试、关键源码和已有计划/报告线索，"
            "然后创建一个新文件 审查与开发计划.md（放在仓库根目录），用中文写："
            "## 产品判断、## 当前状态、## 主要风险、## 建议开发计划、## 验收方式。"
            "计划必须具体到可执行步骤，并说明每一步的验收信号。"
        )

    worker_command = build_worker_command(
        plan=plan,
        track=selected_track,
        worktree=worktree,
        verification_command=effective_verification_command,
        phase="implementation",
        model_policy=model_policy,
        repo_map_text=repo_map_text,
        prompt_override=report_prompt,
        prompt_suffix=dirty_context_text,
    )
    toolchain_policy = _toolchain_policy_for_command(effective_verification_command)
    toolchain_preflight = _toolchain_preflight_for_command(effective_verification_command, policy=toolchain_policy)
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
            "command": effective_verification_command,
            "checkpoint_command": verification_command if str(test_command or "").strip() else "",
            "mode": "command" if str(test_command or "").strip() else "workflow",
            "workspace_root": str(verification_workspace),
            "records_workspace_root": str(workspace_path),
            "run_profile": run_profile,
            "include_slow": bool(include_slow),
            "max_workflows": int(max_workflows),
        },
        "repo_map": repo_map_stats,
        "toolchain_policy": toolchain_policy,
        "subscription_quota": _quota_preview(),
        "warnings": _dispatch_warnings(plan, selected_track),
        "records": {
            "plan_dir": str(plan_dir(workspace_path, plan_id)),
            "workers_jsonl": str(plan_dir(workspace_path, plan_id) / "workers.jsonl"),
            "verification_json": str(plan_dir(workspace_path, plan_id) / "verification.json"),
        },
    }
    if dirty_context:
        preview["worktree"]["source_dirty_context"] = dirty_context

    blocked_reason = _dispatch_block_reason(
        plan=plan,
        track=selected_track,
        allow_coverage_gap=allow_coverage_gap,
        execute=execute and not dry_run,
        has_test_command=bool(str(test_command or "").strip()),
    )
    save_mission_progress(
        workspace_path,
        progress_mission_id,
        stage="dispatch_ready",
        stage_label="Dispatch ready",
        status="preview",
        plan_id=plan_id,
        agent=str(selected_track.get("agent") or ""),
        worktree=str(worktree),
        verification_command=effective_verification_command,
    )
    if blocked_reason:
        save_mission_progress(
            workspace_path,
            progress_mission_id,
            stage="blocked",
            stage_label="Dispatch blocked",
            status="blocked",
            blocker=blocked_reason,
        )
        return {**preview, "status": "blocked", "reason": blocked_reason}
    if toolchain_preflight.get("status") == "blocked":
        save_mission_progress(
            workspace_path,
            progress_mission_id,
            stage="blocked",
            stage_label="Toolchain preflight blocked",
            status="blocked",
            blocker=str(toolchain_preflight.get("message") or "toolchain_preflight"),
        )
        return {
            **preview,
            "status": "blocked",
            "reason": str(toolchain_preflight.get("message") or "Toolchain preflight blocked dispatch."),
            "toolchain_preflight": toolchain_preflight,
        }
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

    requested_agent = str(selected_track.get("agent") or "codex").strip()
    requested_agent_norm = requested_agent.lower()
    worker_agent = canonical_agent_name(requested_agent)
    worker_profile = load_agent_profile(worker_agent) or {}
    exe_name = str(worker_profile.get("executable") or worker_agent)
    executable = "" if worker_agent == "mimo" else shutil.which(exe_name)
    if worker_agent != "mimo" and not executable and command_runner is None:
        return {**preview, "status": "blocked", "reason": f"{exe_name} executable was not found on PATH."}
    if worker_agent != "mimo":
        worker_command["argv"][0] = executable or exe_name

    setup = create_worktree(repo_root=repo_root, worktree=worktree, branch=branch, allow_dirty=allow_dirty)
    if setup.get("status") not in {"created", "reused"}:
        return {**preview, "status": "blocked", "reason": setup.get("reason"), "worktree_setup": setup}
    preview["worktree"]["setup_status"] = str(setup.get("status") or "")
    for key, value in setup.items():
        if key not in {"status", "path", "branch"}:
            preview["worktree"][key] = value
    preview["worktree"]["created"] = True
    if setup.get("status") == "reused":
        preview["worktree"]["reused"] = True
    _write_worktree_gitignore(worktree, repo_root)
    save_mission_progress(
        workspace_path,
        progress_mission_id,
        stage="worker_starting",
        stage_label="Worker starting",
        status="running",
        plan_id=plan_id,
        agent=str(selected_track.get("agent") or ""),
        worktree=str(worktree),
    )
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

    # Proactive quota check: if this agent recently failed due to quota exhaustion,
    # warn the user and suggest alternatives.
    from .agent_backends import has_recent_quota_failure, get_available_agents
    if has_recent_quota_failure(worker_agent_norm):
        available = get_available_agents()
        if available:
            preview["worker"]["quota_warning"] = (
                f"Agent '{worker_agent}' recently failed due to quota exhaustion. "
                f"Consider switching to: {', '.join(available[:3])}"
            )

    force_backend_name = canonical_backend_name(requested_agent_norm) if requested_agent_norm in {"bugteam", "bug-team", "gpt-bug-team", "mimo", "xiaomimimo"} else ""
    worker_backend = None
    if force_backend_name:
        worker_backend = resolve_backend_by_name(force_backend_name)
        if not worker_backend:
            return {
                **preview,
                "status": "blocked",
                "reason": (
                    "MiMo backend was requested, but no MiMo token was found. "
                    "Set CHECKPOINT_BUGTEAM_API_KEY plus CHECKPOINT_BUGTEAM_BASE_URL, "
                    "or add a backend token to model_api_keys.txt."
                ),
                "worker": {
                    **(preview.get("worker") if isinstance(preview.get("worker"), dict) else {}),
                    "requested_backend": force_backend_name,
                },
            }
    elif worker_agent_norm == "claude-code":
        worker_backend = resolve_backend_for_tier(str(selected_track.get("tier") or ""))
    initial_argv = list(worker_command["argv"])
    initial_env: dict[str, str] | None = None
    if worker_backend:
        initial_argv = _override_model_flag(initial_argv, worker_backend["model"])
        initial_env = worker_backend["env"]
        preview["worker"]["backend"] = redact_backend(worker_backend)
        if force_backend_name:
            preview["worker"]["forced_backend"] = force_backend_name
    initial_env = _apply_toolchain_policy_env(initial_env, toolchain_policy)
    if initial_env and initial_env.get("DEVPACER_TOOLCHAIN_POLICY"):
        preview["worker"]["toolchain_policy_env"] = "enabled"

    if worker_agent == "mimo":
        worker_record = _run_mimo_patch_attempt(
            workspace_root=workspace_path,
            plan_id=plan_id,
            mission_id=progress_mission_id,
            attempt="initial",
            track=selected_track,
            plan=plan,
            cwd=worktree,
            timeout_seconds=timeout_seconds,
            log_path=logs_dir / f"{track_token}-initial.log",
            backend=worker_backend,
            repo_map_text=repo_map_text,
            verification_command=effective_verification_command,
            prompt_override=report_prompt,
        )
        repair_backend = worker_backend
    else:
        worker_record = _run_worker_attempt(
            workspace_root=workspace_path,
            plan_id=plan_id,
            mission_id=progress_mission_id,
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

    # Quota failover: if a non-Codex subscription worker was blocked by a
    # usage/rate limit, continue once through the standalone MiMo patch worker
    # when a MiMo token is configured. Codex is user-explicit here; do not
    # silently substitute it with MiMo and report that as a Codex result.
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
    if quota_hit:
        # Record the quota failure so future dispatches can skip this agent
        from .agent_backends import record_quota_failure
        record_quota_failure(worker_agent_norm)
    if quota_hit and not _allow_automatic_low_cost_failover(worker_agent_norm):
        preview["worker"]["failover_disabled"] = (
            "Codex was explicitly requested; automatic MiMo failover is disabled. "
            "Retry explicitly with --agent mimo if you want MiMo."
        )
    elif quota_hit:
        failover_backend = resolve_backend_by_name("bugteam") or resolve_backend_by_name("mimo")
        if failover_backend:
            failover_name = str(failover_backend.get("name") or "backend")
            failover_track = {
                **selected_track,
                "id": f"{track_token}_{failover_name}_failover",
                "agent": failover_name,
                "model": failover_backend.get("model"),
            }
            failover_record = _run_mimo_patch_attempt(
                workspace_root=workspace_path,
                plan_id=plan_id,
                mission_id=progress_mission_id,
                attempt=f"quota_failover_{failover_name}",
                track=failover_track,
                plan=plan,
                cwd=worktree,
                timeout_seconds=timeout_seconds,
                log_path=logs_dir / f"{track_token}-quota-failover-{failover_name}.log",
                backend=failover_backend,
                repo_map_text=repo_map_text,
                verification_command=effective_verification_command,
            )
            if failover_record.get("status") == "completed":
                repair_track = failover_track
                repair_executable = ""
                repair_env = None
                repair_backend = failover_backend
        else:
            preview["worker"]["failover_disabled"] = "Low-cost backend failover unavailable: no backend token was found."

    verification_attempts: list[dict[str, Any]] = []
    if diagnosis_mode or review_plan_mode:
        # The deliverable IS the report; running seeded demo workflows against a
        # report mission produces irrelevant failures (V7 dogfood finding).
        verification = _diagnosis_verification(worktree) if diagnosis_mode else _review_plan_verification(worktree)
    else:
        verification = run_dispatch_verification(
            workspace_root=workspace_path,
            verification_workspace_root=verification_workspace,
            plan_id=plan_id,
            mission_id=progress_mission_id,
            repo_root=worktree,
            run_profile=run_profile,
            include_slow=include_slow,
            max_workflows=max_workflows,
            codex_runner=codex_runner,
            failure_evidence_builder=failure_evidence_builder,
            test_command=test_command,
            verification_env=verification_env,
            worktree_base=worktree_base,
            allow_test_edits=allow_test_edits,
        )
    verification_attempts.append(verification)

    repair_worker_record: dict[str, Any] | None = None
    if auto_repair_once and verification.get("verdict") == "fail" and _verification_is_repairable(verification):
        repair_prompt = str((verification.get("repair_brief") or {}).get("repair_prompt") or "").strip()
        if repair_prompt:
            repair_prompt_full = repair_prompt + "\n\nAfter repairing, run: " + effective_verification_command
            if canonical_agent_name(str(repair_track.get("agent") or "")) == "mimo":
                repair_worker_record = _run_mimo_patch_attempt(
                    workspace_root=workspace_path,
                    plan_id=plan_id,
                    mission_id=progress_mission_id,
                    attempt="auto_repair_once",
                    track=repair_track,
                    plan=plan,
                    cwd=worktree,
                    timeout_seconds=timeout_seconds,
                    log_path=logs_dir / f"{track_token}-auto-repair-once.log",
                    backend=repair_backend or resolve_failover_backend(),
                    repo_map_text=repo_map_text,
                    verification_command=effective_verification_command,
                    prompt_override=repair_prompt_full,
                )
            else:
                repair_command = build_worker_command(
                    plan=plan,
                    track=repair_track,
                    worktree=worktree,
                    verification_command=effective_verification_command,
                    prompt_override=repair_prompt_full,
                    phase="repair",
                    model_policy=model_policy,
                    repo_map_text=repo_map_text,
                )
                repair_argv = list(repair_command["argv"])
                if repair_executable:
                    repair_argv[0] = repair_executable
                if repair_backend:
                    repair_argv = _override_model_flag(repair_argv, repair_backend["model"])
                repair_env = _apply_toolchain_policy_env(repair_env, toolchain_policy)
                repair_worker_record = _run_worker_attempt(
                    workspace_root=workspace_path,
                    plan_id=plan_id,
                    mission_id=progress_mission_id,
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
                    mission_id=progress_mission_id,
                    repo_root=worktree,
                    run_profile=run_profile,
                    include_slow=include_slow,
                    max_workflows=max_workflows,
                    codex_runner=codex_runner,
                    failure_evidence_builder=failure_evidence_builder,
                    test_command=test_command,
                    verification_env=verification_env,
                    worktree_base=worktree_base,
                    allow_test_edits=allow_test_edits,
                )
            )

    latest_verification = verification_attempts[-1]
    effective_worker_record = (
        failover_record
        if isinstance(failover_record, dict) and failover_record.get("status") == "completed"
        else worker_record
    )
    worker_completed = effective_worker_record.get("status") == "completed"
    # Every failover hop is exhausted and the worker still hit a quota wall:
    # surface that honestly instead of letting the gate failure masquerade as
    # a code problem ("same_failure_repeated" on an untouched worktree).
    # Check both the primary worker and any failover for quota exhaustion signs.
    primary_quota = looks_like_quota_exhaustion(worker_record.get("stdout_tail"), worker_record.get("stderr_tail"))
    failover_quota = (
        isinstance(failover_record, dict)
        and failover_record.get("status") != "completed"
        and looks_like_quota_exhaustion(failover_record.get("stdout_tail"), failover_record.get("stderr_tail"))
    )
    quota_exhausted = not worker_completed and (primary_quota or failover_quota)
    has_product_changes = _worktree_has_product_changes(worktree)
    has_prior_verified = bool(
        allow_prior_verified_evidence and _mission_has_prior_verified_evidence(workspace_path, plan_id)
    )
    toolchain_violation = _first_worker_toolchain_violation(
        [worker_record, failover_record, repair_worker_record],
        test_command or verification_command,
    )
    status = "completed" if worker_completed else "worker_failed"
    latest_verdict = str(latest_verification.get("verdict") or "")
    verification_changed = latest_verification.get("changed_files")
    if not isinstance(verification_changed, list):
        diff = latest_verification.get("diff_summary") if isinstance(latest_verification.get("diff_summary"), dict) else {}
        verification_changed = diff.get("changed_files") if isinstance(diff.get("changed_files"), list) else []
    has_change_evidence = has_product_changes or bool(verification_changed) or has_prior_verified
    report_deliverable_verified = bool(diagnosis_mode or review_plan_mode) and latest_verdict == "pass"
    if toolchain_violation and latest_verdict == "pass":
        status = "verified_blocked"
    elif toolchain_violation:
        status = "worker_toolchain_violation"
    elif latest_verdict == "fail":
        status = "verification_failed"
    elif report_deliverable_verified:
        status = "verified"
    elif latest_verdict == "pass" and worker_completed and has_change_evidence:
        status = "verified"
    elif latest_verdict == "pass" and worker_completed:
        status = "no_product_changes"
    elif latest_verdict == "pass" and has_prior_verified:
        status = "verified"
    elif latest_verdict == "pass" and not worker_completed and has_product_changes:
        status = "worker_failed_tests_pass"
    elif latest_verdict == "pass":
        status = "worker_failed"
    elif latest_verdict in {"coverage_gap", "no_workflows", "no_changed_files"}:
        status = "coverage_gap"
    elif latest_verdict == "inspection_only":
        # Inspection-only proves the page rendered, not that the product works.
        # It must never surface as verified (principle: dry-run cannot pose as
        # real acceptance).
        status = "inspection_only"

    # Clear quota failure record if the task succeeded (verified or merged)
    if status in {"verified", "merged"}:
        from .agent_backends import clear_quota_failure
        clear_quota_failure(worker_agent_norm)

    # Close the loop: merge the worker's isolated branch back only when the
    # change is genuinely verified. Verification is the sole gate — never merge
    # anything that did not pass, and abort on any conflict rather than force it.
    merge_result: dict[str, Any] | None = None
    if merge:
        if status == "verified" and worker_completed:
            merge_result = merge_worktree_branch(
                repo_root=repo_root,
                worktree=worktree,
                branch=branch,
                message=f"{plan.get('objective') or 'DevPacer change'} [plan {plan_id}]",
            )
        elif status == "verified":
            merge_result = {
                "status": "skipped",
                "reason": "not merged: worker did not complete normally, so verified evidence requires manual review.",
            }
        else:
            merge_result = {"status": "skipped", "reason": f"not merged: status is {status}, not verified."}
        if (
            merge_result.get("status") in {"merged", "nothing_to_merge"}
            and str(test_command or "").strip()
        ):
            post_merge_verification = _run_post_merge_command_verification(
                workspace_root=workspace_path,
                plan_id=plan_id,
                mission_id=progress_mission_id,
                repo_root=repo_root,
                command=effective_verification_command,
                verification_env=verification_env,
            )
            merge_result["post_merge_verification"] = {
                "verdict": post_merge_verification.get("verdict"),
                "failure_kind": (
                    (post_merge_verification.get("command_verification") or {}).get("failure_kind")
                    if isinstance(post_merge_verification.get("command_verification"), dict)
                    else ""
                ),
                "saved_path": post_merge_verification.get("saved_path"),
            }
            verification_attempts.append(post_merge_verification)
            latest_verification = post_merge_verification
            if post_merge_verification.get("verdict") != "pass":
                status = "merged_verification_failed"

    payload = {
        **preview,
        "status": status,
        "dry_run": False,
        "quota_exhausted": quota_exhausted,
        "worker_record": worker_record,
        "failover_worker_record": failover_record,
        "repair_worker_record": repair_worker_record,
        "toolchain_violation": toolchain_violation,
        "verification_attempts": verification_attempts,
        "latest_verification": latest_verification,
        "usage_summary": summarize_worker_usage(
            [worker_record, failover_record, repair_worker_record]
        ),
    }
    if diagnosis_mode and latest_verification.get("diagnosis_report"):
        payload["diagnosis_report"] = latest_verification["diagnosis_report"]
    if review_plan_mode and latest_verification.get("review_plan_report"):
        payload["review_plan_report"] = latest_verification["review_plan_report"]
    if quota_exhausted:
        payload.setdefault("warnings", []).append(
            "Worker hit a subscription quota/usage limit and no failover completed."
            " The task itself was not attempted; retry after the quota resets."
        )
    if merge_result is not None:
        payload["merge"] = merge_result
    if status == "worker_failed_tests_pass":
        payload.setdefault("warnings", []).append(
            "Worker did not complete normally, but the current worktree changes passed the "
            "test command. Inspect the worktree manually before merging."
        )
    return payload


def _run_post_merge_command_verification(
    *,
    workspace_root: Path,
    plan_id: str,
    mission_id: str,
    repo_root: Path,
    command: str,
    verification_env: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    command_result = run_command_verification(
        command=command,
        repo_root=repo_root,
        verification_env=verification_env,
    )
    verdict = "pass" if command_result.get("verdict") == "pass" else "fail"
    payload: dict[str, Any] = {
        "plan_id": plan_id,
        "repo_root": str(repo_root),
        "workspace_root": str(workspace_root),
        "run_profile": "post-merge",
        "passed": 1 if verdict == "pass" else 0,
        "inspection_only": 0,
        "failed": 0 if verdict == "pass" else 1,
        "total": 1,
        "verdict": verdict,
        "command_verification": command_result,
        "markdown": (
            f"Post-merge test command `{command}` passed (exit 0)."
            if verdict == "pass"
            else f"Post-merge test command failed: `{command}` (exit {command_result.get('exit_code')})."
        ),
        "recorded_at": now,
    }
    if verdict == "fail":
        payload["repair_brief"] = command_repair_brief(command_result)
    saved = save_verification(workspace_root, plan_id, payload)
    payload["saved_path"] = saved["path"]
    save_mission_progress(
        workspace_root,
        mission_id,
        stage="post_merge_verification_passed" if verdict == "pass" else "post_merge_verification_failed",
        stage_label="Post-merge verification passed" if verdict == "pass" else "Post-merge verification failed",
        verification_verdict=verdict,
        verification_command=command,
        blocker="" if verdict == "pass" else str(command_result.get("failure_kind") or "post_merge_verification_failed"),
    )
    return payload


def _verification_is_repairable(verification: dict[str, Any]) -> bool:
    repair = verification.get("repair_brief") if isinstance(verification.get("repair_brief"), dict) else {}
    source = str(repair.get("source") or "")
    failure_kind = str(repair.get("failure_kind") or "")
    if source in {"test_tampering", "acceptance_chain_tampering"}:
        return False
    if failure_kind in NON_REPAIRABLE_COMMAND_FAILURE_KINDS:
        return False
    return True


def _worktree_has_product_changes(worktree: Path) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(worktree), "-c", "core.quotePath=false", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    for raw in completed.stdout.splitlines():
        path = raw[3:].replace("\\", "/").strip()
        if not path:
            continue
        if path.startswith(".agent-workspace/") or path == ".visual-agent-status.md":
            continue
        if is_runtime_changed_file(path, repo_root=worktree):
            continue
        if is_test_path(path):
            continue
        if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in _ARTIFACT_DIR_PREFIXES):
            continue
        if _is_tool_generated_change(worktree, path):
            continue
        # Filter out Python cache and compiled files
        if any(path.startswith(prefix) for prefix in _CACHE_DIR_PREFIXES):
            continue
        basename = path.rsplit("/", 1)[-1]
        if any(basename.endswith(ext) for ext in _CACHE_EXTENSIONS):
            continue
        return True
    return False


def _is_tool_generated_change(repo_root: Path, path: str) -> bool:
    normalized = str(path).replace("\\", "/").strip().lstrip("/")
    if not normalized:
        return True
    if normalized in _GENERATED_NOISE_PATHS:
        return True
    if normalized == ".gitignore":
        return _gitignore_change_is_only_devpacer_block(repo_root)
    return False


def _gitignore_change_is_only_devpacer_block(repo_root: Path) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--", ".gitignore"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    added = []
    for raw in completed.stdout.splitlines():
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            added.append(raw[1:].strip())
        elif raw.startswith("-"):
            return False
    if not added:
        return False
    allowed = {
        "# Auto-generated by DevPacer for this worktree - safe to commit",
        "# Auto-generated by DevPacer for this worktree — safe to commit",
        "__pycache__/",
        "*.pyc",
        "*.pyo",
        ".pytest_cache/",
        "*.egg-info/",
        ".eggs/",
        "dist/",
        "build/",
        "node_modules/",
        ".cache/",
        ".npm-cache/",
        ".yarn/",
        ".pnpm-store/",
        "coverage/",
        ".dart_tool/",
        ".dart-home/",
    }
    return all(line in allowed for line in added)


def _mission_has_prior_verified_evidence(workspace_root: Path, plan_id: str) -> bool:
    rounds_path = workspace_root / "missions" / str(plan_id) / "rounds.jsonl"
    if not rounds_path.exists():
        return False
    try:
        lines = rounds_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for raw in lines:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        round_type = str(record.get("type") or "")
        status = str(record.get("status") or "")
        if round_type == "verification" and status == "pass":
            return True
        if round_type == "merge" and status in {"merged", "nothing_to_merge"}:
            return True
    return False


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


_DIAGNOSIS_REPORT_NAMES = ("诊断报告.md", "DIAGNOSIS.md", "diagnosis.md", "诊断.md")
_REVIEW_PLAN_REPORT_NAMES = ("审查与开发计划.md", "REVIEW_AND_PLAN.md", "review_and_plan.md", "开发计划.md", "审查报告.md")


def _diagnosis_verification(worktree: Path) -> dict[str, Any]:
    """Acceptance for diagnosis missions: the worker must have written a
    non-trivial root-cause report file. Deterministic — no workflows, no LLM."""
    for name in _DIAGNOSIS_REPORT_NAMES:
        path = worktree / name
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if len(text) >= 80:
                return {
                    "verdict": "pass",
                    "passed": 1,
                    "failed": 0,
                    "total": 1,
                    "run_profile": "diagnosis",
                    "diagnosis_report": text[:6000],
                    "diagnosis_report_path": str(path),
                }
    return {
        "verdict": "fail",
        "passed": 0,
        "failed": 1,
        "total": 1,
        "run_profile": "diagnosis",
        "reason": "worker 未产出 诊断报告.md（或内容过短），无法交付诊断结论。",
    }


def _review_plan_verification(worktree: Path) -> dict[str, Any]:
    """Acceptance for review/plan missions: the worker must have written a
    concrete markdown review and development plan. Deterministic — no workflows,
    no LLM."""
    for name in _REVIEW_PLAN_REPORT_NAMES:
        path = worktree / name
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if len(text) >= 120:
                return {
                    "verdict": "pass",
                    "passed": 1,
                    "failed": 0,
                    "total": 1,
                    "run_profile": "review_plan",
                    "review_plan_report": text[:10000],
                    "review_plan_report_path": str(path),
                }
    return {
        "verdict": "fail",
        "passed": 0,
        "failed": 1,
        "total": 1,
        "run_profile": "review_plan",
        "reason": "worker 未产出 审查与开发计划.md（或内容过短），无法交付审查和开发计划。",
    }


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
    # exemption in run_chief_mission. Report missions are exempt too: their
    # deliverable is a markdown report, not a verifiable code change.
    from .chief_engineer import is_diagnosis_goal
    from .mission_intake import is_review_plan_goal

    if (
        plan_status == "needs_workflow_coverage"
        and not allow_coverage_gap
        and not has_test_command
        and not is_diagnosis_goal(str(plan.get("objective") or ""))
        and not is_review_plan_goal(str(plan.get("objective") or ""))
    ):
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
    prompt_suffix: str | None = None,
) -> dict[str, Any]:
    agent = canonical_agent_name(str(track.get("agent") or "codex"))
    profile = load_agent_profile(agent) or {}
    task_kind = _task_kind_for_phase(phase, model_policy)
    config = recommend_worker_config(profile, task_kind=task_kind) if profile else {}
    prompt = prompt_override or build_worker_prompt(plan=plan, track=track, worktree=worktree, verification_command=verification_command)
    if str(prompt_suffix or "").strip():
        prompt = prompt.rstrip() + "\n\n" + str(prompt_suffix).strip()
    # Every attempt (including repair) is a fresh model session; the map is how
    # the worker orients without re-reading the repository each round.
    if repo_map_text:
        prompt = (
            prompt
            + "\n\nRepository map (DevPacer local index excerpt, current at dispatch; use it for"
            " orientation, then verify the real paths you need):\n"
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
    objective_paths = [
        item for item in _extract_context_paths(str(plan.get("objective") or ""))
        if not _is_ignored_context_path(item) and not _is_non_product_path(item)
    ]
    changed = plan.get("changed_files") if isinstance(plan.get("changed_files"), list) else []
    scoped: list[str] = []
    for item in [*objective_paths, *(str(raw) for raw in changed)]:
        normalized = str(item).replace("\\", "/").strip()
        if not normalized or normalized in scoped:
            continue
        if normalized.startswith(".agent-workspace") or _is_ignored_context_path(normalized) or _is_non_product_path(normalized):
            continue
        scoped.append(normalized)
        if len(scoped) >= 12:
            break
    if scoped:
        lines.append("Touch only the files this task requires; target files in scope are:")
        lines.extend(f"- {item}" for item in scoped)
    else:
        lines.append(
            "No trusted target file was inferred from the objective. Do not edit archive,"
            " graveyard, checkpoint worktree, or third-party sample directories unless the objective names them."
        )
    lines.extend(
        [
            "Work efficiently to conserve model budget: read only the files you need for this objective;"
            " do not scan or grep the whole repository, and do not refactor unrelated code.",
        ]
    )
    toolchain_policy = _toolchain_policy_for_command(verification_command)
    if toolchain_policy:
        forbidden = ", ".join(str(item) for item in toolchain_policy.get("forbidden_paths", [])[:4])
        lines.extend(
            [
                "Toolchain policy: reuse that exact toolchain; use the exact SDK executable named by verification for related local commands.",
                f"Expected Dart executable: {toolchain_policy.get('expected_executable')}",
                f"Forbidden sibling wrappers/paths: {forbidden}",
                "For Flutter/Dart tasks, do not call bare `dart` or `flutter` when verification names a specific SDK executable.",
                r"Do not derive sibling SDK paths: not `...\bin\dart.exe`, `...\bin\dart.bat`, or a PATH-resolved wrapper.",
                "If worker logs include any forbidden path, DevPacer will withhold verified/merge even if the final command passes.",
            ]
        )
    else:
        lines.append("If the verification command contains an absolute tool path, reuse that exact toolchain for related local commands.")
    lines.extend(
        [
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
        "python -m visual_agent.cli codex-check"
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
    mission_id: str | None = None,
    repo_root: Path,
    run_profile: str,
    include_slow: bool,
    max_workflows: int,
    codex_runner: Any = None,
    failure_evidence_builder: Any = None,
    test_command: str | None = None,
    verification_env: list[dict[str, Any]] | None = None,
    worktree_base: str | None = None,
    allow_test_edits: bool = False,
) -> dict[str, Any]:
    verification_workspace_path = (verification_workspace_root or workspace_root).expanduser().resolve()
    progress_mission_id = str(mission_id or plan_id)
    save_mission_progress(
        workspace_root,
        progress_mission_id,
        stage="verification_running",
        stage_label="Verification running",
        status="running",
        plan_id=plan_id,
        worktree=str(Path(repo_root).expanduser().resolve()),
    )
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
            save_mission_progress(
                workspace_root,
                progress_mission_id,
                stage="verification_failed",
                stage_label="Verification failed",
                verification_verdict="fail",
                blocker="test_tampering",
            )
            return payload
        chain_tampered = changed_acceptance_chain_files(
            repo_root=Path(repo_root),
            command=test_command,
            base_ref=worktree_base,
        )
        if chain_tampered and not allow_test_edits:
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
                "tampered_acceptance_chain_files": chain_tampered,
                "repair_brief": acceptance_chain_repair_brief(chain_tampered, base_ref=worktree_base),
                "markdown": (
                    "Verification refused: worker modified acceptance-chain file(s) "
                    f"{', '.join(chain_tampered[:5])}."
                    " These files define the command gate; pass --allow-test-edits only when"
                    " the task is explicitly about changing tests or test configuration."
                ),
                "recorded_at": now,
            }
            saved = save_verification(workspace_root, plan_id, payload)
            payload["saved_path"] = saved["path"]
            save_mission_progress(
                workspace_root,
                progress_mission_id,
                stage="verification_failed",
                stage_label="Verification failed",
                verification_verdict="fail",
                blocker="acceptance_chain_tampering",
            )
            return payload
        command_result = run_command_verification(
            command=test_command,
            repo_root=Path(repo_root),
            verification_env=verification_env,
        )
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
            "acceptance_chain_files_changed": chain_tampered,
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
        save_mission_progress(
            workspace_root,
            progress_mission_id,
            stage="verification_passed" if verdict == "pass" else "verification_failed",
            stage_label="Verification passed" if verdict == "pass" else "Verification failed",
            verification_verdict=verdict,
            verification_command=str(command_result.get("command") or ""),
            blocker="" if verdict == "pass" else str(command_result.get("failure_kind") or "command_failed"),
        )
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
    coverage_status = str((result.coverage or {}).get("status") or "")
    if coverage_status in {"fallback_only", "no_changed_files"} and verdict != "fail":
        verdict = "coverage_gap"
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
    save_mission_progress(
        workspace_root,
        progress_mission_id,
        stage="verification_passed" if verdict == "pass" else "verification_failed",
        stage_label="Verification passed" if verdict == "pass" else "Verification failed",
        verification_verdict=verdict,
        verification_command=str(payload.get("command") or ""),
        blocker="" if verdict == "pass" else "workflow_verification_failed",
    )
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


def _run_mimo_patch_attempt(
    *,
    workspace_root: Path,
    plan_id: str,
    mission_id: str | None = None,
    attempt: str,
    track: dict[str, Any],
    plan: dict[str, Any],
    cwd: Path,
    timeout_seconds: float,
    log_path: Path,
    backend: dict[str, Any] | None,
    repo_map_text: str | None,
    verification_command: str,
    prompt_override: str | None = None,
) -> dict[str, Any]:
    started = monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    progress_mission_id = str(mission_id or plan_id)
    save_mission_progress(
        workspace_root,
        progress_mission_id,
        stage="worker_running",
        stage_label="Worker running",
        status="running",
        plan_id=plan_id,
        agent=str(track.get("agent") or ""),
        worktree=str(cwd),
        log_path=str(log_path),
    )
    backend = backend or resolve_failover_backend()
    if not backend:
        stderr_tail = "Low-cost backend token was not found."
        log_path.write_text(stderr_tail, encoding="utf-8")
        record = _worker_record(
            plan_id=plan_id,
            attempt=attempt,
            track=track,
            status="failed",
            exit_code=2,
            elapsed_seconds=round(monotonic() - started, 6),
            cwd=cwd,
            command="low-cost-backend patch-worker",
            log_path=log_path,
            stdout_tail="",
            stderr_tail=stderr_tail,
            backend=None,
        )
        append_worker_record(workspace_root, plan_id, record)
        save_mission_progress(
            workspace_root,
            progress_mission_id,
            stage="worker_blocked",
            stage_label="Worker blocked",
            worker_status="failed",
            blocker="backend_missing",
            log_path=str(log_path),
        )
        return record

    prompt = _build_mimo_patch_prompt(
        plan=plan,
        track=track,
        cwd=cwd,
        repo_map_text=repo_map_text or "",
        verification_command=verification_command,
        prompt_override=prompt_override,
    )
    response = ""
    exit_code = 1
    stdout_tail = ""
    stderr_tail = ""
    try:
        save_mission_progress(
            workspace_root,
            progress_mission_id,
            stage="worker_running",
            stage_label="Worker querying model",
            last_activity_at=datetime.now(timezone.utc).isoformat(),
            log_path=str(log_path),
        )
        response = run_llm_completion(
            backend=LLMBackend(
                provider=str(backend.get("provider") or "openai"),
                model_id=str(backend.get("model") or "gpt-4o-mini"),
            ),
            system_prompt=(
                "You are xiao's unattended coding worker. Return only a unified git diff. "
                "Do not explain. Do not include markdown unless it wraps the diff."
            ),
            prompt=prompt,
            max_tokens=6000,
            api_key=str((backend.get("env") or {}).get("ANTHROPIC_API_KEY") or ""),
            base_url=_mimo_openai_base_url(str((backend.get("env") or {}).get("ANTHROPIC_BASE_URL") or "")),
            endpoint="/chat/completions",
            timeout_seconds=max(30.0, min(float(timeout_seconds), 300.0)),
        )
        diff_text = _extract_unified_diff(response)
        if not diff_text:
            stderr_tail = f"{backend.get('name') or 'backend'} returned no unified diff."
        else:
            save_mission_progress(
                workspace_root,
                progress_mission_id,
                stage="worker_running",
                stage_label="Worker applying patch",
                last_activity_at=datetime.now(timezone.utc).isoformat(),
                log_path=str(log_path),
            )
            apply_result = _apply_unified_diff(cwd=cwd, diff_text=diff_text, timeout_seconds=min(float(timeout_seconds), 120.0))
            exit_code = int(apply_result.get("exit_code") or 0)
            stdout_tail = str(apply_result.get("stdout_tail") or "")[-4000:]
            stderr_tail = str(apply_result.get("stderr_tail") or "")[-4000:]
            if exit_code != 0 and _patch_failure_is_retriable(stderr_tail):
                retry_prompt = _build_mimo_patch_retry_prompt(
                    original_prompt=prompt,
                    diff_text=diff_text,
                    stderr_tail=stderr_tail,
                    cwd=cwd,
                )
                response = run_llm_completion(
                    backend=LLMBackend(
                        provider=str(backend.get("provider") or "openai"),
                        model_id=str(backend.get("model") or "gpt-4o-mini"),
                    ),
                    system_prompt=(
                        "You are xiao's patch repair worker. Return only a corrected unified git diff. "
                        "Use the current file contents in the prompt as the source of truth."
                    ),
                    prompt=retry_prompt,
                    max_tokens=6000,
                    api_key=str((backend.get("env") or {}).get("ANTHROPIC_API_KEY") or ""),
                    base_url=_mimo_openai_base_url(str((backend.get("env") or {}).get("ANTHROPIC_BASE_URL") or "")),
                    endpoint="/chat/completions",
                    timeout_seconds=max(30.0, min(float(timeout_seconds), 300.0)),
                )
                retry_diff = _extract_unified_diff(response)
                if retry_diff:
                    apply_result = _apply_unified_diff(cwd=cwd, diff_text=retry_diff, timeout_seconds=min(float(timeout_seconds), 120.0))
                    exit_code = int(apply_result.get("exit_code") or 0)
                    stdout_tail = str(apply_result.get("stdout_tail") or "")[-4000:]
                    stderr_tail = str(apply_result.get("stderr_tail") or "")[-4000:]
            if exit_code == 0:
                stdout_tail = (stdout_tail + f"\n{backend.get('name') or 'backend'} unified diff applied.").strip()
    except Exception as exc:  # noqa: BLE001
        stderr_tail = f"Low-cost backend patch worker failed: {exc}"

    log_path.write_text(
        json.dumps(
            {
                "attempt": attempt,
                "backend": redact_backend(backend),
                "response": response[-12000:],
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    record = _worker_record(
        plan_id=plan_id,
        attempt=attempt,
        track=track,
        status="completed" if exit_code == 0 else "failed",
        exit_code=exit_code,
        elapsed_seconds=round(monotonic() - started, 6),
        cwd=cwd,
        command="low-cost-backend patch-worker",
        log_path=log_path,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        backend=backend,
    )
    append_worker_record(workspace_root, plan_id, record)
    return record


def _worker_record(
    *,
    plan_id: str,
    attempt: str,
    track: dict[str, Any],
    status: str,
    exit_code: int,
    elapsed_seconds: float,
    cwd: Path,
    command: str,
    log_path: Path,
    stdout_tail: str,
    stderr_tail: str,
    backend: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": 1,
        "plan_id": plan_id,
        "attempt": attempt,
        "track_id": str(track.get("id") or ""),
        "agent": str(track.get("agent") or ""),
        "status": status,
        "exit_code": int(exit_code),
        "elapsed_seconds": elapsed_seconds,
        "cwd": str(cwd),
        "command": command,
        "log_path": str(log_path),
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }
    if backend:
        record["backend"] = {"name": backend.get("name"), "model": backend.get("model")}
        if backend.get("cost_is_savings"):
            record["usage"] = {"cost_is_savings": True, "backend": backend.get("name")}
    return record


def _first_worker_toolchain_violation(records: list[dict[str, Any] | None], command: str | None) -> dict[str, str] | None:
    expected = _extract_dart_sdk_executable(command)
    if not expected:
        return None
    for record in records:
        if not isinstance(record, dict):
            continue
        violation = _worker_toolchain_violation(record, expected)
        if violation:
            return violation
    return None


def _toolchain_policy_for_command(command: str | None) -> dict[str, Any]:
    expected = _extract_dart_sdk_executable(command)
    if not expected:
        return {}
    forbidden = _forbidden_dart_sibling_paths(expected)
    return {
        "status": "active",
        "kind": "dart_exact_sdk_executable",
        "expected_executable": expected,
        "forbidden_paths": forbidden,
        "message": (
            "Dart/Flutter commands must use the exact dart.exe from the verification command; "
            "sibling Flutter wrapper paths block verified/merge."
        ),
    }


def _toolchain_preflight_for_command(command: str | None, *, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = policy if isinstance(policy, dict) else _toolchain_policy_for_command(command)
    if not policy:
        return {"status": "none"}
    text = _normalize_toolchain_text(command or "")
    expected = _normalize_toolchain_text(str(policy.get("expected_executable") or ""))
    for raw in policy.get("forbidden_paths") or []:
        forbidden = _normalize_toolchain_text(str(raw))
        if forbidden and forbidden in text and forbidden != expected:
            return {
                "status": "blocked",
                "expected_executable": str(policy.get("expected_executable") or ""),
                "forbidden_path": str(raw),
                "message": (
                    "Toolchain preflight blocked dispatch: the verification command itself contains "
                    "a forbidden sibling Flutter/Dart wrapper path."
                ),
            }
    return {
        "status": "active",
        "expected_executable": str(policy.get("expected_executable") or ""),
        "forbidden_paths": list(policy.get("forbidden_paths") or []),
    }


def _apply_toolchain_policy_env(env: dict[str, str] | None, policy: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(policy, dict) or not policy.get("expected_executable"):
        return env
    merged = {str(k): str(v) for k, v in (env or {}).items()}
    base_path = str(merged.get("PATH") or merged.get("Path") or os.environ.get("PATH") or "")
    forbidden_dirs = {
        str(Path(str(path)).parent)
        for path in policy.get("forbidden_paths") or []
        if str(path).strip()
    }
    if base_path and forbidden_dirs:
        normalized_forbidden = {_normalize_toolchain_text(item).rstrip("/") for item in forbidden_dirs}
        kept_parts: list[str] = []
        for part in base_path.split(os.pathsep):
            normalized_part = _normalize_toolchain_text(part).rstrip("/")
            if normalized_part and normalized_part in normalized_forbidden:
                continue
            kept_parts.append(part)
        merged["PATH"] = os.pathsep.join(kept_parts)
        merged.pop("Path", None)
    merged["DEVPACER_TOOLCHAIN_POLICY"] = str(policy.get("kind") or "active")
    merged["DEVPACER_EXPECTED_DART_EXE"] = str(policy.get("expected_executable") or "")
    merged["DEVPACER_FORBIDDEN_TOOLCHAIN_PATHS"] = os.pathsep.join(str(item) for item in policy.get("forbidden_paths") or [])
    return merged


def _extract_dart_sdk_executable(command: str | None) -> str:
    text = str(command or "")
    match = re.search(
        r"([A-Za-z]:[\\/][^\r\n\"']*?[\\/]bin[\\/]cache[\\/]dart-sdk[\\/]bin[\\/]dart\.exe)",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _worker_toolchain_violation(record: dict[str, Any], expected_executable: str) -> dict[str, str] | None:
    log_path = Path(str(record.get("log_path") or ""))
    if not log_path.is_file():
        return None
    try:
        raw = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = _normalize_toolchain_text(raw)
    expected = _normalize_toolchain_text(expected_executable)
    marker = "/bin/cache/dart-sdk/bin/dart.exe"
    if marker not in expected:
        return None
    for raw in _forbidden_dart_sibling_paths(expected_executable):
        path = _normalize_toolchain_text(raw)
        if path in text:
            return {
                "status": "violated",
                "expected_executable": expected_executable,
                "forbidden_path": raw,
                "log_path": str(log_path),
                "message": "Worker used a sibling Flutter wrapper instead of the exact Dart SDK executable from the verification command.",
            }
    return None


def _forbidden_dart_sibling_paths(expected_executable: str) -> list[str]:
    expected = _normalize_toolchain_text(expected_executable)
    marker = "/bin/cache/dart-sdk/bin/dart.exe"
    if marker not in expected:
        return []
    flutter_root = expected.split(marker, 1)[0]
    return [
        f"{flutter_root}/bin/dart.exe".replace("/", "\\"),
        f"{flutter_root}/bin/dart.bat".replace("/", "\\"),
        f"{flutter_root}/bin/flutter.exe".replace("/", "\\"),
        f"{flutter_root}/bin/flutter.bat".replace("/", "\\"),
    ]


def _normalize_toolchain_text(value: str) -> str:
    return str(value or "").replace("\\\\", "\\").replace("\\", "/").lower()


def _build_mimo_patch_prompt(
    *,
    plan: dict[str, Any],
    track: dict[str, Any],
    cwd: Path,
    repo_map_text: str,
    verification_command: str,
    prompt_override: str | None = None,
) -> str:
    context = _mimo_file_context(cwd=cwd, plan=plan, extra_text=prompt_override or "")
    objective = str(prompt_override or plan.get("objective") or "")
    policy = _toolchain_policy_for_command(verification_command)
    toolchain_block = ""
    if policy:
        toolchain_block = (
            "\nToolchain policy:\n"
            f"- Expected Dart executable: {policy.get('expected_executable')}\n"
            f"- Forbidden sibling wrappers/paths: {', '.join(str(item) for item in policy.get('forbidden_paths', [])[:4])}\n"
            "- If logs or diff instructions use a forbidden path, DevPacer withholds verified/merge.\n"
        )
    return (
        f"Objective:\n{objective}\n\n"
        f"Plan id: {plan.get('plan_id') or ''}\n"
        f"Worker track: {track.get('id') or ''} ({track.get('agent') or ''})\n"
        f"Repository root: {cwd}\n"
        "Rules:\n"
        "- Produce the smallest useful code change for the objective.\n"
        "- Return only a unified git diff with paths relative to the repository root.\n"
        "- Do not edit .agent-workspace, .visual-agent-status.md, or test artifacts unless the objective explicitly asks for tests.\n"
        "- If no safe change is possible from the provided context, return no diff.\n\n"
        f"Verification command to satisfy after patch:\n{verification_command}\n\n"
        f"{toolchain_block}"
        f"Repository map:\n{repo_map_text[:12000]}\n\n"
        f"File context:\n{context}"
    )


def _mimo_file_context(*, cwd: Path, plan: dict[str, Any], extra_text: str = "", max_chars: int = 52000) -> str:
    candidates: list[str] = []
    for raw in plan.get("changed_files") or []:
        if isinstance(raw, str):
            candidates.append(raw)
    candidates.extend(_extract_context_paths(str(plan.get("objective") or "")))
    candidates.extend(_extract_context_paths(extra_text))
    candidates.extend(["README.md", "package.json", "pubspec.yaml", "lib/main.dart", "src/app.js", "src/server.js", "app.js", "server.js"])
    seen: set[str] = set()
    chunks: list[str] = []
    used = 0
    for rel in candidates:
        normalized = str(rel).replace("\\", "/").strip().lstrip("/")
        if not normalized or normalized in seen or _is_ignored_context_path(normalized):
            continue
        seen.add(normalized)
        path = cwd / normalized
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        budget = max_chars - used
        if budget <= 0:
            break
        snippet = text[: min(len(text), budget, 12000)]
        chunks.append(f"\n--- {normalized} ---\n{snippet}")
        used += len(snippet)
    return "\n".join(chunks) if chunks else "(No small text files were selected for context.)"


def _extract_context_paths(text: str) -> list[str]:
    pattern = r"(?:(?:[A-Za-z]:)?[\\/])?([A-Za-z0-9_.@()-]+(?:[\\/][A-Za-z0-9_.@()-]+)+)"
    paths: list[str] = []
    for match in re.finditer(pattern, text):
        raw = match.group(1).replace("\\", "/")
        if "." not in raw.rsplit("/", 1)[-1]:
            continue
        paths.append(raw)
    return paths


def _build_mimo_patch_retry_prompt(*, original_prompt: str, diff_text: str, stderr_tail: str, cwd: Path) -> str:
    paths = _extract_diff_paths(diff_text)
    file_context = _file_context_for_paths(cwd=cwd, paths=paths, max_chars=42000)
    base_prompt = str(original_prompt or "").split("\n\nFile context:\n", 1)[0]
    return (
        base_prompt
        + "\n\nPATCH RETRY RULES:\n"
        + "- The previous diff failed to apply. Generate a smaller corrected diff against the CURRENT file contents below.\n"
        + "- Treat the current target file contents as the source of truth; ignore any earlier file snippets or hunk line numbers.\n"
        + "- Return only a unified git diff with paths relative to the repository root.\n"
        + "\n\nCurrent target file contents:\n"
        + file_context
        + "\n\nGit apply error:\n"
        + stderr_tail[-4000:]
        + "\n\nPrevious failed diff, for intent only:\n"
        + diff_text[-12000:]
    )


def _extract_diff_paths(diff_text: str) -> list[str]:
    paths: list[str] = []
    for line in str(diff_text or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                for raw in parts[2:4]:
                    cleaned = raw[2:] if raw.startswith(("a/", "b/")) else raw
                    if cleaned != "/dev/null":
                        paths.append(cleaned)
        elif line.startswith(("--- a/", "+++ b/")):
            raw = line.split(maxsplit=1)[1]
            paths.append(raw[2:])
    kept: list[str] = []
    seen: set[str] = set()
    for path in paths:
        norm = path.replace("\\", "/").strip()
        if norm and norm not in seen:
            seen.add(norm)
            kept.append(norm)
    return kept


def _file_context_for_paths(*, cwd: Path, paths: list[str], max_chars: int) -> str:
    chunks: list[str] = []
    used = 0
    for rel in paths:
        normalized = str(rel).replace("\\", "/").strip().lstrip("/")
        if not normalized or _is_ignored_context_path(normalized):
            continue
        path = cwd / normalized
        if not path.exists():
            chunks.append(f"\n--- {normalized} ---\n(FILE DOES NOT EXIST)")
            continue
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        budget = max_chars - used
        if budget <= 0:
            break
        snippet = text[: min(len(text), budget, 14000)]
        chunks.append(f"\n--- {normalized} ---\n{snippet}")
        used += len(snippet)
    return "\n".join(chunks) if chunks else "(No target file context found.)"


def _mimo_openai_base_url(raw_base_url: str) -> str:
    value = str(raw_base_url or "").strip().rstrip("/")
    if not value:
        return "https://api.xiaomimimo.com/v1"
    if value.endswith("/anthropic"):
        return value[: -len("/anthropic")] + "/v1"
    return value


def _is_ignored_context_path(path: str) -> bool:
    lowered = path.lower()
    ignored = ("node_modules/", ".git/", ".agent-workspace/", "artifacts/", "dist/", "build/", ".venv/", "__pycache__/")
    return any(lowered == prefix.rstrip("/") or lowered.startswith(prefix) for prefix in ignored)


def _extract_unified_diff(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""

    # Handle code blocks that wrap the diff (e.g., ```diff ... ``` or ``` ... ```)
    # Be careful with nested code blocks inside the diff itself.
    if raw.startswith("```"):
        # Find the first newline after the opening code fence
        first_newline = raw.find("\n", 3)
        if first_newline > 0:
            # Check if there's a language specifier (e.g., "diff")
            lang = raw[3:first_newline].strip().lower()
            # Find the closing code fence - look for ``` at the start of a line
            # Skip the first ``` and look for the next one that's on its own line
            rest = raw[first_newline + 1:]
            # Find closing ``` - it should be on its own line (or at the end)
            lines = rest.split("\n")
            end_idx = -1
            for i, line in enumerate(lines):
                if line.strip() == "```":
                    end_idx = i
                    break
            if end_idx > 0:
                content = "\n".join(lines[:end_idx]).strip()
                if content.lower().startswith("diff\n"):
                    content = content.split("\n", 1)[1].strip()
                if "diff --git " in content or content.startswith("--- "):
                    return content + "\n"

    # Fallback: look for diff patterns in the raw text
    idx = raw.find("diff --git ")
    if idx >= 0:
        return raw[idx:].strip() + "\n"
    if raw.startswith("--- ") and "\n+++ " in raw:
        return raw + "\n"

    # Last resort: try to extract from code blocks more aggressively
    if "```" in raw:
        # Find all code blocks and try each one
        parts = raw.split("```")
        for i, part in enumerate(parts):
            if i % 2 == 1:  # Odd indices are inside code blocks
                candidate = part.strip()
                if candidate.lower().startswith("diff\n"):
                    candidate = candidate.split("\n", 1)[1].strip()
                if candidate.startswith("--- ") and "\n+++ " in candidate:
                    return candidate + "\n"

    return ""


def _apply_unified_diff(*, cwd: Path, diff_text: str, timeout_seconds: float) -> dict[str, Any]:
    # If the diff tries to create a new file that already exists, convert it to a modification diff.
    # This handles the case where MiMo generates a "new file" diff for an existing file.
    diff_text = _fix_new_file_diff_for_existing_files(cwd, diff_text)

    proc = _git_apply(cwd=cwd, diff_text=diff_text, timeout_seconds=timeout_seconds, extra_args=[])
    if proc.returncode != 0 and ("corrupt patch" in (proc.stderr or "").lower() or "patch does not apply" in (proc.stderr or "").lower()):
        retry = _git_apply(cwd=cwd, diff_text=diff_text, timeout_seconds=timeout_seconds, extra_args=["--recount"])
        if retry.returncode == 0:
            proc = retry
        else:
            proc = retry
    if proc.returncode != 0 and _patch_failure_is_retriable(str(proc.stderr or "")):
        retry = _git_apply(cwd=cwd, diff_text=diff_text, timeout_seconds=timeout_seconds, extra_args=["--3way"])
        if retry.returncode == 0:
            proc = retry
    return {
        "exit_code": int(proc.returncode),
        "stdout_tail": str(proc.stdout or "")[-4000:],
        "stderr_tail": str(proc.stderr or "")[-4000:],
    }


def _fix_new_file_diff_for_existing_files(cwd: Path, diff_text: str) -> str:
    """Convert 'new file' diffs to 'modification' diffs when the target file already exists.

    This handles the case where an LLM generates a diff with '--- /dev/null' for a
    file that already exists in the working directory."""
    import re

    lines = diff_text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Check for "new file mode" pattern
        if line.strip() == 'new file mode 100644':
            # Look back to find the diff header
            header_idx = len(result) - 1
            while header_idx >= 0 and not result[header_idx].startswith('diff --git'):
                header_idx -= 1
            if header_idx >= 0:
                # Extract the file path from the diff header
                diff_header = result[header_idx]
                match = re.search(r'b/(.+)$', diff_header)
                if match:
                    file_path = match.group(1)
                    full_path = cwd / file_path
                    if full_path.exists():
                        # Skip the "new file mode" line and convert "--- /dev/null" to "--- a/file"
                        i += 1
                        if i < len(lines) and lines[i].startswith('--- /dev/null'):
                            result.append(f'--- a/{file_path}')
                            i += 1
                            if i < len(lines) and lines[i].startswith('+++ b/'):
                                result.append(lines[i])
                                i += 1
                            continue
        result.append(line)
        i += 1
    return '\n'.join(result)


def _patch_failure_is_retriable(stderr: str) -> bool:
    blob = str(stderr or "").lower()
    return any(marker in blob for marker in ("patch does not apply", "corrupt patch", "patch failed"))


def _git_apply(*, cwd: Path, diff_text: str, timeout_seconds: float, extra_args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "apply", "--whitespace=nowarn", *extra_args, "-"],
        cwd=str(cwd),
        input=diff_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=float(timeout_seconds),
        check=False,
    )


def _run_worker_attempt(
    *,
    workspace_root: Path,
    plan_id: str,
    mission_id: str | None = None,
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
    progress_mission_id = str(mission_id or plan_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    save_mission_progress(
        workspace_root,
        progress_mission_id,
        stage="worker_running",
        stage_label="Worker running",
        status="running",
        plan_id=plan_id,
        agent=str(track.get("agent") or ""),
        worktree=str(cwd),
        log_path=str(log_path),
    )

    def progress_callback(event: dict[str, Any]) -> None:
        record_worker_output(
            workspace_root,
            progress_mission_id,
            stream=str(event.get("stream") or ""),
            chunk=str(event.get("chunk") or ""),
            log_path=log_path,
        )

    if stdin_text is not None:
        try:
            result = runner(argv, cwd, timeout_seconds, log_path, env=env, stdin_text=stdin_text, progress_callback=progress_callback)
        except TypeError:
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
            result = runner(argv, cwd, timeout_seconds, log_path, env=env, progress_callback=progress_callback)
        except TypeError:
            try:
                result = runner(argv, cwd, timeout_seconds, log_path, env=env)
            except TypeError:
                result = runner(argv, cwd, timeout_seconds, log_path)
    exit_code = int(result.get("exit_code") or 0)
    stdout_tail = str(result.get("stdout_tail") or "")
    stderr_tail = str(result.get("stderr_tail") or "")
    status = "completed" if exit_code == 0 else "failed"
    blocked_reason = _worker_blocked_reason(stdout_tail=stdout_tail, stderr_tail=stderr_tail, log_path=log_path)
    if blocked_reason:
        status = "blocked"
    record = {
        "schema_version": 1,
        "plan_id": plan_id,
        "attempt": attempt,
        "track_id": str(track.get("id") or ""),
        "agent": str(track.get("agent") or ""),
        "status": status,
        "exit_code": exit_code,
        "elapsed_seconds": round(monotonic() - started, 6),
        "cwd": str(cwd),
        "command": format_argv(argv),
        "log_path": str(log_path),
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
    }
    if blocked_reason:
        record["blocked_reason"] = blocked_reason
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
    save_mission_progress(
        workspace_root,
        progress_mission_id,
        stage="worker_completed" if record["status"] == "completed" else "worker_blocked",
        stage_label="Worker completed" if record["status"] == "completed" else "Worker blocked",
        worker_status=record["status"],
        worker_exit_code=record["exit_code"],
        blocker="" if record["status"] == "completed" else (stderr_tail or "worker_failed")[:500],
        log_path=str(log_path),
        worktree=str(cwd),
    )
    return record


def _worker_blocked_reason(*, stdout_tail: str, stderr_tail: str, log_path: Path) -> str:
    text = "\n".join([stdout_tail or "", stderr_tail or ""])
    if not text.strip():
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    lower = text.lower()
    permission_markers = (
        "waiting for your approval",
        "need your approval",
        "requires your approval",
        "approval to run",
        "waiting for permission",
        "need permission",
        "permission required",
        "approve this command",
    )
    if any(marker in lower for marker in permission_markers):
        return "worker_waiting_for_permission"
    if "would you like to proceed" in lower or "continue?" in lower:
        return "worker_waiting_for_interactive_input"
    return ""


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
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    run_env = None
    if env:
        run_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}}
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    lock = threading.Lock()

    def append_stream(stream: Any, target: list[str], stream_name: str) -> None:
        try:
            for chunk in iter(stream.readline, ""):
                if not chunk:
                    break
                with lock:
                    target.append(chunk)
                    with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                        handle.write(chunk)
                        handle.flush()
                if progress_callback is not None:
                    try:
                        progress_callback({"event": "output", "stream": stream_name, "chunk": chunk})
                    except Exception:
                        pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    try:
        log_path.write_text("", encoding="utf-8")
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            text=True,
            env=run_env,
            **hidden_subprocess_kwargs(),
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        stdout_thread = threading.Thread(target=append_stream, args=(proc.stdout, stdout_parts, "stdout"), daemon=True)
        stderr_thread = threading.Thread(target=append_stream, args=(proc.stderr, stderr_parts, "stderr"), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.flush()
            finally:
                proc.stdin.close()
        try:
            return_code = proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            return_code = 124
            with lock:
                stderr_parts.append(f"Timed out after {timeout_seconds:.0f}s\n")
                with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                    handle.write(f"\nTimed out after {timeout_seconds:.0f}s\n")
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        return {
            "exit_code": return_code,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
        }
    except OSError as exc:
        stderr = f"{type(exc).__name__}: {exc}"
        log_path.write_text(stderr, encoding="utf-8", errors="replace")
        return {
            "exit_code": 127,
            "stdout_tail": "",
            "stderr_tail": stderr[-2000:],
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

    # 1) Commit the worker's (uncommitted) edits onto its own branch — user code
    # only. Pacer's own runtime artifacts (workspace dir, generated .gitignore,
    # mandatory record, tool caches) must never ride along: committing them makes
    # the merge collide with the main repo's untracked copies of the same files.
    drop_gitignore = _gitignore_change_is_only_devpacer_block(worktree)
    g(worktree, "add", "-A")
    for artifact in (
        ".agent-workspace",
        ".npm-cache",
        ".dart-home",
        ".dart_tool",
        "强制测试记录.md",
        "*__pycache__*",
        "*.pytest_cache*",
    ):
        g(worktree, "reset", "-q", "HEAD", "--", artifact)
    if drop_gitignore:
        g(worktree, "reset", "-q", "HEAD", "--", ".gitignore")
    wt_status = g(worktree, "status", "--porcelain")
    staged = g(worktree, "diff", "--cached", "--name-only")
    if wt_status.stdout.strip() and staged.stdout.strip():
        commit = g(
            worktree,
            "-c", "user.email=pacer@local", "-c", "user.name=Pacer",
            "commit", "-m", f"Pacer verified change: {message}",
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
    merge = g(repo_root, "merge", "--no-ff", "-m", f"Pacer merge {branch}: {message}", branch)
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


def create_worktree(*, repo_root: Path, worktree: Path, branch: str, allow_dirty: bool = False) -> dict[str, Any]:
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
            # Auto-clean untracked/ignored files left by a previous failed attempt
            # so the worktree can be reused safely. Only removes untracked files
            # (git clean -fd) — never touches committed work.
            clean = subprocess.run(
                ["git", "-C", str(worktree), "clean", "-fd"],
                capture_output=True,
                text=True,
                check=False,
            )
            # Re-check: if tracked files were modified we still have to block.
            recheck = subprocess.run(
                ["git", "-C", str(worktree), "-c", "core.quotePath=false", "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=False,
            )
            still_dirty = [l for l in recheck.stdout.splitlines() if l.strip()]
            if still_dirty:
                if allow_dirty:
                    return {
                        "status": "reused",
                        "path": str(worktree),
                        "branch": branch,
                        "dirty": still_dirty[:20],
                        "clean_output": clean.stdout.strip()[:200],
                    }
                return {
                    "status": "blocked",
                    "reason": f"Worktree has uncommitted tracked changes that cannot be auto-cleaned: {worktree}",
                    "dirty": still_dirty[:10],
                    "clean_output": clean.stdout.strip()[:200],
                }
        overlay = _overlay_dirty_context(repo_root=repo_root, worktree=worktree, allow_dirty=allow_dirty)
        return {"status": "reused", "path": str(worktree), "branch": branch, **overlay}
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
    overlay = _overlay_dirty_context(repo_root=repo_root, worktree=worktree, allow_dirty=allow_dirty)
    return {"status": "created", "path": str(worktree), "branch": branch, **overlay}


def _overlay_dirty_context(*, repo_root: Path, worktree: Path, allow_dirty: bool) -> dict[str, Any]:
    overlay = _overlay_nested_dirty_repo_root(repo_root=repo_root, worktree=worktree, allow_dirty=allow_dirty)
    if overlay:
        return overlay
    return _overlay_local_dirty_files(repo_root=repo_root, worktree=worktree, allow_dirty=allow_dirty)


def _dirty_context_summary(*, repo_root: Path, allow_dirty: bool, ignored_prefixes: tuple[str, ...] = ()) -> dict[str, Any]:
    if not allow_dirty:
        return {}
    repo = Path(repo_root).expanduser().resolve()
    source_dirty = git_dirty_files(repo, ignored_prefixes=ignored_prefixes)
    summary: dict[str, Any] = {"allow_dirty": True}
    if source_dirty:
        summary["source_dirty_files"] = source_dirty[:50]
        summary["source_dirty_count"] = len(source_dirty)

    if (repo / ".git").exists():
        candidates: list[str] = []
        ignored_candidates: list[str] = []
        skipped = 0
        seen: set[str] = set()
        for path, ignored in _local_dirty_overlay_candidates(repo):
            if path in seen:
                continue
            seen.add(path)
            if not _local_dirty_overlay_allowed(path, ignored=ignored):
                skipped += 1
                continue
            if ignored:
                ignored_candidates.append(path)
            else:
                candidates.append(path)
        if candidates:
            summary["overlay_candidate_files"] = candidates[:50]
            summary["overlay_candidate_count"] = len(candidates)
        if ignored_candidates:
            summary["ignored_overlay_candidate_files"] = ignored_candidates[:50]
            summary["ignored_overlay_candidate_count"] = len(ignored_candidates)
        if skipped:
            summary["overlay_skipped_count"] = skipped
    else:
        nested = _nested_repo_root_summary(repo)
        if nested:
            summary.update(nested)
    return summary if len(summary) > 1 else {}


def _nested_repo_root_summary(repo: Path) -> dict[str, Any]:
    top = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if top.returncode != 0:
        return {}
    git_root = Path(top.stdout.strip()).expanduser().resolve()
    if repo == git_root:
        return {}
    try:
        relative = repo.relative_to(git_root).as_posix()
    except ValueError:
        return {}
    return {
        "nested_dirty_project": True,
        "nested_dirty_project_path": str(repo),
        "nested_dirty_project_relative_path": relative,
    }


def _dirty_context_prompt(summary: dict[str, Any]) -> str:
    if not summary:
        return ""
    lines = [
        "Source checkout dirty context (captured before DevPacer created/copied the isolated worktree):",
        "- `--allow-dirty` is enabled. The isolated worktree may contain a baseline commit for these files, so do not assume an empty `git status` there means the user's original checkout was clean.",
    ]
    dirty = summary.get("source_dirty_files") if isinstance(summary.get("source_dirty_files"), list) else []
    candidates = summary.get("overlay_candidate_files") if isinstance(summary.get("overlay_candidate_files"), list) else []
    ignored = summary.get("ignored_overlay_candidate_files") if isinstance(summary.get("ignored_overlay_candidate_files"), list) else []
    if dirty:
        lines.append("Original dirty files:")
        lines.extend(f"- {item}" for item in dirty[:20])
    if candidates:
        lines.append("Dirty files copied into the worktree overlay:")
        lines.extend(f"- {item}" for item in candidates[:20])
    if ignored:
        lines.append("Ignored source/test files copied into the worktree overlay:")
        lines.extend(f"- {item}" for item in ignored[:20])
    if summary.get("nested_dirty_project"):
        lines.append(f"Nested untracked project root copied into the worktree: {summary.get('nested_dirty_project_relative_path')}")
    skipped = int(summary.get("overlay_skipped_count") or 0)
    if skipped:
        lines.append(f"Overlay skipped {skipped} dirty candidate(s), usually caches, dependencies, or secret-like files.")
    return "\n".join(lines)


_LOCAL_OVERLAY_SKIP_PARTS = {
    ".git",
    ".agent-workspace",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".cache",
    ".npm-cache",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "coverage",
}
_LOCAL_OVERLAY_SECRET_BASENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    "model_api_keys.txt",
    "api_keys.txt",
}
_LOCAL_OVERLAY_SECRET_EXTS = {".key", ".pem", ".pfx", ".p12", ".crt"}
_LOCAL_OVERLAY_IGNORED_PREFIXES = (
    "src/",
    "test/",
    "tests/",
    "spec/",
    "specs/",
    "fixtures/",
    "knowledge/",
    "benchmarks/",
)


def _overlay_local_dirty_files(*, repo_root: Path, worktree: Path, allow_dirty: bool) -> dict[str, Any]:
    """Copy local dirty source/test context into an allow-dirty worktree.

    Git worktrees are based on committed files. In dogfood projects it is common
    to have required fixtures or generated source files ignored by broad
    patterns such as ``data/``. Copy only non-secret source/test context so the
    worker can run the same project the user is running, without pulling in
    node_modules, .env files, or tool caches.
    """
    if not allow_dirty:
        return {}
    repo = Path(repo_root).expanduser().resolve()
    target = Path(worktree).expanduser().resolve()
    if not (repo / ".git").exists():
        return {}

    copied = 0
    ignored_copied = 0
    skipped = 0
    errors: list[str] = []
    seen: set[str] = set()
    for path, ignored in _local_dirty_overlay_candidates(repo):
        if path in seen:
            continue
        seen.add(path)
        if not _local_dirty_overlay_allowed(path, ignored=ignored):
            skipped += 1
            continue
        src = repo / path
        dest = target / path
        if not src.is_file():
            skipped += 1
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied += 1
            if ignored:
                ignored_copied += 1
        except OSError as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}"[:200])

    if copied == 0 and not errors:
        return {}
    baseline = _prefix_overlay_baseline(_commit_dirty_overlay_baseline(target), "dirty_file_overlay")
    result: dict[str, Any] = {
        "dirty_file_overlay": "copied" if copied else "failed",
        "dirty_file_overlay_files": copied,
        "dirty_file_overlay_ignored_files": ignored_copied,
        "dirty_file_overlay_skipped": skipped,
        **baseline,
    }
    if errors:
        result["dirty_file_overlay_errors"] = errors[:5]
    return result


def _local_dirty_overlay_candidates(repo: Path) -> list[tuple[str, bool]]:
    tracked = _git_lines(repo, ["git", "-C", str(repo), "ls-files", "--modified"])
    untracked = _git_lines(repo, ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard"])
    ignored = _git_lines(repo, ["git", "-C", str(repo), "ls-files", "--others", "--ignored", "--exclude-standard"])
    candidates: list[tuple[str, bool]] = []
    for path in tracked + untracked:
        normalized = _normalize_repo_path(path)
        if normalized:
            candidates.append((normalized, False))
    for path in ignored:
        normalized = _normalize_repo_path(path)
        if normalized:
            candidates.append((normalized, True))
    return candidates


def _git_lines(repo: Path, args: list[str]) -> list[str]:
    completed = subprocess.run(args, cwd=str(repo), capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _normalize_repo_path(path: str) -> str:
    normalized = str(path).replace("\\", "/").strip().lstrip("/")
    if not normalized or normalized.startswith("../") or "/../" in normalized:
        return ""
    return normalized


def _local_dirty_overlay_allowed(path: str, *, ignored: bool) -> bool:
    normalized = _normalize_repo_path(path)
    if not normalized:
        return False
    parts = normalized.split("/")
    if any(part in _LOCAL_OVERLAY_SKIP_PARTS for part in parts):
        return False
    basename = parts[-1].lower()
    if basename in _LOCAL_OVERLAY_SECRET_BASENAMES:
        return False
    if Path(basename).suffix.lower() in _LOCAL_OVERLAY_SECRET_EXTS:
        return False
    if ignored and not normalized.startswith(_LOCAL_OVERLAY_IGNORED_PREFIXES):
        return False
    return True


def _prefix_overlay_baseline(payload: dict[str, Any], prefix: str) -> dict[str, Any]:
    return {f"{prefix}_{key}": value for key, value in payload.items()}


def _overlay_nested_dirty_repo_root(*, repo_root: Path, worktree: Path, allow_dirty: bool) -> dict[str, Any]:
    """Copy a nested, untracked project root into the isolated worktree.

    Some local Pacer dogfood checkouts live as an untracked subdirectory inside
    a larger Git repository. Git worktree can only check out tracked content, so
    the worker otherwise sees the parent repo without the actual project files.
    """
    if not allow_dirty:
        return {}
    repo = Path(repo_root).expanduser().resolve()
    target = Path(worktree).expanduser().resolve()
    if (repo / ".git").exists():
        return {}
    top = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if top.returncode != 0:
        return {}
    git_root = Path(top.stdout.strip()).expanduser().resolve()
    if repo == git_root:
        return {}
    try:
        repo.relative_to(git_root)
    except ValueError:
        return {}

    ignored_dirs = {
        ".git",
        ".agent-workspace",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".ruff_cache",
    }
    copied = 0
    for item in repo.iterdir():
        name = item.name
        if name in ignored_dirs or name.endswith(".checkpoint-worktrees"):
            continue
        dest = target / name
        try:
            if item.is_dir():
                shutil.copytree(
                    item,
                    dest,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        ".git",
                        ".agent-workspace",
                        ".pytest_cache",
                        "__pycache__",
                        "node_modules",
                        "*.pyc",
                        "*.pyo",
                    ),
                )
            elif item.is_file():
                shutil.copy2(item, dest)
            else:
                continue
            copied += 1
        except OSError as exc:
            return {"dirty_overlay": "failed", "dirty_overlay_error": str(exc)[:200]}
    baseline = _commit_dirty_overlay_baseline(target)
    return {
        "dirty_overlay": "copied",
        "dirty_overlay_items": copied,
        "dirty_overlay_source": str(repo),
        **baseline,
    }


def _commit_dirty_overlay_baseline(worktree: Path) -> dict[str, Any]:
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        return {"dirty_overlay_commit": "failed", "dirty_overlay_commit_error": status.stderr.strip()[:200]}
    if not status.stdout.strip():
        return {"dirty_overlay_commit": "unchanged"}
    add = subprocess.run(["git", "-C", str(worktree), "add", "-A"], capture_output=True, text=True, check=False)
    if add.returncode != 0:
        return {"dirty_overlay_commit": "failed", "dirty_overlay_commit_error": add.stderr.strip()[:200]}
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.email=devpacer@example.local",
            "-c",
            "user.name=DevPacer",
            "commit",
            "-m",
            "DevPacer dirty overlay baseline",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode != 0:
        return {"dirty_overlay_commit": "failed", "dirty_overlay_commit_error": (commit.stderr or commit.stdout).strip()[:200]}
    head = _git_head(worktree)
    return {"dirty_overlay_commit": "created", "dirty_overlay_base_commit": head}


_PYTHON_GITIGNORE = """\
# Auto-generated by DevPacer for this worktree runtime exclude
__pycache__/
*.pyc
*.pyo
.pytest_cache/
*.egg-info/
.eggs/
dist/
build/
"""

_NODE_GITIGNORE = """\
# Auto-generated by DevPacer for this worktree runtime exclude
node_modules/
.cache/
.npm-cache/
.yarn/
.pnpm-store/
dist/
build/
coverage/
"""

_DART_GITIGNORE = """\
# Auto-generated by DevPacer for this worktree runtime exclude
.dart_tool/
.dart-home/
.flutter-plugins
.flutter-plugins-dependencies
"""


def _write_worktree_gitignore(worktree: Path, repo_root: Path) -> None:
    """Ensure a worktree locally ignores DevPacer/runtime caches.

    Prevents runtime caches (__pycache__, node_modules) from being accidentally
    committed in verified merge commits without changing the product's
    versioned .gitignore.
    """
    exclude = _worktree_exclude_path(worktree)
    # Detect project type from root; fall back to Python patterns (most common).
    has_python = any(repo_root.glob("**/*.py")) or (repo_root / "pyproject.toml").exists() or (repo_root / "setup.py").exists()
    has_node = (repo_root / "package.json").exists()
    has_dart = (repo_root / "pubspec.yaml").exists() or any(repo_root.glob("lib/**/*.dart"))
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        lines = []
        if has_python and "__pycache__/" not in existing:
            lines.append(_PYTHON_GITIGNORE)
        if has_node and ".npm-cache/" not in existing:
            lines.append(_NODE_GITIGNORE)
        if has_dart and ".dart-home/" not in existing:
            lines.append(_DART_GITIGNORE)
        if not lines and not existing:
            lines.append(_PYTHON_GITIGNORE)
        if not lines:
            return
        additions = "".join(lines)
        separator = "" if not existing or existing.endswith("\n") else "\n"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(existing + separator + additions, encoding="utf-8")
    except OSError:
        pass


def _worktree_exclude_path(worktree: Path) -> Path:
    try:
        completed = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return worktree / ".git" / "info" / "exclude"
    if completed.returncode == 0 and completed.stdout.strip():
        path = Path(completed.stdout.strip())
        return path if path.is_absolute() else (worktree / path)
    return worktree / ".git" / "info" / "exclude"


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


# Derived tool caches are never user source. They appear the moment a user runs
# their own test suite (which the quickstart tells them to do), so treating them
# as "uncommitted changes" blocks the golden path on step two.
_DERIVED_CACHE_SEGMENTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}


def _dirty_path_ignored(line: str, ignored_prefixes: tuple[str, ...], *, repo_root: Path | None = None) -> bool:
    path = _porcelain_path(line)
    if any(segment in _DERIVED_CACHE_SEGMENTS for segment in path.rstrip("/").split("/")):
        return True
    if not ignored_prefixes:
        return False
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
        command_verification = (
            latest.get("command_verification")
            if isinstance(latest.get("command_verification"), dict)
            else {}
        )
        if command_verification:
            lines.append(f"- Failure kind: `{command_verification.get('failure_kind') or ''}`")
            if command_verification.get("classification_confidence"):
                lines.append(f"- Classification confidence: `{command_verification.get('classification_confidence')}`")
                if command_verification.get("classification_confidence") == "heuristic":
                    lines.append("- Classification note: heuristic判定，建议人工确认。")
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
