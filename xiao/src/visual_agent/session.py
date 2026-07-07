from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from time import time
from typing import Any

from .models import ActionStatus
from .security import scrub_secrets


SESSION_FILE = "agent_session.json"
MAX_SNAPSHOT_CHARS = 2000
SENSITIVE_WORDS = ("password", "passwd", "cookie", "token", "bearer", "api_key", "apikey", "secret")


@dataclass(frozen=True)
class FailureSummary:
    workflow: str
    run_id: str
    step_id: str
    action: str
    expected: str
    actual: str
    hint: str
    artifact_dir: str


@dataclass(frozen=True)
class AgentSession:
    updated_at: float
    passing_workflows: list[str]
    failing_workflows: list[str]
    latest_failure: FailureSummary | None
    next_action: str
    token_estimate: int
    runs_this_month: int = 0
    cloud_runs_used: int = 0
    usage_reset_date: str = ""
    ai_task_context: AiTaskContext | None = None


@dataclass(frozen=True)
class AiTaskContext:
    task: str
    analyzed_files: list[str]
    root_cause: str
    plan: str
    tried: list[str]
    updated_at: float


def session_path(workspace: Path) -> Path:
    return workspace / SESSION_FILE


def update_agent_session(workspace: Path, run_result: Any) -> AgentSession:
    existing = load_agent_session(workspace)
    session = _build_session(workspace, run_result, existing)
    _write_session(workspace, session)
    return session


def record_cloud_run_usage(workspace: Path, *, count: int = 1) -> AgentSession:
    existing = load_agent_session(workspace)
    usage = _next_usage(existing, local_runs_delta=0, cloud_runs_delta=max(0, count))
    if existing is None:
        session = AgentSession(
            updated_at=time(),
            passing_workflows=[],
            failing_workflows=[],
            latest_failure=None,
            next_action="Cloud run usage recorded. Run verification workflows after code changes.",
            token_estimate=0,
            runs_this_month=usage["runs_this_month"],
            cloud_runs_used=usage["cloud_runs_used"],
            usage_reset_date=usage["usage_reset_date"],
        )
    else:
        session = replace(
            existing,
            updated_at=time(),
            runs_this_month=usage["runs_this_month"],
            cloud_runs_used=usage["cloud_runs_used"],
            usage_reset_date=usage["usage_reset_date"],
        )
    session = replace(session, token_estimate=_estimate_tokens(session))
    _write_session(workspace, session)
    return session


def load_agent_session(workspace: Path) -> AgentSession | None:
    path = session_path(workspace)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        failure = data.get("latest_failure")
        raw_task_context = data.get("ai_task_context")
        return AgentSession(
            updated_at=float(data.get("updated_at", 0.0)),
            passing_workflows=[str(item) for item in data.get("passing_workflows", [])],
            failing_workflows=[str(item) for item in data.get("failing_workflows", [])],
            latest_failure=FailureSummary(**failure) if isinstance(failure, dict) else None,
            next_action=str(data.get("next_action", "")),
            token_estimate=int(data.get("token_estimate", 0)),
            runs_this_month=int(data.get("runs_this_month", 0) or 0),
            cloud_runs_used=int(data.get("cloud_runs_used", 0) or 0),
            usage_reset_date=str(data.get("usage_reset_date", "") or ""),
            ai_task_context=_task_context_from_dict(raw_task_context),
        )
    except Exception:
        return None


def session_to_snapshot_text(session: AgentSession, *, max_chars: int = MAX_SNAPSHOT_CHARS) -> str:
    text = _session_to_snapshot_text(session)
    return clamp_ai_text(text, max_chars=max_chars, suffix="...[truncated, use MCP tools for details]")


def workspace_session_snapshot_text(workspace: Path, *, max_chars: int = MAX_SNAPSHOT_CHARS) -> str:
    session = load_agent_session(workspace)
    if session is None:
        text = "No session data yet. Run a workflow first."
    else:
        text = _session_to_snapshot_text(session)
    text = append_latest_repair_summary(workspace, text)
    status_path = workspace.parent / ".visual-agent-status.md"
    text = text.rstrip() + f"\n\nVisual status file: {status_path.resolve()}"
    return clamp_ai_text(text, max_chars=max_chars, suffix="...[truncated, use MCP tools for details]")


def save_task_context(
    workspace: Path,
    *,
    task: str,
    analyzed_files: list[str] | None = None,
    root_cause: str = "",
    plan: str = "",
    tried: list[str] | None = None,
) -> AgentSession:
    ctx = AiTaskContext(
        task=_sanitize_text(task)[:240],
        analyzed_files=[_sanitize_text(str(item))[:160] for item in (analyzed_files or [])[:12]],
        root_cause=_sanitize_text(root_cause)[:240],
        plan=_sanitize_text(plan)[:360],
        tried=[_sanitize_text(str(item))[:180] for item in (tried or [])[:8]],
        updated_at=time(),
    )
    existing = load_agent_session(workspace)
    if existing is None:
        session = AgentSession(
            updated_at=time(),
            passing_workflows=[],
            failing_workflows=[],
            latest_failure=None,
            next_action="Task context saved. Run verification to check status.",
            token_estimate=0,
            ai_task_context=ctx,
        )
    else:
        session = replace(existing, updated_at=time(), ai_task_context=ctx)
    session = replace(session, token_estimate=_estimate_tokens(session))
    _write_session(workspace, session)
    return session


