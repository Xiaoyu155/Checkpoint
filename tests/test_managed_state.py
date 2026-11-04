from __future__ import annotations

from copy import deepcopy

import pytest

from visual_agent.managed_state import (
    EXCEPTION_STATES,
    PROPAGATE_STATES,
    READY_STATES,
    UNREADY_STATES,
    AttemptIdentityMismatch,
    InvalidStateTransition,
    ManagedBudgetPolicy,
    ManagedBudgetUsage,
    ManagedState,
    RevisionConflict,
    TerminalStateImmutable,
    assess_managed_budget,
    evaluate_retry,
    full_jitter_backoff,
    managed_run_from_dict,
    new_managed_run,
    transition_managed_run,
)


def test_canonical_state_groups_are_disjoint_and_complete() -> None:
    assert READY_STATES.isdisjoint(UNREADY_STATES)
    assert READY_STATES | UNREADY_STATES == set(ManagedState)
    assert EXCEPTION_STATES <= READY_STATES
    assert PROPAGATE_STATES == {ManagedState.FAILED, ManagedState.CRASHED}


def test_managed_run_requires_verification_before_success() -> None:
    record = new_managed_run(run_id="run-1", idempotency_key="task:abc", now="t0")
    with pytest.raises(InvalidStateTransition):
        transition_managed_run(
            record,
            expected_revision=0,
            next_state=ManagedState.SUCCEEDED,
            event="claimed_success",
            now="t1",
        )

    running = transition_managed_run(
        record,
        expected_revision=0,
        next_state=ManagedState.RUNNING,
        event="worker_started",
        attempt_id="attempt-1",
        now="t1",
    )
    verifying = transition_managed_run(
        running,
        expected_revision=1,
        next_state=ManagedState.VERIFYING,
        event="verification_started",
        attempt_id="attempt-1",
        now="t2",
    )
    succeeded = transition_managed_run(
        verifying,
        expected_revision=2,
        next_state=ManagedState.SUCCEEDED,
        event="verification_passed",
        attempt_id="attempt-1",
        now="t3",
    )

    assert succeeded.terminal is True
    assert succeeded.revision == 3
    assert [item.to_state for item in succeeded.history] == [
        "PENDING",
        "RUNNING",
        "VERIFYING",
        "SUCCEEDED",
    ]


def test_transition_rejects_stale_revision_and_terminal_reopen() -> None:
    record = new_managed_run(run_id="run-1", idempotency_key="key-1")
    running = transition_managed_run(
        record,
        expected_revision=0,
        next_state="RUNNING",
        event="started",
        attempt_id="attempt-1",
    )
    with pytest.raises(RevisionConflict):
        transition_managed_run(
            running,
            expected_revision=0,
            next_state="FAILED",
            event="failed",
            reason_code="worker_error",
        )
    failed = transition_managed_run(
        running,
        expected_revision=1,
        next_state="FAILED",
        event="failed",
        reason_code="worker_error",
    )
    with pytest.raises(TerminalStateImmutable):
        transition_managed_run(
            failed,
            expected_revision=2,
            next_state="PENDING",
            event="reopened",
        )


def test_retry_transition_clears_old_attempt_before_new_claim() -> None:
    record = new_managed_run(run_id="run-1", idempotency_key="key-1")
    running = transition_managed_run(
        record,
        expected_revision=0,
        next_state="RUNNING",
        event="started",
        attempt_id="attempt-1",
    )
    waiting = transition_managed_run(
        running,
        expected_revision=1,
        next_state="RETRY_WAIT",
        event="retry_scheduled",
        attempt_id="attempt-1",
        reason_code="provider_5xx",
        scheduled_at="2026-07-15T12:00:10+00:00",
    )
    pending = transition_managed_run(
        waiting,
        expected_revision=2,
        next_state="PENDING",
        event="retry_ready",
    )
    second = transition_managed_run(
        pending,
        expected_revision=3,
        next_state="RUNNING",
        event="started",
        attempt_id="attempt-2",
    )

    assert pending.attempt_id == ""
    assert pending.scheduled_at == ""
    assert second.attempt_id == "attempt-2"


