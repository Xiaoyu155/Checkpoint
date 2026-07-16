"""Reliable, dependency-free state and retry primitives for managed runs.

The canonical state groups follow Celery's ready/unready distinction while the
transition guard and human-readable reason fields follow Prefect's separation
of state type from state name.  This module is deliberately pure: callers own
persistence and must use ``revision`` as a compare-and-swap value.
"""

from __future__ import annotations

import math
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


MANAGED_STATE_SCHEMA_VERSION = 1


class ManagedState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    CRASHED = "CRASHED"


READY_STATES = frozenset(
    {
        ManagedState.SUCCEEDED,
        ManagedState.FAILED,
        ManagedState.BLOCKED,
        ManagedState.CANCELLED,
        ManagedState.CRASHED,
    }
)
UNREADY_STATES = frozenset(set(ManagedState) - set(READY_STATES))
EXCEPTION_STATES = frozenset(
    {
        ManagedState.FAILED,
        ManagedState.BLOCKED,
        ManagedState.CRASHED,
    }
)
PROPAGATE_STATES = frozenset({ManagedState.FAILED, ManagedState.CRASHED})
TERMINAL_STATES = READY_STATES

ALLOWED_TRANSITIONS: dict[ManagedState, frozenset[ManagedState]] = {
    ManagedState.PENDING: frozenset(
        {ManagedState.RUNNING, ManagedState.BLOCKED, ManagedState.CANCELLED}
    ),
    ManagedState.RUNNING: frozenset(
        {
            ManagedState.RETRY_WAIT,
            ManagedState.VERIFYING,
            ManagedState.FAILED,
            ManagedState.BLOCKED,
            ManagedState.CANCELLED,
            ManagedState.CRASHED,
        }
    ),
    ManagedState.RETRY_WAIT: frozenset(
        {
            ManagedState.PENDING,
            ManagedState.FAILED,
            ManagedState.BLOCKED,
            ManagedState.CANCELLED,
        }
    ),
    ManagedState.VERIFYING: frozenset(
        {
            ManagedState.SUCCEEDED,
            ManagedState.RETRY_WAIT,
            ManagedState.FAILED,
            ManagedState.BLOCKED,
            ManagedState.CANCELLED,
            ManagedState.CRASHED,
        }
    ),
    **{state: frozenset() for state in TERMINAL_STATES},
}


class ManagedStateError(ValueError):
    """Base error for a rejected managed-state mutation."""


class RevisionConflict(ManagedStateError):
    """The persisted record changed after the caller read it."""


class InvalidStateTransition(ManagedStateError):
    """The requested canonical state edge is not allowed."""


class TerminalStateImmutable(ManagedStateError):
    """A terminal attempt cannot be reopened or rewritten."""


class AttemptIdentityMismatch(ManagedStateError):
    """Evidence from one attempt was offered for another attempt."""


@dataclass(frozen=True)
class StateEvent:
    revision: int
    from_state: str
    to_state: str
    event: str
    at: str
    attempt_id: str = ""
    reason_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "event": self.event,
            "at": self.at,
            "attempt_id": self.attempt_id,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class ManagedRunState:
    run_id: str
    idempotency_key: str
    state: ManagedState
    revision: int
    updated_at: str
    attempt_id: str = ""
    reason_code: str = ""
    scheduled_at: str = ""
    history: tuple[StateEvent, ...] = ()
    schema_version: int = MANAGED_STATE_SCHEMA_VERSION

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "idempotency_key": self.idempotency_key,
            "state": self.state.value,
            "revision": self.revision,
            "attempt_id": self.attempt_id,
            "reason_code": self.reason_code,
            "scheduled_at": self.scheduled_at,
            "updated_at": self.updated_at,
            "terminal": self.terminal,
            "history": [item.to_dict() for item in self.history],
        }


def managed_idempotency_key(value: Any) -> str:
    """Build a stable key from a task contract or launch request."""
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("idempotency input must be JSON serializable") from exc
    return "managed:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def managed_task_idempotency_key(
    *,
    goal: str,
    repo_root: str,
    test_command: str = "",
    requirement_contract: Any = None,
) -> str:
    normalized_root = str(repo_root or "").strip().replace("\\", "/").rstrip("/")
    normalized_command = " ".join(str(test_command or "").split())
    contract = requirement_contract if isinstance(requirement_contract, dict) else {}
    return managed_idempotency_key(
        {
            "goal": str(goal or "").strip(),
            "repo_root": normalized_root,
            "test_command": normalized_command,
            "requirement_contract": contract,
        }
    )