def clamp_ai_text(text: str, *, max_chars: int, suffix: str) -> str:
    safe = _sanitize_text(text)
    if len(safe) <= max_chars:
        return safe
    budget = max(0, max_chars - len(suffix) - 1)
    return safe[:budget].rstrip() + "\n" + suffix


def _build_session(workspace: Path, run_result: Any, existing: AgentSession | None) -> AgentSession:
    workflow_name = _sanitize_text(str(getattr(run_result, "workflow_name", "unknown")))[:120]
    steps = list(getattr(run_result, "steps", []))
    run_id = _sanitize_text(str(getattr(run_result, "run_id", "")))[:80]

    failed_steps = [step for step in steps if getattr(step, "status", None) == ActionStatus.FAILED]
    run_passed = not failed_steps

    passing = list(existing.passing_workflows) if existing else []
    failing = list(existing.failing_workflows) if existing else []
    latest_failure = existing.latest_failure if existing else None

    if run_passed:
        if workflow_name not in passing:
            passing.append(workflow_name)
        failing = [name for name in failing if name != workflow_name]
        if latest_failure is not None and latest_failure.workflow == workflow_name:
            latest_failure = None
    else:
        if workflow_name not in failing:
            failing.append(workflow_name)
        passing = [name for name in passing if name != workflow_name]
        latest_failure = _extract_failure_summary(workflow_name, run_id, failed_steps[0], workspace)

    next_action = _suggest_next_action(run_passed, workflow_name, latest_failure)
    usage = _next_usage(existing, local_runs_delta=1)
    session = AgentSession(
        updated_at=time(),
        passing_workflows=passing[-10:],
        failing_workflows=failing[-5:],
        latest_failure=latest_failure,
        next_action=next_action,
        token_estimate=0,
        runs_this_month=usage["runs_this_month"],
        cloud_runs_used=usage["cloud_runs_used"],
        usage_reset_date=usage["usage_reset_date"],
    )
    return AgentSession(
        updated_at=session.updated_at,
        passing_workflows=session.passing_workflows,
        failing_workflows=session.failing_workflows,
        latest_failure=session.latest_failure,
        next_action=session.next_action,
        token_estimate=_estimate_tokens(session),
        runs_this_month=session.runs_this_month,
        cloud_runs_used=session.cloud_runs_used,
        usage_reset_date=session.usage_reset_date,
        ai_task_context=existing.ai_task_context if existing is not None else None,
    )


def _task_context_from_dict(value: Any) -> AiTaskContext | None:
    if not isinstance(value, dict):
        return None
    return AiTaskContext(
        task=_sanitize_text(str(value.get("task") or ""))[:240],
        analyzed_files=[_sanitize_text(str(item))[:160] for item in value.get("analyzed_files", []) if str(item)],
        root_cause=_sanitize_text(str(value.get("root_cause") or ""))[:240],
        plan=_sanitize_text(str(value.get("plan") or ""))[:360],
        tried=[_sanitize_text(str(item))[:180] for item in value.get("tried", []) if str(item)],
        updated_at=float(value.get("updated_at", 0.0) or 0.0),
    )


def _next_usage(
    existing: AgentSession | None,
    *,
    local_runs_delta: int = 0,
    cloud_runs_delta: int = 0,
) -> dict[str, int | str]:
    current_month = datetime.now().strftime("%Y-%m")
    if existing is not None and existing.usage_reset_date == current_month:
        previous_runs = existing.runs_this_month
        previous_cloud_runs = existing.cloud_runs_used
    else:
        previous_runs = 0
        previous_cloud_runs = 0
    return {
        "runs_this_month": previous_runs + max(0, local_runs_delta),
        "cloud_runs_used": previous_cloud_runs + max(0, cloud_runs_delta),
        "usage_reset_date": current_month,
    }


def _extract_failure_summary(workflow: str, run_id: str, failed_step: Any, workspace: Path) -> FailureSummary:
    step_id = _sanitize_text(str(getattr(failed_step, "id", "")))[:80]
    action = _sanitize_text(str(getattr(failed_step, "action", "")))[:80]
    meta = dict(getattr(failed_step, "metadata", {}) or {})
    diag = meta.get("failure_diagnosis", {}) or {}

    expected = _sanitize_text(str(diag.get("expected", "")))[:100]
    actual_raw = _sanitize_text(str(diag.get("actual", "")))
    actual_source = _prioritize_visible_text(actual_raw)
    actual = actual_source[:160] + ("..." if len(actual_source) > 160 else "")
    suggestions = diag.get("recovery_suggestions", [])
    hint = _sanitize_text(str(suggestions[0]) if suggestions else "Review step parameters.")[:120]

    run_dir = workspace / "runs" / run_id
    try:
        artifact_dir = run_dir.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        artifact_dir = "runs/" + run_id

    return FailureSummary(
        workflow=workflow,
        run_id=run_id,
        step_id=step_id,
        action=action,
        expected=expected,
        actual=actual,
        hint=hint,
        artifact_dir=artifact_dir,
    )


