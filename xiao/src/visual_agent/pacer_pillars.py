from __future__ import annotations

from typing import Any

from .managed_state import ManagedState, managed_run_from_dict


PILLAR_ASSESSMENT_SCHEMA_VERSION = 1
PILLAR_NAMES = ("routing", "memory", "managed", "acceptance", "dogfood")
PILLAR_STATUSES = frozenset({"passed", "failed", "partial", "indeterminate"})


def assess_pillar(name: str, evidence: Any) -> dict[str, Any]:
    pillar = evidence if isinstance(evidence, dict) else {}
    evaluator = {
        "routing": _assess_routing,
        "memory": _assess_memory,
        "managed": _assess_managed,
        "acceptance": _assess_acceptance,
        "dogfood": _assess_dogfood,
    }.get(str(name))
    if evaluator is None:
        return _assessment(
            status="indeterminate",
            available=False,
            adequacy="unknown",
            reasons=["unknown_pillar"],
        )
    return evaluator(pillar)


def assess_five_pillars(value: Any) -> dict[str, Any]:
    launch = value if isinstance(value, dict) else {}
    raw_pillars = launch.get("pillars") if isinstance(launch.get("pillars"), dict) else launch
    pillars = {
        name: assess_pillar(name, raw_pillars.get(name) if isinstance(raw_pillars, dict) else {})
        for name in PILLAR_NAMES
    }
    counts = {
        status: sum(1 for item in pillars.values() if item["status"] == status)
        for status in ("passed", "failed", "partial", "indeterminate")
    }
    passed = counts["passed"] == len(PILLAR_NAMES)
    if passed:
        status = "passed"
    elif counts["failed"]:
        status = "failed"
    elif counts["partial"]:
        status = "partial"
    else:
        status = "indeterminate"
    return {
        "schema_version": PILLAR_ASSESSMENT_SCHEMA_VERSION,
        "status": status,
        "passed": passed,
        "counts": counts,
        "pillars": pillars,
    }


def pillar_passed(name: str, evidence: Any) -> bool:
    return bool(assess_pillar(name, evidence)["passed"])


def _assess_routing(pillar: dict[str, Any]) -> dict[str, Any]:
    runtime = pillar.get("runtime") if isinstance(pillar.get("runtime"), dict) else {}
    observed = bool(
        pillar.get("ownership_matched")
        and str(pillar.get("attribution_confidence") or "") == "high"
        and str(runtime.get("provider") or "")
        and str(runtime.get("model") or "")
        and not pillar.get("mimo_used")
    )
    if not observed:
        initial = str(pillar.get("state") or "") in {"", "not_observed"}
        return _assessment(
            status="indeterminate" if initial else "failed",
            available=bool(pillar),
            adequacy="insufficient" if pillar else "unknown",
            reasons=["routing_identity_unverified"],
        )
    policy_match = pillar.get("policy_match")
    request_evidence = (
        pillar.get("request_evidence")
        if isinstance(pillar.get("request_evidence"), dict)
        else {}
    )
    decision = (
        request_evidence.get("decision")
        if isinstance(request_evidence.get("decision"), dict)
        else {}
    )
    request = (
        request_evidence.get("request")
        if isinstance(request_evidence.get("request"), dict)
        else {}
    )
    decision_id = str(pillar.get("decision_id") or "")
    chain_match = bool(
        decision_id
        and decision_id == str(request_evidence.get("decision_id") or "")
        and request_evidence.get("policy_match") is True
        and _same_identity(decision, request)
        and _same_identity(request, runtime)
    )
    if policy_match is True and chain_match:
        return _assessment(status="passed", available=True, adequacy="sufficient", reasons=[])
    if policy_match is False or request_evidence.get("policy_match") is False:
        return _assessment(
            status="failed",
            available=True,
            adequacy="sufficient",
            reasons=["routing_policy_mismatch"],
        )
    return _assessment(
        status="partial",
        available=True,
        adequacy="insufficient",
        reasons=["routing_policy_unverified"],
    )