def managed_run_from_dict(value: Any) -> ManagedRunState:
    payload = value if isinstance(value, dict) else {}
    if payload.get("schema_version") != MANAGED_STATE_SCHEMA_VERSION:
        raise ValueError("managed state schema_version must be 1")
    history_rows = payload.get("history") if isinstance(payload.get("history"), list) else []
    history: list[StateEvent] = []
    for item in history_rows:
        if not isinstance(item, dict):
            raise ValueError("managed state history entries must be objects")
        revision_value = item.get("revision")
        if not isinstance(revision_value, int) or isinstance(revision_value, bool):
            raise ValueError("managed state history revision must be an integer")
        history.append(
            StateEvent(
                revision=revision_value,
                from_state=str(item.get("from_state") or ""),
                to_state=str(item.get("to_state") or ""),
                event=str(item.get("event") or ""),
                at=str(item.get("at") or ""),
                attempt_id=str(item.get("attempt_id") or ""),
                reason_code=str(item.get("reason_code") or ""),
            )
        )
    revision_value = payload.get("revision")
    if not isinstance(revision_value, int) or isinstance(revision_value, bool):
        raise ValueError("managed state revision must be an integer")
    record = ManagedRunState(
        run_id=str(payload.get("run_id") or "").strip(),
        idempotency_key=str(payload.get("idempotency_key") or "").strip(),
        state=_managed_state(payload.get("state") or ""),
        revision=revision_value,
        updated_at=str(payload.get("updated_at") or ""),
        attempt_id=str(payload.get("attempt_id") or ""),
        reason_code=str(payload.get("reason_code") or ""),
        scheduled_at=str(payload.get("scheduled_at") or ""),
        history=tuple(history),
    )
    if not record.run_id or not record.idempotency_key:
        raise ValueError("managed state requires run_id and idempotency_key")
    _validate_managed_history(record, payload=payload)
    return record


def _validate_managed_history(record: ManagedRunState, *, payload: dict[str, Any]) -> None:
    if not record.history:
        raise ValueError("managed state history is required")
    first = record.history[0]
    if (
        first.revision != 0
        or first.from_state
        or first.to_state != ManagedState.PENDING.value
        or first.event != "run_created"
    ):
        raise ValueError("managed state history must start with run_created revision 0 PENDING")
    previous = ManagedState.PENDING
    for expected_revision, item in enumerate(record.history[1:], start=1):
        if item.revision != expected_revision:
            raise ValueError("managed state history revisions must be contiguous")
        if item.from_state != previous.value:
            raise ValueError("managed state history from_state is not contiguous")
        target = _managed_state(item.to_state)
        if target not in ALLOWED_TRANSITIONS[previous]:
            raise ValueError(
                f"managed state history contains invalid transition: {previous.value} -> {target.value}"
            )
        previous = target
    last = record.history[-1]
    if last.revision != record.revision:
        raise ValueError("managed state history revision does not match record revision")
    if last.to_state != record.state.value:
        raise ValueError("managed state history final state does not match record state")
    if last.attempt_id != record.attempt_id:
        raise ValueError("managed state history final attempt does not match record attempt")
    if len(record.history) != record.revision + 1:
        raise ValueError("managed state history length does not match record revision")
    if "terminal" in payload:
        terminal = payload.get("terminal")
        if not isinstance(terminal, bool) or terminal != record.terminal:
            raise ValueError("managed state terminal flag does not match canonical state")


def new_managed_run(
    *,
    run_id: str,
    idempotency_key: str,
    now: str | None = None,
) -> ManagedRunState:
    clean_run_id = str(run_id or "").strip()
    clean_key = str(idempotency_key or "").strip()
    if not clean_run_id:
        raise ValueError("run_id is required")
    if not clean_key:
        raise ValueError("idempotency_key is required")
    at = str(now or _now())
    event = StateEvent(
        revision=0,
        from_state="",
        to_state=ManagedState.PENDING.value,
        event="run_created",
        at=at,
    )
    return ManagedRunState(
        run_id=clean_run_id,
        idempotency_key=clean_key,
        state=ManagedState.PENDING,
        revision=0,
        updated_at=at,
        history=(event,),
    )