def _suggest_next_action(passed: bool, workflow: str, failure: FailureSummary | None) -> str:
    if passed:
        return f"{workflow} passed. Run verification workflows after code changes."
    if failure:
        return f"{failure.workflow} fails at {failure.step_id}. {failure.hint} Then run verification again."
    return "Run verification workflows to check current status."


def _prioritize_visible_text(actual: str) -> str:
    marker = "visible_text="
    if marker not in actual:
        return actual
    visible = actual.split(marker, 1)[1].strip()
    return f"{marker}{visible}" if visible else actual


def _estimate_tokens(session: AgentSession) -> int:
    return len(_session_to_snapshot_text(session)) // 4


def _session_to_snapshot_text(session: AgentSession) -> str:
    lines = ["## Checkpoint Context"]
    lines.append(f"Status: {len(session.failing_workflows)} failing / {len(session.passing_workflows)} passing")

    if session.latest_failure is not None:
        failure = session.latest_failure
        lines.extend(
            [
                "",
                "Latest Failure:",
                f"  Workflow: {failure.workflow}",
                f"  Step: {failure.step_id} ({failure.action})",
                f"  Expected: {failure.expected}",
                f"  Actual: {failure.actual}",
                f"  Hint: {failure.hint}",
                f"  Artifact: {failure.artifact_dir}",
            ]
        )

    if session.passing_workflows:
        if len(session.passing_workflows) > 5:
            passes = f"{len(session.passing_workflows)} workflows passed"
        else:
            passes = ", ".join(session.passing_workflows)
        lines.append("")
        lines.append(f"Recent Passes: {passes}")

    if session.ai_task_context is not None:
        ctx = session.ai_task_context
        lines.extend(["", "AI Task Context:", f"  Task: {ctx.task}"])
        if ctx.root_cause:
            lines.append(f"  Root cause: {ctx.root_cause}")
        if ctx.plan:
            lines.append(f"  Plan: {ctx.plan}")
        if ctx.analyzed_files:
            lines.append(f"  Files: {', '.join(ctx.analyzed_files[:5])}")
        if ctx.tried:
            lines.append(f"  Tried: {', '.join(ctx.tried[:3])}")

    if session.usage_reset_date or session.runs_this_month or session.cloud_runs_used:
        lines.extend(
            [
                "",
                "Usage:",
                f"  Local runs this month: {session.runs_this_month}",
                f"  Cloud runs used: {session.cloud_runs_used}",
                f"  Reset month: {session.usage_reset_date}",
            ]
        )

    lines.append("")
    lines.append(f"Next action: {session.next_action}")
    lines.append(f"Context fetched: {int(session.updated_at)}")
    return _sanitize_text("\n".join(lines))


def append_latest_repair_summary(workspace: Path, text: str) -> str:
    try:
        from .repair_history import list_repair_history

        history = list_repair_history(workspace, limit=1)
    except Exception:
        return text
    entries = history.get("entries") if isinstance(history.get("entries"), list) else []
    if not entries:
        return text
    latest = entries[0] if isinstance(entries[0], dict) else {}
    if not latest:
        return text
    lines = [
        text.rstrip(),
        "",
        "Latest Repair:",
        f"  Status: {_sanitize_text(str(latest.get('status') or ''))[:80]}",
        f"  Workflow: {_sanitize_text(str(latest.get('workflow') or ''))[:120]}",
        f"  Classification: {_sanitize_text(str(latest.get('classification') or ''))[:80]}",
    ]
    if latest.get("verification_status"):
        lines.append(f"  Verification: {_sanitize_text(str(latest.get('verification_status') or ''))[:80]}")
    if latest.get("rollback_status"):
        lines.append(f"  Rollback: {_sanitize_text(str(latest.get('rollback_status') or ''))[:80]}")
    if latest.get("recommended_fix"):
        lines.append(f"  Fix: {_sanitize_text(str(latest.get('recommended_fix') or ''))[:180]}")
    return "\n".join(lines)


def _write_session(workspace: Path, session: AgentSession) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    session_path(workspace).write_text(json.dumps(asdict(session), ensure_ascii=False, indent=2), encoding="utf-8")


def _sanitize_text(text: str) -> str:
    safe = str(scrub_secrets(text))
    for word in SENSITIVE_WORDS:
        safe = re.sub(re.escape(word), "[redacted]", safe, flags=re.IGNORECASE)
    return safe

