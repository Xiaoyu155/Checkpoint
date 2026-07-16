from __future__ import annotations

from visual_agent.managed_state import new_managed_run, transition_managed_run
from visual_agent.pacer_pillars import assess_five_pillars, assess_pillar


def _managed_state() -> dict:
    pending = new_managed_run(run_id="run-1", idempotency_key="launch:run-1", now="t0")
    running = transition_managed_run(
        pending,
        expected_revision=0,
        next_state="RUNNING",
        event="worker_started",
        attempt_id="attempt-1",
        now="t1",
    )
    verifying = transition_managed_run(
        running,
        expected_revision=1,
        next_state="VERIFYING",
        event="verification_started",
        attempt_id="attempt-1",
        now="t2",
    )
    return transition_managed_run(
        verifying,
        expected_revision=2,
        next_state="SUCCEEDED",
        event="verification_passed",
        attempt_id="attempt-1",
        now="t3",
    ).to_dict()


def test_loaded_empty_memory_is_available_but_not_passed() -> None:
    result = assess_pillar(
        "memory",
        {
            "active": True,
            "state": "loaded_empty",
            "retrieval_succeeded": True,
            "effective_hit": False,
        },
    )

    assert result == {
        "schema_version": 1,
        "status": "partial",
        "passed": False,
        "available": True,
        "adequacy": "insufficient",
        "reason_codes": ["memory_lookup_miss"],
    }


def test_fresh_launch_is_indeterminate_not_failed() -> None:
    result = assess_five_pillars(
        {
            "pillars": {
                "routing": {"active": False, "state": "not_observed"},
                "memory": {"active": False, "state": "not_loaded"},
                "managed": {"active": False, "state": "launch_started"},
                "acceptance": {"active": False, "state": "not_verified"},
                "dogfood": {"active": False, "state": "project_not_bound"},
            }
        }
    )

    assert result["status"] == "indeterminate"
    assert result["counts"] == {"passed": 0, "failed": 0, "partial": 0, "indeterminate": 5}


def test_observed_routing_without_policy_match_is_partial() -> None:
    result = assess_pillar(
        "routing",
        {
            "active": True,
            "state": "observed",
            "runtime": {"provider": "custom", "model": "gpt"},
            "ownership_matched": True,
            "attribution_confidence": "high",
            "mimo_used": False,
        },
    )

    assert result["status"] == "partial"
    assert result["reason_codes"] == ["routing_policy_unverified"]


def test_routing_policy_boolean_without_request_chain_is_not_a_pass() -> None:
    result = assess_pillar(
        "routing",
        {
            "runtime": {"provider": "openai", "model": "gpt-5"},
            "ownership_matched": True,
            "attribution_confidence": "high",
            "mimo_used": False,
            "decision_id": "decision-1",
            "policy_match": True,
        },
    )

    assert result["status"] == "partial"
    assert result["reason_codes"] == ["routing_policy_unverified"]


def test_legacy_verified_acceptance_is_partial_without_standard_adequacy() -> None:
    result = assess_pillar("acceptance", {"active": True, "state": "verified"})

    assert result["status"] == "partial"
    assert result["reason_codes"] == ["acceptance_standard_insufficient"]


def test_source_discipline_is_not_true_dogfood() -> None:
    result = assess_pillar(
        "dogfood",
        {
            "active": True,
            "state": "verified_source_discipline",
            "verified_batch": True,
            "task_review_valid": True,
        },
    )

    assert result["status"] == "partial"
    assert result["reason_codes"] == ["dogfood_self_development_unverified"]


def test_hmac_only_dogfood_stays_partial_below_95_target() -> None:
    result = assess_pillar(
        "dogfood",
        {
            "verified_batch": True,
            "task_review_valid": True,
            "pacer_on_pacer": True,
            "self_change_attributed": True,
            "installed_artifact_verified": True,
            "artifact_files_verified": True,
            "evidence_digest": "a" * 64,
            "quality_score": 85,
            "quality_target_score": 95,
            "quality_target_met": False,
        },
    )

    assert result["status"] == "partial"
    assert result["reason_codes"] == ["dogfood_self_development_unverified"]


def test_five_pillars_pass_only_when_every_strict_assessment_passes() -> None:
    result = assess_five_pillars(
        {
            "pillars": {
                "routing": {
                    "active": True,
                    "runtime": {"provider": "custom", "model": "gpt"},
                    "ownership_matched": True,
                    "attribution_confidence": "high",
                    "mimo_used": False,
                    "decision_id": "decision-1",
                    "policy_match": True,
                    "request_evidence": {
                        "decision_id": "decision-1",
                        "policy_match": True,
                        "decision": {"provider": "custom", "model": "gpt"},
                        "request": {"provider": "custom", "model": "gpt"},
                    },
                },
                "memory": {
                    "retrieval_succeeded": True,
                    "lookup_hit": True,
                    "relevant_hit": True,
                    "injected_hit": True,
                    "used_hit": True,
                    "retrieved_memory_ids": ["memory-1"],
                    "injected_memory_ids": ["memory-1"],
                    "memory_ids_used": ["memory-1"],
                },
                "managed": {
                    "active": True,
                    "state": "completed_in_place",
                    "outcome_recorded": True,
                    "run_id": "run-1",
                    "transition_valid": True,
                    "idempotency_key": "launch:run-1",
                    "budget_status": "within_budget",
                    "managed_state": _managed_state(),
                },
                "acceptance": {
                    "evidence_integrity": "verified",
                    "acceptance_adequacy": "sufficient",
                    "product_verdict": "pass",
                    "digest_verified": True,
                },
                "dogfood": {
                    "active": True,
                    "verified_batch": True,
                    "task_review_valid": True,
                    "pacer_on_pacer": True,
                    "self_change_attributed": True,
                    "installed_artifact_verified": True,
                    "artifact_files_verified": True,
                    "evidence_digest": "a" * 64,
                    "quality_score": 100,
                    "quality_target_score": 95,
                    "quality_target_met": True,
                    "quality_level": "release",
                },
            }
        }
    )

    assert result["passed"] is True
    assert result["status"] == "passed"
    assert result["counts"] == {"passed": 5, "failed": 0, "partial": 0, "indeterminate": 0}