def transition_managed_run(
    record: ManagedRunState,
    *,
    expected_revision: int,
    next_state: ManagedState | str,
    event: str,
    attempt_id: str | None = None,
    reason_code: str = "",
    scheduled_at: str = "",
    now: str | None = None,
) -> ManagedRunState:
    if int(expected_revision) != record.revision:
        raise RevisionConflict(
            f"revision conflict for {record.run_id}: expected {expected_revision}, current {record.revision}"
        )
    if record.terminal:
        raise TerminalStateImmutable(
            f"run {record.run_id} attempt is terminal in {record.state.value}"
        )
    target = _managed_state(next_state)
    if target not in ALLOWED_TRANSITIONS[record.state]:
        raise InvalidStateTransition(
            f"invalid managed-state transition: {record.state.value} -> {target.value}"
        )
    clean_event = str(event or "").strip()
    if not clean_event:
        raise ValueError("event is required")

    offered_attempt = None if attempt_id is None else str(attempt_id).strip()
    if target == ManagedState.RUNNING:
        if not offered_attempt:
            raise ValueError("attempt_id is required when entering RUNNING")
        next_attempt = offered_attempt
    elif target == ManagedState.PENDING:
        next_attempt = ""
    else:
        if offered_attempt and record.attempt_id and offered_attempt != record.attempt_id:
            raise AttemptIdentityMismatch(
                f"attempt mismatch for {record.run_id}: {offered_attempt} != {record.attempt_id}"
            )
        next_attempt = record.attempt_id or (offered_attempt or "")

    clean_reason = str(reason_code or "").strip()
    if target in {
        ManagedState.RETRY_WAIT,
        ManagedState.FAILED,
        ManagedState.BLOCKED,
        ManagedState.CANCELLED,
        ManagedState.CRASHED,
    } and not clean_reason:
        raise ValueError(f"reason_code is required when entering {target.value}")
    clean_schedule = str(scheduled_at or "").strip()
    if target == ManagedState.RETRY_WAIT and not clean_schedule:
        raise ValueError("scheduled_at is required when entering RETRY_WAIT")
    if target != ManagedState.RETRY_WAIT:
        clean_schedule = ""

    at = str(now or _now())
    revision = record.revision + 1
    history_event = StateEvent(
        revision=revision,
        from_state=record.state.value,
        to_state=target.value,
        event=clean_event,
        at=at,
        attempt_id=next_attempt,
        reason_code=clean_reason,
    )
    return ManagedRunState(
        run_id=record.run_id,
        idempotency_key=record.idempotency_key,
        state=target,
        revision=revision,
        updated_at=at,
        attempt_id=next_attempt,
        reason_code=clean_reason,
        scheduled_at=clean_schedule,
        history=record.history + (history_event,),
    )


RETRYABLE_FAILURE_KINDS = frozenset(
    {
        "provider_5xx",
        "provider_rate_limit",
        "network_timeout",
        "process_crash",
    }
)


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    status: str
    failure_kind: str
    attempts_completed: int
    max_attempts: int
    delay_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "retry": self.retry,
            "status": self.status,
            "failure_kind": self.failure_kind,
            "attempts_completed": self.attempts_completed,
            "max_attempts": self.max_attempts,
            "delay_seconds": self.delay_seconds,
        }


def full_jitter_backoff(
    retry_index: int,
    *,
    factor_seconds: float = 1.0,
    maximum_seconds: float = 600.0,
    random_fraction: float,
) -> float:
    """Return capped exponential backoff with caller-supplied full jitter.

    ``retry_index`` is zero based. ``random_fraction`` is injected instead of
    read from global randomness, keeping scheduling tests deterministic.
    """
    index = int(retry_index)
    factor = float(factor_seconds)
    maximum = float(maximum_seconds)
    fraction = float(random_fraction)
    if index < 0:
        raise ValueError("retry_index must be non-negative")
    if factor <= 0 or maximum <= 0:
        raise ValueError("backoff factor and maximum must be positive")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("random_fraction must be between 0 and 1")
    if factor >= maximum:
        capped = maximum
    else:
        cap_index = math.log2(maximum / factor)
        capped = maximum if index >= cap_index else factor * (2.0**index)
    return round(capped * fraction, 6)


