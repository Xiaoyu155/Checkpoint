from __future__ import annotations

import json
import hashlib
import inspect
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from .agent_backends import (
    canonical_backend_name,
    looks_like_quota_exhaustion,
    redact_backend,
    resolve_backend_by_name,
    resolve_failover_backend,
)
from .agent_capabilities import canonical_agent_name, load_agent_profile, recommend_worker_config
from .command_verification import (
    NON_REPAIRABLE_COMMAND_FAILURE_KINDS,
    acceptance_chain_repair_brief,
    changed_test_files,
    changed_acceptance_chain_files,
    command_repair_brief,
    is_test_path,
    normalize_verification_env,
    run_command_verification,
    tamper_repair_brief,
)
from .chief_plans_store import append_dispatch_record, append_worker_record, load_plan, plan_dir, save_plan, save_verification
from .change_set import collect_repository_change_set
from .codex_check import codex_check_to_markdown, is_runtime_changed_file, run_codex_check
from .diff_summary import build_diff_summary
from .dynamic_model_selector import routing_request_evidence
from .execution_alignment import build_worker_prompt_alignment_check
from .mission_progress import record_worker_output, save_mission_progress
from .missions import load_mission, load_rounds
from .managed_state import (
    ManagedBudgetPolicy,
    ManagedBudgetUsage,
    assess_managed_budget,
    evaluate_retry,
    managed_idempotency_key,
)
from .preflight import dependency_preflight
from .subprocess_window import (
    hidden_subprocess_kwargs,
    isolated_process_group_kwargs,
    prepare_subprocess_command,
    terminate_process_tree,
)
from .git_diff import changed_files
from .llm_providers import LLMBackend, run_llm_completion
from .models import to_jsonable
from .project_memory import build_project_memory, project_memory_handoff_notes
from .repo_map import build_repo_map, render_repo_map, repo_map_cache_path
from .security import redact_secret_text, scrub_secrets
from .verification_profiles import (
    conditional_test_command_short_circuit,
    estimate_verification_timeout,
    resolve_test_command,
    verification_timeout_reason,
)
from .workspace import open_workspace


# Checkpoint writes its own run artifacts (workspace outputs, status file, browser
# cache) into the repo it verifies. Those are tool outputs, not code under test,
# and must never be counted as changed files — otherwise a second verification
# round sees them as uncovered changes and reports a phantom coverage gap.
_ARTIFACT_BASENAMES = {".visual-agent-status.md", "强制测试记录.md"}
_ARTIFACT_DIR_PREFIXES = (".pw-browsers/", ".npm-cache/", ".dart-home/", ".dart_tool/", "artifacts/")
_NON_PRODUCT_DIR_PREFIXES = (
    ".agent-workspace/",
    "_graveyard_",
    "graveyard/",
    "archive/",
    "archives/",
    "_open_source_cases/",
)
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
DISPATCH_MODES = {"tracked", "delegated"}
PROMPT_STYLES = {"expanded", "legacy"}
REPAIR_STRATEGIES = {"resume", "fresh"}
NON_REPAIRABLE_WORKER_FAILURE_KINDS = frozenset(
    {
        "provider_rate_limit",
        "provider_5xx",
        "network_timeout",
        "not_authenticated",
    }
)


