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

from .agent_backends import redact_backend, resolve_backend_for_tier
from .agent_capabilities import canonical_agent_name, load_agent_profile, recommend_worker_config
from .command_verification import command_repair_brief, run_command_verification
from .chief_plans_store import append_worker_record, load_plan, plan_dir, save_verification
from .codex_check import codex_check_to_markdown, run_codex_check
from .git_diff import changed_files
from .models import to_jsonable
from .workspace import open_workspace


# Checkpoint writes its own run artifacts (workspace outputs, status file, browser
# cache) into the repo it verifies. Those are tool outputs, not code under test,
# and must never be counted as changed files — otherwise a second verification
# round sees them as uncovered changes and reports a phantom coverage gap.
_ARTIFACT_BASENAMES = {".visual-agent-status.md"}
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


CommandRunner = Callable[[list[str], Path, float, Path], dict[str, Any]]


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
    worker_command = build_worker_command(
        plan=plan,
        track=selected_track,
        worktree=worktree,
        verification_command=verification_command,
        phase="implementation",
        model_policy=model_policy,
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
    if setup.get("status") != "created":
        return {**preview, "status": "blocked", "reason": setup.get("reason"), "worktree_setup": setup}
    preview["worktree"]["created"] = True
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
        cwd=worktree,
        timeout_seconds=timeout_seconds,
        log_path=logs_dir / f"{track_token}-initial.log",
        runner=runner,
        env=initial_env,
        backend=worker_backend,
    )

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
    )
    verification_attempts.append(verification)

    repair_worker_record: dict[str, Any] | None = None
    if auto_repair_once and verification.get("verdict") == "fail":
        repair_prompt = str((verification.get("repair_brief") or {}).get("repair_prompt") or "").strip()
        if repair_prompt:
            repair_command = build_worker_command(
                plan=plan,
                track=selected_track,
                worktree=worktree,
                verification_command=verification_command,
                prompt_override=repair_prompt + "\n\nAfter repairing, run: " + verification_command,
                phase="repair",
                model_policy=model_policy,
            )
            repair_command["argv"][0] = executable
            repair_worker_record = _run_worker_attempt(
                workspace_root=workspace_path,
                plan_id=plan_id,
                attempt="auto_repair_once",
                track=selected_track,
                argv=repair_command["argv"],
                cwd=worktree,
                timeout_seconds=timeout_seconds,
                log_path=logs_dir / f"{track_token}-auto-repair-once.log",
                runner=runner,
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
                )
            )

    latest_verification = verification_attempts[-1]
    status = "completed" if worker_record.get("status") == "completed" else "worker_failed"
    if latest_verification.get("verdict") == "fail":
        status = "verification_failed"
    elif latest_verification.get("verdict") == "pass":
        status = "verified"
    elif latest_verification.get("verdict") == "inspection_only":
        # Inspection-only proves the page rendered, not that the product works.
        # It must never surface as verified (principle: dry-run cannot pose as
        # real acceptance).
        status = "inspection_only"
    return {
        **preview,
        "status": status,
        "dry_run": False,
        "worker_record": worker_record,
        "repair_worker_record": repair_worker_record,
        "verification_attempts": verification_attempts,
        "latest_verification": latest_verification,
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
) -> str:
    plan_status = str(plan.get("status") or "")
    if plan_status in {"blocked", "needs_clarification"}:
        return f"Plan status is {plan_status}; resolve it before dispatch."
    if plan_status == "needs_workflow_coverage" and not allow_coverage_gap:
        return "Plan has weak workflow coverage; rerun chief-plan after adding coverage or pass --allow-coverage-gap for a dry experiment."
    agent = canonical_agent_name(str(track.get("agent") or ""))
    if str(track.get("track_kind") or "implementation") == "inspection":
        return "Selected track is an inspection lane, not a coding worker."
    if execute and agent not in EXECUTABLE_CODING_AGENTS:
        return f"Agent '{agent}' has no executable worker adapter (supported: {', '.join(sorted(EXECUTABLE_CODING_AGENTS))})."
    return ""


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
) -> dict[str, Any]:
    agent = canonical_agent_name(str(track.get("agent") or "codex"))
    profile = load_agent_profile(agent) or {}
    task_kind = _task_kind_for_phase(phase, model_policy)
    config = recommend_worker_config(profile, task_kind=task_kind) if profile else {}
    prompt = prompt_override or build_worker_prompt(plan=plan, track=track, worktree=worktree, verification_command=verification_command)
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
    argv.append(prompt)
    return {"argv": argv, "display": format_argv(argv)}


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
) -> dict[str, Any]:
    verification_workspace_path = (verification_workspace_root or workspace_root).expanduser().resolve()
    now = datetime.now(timezone.utc).isoformat()

    # Command gate: when a test/build command is given, it IS the acceptance
    # (works on any project, no authored workflows). It is deterministic and
    # cannot be gamed by the model, so it fully replaces workflow verification —
    # otherwise a workspace's seeded demo workflows would fail on an unrelated
    # project.
    if test_command:
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
    env: dict[str, str] | None = None,
    backend: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = monotonic()
    try:
        result = runner(argv, cwd, timeout_seconds, log_path, env=env)
    except TypeError:
        # A custom/legacy runner may not accept env; fall back without it.
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


def run_process_capture(argv: list[str], cwd: Path, timeout_seconds: float, log_path: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
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


def create_worktree(*, repo_root: Path, worktree: Path, branch: str) -> dict[str, Any]:
    if worktree.exists():
        return {"status": "blocked", "reason": f"Worktree path already exists: {worktree}"}
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
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return [line for line in lines if not _dirty_path_ignored(line, ignored_prefixes)]


def workspace_record_dirty_prefixes(*, repo_root: Path, workspace_root: Path) -> tuple[str, ...]:
    try:
        workspace_relative = workspace_root.resolve().relative_to(repo_root.resolve()).as_posix().rstrip("/")
    except ValueError:
        return ()
    if not workspace_relative:
        return ()
    generated_dirs = (
        "chief_plans",
        "missions",
        "runs",
        "reports",
        "demo_repos",
    )
    return tuple(f"{workspace_relative}/{dirname}/" for dirname in generated_dirs)


def _dirty_path_ignored(line: str, ignored_prefixes: tuple[str, ...]) -> bool:
    if not ignored_prefixes:
        return False
    path = _porcelain_path(line)
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in ignored_prefixes)


def _porcelain_path(line: str) -> str:
    raw = line[3:] if len(line) > 3 else line
    if " -> " in raw:
        raw = raw.split(" -> ", 1)[1]
    return raw.strip().replace("\\", "/")


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