def evaluate_retry(
    failure_kind: str,
    *,
    attempts_completed: int,
    max_attempts: int,
    factor_seconds: float = 1.0,
    maximum_seconds: float = 600.0,
    random_fraction: float,
) -> RetryDecision:
    kind = str(failure_kind or "").strip().lower()
    attempts = int(attempts_completed)
    limit = int(max_attempts)
    if attempts < 1:
        raise ValueError("attempts_completed must be at least 1")
    if limit < 1:
        raise ValueError("max_attempts must be at least 1")
    if kind not in RETRYABLE_FAILURE_KINDS:
        return RetryDecision(False, "not_retryable", kind, attempts, limit)
    if attempts >= limit:
        return RetryDecision(False, "attempts_exhausted", kind, attempts, limit)
    delay = full_jitter_backoff(
        attempts - 1,
        factor_seconds=factor_seconds,
        maximum_seconds=maximum_seconds,
        random_fraction=random_fraction,
    )
    return RetryDecision(True, "scheduled", kind, attempts, limit, delay)


@dataclass(frozen=True)
class ManagedBudgetPolicy:
    max_wall_seconds: float
    max_total_tokens: int
    max_attempts: int
    max_repair_rounds: int
    max_same_failure_count: int = 2

    def __post_init__(self) -> None:
        if self.max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be positive")
        if self.max_total_tokens <= 0:
            raise ValueError("max_total_tokens must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.max_repair_rounds < 0:
            raise ValueError("max_repair_rounds must be non-negative")
        if self.max_same_failure_count < 1:
            raise ValueError("max_same_failure_count must be at least 1")


@dataclass(frozen=True)
class ManagedBudgetUsage:
    elapsed_seconds: float
    total_tokens: int | None
    attempts: int
    repair_rounds: int
    same_failure_count: int = 0

    def __post_init__(self) -> None:
        if self.elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be non-negative")
        if self.total_tokens is not None and self.total_tokens < 0:
            raise ValueError("total_tokens must be non-negative")
        if min(self.attempts, self.repair_rounds, self.same_failure_count) < 0:
            raise ValueError("usage counters must be non-negative")


@dataclass(frozen=True)
class BudgetAssessment:
    allowed: bool
    status: str
    operation: str
    reason_codes: tuple[str, ...]
    remaining_wall_seconds: float
    remaining_tokens: int | None
    remaining_attempts: int
    remaining_repair_rounds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "status": self.status,
            "operation": self.operation,
            "reason_codes": list(self.reason_codes),
            "remaining_wall_seconds": self.remaining_wall_seconds,
            "remaining_tokens": self.remaining_tokens,
            "remaining_attempts": self.remaining_attempts,
            "remaining_repair_rounds": self.remaining_repair_rounds,
        }


def assess_managed_budget(
    policy: ManagedBudgetPolicy,
    usage: ManagedBudgetUsage,
    *,
    operation: str = "continue",
) -> BudgetAssessment:
    operation_name = str(operation or "").strip().lower()
    if operation_name not in {"continue", "worker_attempt", "repair", "verification"}:
        raise ValueError(f"unsupported budget operation: {operation}")
    reasons: list[str] = []
    if usage.elapsed_seconds >= policy.max_wall_seconds:
        reasons.append("wall_budget_exhausted")
    if usage.total_tokens is None:
        reasons.append("token_usage_unknown")
    elif usage.total_tokens >= policy.max_total_tokens:
        reasons.append("token_budget_exhausted")
    if operation_name in {"worker_attempt", "repair"} and usage.attempts >= policy.max_attempts:
        reasons.append("attempt_budget_exhausted")
    if operation_name == "repair" and usage.repair_rounds >= policy.max_repair_rounds:
        reasons.append("repair_budget_exhausted")
    if (
        operation_name in {"worker_attempt", "repair"}
        and usage.same_failure_count >= policy.max_same_failure_count
    ):
        reasons.append("same_failure_repeated")
    reasons = list(dict.fromkeys(reasons))
    only_unknown = reasons == ["token_usage_unknown"]
    return BudgetAssessment(
        allowed=not reasons,
        status="usage_unknown" if only_unknown else "exhausted" if reasons else "within_budget",
        operation=operation_name,
        reason_codes=tuple(reasons),
        remaining_wall_seconds=max(0.0, policy.max_wall_seconds - usage.elapsed_seconds),
        remaining_tokens=(
            None
            if usage.total_tokens is None
            else max(0, policy.max_total_tokens - usage.total_tokens)
        ),
        remaining_attempts=max(0, policy.max_attempts - usage.attempts),
        remaining_repair_rounds=max(0, policy.max_repair_rounds - usage.repair_rounds),
    )


def _managed_state(value: ManagedState | str) -> ManagedState:
    if isinstance(value, ManagedState):
        return value
    try:
        return ManagedState(str(value or "").strip().upper())
    except ValueError as exc:
        raise ValueError(f"unknown managed state: {value}") from exc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