def _assess_memory(pillar: dict[str, Any]) -> dict[str, Any]:
    available = bool(pillar.get("retrieval_succeeded"))
    if not available:
        initial = str(pillar.get("state") or "") in {"", "not_loaded"}
        return _assessment(
            status="indeterminate" if initial else "failed",
            available=False,
            adequacy="insufficient" if pillar else "unknown",
            reasons=["memory_retrieval_unavailable"],
        )
    lookup_hit = pillar.get("lookup_hit") is True
    relevant_hit = pillar.get("relevant_hit") is True
    injected_hit = pillar.get("injected_hit") is True
    used_hit = pillar.get("used_hit") is True
    reasons: list[str] = []
    if not lookup_hit:
        reasons.append("memory_lookup_miss")
    if lookup_hit and not relevant_hit:
        reasons.append("memory_relevance_unverified")
    if relevant_hit and not injected_hit:
        reasons.append("memory_not_injected")
    if injected_hit and not used_hit:
        reasons.append("memory_use_unverified")
    if not reasons:
        retrieved_ids = _memory_ids(pillar.get("retrieved_memory_ids"))
        injected_ids = _memory_ids(pillar.get("injected_memory_ids"))
        used_ids = _memory_ids(pillar.get("memory_ids_used"))
        identity_chain = bool(
            retrieved_ids
            and injected_ids
            and used_ids
            and set(used_ids) <= set(injected_ids) <= set(retrieved_ids)
        )
        if not identity_chain:
            reasons.append("memory_identity_chain_unverified")
    if not reasons:
        return _assessment(status="passed", available=True, adequacy="sufficient", reasons=[])
    return _assessment(
        status="partial",
        available=True,
        adequacy="insufficient",
        reasons=reasons,
    )


def _assess_managed(pillar: dict[str, Any]) -> dict[str, Any]:
    completed = bool(
        str(pillar.get("state") or "") == "completed_in_place"
        and pillar.get("outcome_recorded")
        and str(pillar.get("run_id") or "")
    )
    if not completed:
        state = str(pillar.get("state") or "")
        if state == "ready_in_place":
            return _assessment(
                status="partial",
                available=True,
                adequacy="insufficient",
                reasons=["managed_not_completed"],
            )
        initial = state in {"", "launch_started"}
        return _assessment(
            status="indeterminate" if initial else "failed",
            available=bool(pillar),
            adequacy="insufficient" if pillar else "unknown",
            reasons=["managed_completion_unverified"],
        )
    controls = bool(
        pillar.get("transition_valid")
        and str(pillar.get("idempotency_key") or "")
        and str(pillar.get("budget_status") or "") == "within_budget"
    )
    try:
        managed = managed_run_from_dict(pillar.get("managed_state"))
    except (TypeError, ValueError):
        managed = None
    state_verified = bool(
        managed is not None
        and managed.terminal
        and managed.state == ManagedState.SUCCEEDED
        and managed.idempotency_key == str(pillar.get("idempotency_key") or "")
        and _managed_revision_matches(pillar, managed.revision)
    )
    if controls and state_verified:
        return _assessment(status="passed", available=True, adequacy="sufficient", reasons=[])
    reasons = ["managed_reliability_controls_unverified"]
    if controls and not state_verified:
        reasons = ["managed_state_history_unverified"]
    return _assessment(
        status="partial",
        available=True,
        adequacy="insufficient",
        reasons=reasons,
    )