def _normalized_choice(value: str, choices: set[str], *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in choices:
        raise ValueError(f"Unsupported {field}: {value!r}; expected one of {', '.join(sorted(choices))}.")
    return normalized


def _codex_user_defaults() -> dict[str, str]:
    try:
        from .codex_exec import load_codex_user_defaults

        return load_codex_user_defaults()
    except (OSError, ValueError, ImportError):
        return {}


def _resolved_or_inherited(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if normalized and normalized.lower() != "inherit":
        return normalized
    return f"inherited({field})"


def _allow_automatic_low_cost_failover(worker_agent_norm: str) -> bool:
    return False


def _grade_acceptance(
    *,
    command_result: dict[str, Any] | None,
    command: str,
    repo_root: Path,
    base_ref: str,
    timeout_seconds: float,
    verification_env: list[dict[str, Any]] | None,
    workspace_root: str | Path,
    enabled: bool = True,
) -> dict[str, Any]:
    """Grade the acceptance evidence: did this gate actually test this task?

    Never raises: a probe failure must degrade the claim, not the mission.
    """

    from .acceptance_discrimination import classify_acceptance, probe_base_command

    probe: dict[str, Any] = {"status": "unknown", "reason": "base_probe_disabled"}
    graded_pass = isinstance(command_result, dict) and str(command_result.get("verdict") or "") == "pass"
    if enabled and graded_pass:
        try:
            probe = probe_base_command(
                command=command,
                repo_root=repo_root,
                base_ref=base_ref,
                timeout_seconds=timeout_seconds,
                verification_env=verification_env,
                workspace_root=workspace_root,
            )
        except Exception as exc:  # noqa: BLE001 - probing is evidence, not execution
            probe = {"status": "unknown", "reason": "base_probe_error", "detail": str(exc)[:400]}
    graded = classify_acceptance(command_result=command_result, base_probe=probe)
    return {**graded, "base_probe": probe}


def _is_weak_command_gate(command: str) -> bool:
    value = str(command or "").strip()
    if not value:
        return False
    try:
        words = shlex.split(value, posix=False)
    except ValueError:
        words = value.split()
    executable = Path(str(words[0] if words else "")).name.lower()
    if executable in {"rg", "rg.exe", "grep", "grep.exe", "findstr", "findstr.exe"}:
        return not any(token in value for token in ("&&", "||", ";", "|"))
    return False


def _roadmap_context_prompt(policy: dict[str, Any]) -> str:
    if str(policy.get("roadmap_mode") or "") != "locked":
        return ""
    return "\n".join(
        [
            "Roadmap contract (locked for this program task):",
            f"- Program: {policy.get('program_id') or ''}",
            f"- Task: {policy.get('task_id') or ''}",
            f"- Source plan: {policy.get('source_plan') or ''}",
            f"- Source plan SHA-256: {policy.get('source_plan_sha256') or ''}",
            "Keep the implementation aligned with this task. Do not replace it with a new ad-hoc objective.",
        ]
    )


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
    if ".agent-workspace" in parts:
        return True
    if any(part.endswith(".checkpoint-worktrees") or ".checkpoint-worktrees" in part for part in parts):
        return True
    return any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in _NON_PRODUCT_DIR_PREFIXES)


def _is_restricted_worker_path(path: str) -> bool:
    normalized = str(path).replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    if not normalized:
        return False
    if normalized in _ARTIFACT_BASENAMES:
        return True
    return _is_non_product_path(normalized)


CommandRunner = Callable[..., dict[str, Any]]


def _refresh_resume_project_memory(
    *,
    workspace_root: Path,
    plan: dict[str, Any],
    plan_id: str,
    mission_id: str | None,
    repo_root: Path,
    memory_mode: str,
) -> dict[str, Any] | None:
    """Refresh evidence memory only when resuming an already-running mission.

    A first dispatch receives the plan snapshot created by ``chief-plan``. Once
    a worker has failed, however, its new round/failure evidence must be visible
    to the next worker in the same long-host mission. Refreshing here keeps the
    resume path local and deterministic while preserving synthetic memory
    fixtures and caller-provided plan context on the first attempt.
    """
    if memory_mode == "disabled" or not mission_id:
        return None
    mission = load_mission(workspace_root, str(mission_id))
    if not isinstance(mission, dict) or int(mission.get("current_round") or 0) <= 0:
        return None
    rounds = load_rounds(workspace_root, str(mission_id))
    if not any(
        str(item.get("type") or "") in {"dispatch", "verification", "auto_resume"}
        for item in rounds
        if isinstance(item, dict)
    ):
        return None
    try:
        refreshed = build_project_memory(
            workspace_root=workspace_root,
            repo_root=repo_root,
            goal=str(plan.get("objective") or mission.get("objective") or ""),
            limit=5,
        )
    except Exception:
        return None
    previous = plan.get("project_memory") if isinstance(plan.get("project_memory"), dict) else {}
    previous_usage = previous.get("usage") if isinstance(previous.get("usage"), dict) else {}
    refreshed["usage"] = {
        **previous_usage,
        "memory_mode": "enabled",
        "dispatch_injected": False,
        "dispatch_note_count": 0,
        "dispatch_chars": 0,
        "dispatch_memory_ids": [],
        "refreshed_for_resume": True,
    }
    plan["project_memory"] = refreshed
    try:
        save_plan(plan, workspace_root=workspace_root, plan_id=plan_id)
    except OSError:
        # A read-only/locked plan must not prevent the worker from using the
        # in-memory refreshed snapshot for this attempt.
        pass
    return refreshed


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
    base_probe_enabled: bool = True,
    merge: bool = False,
    command_runner: CommandRunner | None = None,
    codex_runner: Any = None,
    failure_evidence_builder: Any = None,
    allow_prior_verified_evidence: bool = False,
    verification_env: list[dict[str, Any]] | None = None,
    reasoning_effort: str | None = None,
    dispatch_mode: str = "tracked",
    prompt_style: str = "expanded",
    repair_strategy: str = "resume",
    max_repair_rounds: int | None = 2,
    delegated_timeout_seconds: float | None = None,
    execution_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Preview or execute one chief-engineer worker dispatch.

    Execution is intentionally narrow: only the Codex implementation adapter is
    supported. Other profiles can appear as inspection lanes in the plan, but
    dispatch will not run them as coding workers.
    """
    dispatch_started = monotonic()
    workspace_path = Path(workspace_root).expanduser().resolve()
    dispatch_mode = _normalized_choice(dispatch_mode, DISPATCH_MODES, field="dispatch_mode")
    prompt_style = _normalized_choice(prompt_style, PROMPT_STYLES, field="prompt_style")
    repair_strategy = _normalized_choice(repair_strategy, REPAIR_STRATEGIES, field="repair_strategy")
    worker_timeout_seconds = float(timeout_seconds)
    if dispatch_mode == "delegated":
        worker_timeout_seconds = float(delegated_timeout_seconds or (float(timeout_seconds) * 2.0))
    progress_mission_id = str(mission_id or plan_id)
    plan = load_plan(workspace_path, plan_id)
    if plan is None:
        return _blocked(plan_id=plan_id, reason=f"No saved plan found: {plan_id}")
    from .task_memory import (
        TaskMemoryError,
        append_task_memory_event,
        initialize_task_memory,
        read_task_memory,
        task_memory_id,
    )

    dispatch_memory_id = task_memory_id(scope=f"dispatch:{plan_id}:{mission_id or plan_id}")
    try:
        initialize_task_memory(
            workspace_path,
            memory_id=dispatch_memory_id,
            goal=str(plan.get("objective") or ""),
            repo_root=str(plan.get("repo_root") or "."),
            source="pacer_dispatch",
        )
        append_task_memory_event(
            workspace_path,
            memory_id=dispatch_memory_id,
            event_type="dispatch_started",
            data={
                "plan_id": plan_id,
                "mission_id": mission_id or plan_id,
                "execute": bool(execute and not dry_run),
            },
            goal=str(plan.get("objective") or ""),
            repo_root=str(plan.get("repo_root") or "."),
        )
    except TaskMemoryError as exc:
        return {
            "status": "blocked",
            "reason": "task_memory_unavailable",
            "message": f"Pacer task memory is mandatory and could not be persisted: {exc}",
            "plan_id": plan_id,
        }
    closed_loop = dict(execution_policy) if isinstance(execution_policy, dict) else {}
    managed_budget_policy: ManagedBudgetPolicy | None = None
    managed_budget_error = ""
    if "managed_budget" in closed_loop:
        try:
            managed_budget_policy = _managed_budget_policy(closed_loop.get("managed_budget"))
        except (TypeError, ValueError) as exc:
            managed_budget_error = str(exc)
    memory_mode = str(closed_loop.get("memory_mode") or "enabled").strip().lower()
    acceptance_policy = str(closed_loop.get("acceptance_policy") or "standard").strip().lower()
    codex_provider = str(closed_loop.get("codex_provider") or "inherit").strip()
    codex_failover_provider = str(closed_loop.get("codex_failover_provider") or "").strip()
    if memory_mode == "disabled":
        memory = plan.get("project_memory") if isinstance(plan.get("project_memory"), dict) else {}
        usage = memory.get("usage") if isinstance(memory.get("usage"), dict) else {}
        plan["project_memory"] = {
            "usage": {
                **usage,
                "memory_mode": "disabled",
                "dispatch_injected": False,
                "dispatch_note_count": 0,
                "dispatch_chars": 0,
                "dispatch_memory_ids": [],
            },
            "entries": [],
            "instruction_memory": {},
        }
    else:
        memory = plan.get("project_memory") if isinstance(plan.get("project_memory"), dict) else {}
        usage = memory.get("usage") if isinstance(memory.get("usage"), dict) else {}
        usage["memory_mode"] = "enabled"
        if memory:
            memory["usage"] = usage

    selected_track = _select_track(plan, track_id=track_id)
    if selected_track is None:
        return _blocked(plan_id=plan_id, reason="No matching worker track found in the saved plan.", plan=plan)

    repo_root = Path(str(plan.get("repo_root") or ".")).expanduser().resolve()
    _refresh_resume_project_memory(
        workspace_root=workspace_path,
        plan=plan,
        plan_id=plan_id,
        mission_id=mission_id,
        repo_root=repo_root,
        memory_mode=memory_mode,
    )
    managed_key = str(closed_loop.get("idempotency_key") or "").strip() or managed_idempotency_key(
        {
            "objective": str(plan.get("objective") or ""),
            "repo_root": str(repo_root),
            "requirement_contract": plan.get("requirement_contract")
            if isinstance(plan.get("requirement_contract"), dict)
            else {},
        }
    )
    raw_test_command = str(test_command or "").strip()
    resolved_test_command, verification_profile = resolve_test_command(test_command, repo_root=repo_root)
    test_command_unresolved = bool(raw_test_command) and not str(resolved_test_command or "").strip()
    test_command = str(resolved_test_command or "").strip() or None
    track_token = str(selected_track.get("id") or "track_1_codex")
    worktree = default_worktree_path(repo_root=repo_root, plan_id=plan_id, track_id=track_token)
    worktree_project_root = _worktree_project_root(repo_root=repo_root, worktree=worktree)
    branch = default_branch_name(plan_id=plan_id, track_id=track_token)
    verification_workspace = default_worktree_workspace_path(
        source_workspace=workspace_path,
        repo_root=repo_root,
        worktree=worktree_project_root,
    )
    verification_command = build_verification_command(
        workspace_root=verification_workspace,
        repo_root=worktree_project_root,
        run_profile=run_profile,
        include_slow=include_slow,
    )
    command_mode = bool(str(test_command or "").strip() or test_command_unresolved)
    effective_verification_command = str(test_command or "").strip() or (raw_test_command if test_command_unresolved else verification_command)
    dependency_check = dependency_preflight(repo_root, str(test_command or raw_test_command or ""))
    timeout_info = (
        _verification_timeout_info(repo_root=repo_root, command=effective_verification_command, base_timeout=timeout_seconds)
        if command_mode
        else {"base_timeout_seconds": float(timeout_seconds), "timeout_seconds": float(timeout_seconds), "reason": "workflow_mode"}
    )
    command_safety = (
        conditional_test_command_short_circuit(repo_root, effective_verification_command)
        if command_mode
        else {}
    )
    verification_env_normalized = normalize_verification_env(verification_env)
    missing_env = _missing_declared_verification_env(verification_env_normalized)
    preflight = _dispatch_preflight_payload(
        raw_test_command=raw_test_command,
        resolved_test_command=str(test_command or ""),
        verification_profile=verification_profile,
        test_command_unresolved=test_command_unresolved,
        verification_env=verification_env_normalized,
        missing_env=missing_env,
        dependency_check=dependency_check,
        timeout_info=timeout_info,
        command_safety=command_safety,
    )
    if managed_budget_error:
        preflight["status"] = "blocked"
        preflight["managed_budget"] = {
            "status": "blocked",
            "reason": "managed_budget_invalid",
            "message": managed_budget_error,
        }
    elif managed_budget_policy is not None:
        preflight["managed_budget"] = {
            "status": "ok",
            "policy": {
                "max_wall_seconds": managed_budget_policy.max_wall_seconds,
                "max_total_tokens": managed_budget_policy.max_total_tokens,
                "max_attempts": managed_budget_policy.max_attempts,
                "max_repair_rounds": managed_budget_policy.max_repair_rounds,
                "max_same_failure_count": managed_budget_policy.max_same_failure_count,
            },
        }
    if acceptance_policy == "strict" and command_mode and _is_weak_command_gate(effective_verification_command):
        preflight["status"] = "blocked"
        preflight["strict_acceptance"] = {
            "status": "blocked",
            "reason": "weak_command_gate",
            "message": "Strict acceptance rejects marker/search-only commands as the sole verification gate.",
        }
    if execute and not dry_run:
        execution_alignment = build_worker_prompt_alignment_check()
        preflight["execution_alignment"] = execution_alignment
        if execution_alignment.get("status") == "blocked":
            preflight["status"] = "blocked"
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
    dirty_context_text = "\n\n".join(
        part for part in (_dirty_context_prompt(dirty_context), _roadmap_context_prompt(closed_loop)) if part
    )
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
        worktree=worktree_project_root,
        verification_command=effective_verification_command,
        phase="implementation",
        model_policy=model_policy,
        repo_map_text=repo_map_text,
        prompt_override=report_prompt,
        prompt_suffix=dirty_context_text,
        reasoning_effort=reasoning_effort,
        dispatch_mode=dispatch_mode,
        prompt_style=prompt_style,
        codex_provider=codex_provider,
        execution_policy=closed_loop,
    )
    toolchain_policy = _toolchain_policy_for_command(effective_verification_command)
    toolchain_preflight = _toolchain_preflight_for_command(effective_verification_command, policy=toolchain_policy)
    initial_budget = _dispatch_budget_assessment(
        managed_budget_policy,
        dispatch_started=dispatch_started,
        records=[],
        repair_rounds=0,
        same_failure_count=0,
        operation="worker_attempt",
    )
    preview = {
        "schema_version": 1,
        "status": "preview",
        "plan_id": plan_id,
        "objective": str(plan.get("objective") or ""),
        "plan_status": str(plan.get("status") or ""),
        "dry_run": bool(dry_run or not execute),
        "execute": bool(execute and not dry_run),
        "project_memory_usage": dict(((plan.get("project_memory") or {}).get("usage") or {})),
        "worker": {
            "track_id": track_token,
            "agent": str(selected_track.get("agent") or ""),
            "track_kind": str(selected_track.get("track_kind") or "implementation"),
            "command": worker_command["display"],
            "argv": worker_command["argv"],
            "resolved_model": worker_command.get("resolved_model", ""),
            "resolved_reasoning_effort": worker_command.get("resolved_reasoning_effort", ""),
            "model_source": worker_command.get("model_source", ""),
            "resolved_provider": worker_command.get("resolved_provider", ""),
            "provider_source": worker_command.get("provider_source", ""),
            "reasoning_effort_source": worker_command.get("reasoning_effort_source", ""),
            "resolved_sandbox": worker_command.get("resolved_sandbox", ""),
            "sandbox_source": worker_command.get("sandbox_source", ""),
            "resolved_approval": worker_command.get("resolved_approval", ""),
            "approval_source": worker_command.get("approval_source", ""),
            "dispatch_mode": dispatch_mode,
            "prompt_style": prompt_style,
            "repair_strategy": repair_strategy,
            "timeout_seconds": worker_timeout_seconds,
            "memory_mode": memory_mode,
            "acceptance_policy": acceptance_policy,
        },
        "worktree": {
            "path": str(worktree),
            "project_root": str(worktree_project_root),
            "branch": branch,
            "created": False,
        },
        "verification": {
            "command": effective_verification_command,
            "checkpoint_command": verification_command if command_mode else "",
            "mode": "command" if command_mode else "workflow",
            "workspace_root": str(verification_workspace),
            "records_workspace_root": str(workspace_path),
            "run_profile": run_profile,
            "include_slow": bool(include_slow),
            "max_workflows": int(max_workflows),
            "timeout_seconds": timeout_info["timeout_seconds"],
            "base_timeout_seconds": timeout_info["base_timeout_seconds"],
            "timeout_reason": timeout_info["reason"],
        },
        "preflight": preflight,
        "managed_runtime": {
            "schema_version": 1,
            "idempotency_key": managed_key,
            "transition_valid": True,
            "budget_status": str(initial_budget.get("status") or "not_configured"),
            "budget": initial_budget,
            "routing_evidence": dict(worker_command.get("routing_evidence") or {}),
            "retry": {},
        },
        "repo_map": repo_map_stats,
        "toolchain_policy": toolchain_policy,
        "subscription_quota": _quota_preview(),
        "warnings": _dispatch_warnings(plan, selected_track),
        "records": {
            "plan_dir": str(plan_dir(workspace_path, plan_id)),
            "workers_jsonl": str(plan_dir(workspace_path, plan_id) / "workers.jsonl"),
            "verification_json": str(plan_dir(workspace_path, plan_id) / "verification.json"),
            "dispatches_jsonl": str(plan_dir(workspace_path, plan_id) / "dispatches.jsonl"),
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
        worktree=str(worktree_project_root),
        verification_command=effective_verification_command,
    )
    preflight_block = _dispatch_preflight_block(preflight)
    if preflight_block:
        save_mission_progress(
            workspace_path,
            progress_mission_id,
            stage="blocked",
            stage_label="Preflight blocked",
            status="preflight_blocked",
            blocker=str(preflight_block.get("reason") or "preflight_blocked"),
        )
        append_task_memory_event(
            workspace_path,
            memory_id=dispatch_memory_id,
            event_type="dispatch_blocked",
            data={"reason": str(preflight_block.get("reason") or "preflight_blocked")},
            goal=str(plan.get("objective") or ""),
            repo_root=repo_root,
        )
        return {
            **preview,
            "status": "preflight_blocked",
            "reason": str(preflight_block.get("reason") or "preflight_blocked"),
            "message": str(preflight_block.get("message") or "Preflight blocked dispatch."),
        }
    if blocked_reason:
        save_mission_progress(
            workspace_path,
            progress_mission_id,
            stage="blocked",
            stage_label="Dispatch blocked",
            status="blocked",
            blocker=blocked_reason,
        )
        append_task_memory_event(
            workspace_path,
            memory_id=dispatch_memory_id,
            event_type="dispatch_blocked",
            data={"reason": blocked_reason},
            goal=str(plan.get("objective") or ""),
            repo_root=repo_root,
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
        append_task_memory_event(
            workspace_path,
            memory_id=dispatch_memory_id,
            event_type="dispatch_blocked",
            data={"reason": "toolchain_preflight", "message": str(toolchain_preflight.get("message") or "")},
            goal=str(plan.get("objective") or ""),
            repo_root=repo_root,
        )
        return {
            **preview,
            "status": "blocked",
            "reason": str(toolchain_preflight.get("message") or "Toolchain preflight blocked dispatch."),
            "toolchain_preflight": toolchain_preflight,
        }
    if dry_run or not execute:
        append_task_memory_event(
            workspace_path,
            memory_id=dispatch_memory_id,
            event_type="dispatch_previewed",
            data={"status": str(preview.get("status") or "preview"), "verification_command": effective_verification_command},
            goal=str(plan.get("objective") or ""),
            repo_root=repo_root,
        )
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
        worktree=str(worktree_project_root),
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
    trusted_workspace_snapshot = (
        workspace_setup.get("trusted_snapshot")
        if isinstance(workspace_setup.get("trusted_snapshot"), dict)
        else None
    )
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
    # warn the user and suggest alternatives. Hard-block lives on background launch
    # / overnight waves (provider_liveness) so unit tests can still simulate live
    # quota failures mid-worker without being short-circuited here.
    from .agent_backends import has_recent_quota_failure, get_available_agents
    if has_recent_quota_failure(worker_agent_norm):
        available = get_available_agents()
        if available:
            preview["worker"]["quota_warning"] = (
                f"Agent '{worker_agent}' recently failed due to quota exhaustion. "
                f"Consider switching to: {', '.join(available[:3])}"
            )
        else:
            preview["worker"]["quota_warning"] = (
                f"Agent '{worker_agent}' recently failed due to quota exhaustion."
            )

    try:
        from .provider_liveness import probe_worker_agent_liveness

        liveness = probe_worker_agent_liveness(worker_agent_norm)
        preview["provider_liveness"] = liveness
        if not liveness.get("ok"):
            preview.setdefault("warnings", []).append(
                str(liveness.get("message") or "Agent liveness probe failed.")
            )
    except Exception:
        pass

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
        # Claude Code is an explicit production tool. Do not silently route it
        # onto cheap OpenAI-compatible backends (bugteam/mimo) — that rewrites
        # --model to ids like gpt-4o-mini and breaks headless runs.
        worker_backend = None
    initial_argv = list(worker_command["argv"])
    initial_env: dict[str, str] | None = None
    if worker_backend:
        initial_argv = _override_model_flag(initial_argv, worker_backend["model"])
        initial_env = worker_backend["env"]
        preview["worker"]["backend"] = redact_backend(worker_backend)
        if force_backend_name:
            preview["worker"]["forced_backend"] = force_backend_name
    initial_env = _apply_toolchain_policy_env(initial_env, toolchain_policy)
    initial_env = _prefer_verification_python_env(initial_env, effective_verification_command)
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
            cwd=worktree_project_root,
            timeout_seconds=worker_timeout_seconds,
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
            cwd=worktree_project_root,
            timeout_seconds=worker_timeout_seconds,
            log_path=logs_dir / f"{track_token}-initial.log",
            runner=runner,
            env=initial_env,
            backend=worker_backend,
            command_metadata=worker_command,
            dispatch_mode=dispatch_mode,
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
    active_codex_provider = codex_provider
    quota_hit = (
        worker_backend is None
        and worker_record.get("status") != "completed"
        and looks_like_quota_exhaustion(worker_record.get("stdout_tail"), worker_record.get("stderr_tail"))
    )
    if quota_hit:
        # Record the quota failure so future dispatches can skip this agent
        from .agent_backends import record_quota_failure
        record_quota_failure(worker_agent_norm)
    if (
        quota_hit
        and worker_agent_norm == "codex"
        and codex_failover_provider
        and codex_failover_provider != str(worker_command.get("resolved_provider") or codex_provider)
    ):
        failover_command = build_worker_command(
            plan=plan,
            track=selected_track,
            worktree=worktree_project_root,
            verification_command=effective_verification_command,
            phase="implementation",
            model_policy=model_policy,
            repo_map_text=repo_map_text,
            prompt_override=report_prompt,
            prompt_suffix=dirty_context_text,
            reasoning_effort=reasoning_effort,
            dispatch_mode=dispatch_mode,
            prompt_style=prompt_style,
            codex_provider=codex_failover_provider,
            execution_policy=closed_loop,
        )
        failover_record = _run_worker_attempt(
            workspace_root=workspace_path,
            plan_id=plan_id,
            mission_id=progress_mission_id,
            attempt=f"quota_failover_codex_{codex_failover_provider}",
            track=selected_track,
            argv=list(failover_command["argv"]),
            stdin_text=failover_command.get("stdin"),
            cwd=worktree_project_root,
            timeout_seconds=worker_timeout_seconds,
            log_path=logs_dir / f"{track_token}-quota-failover-{codex_failover_provider}.log",
            runner=runner,
            env=_apply_toolchain_policy_env(None, toolchain_policy),
            command_metadata=failover_command,
            dispatch_mode=dispatch_mode,
        )
        if failover_record.get("status") == "completed":
            active_codex_provider = codex_failover_provider
    elif quota_hit and not _allow_automatic_low_cost_failover(worker_agent_norm):
        preview["worker"]["failover_disabled"] = (
            "No alternate Codex provider is configured for automatic failover."
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
                cwd=worktree_project_root,
                timeout_seconds=worker_timeout_seconds,
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
    workspace_tamper = _workspace_tamper_verification(
        workspace_root=workspace_path,
        verification_workspace_root=verification_workspace,
        plan_id=plan_id,
        mission_id=progress_mission_id,
        repo_root=worktree_project_root,
        run_profile=run_profile,
        trusted_workspace_snapshot=trusted_workspace_snapshot,
    )
    scope_verification = _scope_violation_verification(
        workspace_root=workspace_path,
        verification_workspace_root=verification_workspace,
        plan_id=plan_id,
        mission_id=progress_mission_id,
        repo_root=worktree_project_root,
        run_profile=run_profile,
        worktree_base=worktree_base,
    )
    if workspace_tamper is not None:
        verification = workspace_tamper
    elif scope_verification is not None:
        verification = scope_verification
    elif diagnosis_mode or review_plan_mode:
        # The deliverable IS the report; running seeded demo workflows against a
        # report mission produces irrelevant failures (V7 dogfood finding).
        verification = _diagnosis_verification(worktree_project_root) if diagnosis_mode else _review_plan_verification(worktree_project_root)
    else:
        verification = run_dispatch_verification(
            workspace_root=workspace_path,
            verification_workspace_root=verification_workspace,
            plan_id=plan_id,
            mission_id=progress_mission_id,
            repo_root=worktree_project_root,
            run_profile=run_profile,
            include_slow=include_slow,
            max_workflows=max_workflows,
            codex_runner=codex_runner,
            failure_evidence_builder=failure_evidence_builder,
            test_command=test_command,
            verification_env=verification_env,
            worktree_base=worktree_base,
            allow_test_edits=allow_test_edits,
            timeout_seconds=timeout_seconds,
            trusted_workspace_snapshot=trusted_workspace_snapshot,
            base_probe_enabled=base_probe_enabled,
        )
    verification_attempts.append(verification)

    repair_worker_records: list[dict[str, Any]] = []
    repair_worker_record: dict[str, Any] | None = None
    repair_rounds_completed = 0
    budget_blocked: dict[str, Any] | None = None
    repair_limit = _repair_round_limit(
        auto_repair_once=auto_repair_once,
        max_repair_rounds=max_repair_rounds,
    )
    repair_context_record = (
        failover_record
        if isinstance(failover_record, dict) and failover_record.get("status") == "completed"
        else worker_record
    )
    current_session_id = _worker_session_id(repair_context_record)
    repair_agent = canonical_agent_name(str(repair_track.get("agent") or ""))
    for repair_round in range(1, repair_limit + 1):
        if verification.get("verdict") != "fail" or not _verification_is_repairable(verification):
            break
        if (
            _managed_retry_failure_kind(repair_context_record, verification=verification)
            in NON_REPAIRABLE_WORKER_FAILURE_KINDS
        ):
            break
        repair_budget = _dispatch_budget_assessment(
            managed_budget_policy,
            dispatch_started=dispatch_started,
            records=[worker_record, failover_record, *repair_worker_records],
            repair_rounds=repair_rounds_completed,
            same_failure_count=_same_verification_failure_count(verification_attempts),
            operation="repair",
        )
        if not bool(repair_budget.get("allowed", True)):
            budget_blocked = repair_budget
            break
        use_resume = bool(
            repair_strategy == "resume"
            and repair_agent == "codex"
            and current_session_id
        )
        repair_prompt_full = _build_dispatch_repair_prompt(
            plan=plan,
            verification=verification,
            verification_command=effective_verification_command,
            repair_round=repair_round,
            resume=use_resume,
            worker_record=repair_context_record,
        )
        if repair_agent == "mimo":
            repair_worker_record = _run_mimo_patch_attempt(
                workspace_root=workspace_path,
                plan_id=plan_id,
                mission_id=progress_mission_id,
                attempt=f"repair_{repair_round}_fresh",
                track=repair_track,
                plan=plan,
                cwd=worktree_project_root,
                timeout_seconds=worker_timeout_seconds,
                log_path=logs_dir / f"{track_token}-repair-{repair_round}-fresh.log",
                backend=repair_backend or resolve_failover_backend(),
                repo_map_text=repo_map_text,
                verification_command=effective_verification_command,
                prompt_override=repair_prompt_full,
            )
        else:
            repair_command = build_worker_command(
                plan=plan,
                track=repair_track,
                worktree=worktree_project_root,
                verification_command=effective_verification_command,
                prompt_override=repair_prompt_full,
                phase="repair",
                model_policy=model_policy,
                repo_map_text=None if use_resume else repo_map_text,
                reasoning_effort=reasoning_effort,
                dispatch_mode=dispatch_mode,
                prompt_style=prompt_style,
                codex_provider=active_codex_provider,
                resume_session_id=current_session_id if use_resume else None,
                execution_policy=closed_loop,
            )
            repair_argv = list(repair_command["argv"])
            if repair_executable:
                repair_argv[0] = repair_executable
            if repair_backend:
                repair_argv = _override_model_flag(repair_argv, repair_backend["model"])
            repair_env = _apply_toolchain_policy_env(repair_env, toolchain_policy)
            attempt_kind = "resume" if use_resume else "fresh"
            repair_worker_record = _run_worker_attempt(
                workspace_root=workspace_path,
                plan_id=plan_id,
                mission_id=progress_mission_id,
                attempt=f"repair_{repair_round}_{attempt_kind}",
                track=repair_track,
                argv=repair_argv,
                stdin_text=repair_command.get("stdin"),
                cwd=worktree_project_root,
                timeout_seconds=worker_timeout_seconds,
                log_path=logs_dir / f"{track_token}-repair-{repair_round}-{attempt_kind}.log",
                runner=runner,
                env=repair_env,
                backend=repair_backend,
                command_metadata=repair_command,
                dispatch_mode=dispatch_mode,
            )
            repair_worker_records.append(repair_worker_record)
            if use_resume and _resume_session_failed(repair_worker_record):
                fallback_prompt = _build_dispatch_repair_prompt(
                    plan=plan,
                    verification=verification,
                    verification_command=effective_verification_command,
                    repair_round=repair_round,
                    resume=False,
                    worker_record=repair_context_record,
                )
                fallback_command = build_worker_command(
                    plan=plan,
                    track=repair_track,
                    worktree=worktree_project_root,
                    verification_command=effective_verification_command,
                    prompt_override=fallback_prompt,
                    phase="repair",
                    model_policy=model_policy,
                    repo_map_text=repo_map_text,
                    reasoning_effort=reasoning_effort,
                    dispatch_mode=dispatch_mode,
                    prompt_style=prompt_style,
                    codex_provider=active_codex_provider,
                    execution_policy=closed_loop,
                )
                fallback_argv = list(fallback_command["argv"])
                if repair_executable:
                    fallback_argv[0] = repair_executable
                repair_worker_record = _run_worker_attempt(
                    workspace_root=workspace_path,
                    plan_id=plan_id,
                    mission_id=progress_mission_id,
                    attempt=f"repair_{repair_round}_fresh_fallback",
                    track=repair_track,
                    argv=fallback_argv,
                    stdin_text=fallback_command.get("stdin"),
                    cwd=worktree_project_root,
                    timeout_seconds=worker_timeout_seconds,
                    log_path=logs_dir / f"{track_token}-repair-{repair_round}-fresh-fallback.log",
                    runner=runner,
                    env=repair_env,
                    backend=repair_backend,
                    command_metadata=fallback_command,
                    dispatch_mode=dispatch_mode,
                )
                repair_worker_records.append(repair_worker_record)
        if repair_agent == "mimo" and isinstance(repair_worker_record, dict):
            repair_worker_records.append(repair_worker_record)
        current_session_id = _worker_session_id(repair_worker_record) or current_session_id
        repair_context_record = repair_worker_record or repair_context_record
        repair_rounds_completed += 1
        verification = run_dispatch_verification(
            workspace_root=workspace_path,
            verification_workspace_root=verification_workspace,
            plan_id=plan_id,
            mission_id=progress_mission_id,
            repo_root=worktree_project_root,
            run_profile=run_profile,
            include_slow=include_slow,
            max_workflows=max_workflows,
            codex_runner=codex_runner,
            failure_evidence_builder=failure_evidence_builder,
            test_command=test_command,
            verification_env=verification_env,
            worktree_base=worktree_base,
            allow_test_edits=allow_test_edits,
            timeout_seconds=timeout_seconds,
            trusted_workspace_snapshot=trusted_workspace_snapshot,
            base_probe_enabled=base_probe_enabled,
        )
        verification_attempts.append(verification)

    latest_verification = verification_attempts[-1]
    effective_worker_record = (
        repair_worker_record
        if isinstance(repair_worker_record, dict) and repair_worker_record.get("status") == "completed"
        else failover_record
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
    runtime_worker_records = [
        record
        for record in [worker_record, failover_record, *repair_worker_records]
        if isinstance(record, dict)
    ]
    usage_summary = summarize_worker_usage(runtime_worker_records)
    final_budget = budget_blocked or _dispatch_budget_assessment(
        managed_budget_policy,
        dispatch_started=dispatch_started,
        records=runtime_worker_records,
        repair_rounds=repair_rounds_completed,
        same_failure_count=_same_verification_failure_count(verification_attempts),
        operation="continue",
    )
    has_product_changes = _worktree_has_product_changes(worktree_project_root)
    has_prior_verified = bool(
        allow_prior_verified_evidence and _mission_has_prior_verified_evidence(workspace_path, plan_id)
    )
    toolchain_violation = _first_worker_toolchain_violation(
        [worker_record, failover_record, *repair_worker_records],
        test_command or verification_command,
    )
    status = "completed" if worker_completed else "worker_failed"
    latest_verdict = str(latest_verification.get("verdict") or "")
    verification_changed = latest_verification.get("changed_files")
    if not isinstance(verification_changed, list):
        diff = latest_verification.get("diff_summary") if isinstance(latest_verification.get("diff_summary"), dict) else {}
        verification_changed = diff.get("changed_files") if isinstance(diff.get("changed_files"), list) else []
    has_change_evidence = has_product_changes or bool(verification_changed) or has_prior_verified
    report_deliverable_verified = (
        bool(diagnosis_mode or review_plan_mode)
        and worker_completed
        and latest_verdict == "pass"
    )
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
        # Tests green + product changes present: treat as verified with a warning.
        # Long-host dogfood showed worker CLI often exits uncleanly after good edits;
        # leaving this as stopped/worker_failed_tests_pass blocks merge and confuses
        # users even when acceptance already passed.
        status = "verified"
    elif latest_verdict == "pass":
        status = "worker_failed"
    elif latest_verdict in {"coverage_gap", "no_workflows", "no_changed_files"}:
        status = "coverage_gap"
    elif latest_verdict == "inspection_only":
        # Inspection-only proves the page rendered, not that the product works.
        # It must never surface as verified (principle: dry-run cannot pose as
        # real acceptance).
        status = "inspection_only"
    if budget_blocked is not None and latest_verdict == "fail":
        status = (
            "managed_budget_exhausted"
            if str(budget_blocked.get("status") or "") == "exhausted"
            else "managed_usage_unknown"
        )
    # Never demote a successful verification to budget_exhausted. Token/time
    # budgets gate retries; they must not rewrite "tests passed + worker done"
    # into a failure that confuses users (completion-feel bug).
    if str(final_budget.get("status") or "") == "exhausted" and status in {
        "verification_failed",
        "worker_failed",
        "worker_failed_tests_pass",
        "coverage_gap",
        "inspection_only",
    }:
        status = "managed_budget_exhausted"

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
                timeout_seconds=timeout_seconds,
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
        # The isolation worktree has served its purpose once the change is
        # merged and re-verified on the target branch. Leaving it behind is how
        # a month of dogfooding accumulated dozens of full repo copies.
        if merge_result.get("status") == "merged" and status == "verified":
            from .worktree_gc import reap_mission_worktree

            merge_result["worktree_cleanup"] = reap_mission_worktree(repo_root, worktree, branch=branch)

    payload = {
        **preview,
        "status": status,
        "dry_run": False,
        "quota_exhausted": quota_exhausted,
        "worker_record": worker_record,
        "failover_worker_record": failover_record,
        "repair_worker_record": repair_worker_record,
        "repair_worker_records": repair_worker_records,
        "repair_rounds": repair_rounds_completed,
        "toolchain_violation": toolchain_violation,
        "verification_attempts": verification_attempts,
        "latest_verification": latest_verification,
        "usage_summary": usage_summary,
        "managed_runtime": {
            "schema_version": 1,
            "idempotency_key": managed_key,
            "transition_valid": True,
            "budget_status": str(final_budget.get("status") or "not_configured"),
            "budget": final_budget,
            "routing_evidence": _managed_routing_evidence(
                effective_worker_record,
                fallback=worker_command.get("routing_evidence"),
            ),
            "retry": _managed_retry_decision(
                effective_worker_record,
                verification=latest_verification,
                idempotency_key=managed_key,
                attempts_completed=max(1, len(runtime_worker_records)),
                max_attempts=(managed_budget_policy.max_attempts if managed_budget_policy else 1),
            ),
        },
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
    if (
        status == "verified"
        and not worker_completed
        and has_product_changes
        and latest_verdict == "pass"
    ):
        payload.setdefault("warnings", []).append(
            "Worker process did not report a clean completion, but product changes exist "
            "and the test command passed. Treated as verified; review the worktree before merge."
        )
    if status == "worker_failed_tests_pass":
        payload.setdefault("warnings", []).append(
            "Worker did not complete normally, but the current worktree changes passed the "
            "test command. Inspect the worktree manually before merging."
        )
    all_worker_records = runtime_worker_records
    session_ids = list(
        dict.fromkeys(session for session in (_worker_session_id(record) for record in all_worker_records) if session)
    )
    dispatch_record = {
        "schema_version": 1,
        "dispatch_id": f"{plan_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}",
        "plan_id": plan_id,
        "mission_id": progress_mission_id,
        "dispatch_mode": dispatch_mode,
        "prompt_style": prompt_style,
        "repair_strategy": repair_strategy,
        "resolved_model": effective_worker_record.get("resolved_model") or preview["worker"].get("resolved_model"),
        "resolved_provider": effective_worker_record.get("resolved_provider") or preview["worker"].get("resolved_provider"),
        "provider_source": effective_worker_record.get("provider_source") or preview["worker"].get("provider_source"),
        "resolved_reasoning_effort": preview["worker"].get("resolved_reasoning_effort"),
        "resolved_sandbox": preview["worker"].get("resolved_sandbox"),
        "resolved_approval": preview["worker"].get("resolved_approval"),
        "session_ids": session_ids,
        "worker_attempts": len(all_worker_records),
        "repair_rounds": repair_rounds_completed,
        "verification_attempts": len(verification_attempts),
        "usage": payload.get("usage_summary"),
        "project_memory_usage": dict(((plan.get("project_memory") or {}).get("usage") or {})),
        "managed_runtime": dict(payload.get("managed_runtime") or {}),
        "merge": dict(merge_result or {}),
        "worktree": dict(preview.get("worktree") or {}),
        "verdict": latest_verification.get("verdict"),
        "status": status,
        "elapsed_seconds": round(monotonic() - dispatch_started, 6),
    }
    try:
        dispatch_saved = append_dispatch_record(workspace_path, plan_id, dispatch_record)
        payload["dispatch_record"] = dispatch_saved["record"]
        payload["dispatch_record_path"] = dispatch_saved["path"]
    except OSError as exc:
        payload.setdefault("warnings", []).append(f"Dispatch ledger could not be written: {type(exc).__name__}: {exc}")
    append_task_memory_event(
        workspace_path,
        memory_id=dispatch_memory_id,
        event_type="dispatch_completed",
        data={
            "status": status,
            "verdict": str(latest_verification.get("verdict") or ""),
            "worker_attempts": len(all_worker_records),
            "verification_attempts": len(verification_attempts),
            "dispatch_id": str(dispatch_record.get("dispatch_id") or ""),
        },
        goal=str(plan.get("objective") or ""),
        repo_root=repo_root,
    )
    payload["task_memory"] = {
        "required": True,
        "memory_id": dispatch_memory_id,
        "event_count": int(read_task_memory(workspace_path, memory_id=dispatch_memory_id).get("event_count") or 0),
    }
    return payload


def _run_post_merge_command_verification(
    *,
    workspace_root: Path,
    plan_id: str,
    mission_id: str,
    repo_root: Path,
    command: str,
    verification_env: list[dict[str, Any]] | None = None,
    timeout_seconds: float = 900.0,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    save_mission_progress(
        workspace_root,
        mission_id,
        stage="verification_running",
        stage_label="Post-merge verification running",
        verification_command=command,
        activity="verification",
        activity_command=command,
        activity_started_at=now,
    )
    timeout_info = _verification_timeout_info(repo_root=repo_root, command=command, base_timeout=timeout_seconds)
    command_result = run_command_verification(
        command=command,
        repo_root=repo_root,
        timeout_seconds=timeout_info["timeout_seconds"],
        verification_env=verification_env,
        timeout_reason=str(timeout_info.get("reason") or ""),
        base_timeout_seconds=timeout_info["base_timeout_seconds"],
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
        "verification_timeout": timeout_info,
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
        activity="",
        activity_command="",
        activity_started_at="",
    )
    return payload


def _verification_timeout_info(*, repo_root: Path, command: str, base_timeout: float) -> dict[str, Any]:
    base = float(base_timeout)
    effective = estimate_verification_timeout(repo_root, command, base)
    reason = verification_timeout_reason(repo_root, command) or "base_timeout"
    return {
        "base_timeout_seconds": base,
        "timeout_seconds": effective,
        "reason": reason,
    }


def _dispatch_preflight_payload(
    *,
    raw_test_command: str,
    resolved_test_command: str,
    verification_profile: dict[str, Any] | None,
    test_command_unresolved: bool,
    verification_env: list[dict[str, Any]],
    missing_env: list[str],
    dependency_check: dict[str, Any],
    timeout_info: dict[str, Any],
    command_safety: dict[str, Any],
) -> dict[str, Any]:
    dependency_warnings = dependency_check.get("warnings") if isinstance(dependency_check.get("warnings"), list) else []
    command_safety_blocked = str(command_safety.get("status") or "") == "blocked"
    pytest_blocked = "pytest_not_importable" in [str(item) for item in dependency_warnings]
    status = (
        "blocked"
        if test_command_unresolved or missing_env or command_safety_blocked or pytest_blocked
        else ("warning" if dependency_warnings else "ok")
    )
    return {
        "schema_version": 1,
        "status": status,
        "test_command": {
            "status": "unresolved" if test_command_unresolved else ("resolved" if resolved_test_command else "not_requested"),
            "requested": raw_test_command,
            "resolved": resolved_test_command,
            "profile": verification_profile or {},
        },
        "verification_env": {
            "status": "missing" if missing_env else "ok",
            "declared": verification_env,
            "missing_env_vars": missing_env,
        },
        "dependency": dependency_check,
        "verification_timeout": timeout_info,
        "command_safety": command_safety,
    }


def _dispatch_preflight_block(preflight: dict[str, Any]) -> dict[str, str] | None:
    managed_budget = (
        preflight.get("managed_budget")
        if isinstance(preflight.get("managed_budget"), dict)
        else {}
    )
    if str(managed_budget.get("status") or "") == "blocked":
        return {
            "reason": str(managed_budget.get("reason") or "managed_budget_invalid"),
            "message": str(managed_budget.get("message") or "Managed budget is invalid."),
        }
    strict_acceptance = (
        preflight.get("strict_acceptance")
        if isinstance(preflight.get("strict_acceptance"), dict)
        else {}
    )
    if str(strict_acceptance.get("status") or "") == "blocked":
        return {
            "reason": str(strict_acceptance.get("reason") or "weak_command_gate"),
            "message": str(strict_acceptance.get("message") or "Strict acceptance blocked dispatch."),
        }
    execution_alignment = (
        preflight.get("execution_alignment")
        if isinstance(preflight.get("execution_alignment"), dict)
        else {}
    )
    if str(execution_alignment.get("status") or "") == "blocked":
        issues = execution_alignment.get("issues") if isinstance(execution_alignment.get("issues"), list) else []
        codes = ", ".join(str(item.get("code") or "") for item in issues[:5] if isinstance(item, dict))
        return {
            "reason": "execution_alignment_prompt_restriction",
            "message": (
                "Worker prompt alignment blocked dispatch because exploration-limiting language was found"
                + (f": {codes}" if codes else ".")
            ),
        }
    test_command = preflight.get("test_command") if isinstance(preflight.get("test_command"), dict) else {}
    if str(test_command.get("status") or "") == "unresolved":
        requested = str(test_command.get("requested") or "auto")
        profile = test_command.get("profile") if isinstance(test_command.get("profile"), dict) else {}
        if str(profile.get("status") or "") == "pytest_unavailable":
            return {
                "reason": "pytest_not_importable",
                "message": (
                    f"验收命令 `{requested}` 需要 pytest，但本机没有可用的 Python 能 `import pytest`。"
                    "修复：在项目 venv 执行 `python -m pip install pytest`，"
                    "或把 --test-command 写成绝对路径，例如 "
                    f"`\"{Path(sys.executable)} -m pytest -q\"`。"
                ),
            }
        return {
            "reason": "test_command_unresolved",
            "message": (
                f"Test command `{requested}` could not be resolved. "
                "Pass an explicit --test-command or add a package/test config that Pacer can detect."
            ),
        }
    dependency = preflight.get("dependency") if isinstance(preflight.get("dependency"), dict) else {}
    dep_warnings = dependency.get("warnings") if isinstance(dependency.get("warnings"), list) else []
    if "pytest_not_importable" in [str(item) for item in dep_warnings]:
        resolved = str(test_command.get("resolved") or test_command.get("requested") or "python -m pytest -q")
        return {
            "reason": "pytest_not_importable",
            "message": (
                f"验收命令 `{resolved}` 绑定的 Python 无法 import pytest。"
                "这不是产品代码失败。修复：安装 pytest，或换用带 pytest 的解释器绝对路径后 resume。"
            ),
        }
    verification_env = preflight.get("verification_env") if isinstance(preflight.get("verification_env"), dict) else {}
    missing = verification_env.get("missing_env_vars") if isinstance(verification_env.get("missing_env_vars"), list) else []
    if missing:
        names = ", ".join(str(item) for item in missing)
        return {
            "reason": "verification_environment_missing",
            "message": f"设置环境变量 {names} 后重试；不要修改产品代码、测试或 eval 脚本来绕过验收环境。",
        }
    command_safety = preflight.get("command_safety") if isinstance(preflight.get("command_safety"), dict) else {}
    if str(command_safety.get("status") or "") == "blocked":
        return {
            "reason": str(command_safety.get("reason") or "command_safety_blocked"),
            "message": str(command_safety.get("message") or "Verification command safety check blocked dispatch."),
        }
    return None


def _missing_declared_verification_env(verification_env: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for item in verification_env:
        if item.get("kind") != "env_var":
            continue
        name = str(item.get("name") or "").strip()
        if name and not os.environ.get(name):
            missing.append(name)
    return missing


def _managed_budget_policy(value: Any) -> ManagedBudgetPolicy:
    if not isinstance(value, dict):
        raise ValueError("managed_budget must be an object")
    return ManagedBudgetPolicy(
        max_wall_seconds=float(value.get("max_wall_seconds") or 0),
        max_total_tokens=int(value.get("max_total_tokens") or 0),
        max_attempts=int(value.get("max_attempts") or 0),
        max_repair_rounds=int(value.get("max_repair_rounds") or 0),
        max_same_failure_count=int(
            value["max_same_failure_count"] if "max_same_failure_count" in value else 2
        ),
    )


def _dispatch_budget_assessment(
    policy: ManagedBudgetPolicy | None,
    *,
    dispatch_started: float,
    records: list[dict[str, Any] | None],
    repair_rounds: int,
    same_failure_count: int,
    operation: str,
) -> dict[str, Any]:
    if policy is None:
        return {
            "allowed": True,
            "status": "not_configured",
            "operation": operation,
            "reason_codes": [],
        }
    worker_records = [item for item in records if isinstance(item, dict)]
    usage_complete = all(isinstance(item.get("usage"), dict) for item in worker_records)
    usage_summary = summarize_worker_usage(worker_records)
    usage = ManagedBudgetUsage(
        elapsed_seconds=max(0.0, monotonic() - dispatch_started),
        total_tokens=(int(usage_summary.get("total_tokens") or 0) if usage_complete else None),
        attempts=len(worker_records),
        repair_rounds=max(0, int(repair_rounds)),
        same_failure_count=max(0, int(same_failure_count)),
    )
    assessment = assess_managed_budget(policy, usage, operation=operation).to_dict()
    assessment["usage_complete"] = usage_complete
    assessment["usage"] = {
        "elapsed_seconds": round(usage.elapsed_seconds, 6),
        "total_tokens": usage.total_tokens,
        "attempts": usage.attempts,
        "repair_rounds": usage.repair_rounds,
        "same_failure_count": usage.same_failure_count,
    }
    return assessment


def _same_verification_failure_count(attempts: list[dict[str, Any]]) -> int:
    signatures = [_verification_failure_signature(item) for item in attempts]
    if not signatures or not signatures[-1]:
        return 0
    latest = signatures[-1]
    count = 0
    for signature in reversed(signatures):
        if signature != latest:
            break
        count += 1
    return count


def _verification_failure_signature(verification: dict[str, Any]) -> str:
    if str(verification.get("verdict") or "") != "fail":
        return ""
    command = (
        verification.get("command_verification")
        if isinstance(verification.get("command_verification"), dict)
        else {}
    )
    repair = (
        verification.get("repair_brief")
        if isinstance(verification.get("repair_brief"), dict)
        else {}
    )
    failed_step = repair.get("failed_step") if isinstance(repair.get("failed_step"), dict) else {}
    material = "|".join(
        [
            str(command.get("failure_kind") or repair.get("failure_kind") or ""),
            str(repair.get("workflow") or ""),
            str(failed_step.get("id") or ""),
            str(repair.get("message") or command.get("output_tail") or "")[:1000],
        ]
    )
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


def _managed_routing_evidence(record: dict[str, Any], *, fallback: Any) -> dict[str, Any]:
    value = record.get("routing_evidence")
    if isinstance(value, dict):
        return dict(value)
    return dict(fallback) if isinstance(fallback, dict) else {}


def _managed_retry_decision(
    worker: dict[str, Any],
    *,
    verification: dict[str, Any],
    idempotency_key: str,
    attempts_completed: int,
    max_attempts: int,
) -> dict[str, Any]:
    failure_kind = _managed_retry_failure_kind(worker, verification=verification)
    fraction_material = f"{idempotency_key}:{attempts_completed}".encode("utf-8")
    fraction_bits = int(hashlib.sha256(fraction_material).hexdigest()[:13], 16)
    random_fraction = fraction_bits / float((16**13) - 1)
    decision = evaluate_retry(
        failure_kind,
        attempts_completed=max(1, int(attempts_completed)),
        max_attempts=max(1, int(max_attempts)),
        factor_seconds=1.0,
        maximum_seconds=600.0,
        random_fraction=random_fraction,
    ).to_dict()
    if decision.get("retry"):
        decision["scheduled_at"] = (
            datetime.now(timezone.utc)
            + timedelta(seconds=float(decision.get("delay_seconds") or 0.0))
        ).isoformat()
    decision["current_attempt_status"] = str(worker.get("status") or "unknown")
    decision["current_attempt_remains_failed"] = str(worker.get("status") or "") != "completed"
    return decision


def _managed_retry_failure_kind(
    worker: dict[str, Any],
    *,
    verification: dict[str, Any],
) -> str:
    repair = (
        verification.get("repair_brief")
        if isinstance(verification.get("repair_brief"), dict)
        else {}
    )
    if str(repair.get("source") or "") in {
        "test_tampering",
        "acceptance_chain_tampering",
        "scope_violation",
        "workspace_tamper",
    }:
        return "evidence_rejected"
    status = str(worker.get("status") or "")
    stdout = str(worker.get("stdout_tail") or "")
    stderr = str(worker.get("stderr_tail") or "")
    text = "\n".join([stdout, stderr]).lower()
    if looks_like_quota_exhaustion(stdout, stderr):
        return "provider_rate_limit"
    if any(
        marker in text
        for marker in (
            "not logged in",
            "not authenticated",
            "authentication required",
            "please run /login",
            "please run `codex login`",
            "please run codex login",
        )
    ):
        return "not_authenticated"
    if any(
        marker in text
        for marker in (
            "internal server error",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
        )
    ):
        return "provider_5xx"
    if "timed out after" in text or "network timeout" in text or "connection timed out" in text:
        return "network_timeout"
    if status == "crashed":
        return "process_crash"
    if status == "completed":
        return "verification_failed" if str(verification.get("verdict") or "") == "fail" else "none"
    return "worker_failed"


def _verification_is_repairable(verification: dict[str, Any]) -> bool:
    repair = verification.get("repair_brief") if isinstance(verification.get("repair_brief"), dict) else {}
    source = str(repair.get("source") or "")
    failure_kind = str(repair.get("failure_kind") or "")
    if repair.get("repairable") is False:
        return False
    if source in {"test_tampering", "acceptance_chain_tampering"}:
        return False
    if failure_kind in NON_REPAIRABLE_COMMAND_FAILURE_KINDS:
        return False
    return True


def _repair_round_limit(*, auto_repair_once: bool, max_repair_rounds: int | None) -> int:
    if max_repair_rounds is None:
        return 1 if auto_repair_once else 0
    return max(0, int(max_repair_rounds))


def _worker_session_id(record: dict[str, Any] | None) -> str:
    if not isinstance(record, dict):
        return ""
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    return str(usage.get("session_id") or record.get("session_id") or "").strip()


def _repair_evidence_text(verification: dict[str, Any]) -> str:
    command = verification.get("command_verification") if isinstance(verification.get("command_verification"), dict) else {}
    repair = verification.get("repair_brief") if isinstance(verification.get("repair_brief"), dict) else {}
    raw = str(
        command.get("raw_output_tail")
        or command.get("output_tail")
        or repair.get("raw_evidence_tail")
        or verification.get("markdown")
        or repair.get("message")
        or ""
    )
    redacted = redact_secret_text(raw)
    encoded = redacted.encode("utf-8", errors="replace")
    if len(encoded) <= 32768:
        return redacted
    return encoded[-32768:].decode("utf-8", errors="ignore")


def _build_dispatch_repair_prompt(
    *,
    plan: dict[str, Any],
    verification: dict[str, Any],
    verification_command: str,
    repair_round: int,
    resume: bool,
    worker_record: dict[str, Any] | None,
) -> str:
    repair = verification.get("repair_brief") if isinstance(verification.get("repair_brief"), dict) else {}
    command = verification.get("command_verification") if isinstance(verification.get("command_verification"), dict) else {}
    summary = str(repair.get("repair_prompt") or repair.get("message") or "Verification failed.").strip()
    summary = summary.replace("Do not apply changes.", "").strip()
    evidence = _repair_evidence_text(verification)
    diff = verification.get("diff_summary") if isinstance(verification.get("diff_summary"), dict) else {}
    changed = diff.get("changed_files") if isinstance(diff.get("changed_files"), list) else []
    lines = [
        f"Repair round {repair_round}. Continue implementing a fix for the failed verification.",
        f"Verification verdict: {verification.get('verdict') or 'fail'}",
    ]
    failed_command = str(command.get("command") or verification_command or "").strip()
    if failed_command:
        lines.append("Failed verification command: " + failed_command)
    if not resume:
        lines.append("Objective: " + str(plan.get("objective") or ""))
        criteria = plan.get("acceptance_criteria") if isinstance(plan.get("acceptance_criteria"), list) else []
        if criteria:
            lines.append("Acceptance criteria:")
            lines.extend(f"- {item}" for item in criteria)
    if changed:
        lines.append("Files already changed in this worktree: " + ", ".join(str(item) for item in changed[:50]))
    if isinstance(worker_record, dict) and worker_record.get("log_path"):
        lines.append("Prior worker log: " + str(worker_record.get("log_path")))
    if summary:
        lines.extend(["Repair guidance:", summary])
    if evidence:
        lines.extend(["Raw failure evidence (UTF-8 tail, secrets redacted):", evidence])
    lines.extend(
        [
            "Make the smallest correct product change without weakening tests or acceptance configuration.",
            "After repairing, run the full verification command again:",
            verification_command,
        ]
    )
    return "\n".join(lines)


def _resume_session_failed(record: dict[str, Any] | None) -> bool:
    if not isinstance(record, dict) or record.get("status") == "completed":
        return False
    from .codex_exec import is_resume_unavailable_error

    return is_resume_unavailable_error(record.get("stdout_tail"), record.get("stderr_tail"))


def _worktree_has_product_changes(worktree: Path) -> bool:
    change_set = collect_repository_change_set(repo_root=worktree, base_ref="HEAD")
    if not change_set.complete:
        return False
    for fact in change_set.changes:
        path = fact.path
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

    Codex reports cumulative usage when a session is resumed. Token counters for
    the same session therefore use their maximum observed value, while fresh
    sessions and records without a session id remain additive.
    """
    token_fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    token_totals = {field: 0 for field in token_fields}
    session_totals: dict[str, dict[str, int]] = {}
    num_turns = 0
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
        num_turns += int(usage.get("num_turns") or 0)
        sample = {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
            "reasoning_output_tokens": int(usage.get("reasoning_output_tokens") or 0),
        }
        sample["total_tokens"] = (
            int(usage.get("total_tokens") or 0)
            if usage.get("total_tokens") is not None
            else sample["input_tokens"]
            + sample["output_tokens"]
            + sample["cache_read_tokens"]
            + sample["cache_creation_tokens"]
        )
        session_id = _worker_session_id(record)
        if session_id:
            session = session_totals.setdefault(session_id, {field: 0 for field in token_fields})
            for field in token_fields:
                session[field] = max(session[field], sample[field])
        else:
            for field in token_fields:
                token_totals[field] += sample[field]
        cost = float(usage.get("cost_usd") or 0.0)
        if usage.get("cost_is_savings"):
            saved_usd += cost
        else:
            spent_usd += cost
    for session in session_totals.values():
        for field in token_fields:
            token_totals[field] += session[field]
    return {
        "attempts_with_usage": attempts,
        "input_tokens": token_totals["input_tokens"],
        "output_tokens": token_totals["output_tokens"],
        "cache_read_tokens": token_totals["cache_read_tokens"],
        "cache_creation_tokens": token_totals["cache_creation_tokens"],
        "reasoning_output_tokens": token_totals["reasoning_output_tokens"],
        "num_turns": num_turns,
        "total_tokens": token_totals["total_tokens"],
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
_POLICY_TIER_TASK_KIND = {
    "cheap": "fast",
    "fast": "fast",
    "standard": "balanced",
    "balanced": "balanced",
    "strong": "implementation",
    "multimodal": "implementation",
}


def _task_kind_for_phase(phase: str, model_policy: dict[str, Any] | None) -> str:
    policy = model_policy if isinstance(model_policy, dict) else {}
    tier = str(policy.get(phase) or ("strong" if phase != "classification" else "fast")).strip().lower()
    return _POLICY_TIER_TASK_KIND.get(tier, "implementation")


def _explicit_model_for_phase(phase: str, model_policy: dict[str, Any] | None) -> str:
    policy = model_policy if isinstance(model_policy, dict) else {}
    value = str(policy.get(phase) or "").strip()
    if value.lower() in {*_POLICY_TIER_TASK_KIND, "", "inherit"}:
        return ""
    return value


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
    reasoning_effort: str | None = None,
    dispatch_mode: str = "tracked",
    prompt_style: str = "expanded",
    resume_session_id: str | None = None,
    codex_provider: str = "inherit",
    execution_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    agent = canonical_agent_name(str(track.get("agent") or "codex"))
    profile = load_agent_profile(agent) or {}
    task_kind = _task_kind_for_phase(phase, model_policy)
    config = recommend_worker_config(profile, task_kind=task_kind) if profile else {}
    explicit_model = _explicit_model_for_phase(phase, model_policy)
    prompt = prompt_override or build_worker_prompt(
        plan=plan,
        track=track,
        worktree=worktree,
        verification_command=verification_command,
        dispatch_mode=dispatch_mode,
        prompt_style=prompt_style,
    )
    if prompt_override:
        prompt = _with_dispatch_project_memory(prompt, plan)
    if str(prompt_suffix or "").strip():
        prompt = prompt.rstrip() + "\n\n" + str(prompt_suffix).strip()
    if repo_map_text:
        prompt = (
            prompt
            + "\n\nRepository map (DevPacer local index excerpt, current at dispatch; use it as"
            " a starting point, then inspect the real repository wherever needed):\n"
            + repo_map_text
        )
    selection = track.get("model_selection") if isinstance(track.get("model_selection"), dict) else {}
    selected = selection.get("selected") if isinstance(selection.get("selected"), dict) else {}
    if agent != "codex":
        # Profile-driven headless command (e.g. Claude Code: `claude -p ...`).
        # Any agent with a headless profile becomes an executable worker.
        headless = profile.get("headless") if isinstance(profile.get("headless"), dict) else {}
        if headless.get("command"):
            headless = _headless_with_execution_policy(headless, execution_policy)
            argv = _build_headless_argv(
                headless=headless,
                config=config,
                track=track,
                phase=phase,
                prompt=prompt,
                verification_command=verification_command,
            )
            headless_permission = _headless_permission_mode(argv)
            resolved_model = _resolved_or_inherited(
                str(
                    track.get("model")
                    or selected.get("model")
                    or config.get("model")
                    or ""
                ),
                "model",
            )
            sandbox = track.get("sandbox") if isinstance(track.get("sandbox"), dict) else {}
            approval = track.get("approval") if isinstance(track.get("approval"), dict) else {}
            return {
                "argv": argv,
                "display": format_argv(argv),
                "resolved_model": resolved_model,
                "resolved_reasoning_effort": str(reasoning_effort or track.get("reasoning_effort") or config.get("reasoning_effort") or "inherit"),
                "model_source": "command" if resolved_model != "inherited(model)" else "profile",
                "resolved_provider": agent,
                "provider_source": "agent_profile",
                "resolved_sandbox": headless_permission or str(sandbox.get("name") or ""),
                "sandbox_source": "agent_profile.headless" if headless_permission else "track",
                "resolved_approval": headless_permission or str(approval.get("name") or ""),
                "approval_source": "agent_profile.headless" if headless_permission else "track",
                "session_mode": "new",
                "routing_evidence": routing_request_evidence(
                    selection,
                    requested_provider=agent,
                    requested_model=resolved_model,
                ),
            }
        # Non-headless agents still have to state who ran the task; without a
        # provider and routing evidence the mission journey cannot bind the
        # routing decision to the worker that executed it.
        fallback_model = _resolved_or_inherited(str(track.get("model") or ""), "model")
        return {
            "argv": [agent, prompt],
            "display": format_argv([agent, prompt]),
            "resolved_model": fallback_model,
            "resolved_reasoning_effort": str(reasoning_effort or track.get("reasoning_effort") or "inherit"),
            "resolved_provider": agent,
            "provider_source": "agent_profile",
            "routing_evidence": routing_request_evidence(
                selection,
                requested_provider=agent,
                requested_model=fallback_model,
            ),
        }
    # Sandbox and approval are root Codex options.  ``codex exec`` accepts the
    # sandbox flag too, but ``codex exec resume`` does not, so keeping both
    # policies before ``exec`` preserves the same safety posture on repairs.
    argv = ["codex"]
    for source in (track.get("sandbox"), track.get("approval")):
        flag = str((source or {}).get("flag") or "").strip() if isinstance(source, dict) else ""
        if flag:
            argv.extend(shlex.split(flag, posix=False))
    argv.append("exec")
    if resume_session_id:
        argv.append("resume")
    argv.append("--json")
    selected_provider = str(selected.get("provider") or "").strip()
    provider = str(codex_provider or "inherit").strip()
    if provider.lower() == "inherit" and selected_provider:
        provider = selected_provider
    if provider.lower() != "inherit":
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", provider):
            raise ValueError("codex_provider must be a simple provider id")
        # A TOML literal string survives the Windows npm .cmd shim unchanged;
        # JSON's double quotes are escaped into literal backslashes by cmd.exe.
        argv.extend(["-c", f"model_provider='{provider}'"])
    # The implementation phase honors any model already chosen for the track; other
    # phases (e.g. repair) follow the model_policy tier so a cheaper model can be
    # used when the policy asks for it.
    if explicit_model:
        model = explicit_model
    elif resume_session_id:
        # Keep a resumed thread on the routed implementation model unless the
        # repair policy names a concrete replacement.
        model = str(track.get("model") or selected.get("model") or config.get("model") or "")
    elif phase == "implementation":
        model = str(track.get("model") or config.get("model") or "")
    else:
        model = str(config.get("model") or track.get("model") or "")
    model = "" if model.strip().lower() == "inherit" else model.strip()
    if model:
        argv.extend(["--model", model])
    if reasoning_effort is not None:
        effort = str(reasoning_effort).strip()
        effort_source = "request"
    elif phase == "implementation":
        effort = str(track.get("reasoning_effort") or config.get("reasoning_effort") or "inherit").strip()
        effort_source = "track" if track.get("reasoning_effort") else "profile"
    else:
        effort = str(config.get("reasoning_effort") or track.get("reasoning_effort") or "inherit").strip()
        effort_source = "profile" if config.get("reasoning_effort") else "track"
    effort = effort or "inherit"
    if effort.lower() != "inherit":
        argv.extend(["-c", f"model_reasoning_effort={effort}"])
    # Codex can read the initial prompt from stdin when PROMPT is "-".  Keeping
    # large repo maps out of argv avoids Windows' command-line length limit.
    if resume_session_id:
        argv.append(str(resume_session_id))
    argv.append("-")
    user_defaults = _codex_user_defaults()
    resolved_model = model or str(user_defaults.get("model") or "inherited(config.toml)")
    resolved_effort = effort if effort.lower() != "inherit" else str(
        user_defaults.get("reasoning_effort") or "inherited(config.toml)"
    )
    resolved_provider = provider if provider.lower() != "inherit" else str(
        user_defaults.get("provider") or "inherited(config.toml)"
    )
    routing_evidence = routing_request_evidence(
        selection,
        requested_provider=resolved_provider,
        requested_model=resolved_model,
    )
    resolved_sandbox, sandbox_source = _resolved_codex_policy(
        track.get("sandbox"),
        option_names=("--sandbox", "-s"),
        inherited=str(user_defaults.get("sandbox") or "inherited(config.toml)"),
    )
    resolved_approval, approval_source = _resolved_codex_policy(
        track.get("approval"),
        option_names=("--ask-for-approval", "-a"),
        inherited=str(user_defaults.get("approval") or "inherited(config.toml)"),
    )
    return {
        "argv": argv,
        "display": format_argv(argv) + " < prompt via stdin",
        "stdin": prompt,
        "resolved_model": resolved_model,
        "resolved_reasoning_effort": resolved_effort,
        "model_source": "command" if model else "config.toml",
        "resolved_provider": resolved_provider,
        "provider_source": "command" if provider.lower() != "inherit" else "config.toml",
        "reasoning_effort_source": effort_source if effort.lower() != "inherit" else "config.toml",
        "resolved_sandbox": resolved_sandbox,
        "sandbox_source": sandbox_source,
        "resolved_approval": resolved_approval,
        "approval_source": approval_source,
        "session_mode": "resume" if resume_session_id else "new",
        "json_output": True,
        "routing_evidence": routing_evidence,
    }


def _resolved_codex_policy(
    source: Any,
    *,
    option_names: tuple[str, ...],
    inherited: str,
) -> tuple[str, str]:
    payload = source if isinstance(source, dict) else {}
    name = str(payload.get("name") or "").strip()
    flag = str(payload.get("flag") or "").strip()
    if name:
        return name, "track"
    if flag:
        tokens = shlex.split(flag, posix=False)
        for index, token in enumerate(tokens[:-1]):
            if token in option_names:
                return str(tokens[index + 1]).strip('"'), "track"
        return flag, "track"
    return inherited, "config.toml"


def _override_model_flag(argv: list[str], model: str) -> list[str]:
    argv = list(argv)
    for i, token in enumerate(argv):
        if token == "--model" and i + 1 < len(argv):
            argv[i + 1] = model
            return argv
    return argv


def _headless_permission_mode(argv: list[str]) -> str:
    for index, token in enumerate(argv):
        if token == "--permission-mode" and index + 1 < len(argv):
            return str(argv[index + 1]).strip()
        if token == "--dangerously-skip-permissions":
            return "bypassPermissions"
    return ""


def _headless_with_execution_policy(
    headless: dict[str, Any],
    execution_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    policy = execution_policy if isinstance(execution_policy, dict) else {}
    raw_mode = str(
        policy.get("permission_mode")
        or policy.get("claude_permission_mode")
        or ""
    ).strip()
    mode = _normalize_claude_permission_mode(raw_mode)
    if not mode:
        return dict(headless)
    effective = dict(headless)
    effective["permission_flag"] = f"--permission-mode {mode}"
    if mode == "bypassPermissions":
        # In full-permission mode, do not re-add a narrow Bash allow-list that
        # turns normal development commands into denials or manual prompts.
        effective["allow_verification_command"] = False
    if str(policy.get("tool_permissions") or "").strip().lower() == "default":
        effective["extra_flags"] = _replace_headless_tools_with_default(
            str(effective.get("extra_flags") or "")
        )
    return effective


def _normalize_claude_permission_mode(value: str) -> str:
    normalized = value.strip().replace("-", "_").replace(" ", "_").lower()
    aliases = {
        "yolo": "bypassPermissions",
        "bypass": "bypassPermissions",
        "bypasspermissions": "bypassPermissions",
        "bypass_permissions": "bypassPermissions",
        "danger": "bypassPermissions",
        "danger_full_access": "bypassPermissions",
        "dangerously_skip_permissions": "bypassPermissions",
        "accept_edits": "acceptEdits",
        "acceptedits": "acceptEdits",
        "auto": "auto",
        "manual": "manual",
        "dontask": "dontAsk",
        "dont_ask": "dontAsk",
        "plan": "plan",
    }
    return aliases.get(normalized, "")


def _replace_headless_tools_with_default(flags: str) -> str:
    tokens = shlex.split(str(flags or ""), posix=False)
    result: list[str] = []
    index = 0
    replaced = False
    while index < len(tokens):
        token = tokens[index]
        if token in {"--tools", "--allowedTools", "--allowed-tools"}:
            if token == "--tools":
                result.extend(["--tools", "default"])
                replaced = True
            index += 1
            while index < len(tokens) and not str(tokens[index]).startswith("-"):
                index += 1
            continue
        result.append(token)
        index += 1
    if not replaced:
        result.extend(["--tools", "default"])
    return " ".join(result)


def _build_headless_argv(
    *,
    headless: dict[str, Any],
    config: dict[str, Any],
    track: dict[str, Any],
    phase: str,
    prompt: str,
    verification_command: str = "",
) -> list[str]:
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
    permission_flag = str(headless.get("permission_flag") or "").strip()
    if permission_flag:
        argv.extend(shlex.split(permission_flag, posix=False))
    exact_verification = str(verification_command or "").strip()
    if (
        bool(headless.get("allow_verification_command"))
        and exact_verification
        and "\n" not in exact_verification
        and "\r" not in exact_verification
    ):
        argv.extend(["--allowedTools", f"Bash({exact_verification})"])
    # Claude's --allowedTools accepts multiple values and consumes positional
    # arguments until the next option. Keep another option after it so the
    # final positional prompt cannot be swallowed as an allowed-tool pattern.
    extra_flags = str(headless.get("extra_flags") or "").strip()
    if extra_flags:
        argv.extend(shlex.split(extra_flags, posix=False))
    argv.append(prompt)
    return argv


def build_worker_prompt(
    *,
    plan: dict[str, Any],
    track: dict[str, Any],
    worktree: Path,
    verification_command: str,
    dispatch_mode: str = "tracked",
    prompt_style: str = "expanded",
) -> str:
    dispatch_mode = _normalized_choice(dispatch_mode, DISPATCH_MODES, field="dispatch_mode")
    prompt_style = _normalized_choice(prompt_style, PROMPT_STYLES, field="prompt_style")
    criteria = plan.get("acceptance_criteria") if isinstance(plan.get("acceptance_criteria"), list) else []
    selected = plan.get("selected_workflows") if isinstance(plan.get("selected_workflows"), list) else []
    from .pacer_voice import agent_completion_debate_block, agent_safety_context_lines

    exact_verification = str(verification_command or "").strip()
    self_verification_instruction = (
        "The worker process already starts in the worktree. To self-verify, run exactly this command once "
        f"with no cd prefix or shell wrapper: {exact_verification}"
        if exact_verification
        else "Pacer independently runs acceptance after your edits."
    )
    lines = [
        f"Objective: {plan.get('objective') or ''}",
        f"Plan id: {plan.get('plan_id') or ''}",
        f"Worker mode: {dispatch_mode}",
        f"Worktree: {worktree}",
        "Explore and implement with full senior-engineer autonomy. Pacer does not micromanage your intermediate steps.",
        self_verification_instruction,
        "If a shell or tool action is denied, do not retry equivalent command variants; Pacer will run acceptance.",
    ]
    if re.search(
        r"(?:^|\s)(?:docker(?:\.exe)?|docker-compose(?:\.exe)?)(?:\s|$)",
        exact_verification,
        flags=re.IGNORECASE,
    ):
        lines.append(
            "Docker acceptance note: the worker sandbox may not have access to the host Docker daemon. "
            "If Docker reports a daemon/socket permission error, do not treat it as a product failure or "
            "retry command variants; Pacer runs the exact acceptance command from the host verifier after "
            "your turn."
        )
    if dispatch_mode == "tracked":
        lines.insert(3, f"Worker track: {track.get('id') or ''} ({track.get('agent') or ''})")
    if selected:
        lines.append("Related verification workflows (context): " + ", ".join(str(item) for item in selected))
    if criteria:
        lines.append("Acceptance criteria (completion debate topics, not step-by-step orders):")
        lines.extend(f"- {item}" for item in criteria)
    scoped: list[str] = []
    if dispatch_mode == "tracked":
        objective_paths = [
            item for item in _extract_context_paths(str(plan.get("objective") or ""))
            if not _is_ignored_context_path(item) and not _is_non_product_path(item)
        ]
        changed = plan.get("changed_files") if isinstance(plan.get("changed_files"), list) else []
        scoped_limit = 12 if prompt_style == "legacy" else 30
        for item in [*objective_paths, *(str(raw) for raw in changed)]:
            normalized = str(item).replace("\\", "/").strip()
            if not normalized or normalized in scoped:
                continue
            if normalized.startswith(".agent-workspace") or _is_ignored_context_path(normalized) or _is_non_product_path(normalized):
                continue
            scoped.append(normalized)
            if len(scoped) >= scoped_limit:
                break
        if scoped:
            lines.append("Optional context — likely-relevant files (guidance only, not a whitelist or cage):")
            lines.extend(f"- {item}" for item in scoped)
    if dispatch_mode == "delegated" or not scoped:
        lines.append(
            "No file whitelist is imposed. Choose the implementation surface yourself from the objective and repository."
        )
    memory_lines = _dispatch_project_memory_lines(plan)
    if memory_lines:
        lines.extend(memory_lines)
    lines.extend(agent_safety_context_lines())
    lines.extend([""])
    lines.extend(agent_completion_debate_block(verification_command=str(verification_command or "")))
    lines.extend([
        "",
        "If a conversation turn arrives with no user message — only system-reminder content or empty content — "
        "treat it as a session-continuation signal. Resume the current active task silently. "
        "Do not output a 'no user message' notice.",
    ])
    toolchain_policy = _toolchain_policy_for_command(verification_command)
    if toolchain_policy:
        # Keep toolchain alignment as completion evidence, not mid-flight nagging.
        lines.extend(
            [
                "",
                "If verification names an exact SDK executable, prefer that same toolchain when you run related checks,",
                f"so completion evidence matches what the user will run ({toolchain_policy.get('expected_executable')}).",
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


def _restricted_worker_changes(*, repo_root: Path, base_ref: str | None) -> tuple[list[str], str]:
    base = str(base_ref or "HEAD").strip() or "HEAD"
    change_set = collect_repository_change_set(repo_root=repo_root, base_ref=base)
    if not change_set.complete:
        return [], ", ".join(change_set.errors) or "change_set_incomplete"
    paths: set[str] = set()
    for fact in change_set.changes:
        normalized = fact.path
        # Pacer prepares an untracked verification workspace before the worker
        # starts. Tracked workspace edits remain scope errors.
        if ".agent-workspace" in normalized.split("/") and not _git_path_is_tracked(
            repo_root,
            normalized,
        ):
            continue
        if normalized == ".agent-workspace/workspace.json":
            continue
        paths.add(normalized)
    tracked_restricted, tracked_error = _tracked_restricted_worker_changes(
        repo_root=repo_root,
        base_ref=base,
    )
    if tracked_error:
        return [], tracked_error
    paths.update(tracked_restricted)
    return sorted(path for path in paths if _is_restricted_worker_path(path)), ""


def _tracked_restricted_worker_changes(
    *,
    repo_root: Path,
    base_ref: str,
) -> tuple[list[str], str]:
    """Recover tracked runtime changes intentionally omitted from source facts."""

    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "diff",
                "--name-only",
                "-z",
                "--relative",
                "--no-renames",
                base_ref,
                "--",
                ".",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=10.0,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return [], "tracked_scope_diff_unavailable"
    if completed.returncode != 0:
        return [], "tracked_scope_diff_failed"
    try:
        output = completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return [], "tracked_scope_diff_invalid_utf8"
    paths = {
        item.replace("\\", "/").strip().lstrip("/")
        for item in output.split("\0")
        if item.strip()
    }
    paths.discard(".agent-workspace/workspace.json")
    return sorted(path for path in paths if _is_restricted_worker_path(path)), ""


def _git_path_is_tracked(repo_root: Path, path: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--", path],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return completed.returncode == 0


def _scope_violation_verification(
    *,
    workspace_root: Path,
    verification_workspace_root: Path,
    plan_id: str,
    mission_id: str,
    repo_root: Path,
    run_profile: str,
    worktree_base: str | None,
) -> dict[str, Any] | None:
    if not str(worktree_base or "").strip():
        return None
    if _git_toplevel(repo_root) is None:
        return None
    restricted, audit_error = _restricted_worker_changes(repo_root=repo_root, base_ref=worktree_base)
    if not restricted and not audit_error:
        return None
    now = datetime.now(timezone.utc).isoformat()
    failure_kind = "scope_audit_failed" if audit_error else "scope_violation"
    message = (
        f"Worker scope audit could not prove the diff boundary: {audit_error}"
        if audit_error
        else "Verification refused: worker modified restricted path(s): " + ", ".join(restricted[:10])
    )
    payload = {
        "plan_id": plan_id,
        "repo_root": str(repo_root),
        "workspace_root": str(verification_workspace_root),
        "records_workspace_root": str(workspace_root),
        "run_profile": run_profile,
        "passed": 0,
        "inspection_only": 0,
        "failed": 1,
        "total": 1,
        "verdict": "fail",
        "source": "scope_violation",
        "failure_kind": failure_kind,
        "restricted_worker_files": restricted,
        "scope_audit_error": audit_error,
        "repair_brief": {
            "failure_kind": failure_kind,
            "repairable": False,
            "repair_prompt": "",
            "message": message,
        },
        "markdown": message,
        "recorded_at": now,
    }
    saved = save_verification(workspace_root, plan_id, payload)
    payload["saved_path"] = saved["path"]
    save_mission_progress(
        workspace_root,
        mission_id,
        stage="verification_failed",
        stage_label="Verification failed",
        verification_verdict="fail",
        blocker=failure_kind,
        activity="",
        activity_command="",
        activity_started_at="",
    )
    return payload


def _workspace_tamper_verification(
    *,
    workspace_root: Path,
    verification_workspace_root: Path,
    plan_id: str,
    mission_id: str,
    repo_root: Path,
    run_profile: str,
    trusted_workspace_snapshot: dict[str, str] | None,
) -> dict[str, Any] | None:
    changed = _trusted_workspace_changes(verification_workspace_root, trusted_workspace_snapshot)
    if not changed:
        return None
    now = datetime.now(timezone.utc).isoformat()
    message = "Verification refused: worker modified trusted verification workspace file(s): " + ", ".join(changed[:10])
    payload = {
        "plan_id": plan_id,
        "repo_root": str(repo_root),
        "workspace_root": str(verification_workspace_root),
        "records_workspace_root": str(workspace_root),
        "run_profile": run_profile,
        "passed": 0,
        "inspection_only": 0,
        "failed": 1,
        "total": 1,
        "verdict": "fail",
        "source": "verification_workspace_tampering",
        "failure_kind": "verification_workspace_tampering",
        "tampered_workspace_files": changed,
        "repair_brief": {
            "failure_kind": "verification_workspace_tampering",
            "repairable": False,
            "repair_prompt": "",
            "message": message,
        },
        "markdown": message,
        "recorded_at": now,
    }
    saved = save_verification(workspace_root, plan_id, payload)
    payload["saved_path"] = saved["path"]
    save_mission_progress(
        workspace_root,
        mission_id,
        stage="verification_failed",
        stage_label="Verification failed",
        verification_verdict="fail",
        blocker="verification_workspace_tampering",
        activity="",
        activity_command="",
        activity_started_at="",
    )
    return payload


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
    timeout_seconds: float = 900.0,
    trusted_workspace_snapshot: dict[str, str] | None = None,
    base_probe_enabled: bool = True,
) -> dict[str, Any]:
    verification_workspace_path = (verification_workspace_root or workspace_root).expanduser().resolve()
    progress_mission_id = str(mission_id or plan_id)
    verification_activity_started_at = datetime.now(timezone.utc).isoformat()
    save_mission_progress(
        workspace_root,
        progress_mission_id,
        stage="verification_running",
        stage_label="Verification running",
        status="running",
        plan_id=plan_id,
        worktree=str(Path(repo_root).expanduser().resolve()),
        activity="verification" if test_command else None,
        activity_command=str(test_command or "") if test_command else None,
        activity_started_at=verification_activity_started_at if test_command else None,
    )
    now = datetime.now(timezone.utc).isoformat()

    scope_failure = _scope_violation_verification(
        workspace_root=workspace_root,
        verification_workspace_root=verification_workspace_path,
        plan_id=plan_id,
        mission_id=progress_mission_id,
        repo_root=Path(repo_root),
        run_profile=run_profile,
        worktree_base=worktree_base,
    )
    if scope_failure is not None:
        return scope_failure
    workspace_tamper = _workspace_tamper_verification(
        workspace_root=workspace_root,
        verification_workspace_root=verification_workspace_path,
        plan_id=plan_id,
        mission_id=progress_mission_id,
        repo_root=Path(repo_root),
        run_profile=run_profile,
        trusted_workspace_snapshot=trusted_workspace_snapshot,
    )
    if workspace_tamper is not None:
        return workspace_tamper

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
                activity="",
                activity_command="",
                activity_started_at="",
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
                activity="",
                activity_command="",
                activity_started_at="",
            )
            return payload
        timeout_info = _verification_timeout_info(repo_root=Path(repo_root), command=test_command, base_timeout=timeout_seconds)
        command_result = run_command_verification(
            command=test_command,
            repo_root=Path(repo_root),
            timeout_seconds=timeout_info["timeout_seconds"],
            verification_env=verification_env,
            timeout_reason=str(timeout_info.get("reason") or ""),
            base_timeout_seconds=timeout_info["base_timeout_seconds"],
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
            "acceptance": _grade_acceptance(
                command_result=command_result,
                command=test_command,
                repo_root=Path(repo_root),
                base_ref=str(worktree_base or ""),
                timeout_seconds=timeout_info["timeout_seconds"],
                verification_env=verification_env,
                workspace_root=workspace_root,
                enabled=base_probe_enabled,
            ),
            "verification_timeout": timeout_info,
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
            activity="",
            activity_command="",
            activity_started_at="",
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
        evidence = builder(workspace_root, max_chars=32768)
    except Exception as exc:
        return {
            "status": "unavailable",
            "source": "diagnose-latest-failure",
            "message": f"{type(exc).__name__}: {exc}",
        }
    failed_step = evidence.get("failed_step") if isinstance(evidence.get("failed_step"), dict) else {}
    raw_evidence = json.dumps(scrub_secrets(evidence), ensure_ascii=False, indent=2)
    return {
        "status": str(evidence.get("status") or ""),
        "source": "diagnose-latest-failure",
        "run_id": str(evidence.get("run_id") or ""),
        "workflow": str(evidence.get("workflow") or ""),
        "failed_step": failed_step,
        "repair_prompt": str(evidence.get("repair_prompt") or ""),
        "message": str(evidence.get("message") or ""),
        "raw_evidence_tail": raw_evidence[-32768:],
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
    activity_started_at = datetime.now(timezone.utc).isoformat()
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
        activity="worker_executing",
        activity_command="low-cost-backend patch-worker",
        activity_started_at=activity_started_at,
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
            activity="",
            activity_command="",
            activity_started_at="",
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
    save_mission_progress(
        workspace_root,
        progress_mission_id,
        worker_status=record["status"],
        worker_exit_code=record["exit_code"],
        log_path=str(log_path),
        activity="",
        activity_command="",
        activity_started_at="",
    )
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


def _prefer_verification_python_env(
    env: dict[str, str] | None,
    verification_command: str | None,
) -> dict[str, str] | None:
    """Put the verification Python early on PATH for worker subprocesses.

    Prevents workers from discovering a broken bare ``python`` (e.g. D:\\python.exe
    without pytest) while Pacer's gate itself uses a resolved absolute interpreter.
    """
    command = str(verification_command or "").strip()
    if not command or "pytest" not in command.lower():
        return env
    match = re.match(r'^("?)(?P<path>(?:[A-Za-z]:)?[^"\n]+?python(?:\d+(?:\.\d+)*)?(?:\.exe)?)\1(?:\s|$)', command, re.I)
    if not match:
        return env
    python_path = Path(match.group("path"))
    if not python_path.exists():
        return env
    python_dir = str(python_path.parent)
    merged = dict(env or {})
    current = str(merged.get("PATH") or os.environ.get("PATH") or "")
    parts = [item for item in current.split(os.pathsep) if item]
    if python_dir not in parts:
        merged["PATH"] = os.pathsep.join([python_dir, *parts])
    # Help some tools that honor VIRTUAL_ENV/PYTHON only loosely.
    merged.setdefault("PACER_VERIFICATION_PYTHON", str(python_path))
    return merged


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
        path_parts = base_path.split(os.pathsep)
        # Verification commands may carry a Windows toolchain PATH even when
        # the audit itself runs on Linux. Keep synthetic cross-platform
        # environments testable without changing native PATH semantics.
        if os.pathsep != ";" and ";" in base_path:
            path_parts = [item for part in path_parts for item in part.split(";")]
        for part in path_parts:
            normalized_part = _normalize_toolchain_text(part).rstrip("/")
            if normalized_part and normalized_part in normalized_forbidden:
                continue
            kept_parts.append(part)
        joiner = ";" if os.pathsep != ";" and ";" in base_path else os.pathsep
        merged["PATH"] = joiner.join(kept_parts)
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
    command_metadata: dict[str, Any] | None = None,
    dispatch_mode: str = "tracked",
) -> dict[str, Any]:
    started = monotonic()
    progress_mission_id = str(mission_id or plan_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    activity_started_at = datetime.now(timezone.utc).isoformat()
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
        activity="worker_executing",
        activity_command=format_argv(argv),
        activity_started_at=activity_started_at,
    )

    def progress_callback(event: dict[str, Any]) -> None:
        record_worker_output(
            workspace_root,
            progress_mission_id,
            stream=str(event.get("stream") or ""),
            chunk=str(event.get("chunk") or ""),
            log_path=log_path,
        )

    result = _invoke_worker_runner_once(
        runner,
        argv=argv,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        log_path=log_path,
        env=env,
        stdin_text=stdin_text,
        progress_callback=progress_callback,
    )
    raw_exit_code = result.get("exit_code")
    try:
        exit_code = int(raw_exit_code) if raw_exit_code is not None else 125
    except (TypeError, ValueError, OverflowError):
        exit_code = 125
    stdout_tail = str(result.get("stdout_tail") or "")
    stderr_tail = str(result.get("stderr_tail") or "")
    status = "completed" if exit_code == 0 else "crashed" if raw_exit_code is None else "failed"
    blocked_reason = _worker_blocked_reason(stdout_tail=stdout_tail, stderr_tail=stderr_tail, log_path=log_path)
    if blocked_reason:
        status = "blocked"
    stdout_log_path = str(result.get("stdout_log_path") or "").strip()
    stderr_log_path = str(result.get("stderr_log_path") or "").strip()
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
        "dispatch_mode": dispatch_mode,
    }
    if stdout_log_path:
        record["stdout_log_path"] = stdout_log_path
    if stderr_log_path:
        record["stderr_log_path"] = stderr_log_path
    metadata = command_metadata if isinstance(command_metadata, dict) else {}
    for key in (
        "resolved_model",
        "resolved_provider",
        "provider_source",
        "resolved_reasoning_effort",
        "model_source",
        "reasoning_effort_source",
        "resolved_sandbox",
        "sandbox_source",
        "resolved_approval",
        "approval_source",
        "session_mode",
    ):
        value = metadata.get(key)
        if value not in {None, ""}:
            record[key] = value
    if isinstance(metadata.get("routing_evidence"), dict):
        record["routing_evidence"] = dict(metadata["routing_evidence"])
    if blocked_reason:
        record["blocked_reason"] = blocked_reason
    usage = _extract_worker_usage(
        str(track.get("agent") or ""),
        log_path,
        str(result.get("stdout_tail") or ""),
        stdout_log_path=Path(stdout_log_path) if stdout_log_path else None,
    )
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
        activity="",
        activity_command="",
        activity_started_at="",
    )
    return record


def _invoke_worker_runner_once(
    runner: Callable[..., dict[str, Any]],
    *,
    argv: list[str],
    cwd: Path,
    timeout_seconds: float,
    log_path: Path,
    env: dict[str, str] | None,
    stdin_text: str | None,
    progress_callback: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    optional = {
        "env": env,
        "progress_callback": progress_callback,
        **({"stdin_text": stdin_text} if stdin_text is not None else {}),
    }
    try:
        signature = inspect.signature(runner)
    except (TypeError, ValueError):
        accepted: dict[str, Any] = {}
    else:
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        accepted = (
            optional
            if accepts_kwargs
            else {key: value for key, value in optional.items() if key in signature.parameters}
        )
    result = runner(argv, cwd, timeout_seconds, log_path, **accepted)
    if not isinstance(result, dict):
        raise TypeError("worker runner must return a result object")
    return result


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


def _extract_worker_usage(
    agent: str,
    log_path: Path,
    stdout_tail: str,
    *,
    stdout_log_path: Path | None = None,
) -> dict[str, Any] | None:
    """Pull real cost/token usage from a worker run.

    Claude Code's ``-p --output-format json`` emits a result object with
    ``total_cost_usd`` and a ``usage`` block. Recording it gives DevPacer a real
    budget ledger instead of an estimate.
    """
    canonical = canonical_agent_name(agent)
    if canonical == "codex":
        from .codex_exec import read_codex_usage

        usage = read_codex_usage(stdout_log_path, stdout_tail) if stdout_log_path is not None else None
        if not usage:
            usage = read_codex_usage(log_path, stdout_tail)
        if not usage:
            return None
        normalized = dict(usage)
        normalized["cache_read_input_tokens"] = int(normalized.get("cached_input_tokens") or 0)
        return normalized
    if canonical != "claude-code":
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


def _worker_stream_sidecar_path(log_path: Path, stream: str) -> Path:
    return log_path.with_name(f"{log_path.stem}.{stream}{log_path.suffix}")


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
    stdout_log_path = _worker_stream_sidecar_path(log_path, "stdout")
    stderr_log_path = _worker_stream_sidecar_path(log_path, "stderr")

    # Isolate Pacer control variables from child process environment
    run_env = {}
    source_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}} if env else os.environ
    for k, v in source_env.items():
        k_str = str(k)
        if k_str == "PACER_LAUNCH_ID" or k_str.startswith("PACER_PRELAUNCH_"):
            continue
        run_env[k_str] = str(v)

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    lock = threading.Lock()

    def append_captured_text(target: list[str], stream_name: str, chunk: str) -> None:
        stream_log_path = stdout_log_path if stream_name == "stdout" else stderr_log_path
        with lock:
            target.append(chunk)
            with stream_log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(chunk)
                handle.flush()
            with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(chunk)
                handle.flush()

    def append_stream(stream: Any, target: list[str], stream_name: str) -> None:
        try:
            for chunk in iter(stream.readline, ""):
                if not chunk:
                    break
                append_captured_text(target, stream_name, chunk)
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
        stdout_log_path.write_text("", encoding="utf-8")
        stderr_log_path.write_text("", encoding="utf-8")
        proc = subprocess.Popen(
            prepare_subprocess_command(argv),
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            text=True,
            env=run_env,
            **isolated_process_group_kwargs(),
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
            terminate_process_tree(proc)
            return_code = 124
            append_captured_text(stderr_parts, "stderr", f"\nTimed out after {timeout_seconds:.0f}s\n")
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        return {
            "exit_code": return_code,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
            "stdout_log_path": str(stdout_log_path),
            "stderr_log_path": str(stderr_log_path),
        }
    except OSError as exc:
        stderr = f"{type(exc).__name__}: {exc}"
        log_path.write_text(stderr, encoding="utf-8", errors="replace")
        stdout_log_path.write_text("", encoding="utf-8", errors="replace")
        stderr_log_path.write_text(stderr, encoding="utf-8", errors="replace")
        return {
            "exit_code": 127,
            "stdout_tail": "",
            "stderr_tail": stderr[-2000:],
            "stdout_log_path": str(stdout_log_path),
            "stderr_log_path": str(stderr_log_path),
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
    merge = g(repo_root, "-c", "user.email=pacer@local", "-c", "user.name=Pacer", "merge", "--no-ff", "-m", f"Pacer merge {branch}: {message}", branch)
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
            still_dirty = [line for line in recheck.stdout.splitlines() if line.strip()]
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


def _with_dispatch_project_memory(prompt: str, plan: dict[str, Any]) -> str:
    lines = _dispatch_project_memory_lines(plan)
    if not lines:
        return prompt
    return prompt.rstrip() + "\n\n" + "\n".join(lines)


def _dispatch_project_memory_lines(plan: dict[str, Any]) -> list[str]:
    memory = plan.get("project_memory") if isinstance(plan.get("project_memory"), dict) else {}
    notes = project_memory_handoff_notes(memory)
    usage = memory.get("usage") if isinstance(memory.get("usage"), dict) else {}
    joined = " ".join(notes)
    entries = memory.get("entries") if isinstance(memory.get("entries"), list) else []
    memory_ids = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("memory_id") or item.get("mission_id") or "")
        if mid and mid in joined:
            memory_ids.append(mid)
    usage.update(
        {
            "dispatch_injected": bool(notes),
            "dispatch_note_count": len(notes),
            "dispatch_chars": len(joined),
            "dispatch_memory_ids": memory_ids,
        }
    )
    if memory:
        memory["usage"] = usage
    if not notes:
        return []
    return [
        "Project memory (advisory and non-exhaustive; inspect the repository wherever needed):",
        *[f"- {item}" for item in notes],
    ]


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
        if item.is_file() and not _local_dirty_overlay_allowed(name, ignored=False):
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
                        ".env*",
                        "model_api_keys.txt",
                        "api_keys.txt",
                        "*.key",
                        "*.pem",
                        "*.pfx",
                        "*.p12",
                        "*.crt",
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
        return {"status": "shared", "path": str(target), "trusted_snapshot": _trusted_workspace_snapshot(target)}
    try:
        target.mkdir(parents=True, exist_ok=True)
        manifest = source / "workspace.json"
        if manifest.exists():
            shutil.copyfile(manifest, target / "workspace.json")
        for dirname in ("workflows", "inputs", "fixtures"):
            source_dir = source / dirname
            target_dir = target / dirname
            if target_dir.exists():
                shutil.rmtree(target_dir)
            if not source_dir.exists():
                continue
            shutil.copytree(source_dir, target_dir)
    except OSError as exc:
        return {
            "status": "failed",
            "path": str(target),
            "reason": f"Could not prepare worktree workspace: {type(exc).__name__}: {exc}",
        }
    return {
        "status": "prepared",
        "path": str(target),
        "trusted_snapshot": _trusted_workspace_snapshot(target),
    }


def _trusted_workspace_snapshot(workspace_root: Path) -> dict[str, str]:
    root = Path(workspace_root).expanduser().resolve()
    candidates: list[Path] = []
    manifest = root / "workspace.json"
    if manifest.is_file():
        candidates.append(manifest)
    for dirname in ("workflows", "inputs", "fixtures"):
        directory = root / dirname
        if directory.is_dir():
            candidates.extend(path for path in directory.rglob("*") if path.is_file())
    snapshot: dict[str, str] = {}
    for path in sorted(candidates, key=lambda item: item.as_posix()):
        try:
            relative = path.relative_to(root).as_posix()
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            snapshot[path.name] = "unreadable"
    return snapshot


def _trusted_workspace_changes(workspace_root: Path, expected: dict[str, str] | None) -> list[str]:
    if not isinstance(expected, dict):
        return []
    current = _trusted_workspace_snapshot(workspace_root)
    return sorted(
        path
        for path in set(expected) | set(current)
        if str(expected.get(path) or "") != str(current.get(path) or "")
    )


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
    prefixes: list[str] = []
    try:
        workspace_relative = workspace_root.resolve().relative_to(repo_root.resolve()).as_posix().rstrip("/")
    except ValueError:
        workspace_relative = ""
    if workspace_relative:
        prefixes.append(f"{workspace_relative}/")
    git_root = _git_toplevel(repo_root)
    if git_root is not None:
        try:
            git_relative = workspace_root.resolve().relative_to(git_root).as_posix().rstrip("/")
        except ValueError:
            git_relative = ""
        if git_relative:
            prefixes.append(f"{git_relative}/")
    if not prefixes:
        return ()
    # The whole workspace is DevPacer's runtime dir (plans, missions, runs,
    # repo_map.json, ...), never user source. Ignoring the root prefix keeps the
    # dirty-tree gate from tripping on the tool's own files — including a fresh
    # project where git collapses the entire untracked ".agent-workspace/" into
    # one porcelain entry.
    return (*dict.fromkeys(prefixes), "强制测试记录.md")


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


def _worktree_project_root(*, repo_root: Path, worktree: Path) -> Path:
    """Map a requested repo subdirectory to the same subdirectory in a worktree."""
    source = Path(repo_root).expanduser().resolve()
    target = Path(worktree).expanduser().resolve()
    git_root = _git_toplevel(source)
    if git_root is None or source == git_root:
        return target
    try:
        relative = source.relative_to(git_root)
    except ValueError:
        return target
    if _git_tracks_relative_path(git_root, relative):
        return (target / relative).resolve()
    # Untracked nested project roots are overlaid at the worktree root.
    return target


def _git_toplevel(path: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    return Path(completed.stdout.strip()).expanduser().resolve()


def _git_tracks_relative_path(git_root: Path, relative: Path) -> bool:
    rel = relative.as_posix().strip("/")
    if not rel:
        return False
    try:
        completed = subprocess.run(
            ["git", "-C", str(git_root), "-c", "core.quotePath=false", "ls-files", "--", rel],
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    prefix = rel.rstrip("/") + "/"
    return any(line.strip() == rel or line.strip().startswith(prefix) for line in completed.stdout.splitlines())


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
    preflight_markdown = preflight_to_markdown(payload.get("preflight"), heading="### Preflight")
    if preflight_markdown:
        lines.extend(["", preflight_markdown])
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


def preflight_to_markdown(preflight: Any, *, heading: str = "## Preflight") -> str:
    if not isinstance(preflight, dict) or not preflight:
        return ""
    lines = [heading, ""]
    lines.append(f"Status: `{preflight.get('status') or ''}`")
    lines.extend(["", "| Check | Status | Detail |", "| --- | --- | --- |"])
    test_command = preflight.get("test_command") if isinstance(preflight.get("test_command"), dict) else {}
    if test_command:
        detail = _join_non_empty(
            [
                f"requested={test_command.get('requested') or ''}",
                f"resolved={test_command.get('resolved') or ''}",
            ]
        )
        lines.append(_preflight_table_row("test_command", str(test_command.get("status") or ""), detail))
    verification_env = preflight.get("verification_env") if isinstance(preflight.get("verification_env"), dict) else {}
    if verification_env:
        missing = verification_env.get("missing_env_vars") if isinstance(verification_env.get("missing_env_vars"), list) else []
        detail = "missing=" + ", ".join(str(item) for item in missing) if missing else "all declared env vars present"
        lines.append(_preflight_table_row("verification_env", str(verification_env.get("status") or ""), detail))
    dependency = preflight.get("dependency") if isinstance(preflight.get("dependency"), dict) else {}
    if dependency:
        warnings = dependency.get("warnings") if isinstance(dependency.get("warnings"), list) else []
        status = "ok" if bool(dependency.get("deps_installed")) and not warnings else "warning"
        detail = _join_non_empty(
            [
                f"manager={dependency.get('package_manager') or 'none'}",
                f"lockfile={dependency.get('lockfile') or ''}",
                f"deps_installed={bool(dependency.get('deps_installed'))}",
                f"cache_available={bool(dependency.get('cache_available'))}",
                f"native_install_risk={bool(dependency.get('native_install_risk'))}",
                f"estimated_install_minutes={dependency.get('estimated_install_minutes')}",
                "warnings=" + ", ".join(str(item) for item in warnings) if warnings else "",
            ]
        )
        lines.append(_preflight_table_row("dependency", status, detail))
    timeout_info = preflight.get("verification_timeout") if isinstance(preflight.get("verification_timeout"), dict) else {}
    if timeout_info:
        detail = _join_non_empty(
            [
                f"base={timeout_info.get('base_timeout_seconds')}s",
                f"effective={timeout_info.get('timeout_seconds')}s",
                f"reason={timeout_info.get('reason') or ''}",
            ]
        )
        lines.append(_preflight_table_row("verification_timeout", "ok", detail))
    command_safety = preflight.get("command_safety") if isinstance(preflight.get("command_safety"), dict) else {}
    if command_safety:
        detail = _join_non_empty(
            [
                f"reason={command_safety.get('reason') or ''}",
                f"marker={command_safety.get('marker') or ''}",
                str(command_safety.get("message") or ""),
            ]
        )
        lines.append(_preflight_table_row("command_safety", str(command_safety.get("status") or ""), detail))
    execution_alignment = (
        preflight.get("execution_alignment")
        if isinstance(preflight.get("execution_alignment"), dict)
        else {}
    )
    if execution_alignment:
        issues = execution_alignment.get("issues") if isinstance(execution_alignment.get("issues"), list) else []
        codes = ", ".join(str(item.get("code") or "") for item in issues[:5] if isinstance(item, dict))
        detail = f"issues={execution_alignment.get('issue_count') or 0}" + (f"; codes={codes}" if codes else "")
        lines.append(
            _preflight_table_row(
                "execution_alignment",
                str(execution_alignment.get("status") or ""),
                detail,
            )
        )
    return "\n".join(lines).rstrip()


def _preflight_table_row(name: str, status: str, detail: str) -> str:
    return f"| {_markdown_cell(name)} | `{_markdown_cell(status)}` | {_markdown_cell(detail)} |"


def _markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def _join_non_empty(items: list[str]) -> str:
    return "; ".join(str(item).strip() for item in items if str(item).strip())


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