def test_attempt_evidence_cannot_cross_attempts() -> None:
    record = new_managed_run(run_id="run-1", idempotency_key="key-1")
    running = transition_managed_run(
        record,
        expected_revision=0,
        next_state="RUNNING",
        event="started",
        attempt_id="attempt-1",
    )
    with pytest.raises(AttemptIdentityMismatch):
        transition_managed_run(
            running,
            expected_revision=1,
            next_state="VERIFYING",
            event="verify",
            attempt_id="attempt-2",
        )


def test_retry_policy_is_allowlist_only_and_uses_full_jitter() -> None:
    denied = evaluate_retry(
        "command_failed",
        attempts_completed=1,
        max_attempts=3,
        random_fraction=0.5,
    )
    allowed = evaluate_retry(
        "provider_5xx",
        attempts_completed=2,
        max_attempts=4,
        factor_seconds=2,
        maximum_seconds=60,
        random_fraction=0.25,
    )
    exhausted = evaluate_retry(
        "network_timeout",
        attempts_completed=3,
        max_attempts=3,
        random_fraction=0.5,
    )

    assert denied.status == "not_retryable"
    assert denied.retry is False
    assert allowed.status == "scheduled"
    assert allowed.delay_seconds == 1.0
    assert exhausted.status == "attempts_exhausted"
    assert full_jitter_backoff(
        100,
        factor_seconds=2,
        maximum_seconds=10,
        random_fraction=0.5,
    ) == 5.0


def test_budget_blocks_unknown_tokens_and_every_hard_limit() -> None:
    policy = ManagedBudgetPolicy(
        max_wall_seconds=60,
        max_total_tokens=1000,
        max_attempts=3,
        max_repair_rounds=1,
        max_same_failure_count=2,
    )
    within = assess_managed_budget(
        policy,
        ManagedBudgetUsage(
            elapsed_seconds=10,
            total_tokens=200,
            attempts=1,
            repair_rounds=0,
        ),
        operation="repair",
    )
    unknown = assess_managed_budget(
        policy,
        ManagedBudgetUsage(
            elapsed_seconds=10,
            total_tokens=None,
            attempts=1,
            repair_rounds=0,
        ),
        operation="verification",
    )
    exhausted = assess_managed_budget(
        policy,
        ManagedBudgetUsage(
            elapsed_seconds=60,
            total_tokens=1000,
            attempts=3,
            repair_rounds=1,
            same_failure_count=2,
        ),
        operation="repair",
    )

    assert within.allowed is True
    assert within.status == "within_budget"
    assert unknown.allowed is False
    assert unknown.status == "usage_unknown"
    assert unknown.reason_codes == ("token_usage_unknown",)
    assert exhausted.allowed is False
    assert set(exhausted.reason_codes) == {
        "wall_budget_exhausted",
        "token_budget_exhausted",
        "attempt_budget_exhausted",
        "repair_budget_exhausted",
        "same_failure_repeated",
    }


def _succeeded_payload() -> dict:
    record = new_managed_run(run_id="run-roundtrip", idempotency_key="key-roundtrip", now="t0")
    record = transition_managed_run(
        record,
        expected_revision=0,
        next_state="RUNNING",
        event="started",
        attempt_id="attempt-1",
        now="t1",
    )
    record = transition_managed_run(
        record,
        expected_revision=1,
        next_state="VERIFYING",
        event="verifying",
        attempt_id="attempt-1",
        now="t2",
    )
    record = transition_managed_run(
        record,
        expected_revision=2,
        next_state="SUCCEEDED",
        event="passed",
        attempt_id="attempt-1",
        now="t3",
    )
    return record.to_dict()


def test_managed_state_roundtrip_replays_complete_history() -> None:
    payload = _succeeded_payload()
    restored = managed_run_from_dict(payload)

    assert restored.to_dict() == payload


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["history"][0].update({"event": "forged"}),
        lambda value: value["history"][1].update({"revision": 2}),
        lambda value: value["history"][2].update({"from_state": "PENDING"}),
        lambda value: value["history"][2].update({"to_state": "SUCCEEDED"}),
        lambda value: value.update({"state": "FAILED"}),
        lambda value: value.update({"attempt_id": "attempt-forged"}),
        lambda value: value.update({"terminal": False}),
    ],
)
def test_managed_state_rejects_tampered_persisted_history(mutate) -> None:
    payload = deepcopy(_succeeded_payload())
    mutate(payload)

    with pytest.raises(ValueError):
        managed_run_from_dict(payload)