def _assess_acceptance(pillar: dict[str, Any]) -> dict[str, Any]:
    evidence_integrity = str(pillar.get("evidence_integrity") or "")
    if not evidence_integrity and pillar.get("active") and str(pillar.get("state") or "") == "verified":
        evidence_integrity = "verified"
    if evidence_integrity != "verified":
        state = str(pillar.get("state") or "")
        initial = state in {"", "not_verified"} and not pillar.get("outcome_status")
        return _assessment(
            status="indeterminate" if initial else "failed",
            available=bool(pillar),
            adequacy="insufficient" if pillar else "unknown",
            reasons=["acceptance_evidence_unverified"],
        )
    adequacy = str(pillar.get("acceptance_adequacy") or "unknown")
    verdict = str(pillar.get("product_verdict") or "indeterminate")
    digest_verified = pillar.get("digest_verified") is True
    if adequacy == "sufficient" and verdict == "pass" and digest_verified:
        return _assessment(status="passed", available=True, adequacy="sufficient", reasons=[])
    if verdict == "fail":
        return _assessment(
            status="failed",
            available=True,
            adequacy=adequacy if adequacy in {"sufficient", "insufficient", "unknown"} else "unknown",
            reasons=["acceptance_product_failed"],
        )
    reason = (
        "acceptance_contract_digest_unverified"
        if adequacy == "sufficient" and verdict == "pass"
        else "acceptance_standard_insufficient"
    )
    return _assessment(
        status="partial",
        available=True,
        adequacy=adequacy if adequacy in {"sufficient", "insufficient", "unknown"} else "unknown",
        reasons=[reason],
    )


def _assess_dogfood(pillar: dict[str, Any]) -> dict[str, Any]:
    source_discipline = bool(
        pillar.get("verified_batch")
        and pillar.get("task_review_valid") is not False
    )
    if not source_discipline:
        state = str(pillar.get("state") or "")
        if state == "source_contract_ready":
            return _assessment(
                status="partial",
                available=True,
                adequacy="insufficient",
                reasons=["dogfood_not_completed"],
            )
        initial = state in {"", "project_not_bound"}
        return _assessment(
            status="indeterminate" if initial else "failed",
            available=bool(pillar),
            adequacy="insufficient" if pillar else "unknown",
            reasons=["dogfood_source_discipline_unverified"],
        )
    true_dogfood = bool(
        pillar.get("pacer_on_pacer")
        and pillar.get("self_change_attributed")
        and pillar.get("installed_artifact_verified")
        and pillar.get("artifact_files_verified")
        and str(pillar.get("evidence_digest") or "")
        and pillar.get("quality_target_met") is True
    )
    if true_dogfood:
        return _assessment(status="passed", available=True, adequacy="sufficient", reasons=[])
    return _assessment(
        status="partial",
        available=True,
        adequacy="insufficient",
        reasons=["dogfood_self_development_unverified"],
    )


def _assessment(
    *,
    status: str,
    available: bool,
    adequacy: str,
    reasons: list[str],
) -> dict[str, Any]:
    normalized_status = status if status in PILLAR_STATUSES else "indeterminate"
    return {
        "schema_version": PILLAR_ASSESSMENT_SCHEMA_VERSION,
        "status": normalized_status,
        "passed": normalized_status == "passed",
        "available": bool(available),
        "adequacy": adequacy if adequacy in {"sufficient", "insufficient", "unknown"} else "unknown",
        "reason_codes": list(dict.fromkeys(str(item) for item in reasons if str(item))),
    }


def _same_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_provider = str(left.get("provider") or "").strip().casefold()
    left_model = str(left.get("model") or "").strip().casefold()
    right_provider = str(right.get("provider") or "").strip().casefold()
    right_model = str(right.get("model") or "").strip().casefold()
    return bool(
        left_provider
        and left_model
        and left_provider == right_provider
        and left_model == right_model
    )


def _memory_ids(value: Any) -> list[str]:
    rows = value if isinstance(value, list) else []
    return list(dict.fromkeys(str(item).strip() for item in rows if str(item).strip()))


def _managed_revision_matches(pillar: dict[str, Any], expected: int) -> bool:
    if "managed_revision" not in pillar:
        return True
    value = pillar.get("managed_revision")
    if isinstance(value, bool):
        return False
    try:
        return int(value) == expected
    except (TypeError, ValueError, OverflowError):
        return False
