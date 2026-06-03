from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
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


def session_path(workspace: Path) -> Path:
    return workspace / SESSION_FILE


def update_agent_session(workspace: Path, run_result: Any) -> AgentSession:
    existing = load_agent_session(workspace)
    session = _build_session(workspace, run_result, existing)
    _write_session(workspace, session)
    return session


def load_agent_session(workspace: Path) -> AgentSession | None:
    path = session_path(workspace)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        failure = data.get("latest_failure")
        return AgentSession(
            updated_at=float(data.get("updated_at", 0.0)),
            passing_workflows=[str(item) for item in data.get("passing_workflows", [])],
            failing_workflows=[str(item) for item in data.get("failing_workflows", [])],
            latest_failure=FailureSummary(**failure) if isinstance(failure, dict) else None,
            next_action=str(data.get("next_action", "")),
            token_estimate=int(data.get("token_estimate", 0)),
        )
    except Exception:
        return None


def session_to_snapshot_text(session: AgentSession, *, max_chars: int = MAX_SNAPSHOT_CHARS) -> str:
    text = _session_to_snapshot_text(session)
    return clamp_ai_text(text, max_chars=max_chars, suffix="...[truncated, use MCP tools for details]")


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
    session = AgentSession(
        updated_at=time(),
        passing_workflows=passing[-10:],
        failing_workflows=failing[-5:],
        latest_failure=latest_failure,
        next_action=next_action,
        token_estimate=0,
    )
    return AgentSession(
        updated_at=session.updated_at,
        passing_workflows=session.passing_workflows,
        failing_workflows=session.failing_workflows,
        latest_failure=session.latest_failure,
        next_action=session.next_action,
        token_estimate=_estimate_tokens(session),
    )


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
    lines = ["## Visual Agent Context"]
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

    lines.append("")
    lines.append(f"Next action: {session.next_action}")
    lines.append(f"Context fetched: {int(session.updated_at)}")
    return _sanitize_text("\n".join(lines))


def _write_session(workspace: Path, session: AgentSession) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    session_path(workspace).write_text(json.dumps(asdict(session), ensure_ascii=False, indent=2), encoding="utf-8")


def _sanitize_text(text: str) -> str:
    safe = str(scrub_secrets(text))
    for word in SENSITIVE_WORDS:
        safe = re.sub(re.escape(word), "[redacted]", safe, flags=re.IGNORECASE)
    return safe
