from __future__ import annotations

import json
from typing import Any

from .mcp_common import MCP_RESPONSE_MAX_CHARS


def budget_mcp_payload(payload: dict[str, Any]) -> dict[str, Any]:
    response_max_chars = (
        min(MCP_RESPONSE_MAX_CHARS, 5900)
        if str(payload.get("kind") or "") == "pacer_task_completion"
        else MCP_RESPONSE_MAX_CHARS
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(text) <= response_max_chars:
        return payload

    summary: dict[str, Any] = {}
    for key in (
        "schema_version",
        "workspace",
        "run_id",
        "workflow",
        "workflow_name",
        "status",
        "source",
        "format",
        "error",
        "hint",
        "report_hint",
        "workflow_count",
        "artifact_count",
        "total",
        "passed",
        "failed",
        "launch_id",
        "effective_memory",
        "native_history_total",
        "native_history_returned",
        "five_pillars_active",
        "five_pillars_assessment",
        "evidence_integrity",
        "acceptance_adequacy",
        "product_verdict",
        "acceptance_assessment",
        "pillars",
        "runtime",
        "usage",
        "compactions",
        "context_control",
        "recovery_capsule",
        "event_count",
    ):
        if key not in payload:
            continue
        if key == "pillars":
            summary[key] = _compact_pillars(payload[key])
        elif key == "five_pillars_assessment":
            summary[key] = _compact_five_pillars_assessment(payload[key])
        elif key == "acceptance_assessment":
            summary[key] = _compact_acceptance_assessment(payload[key])
        else:
            summary[key] = payload[key]
    if str(payload.get("status") or "") in {"memory_loaded", "memory_reused"}:
        for key in (
            "response_cache",
            "memory_receipt",
            "lookup",
            "relevance",
            "memory_injection",
            "memory_use",
            "memory_budget",
        ):
            if key in payload:
                summary[key] = payload[key]
        summary["entries"] = [
            _compact_memory_entry(item)
            for item in (payload.get("entries") or [])[:6]
            if isinstance(item, dict)
        ]
        summary["native_codex_history"] = [
            _compact_memory_entry(item)
            for item in (payload.get("native_codex_history") or [])[:6]
            if isinstance(item, dict)
        ]
    task_review = payload.get("task_review") if isinstance(payload.get("task_review"), dict) else None
    if task_review:
        user_report = (
            task_review.get("user_report")
            if isinstance(task_review.get("user_report"), dict)
            else {}
        )
        summary["task_review"] = {
            "schema_version": task_review.get("schema_version"),
            "kind": task_review.get("kind"),
            "valid": task_review.get("valid"),
            "verdict": task_review.get("verdict"),
            "trust": task_review.get("trust"),
            "evidence_integrity": task_review.get("evidence_integrity"),
            "acceptance_adequacy": task_review.get("acceptance_adequacy"),
            "product_verdict": task_review.get("product_verdict"),
            "acceptance_assessment": _compact_acceptance_assessment(
                task_review.get("acceptance_assessment")
            ),
            "errors": (task_review.get("errors") or [])[:8],
            "warnings": (task_review.get("warnings") or [])[:8],
            "user_report": {
                "headline": user_report.get("headline"),
                "goal": str(user_report.get("goal") or "")[:1000],
                "completed": [str(item)[:300] for item in (user_report.get("completed") or [])[:8]],
                "not_completed": [str(item)[:300] for item in (user_report.get("not_completed") or [])[:8]],
                "evidence": [str(item)[:300] for item in (user_report.get("evidence") or [])[:12]],
                "blocking_issues": [
                    str(item)[:300] for item in (user_report.get("blocking_issues") or [])[:8]
                ],
                "risks": [str(item)[:300] for item in (user_report.get("risks") or [])[:8]],
                "can_trust": user_report.get("can_trust"),
                "evidence_integrity": user_report.get("evidence_integrity"),
                "acceptance_adequacy": user_report.get("acceptance_adequacy"),
                "product_verdict": user_report.get("product_verdict"),
                "next_action": str(user_report.get("next_action") or "")[:500],
            },
            "user_report_markdown": str(task_review.get("user_report_markdown") or "")[:3000],
        }
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    if events:
        summary["events"] = events[-20:]
    repair = payload.get("repair") if isinstance(payload.get("repair"), dict) else None
    if repair:
        candidates = repair.get("candidates") if isinstance(repair.get("candidates"), list) else []
        summary["repair"] = {
            "classification": repair.get("classification"),
            "confidence": repair.get("confidence"),
            "recommended_fix": repair.get("recommended_fix"),
            "apply_supported": repair.get("apply_supported"),
            "selected_candidate_id": repair.get("selected_candidate_id"),
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "id": item.get("id"),
                    "kind": item.get("kind"),
                    "status": item.get("status"),
                    "classification": item.get("classification"),
                    "confidence": item.get("confidence"),
                    "apply_supported": item.get("apply_supported"),
                    "recommended_fix": item.get("recommended_fix"),
                    "reason": item.get("reason"),
                }
                for item in candidates[:5]
                if isinstance(item, dict)
            ],
        }
    plan = payload.get("workflow_repair_plan") if isinstance(payload.get("workflow_repair_plan"), dict) else None
    if plan:
        summary["workflow_repair_plan"] = {
            "status": plan.get("status"),
            "applied": plan.get("applied"),
            "apply_requested": plan.get("apply_requested"),
            "verify_requested": plan.get("verify_requested"),
            "rollback_on_fail": plan.get("rollback_on_fail"),
            "verification": plan.get("verification"),
            "rollback": plan.get("rollback"),
        }
    for key in ("history", "auto_repair"):
        value = payload.get(key)
        if isinstance(value, dict):
            summary[key] = value
    repair_result = payload.get("repair_result") if isinstance(payload.get("repair_result"), dict) else None
    if repair_result:
        repair = repair_result.get("repair") if isinstance(repair_result.get("repair"), dict) else {}
        plan = repair_result.get("workflow_repair_plan") if isinstance(repair_result.get("workflow_repair_plan"), dict) else {}
        summary["repair_result"] = {
            "status": repair_result.get("status"),
            "source": repair_result.get("source"),
            "workflow": repair_result.get("workflow"),
            "run_id": repair_result.get("run_id"),
            "repair": {
                "classification": repair.get("classification"),
                "confidence": repair.get("confidence"),
                "selected_candidate_id": repair.get("selected_candidate_id"),
                "candidate_count": len(repair.get("candidates")) if isinstance(repair.get("candidates"), list) else 0,
                "apply_supported": repair.get("apply_supported"),
            },
            "workflow_repair_plan": {
                "status": plan.get("status"),
                "applied": plan.get("applied"),
                "verify_requested": plan.get("verify_requested"),
                "rollback_on_fail": plan.get("rollback_on_fail"),
                "verification": plan.get("verification"),
                "rollback": plan.get("rollback"),
            },
        }
    repair_health = payload.get("repair_health") if isinstance(payload.get("repair_health"), dict) else None
    if repair_health:
        summary["repair_health"] = {
            "risk_level": repair_health.get("risk_level"),
            "reliability_score": repair_health.get("reliability_score"),
            "analyzed_entries": repair_health.get("analyzed_entries"),
            "applied_count": repair_health.get("applied_count"),
            "verified_count": repair_health.get("verified_count"),
            "rollback_count": repair_health.get("rollback_count"),
            "recommendation": repair_health.get("recommendation"),
        }
    regression = payload.get("regression") if isinstance(payload.get("regression"), dict) else None
    if regression:
        summary["regression"] = {
            "status": regression.get("status"),
            "run_id": regression.get("run_id"),
            "test_path": regression.get("test_path"),
            "fixture_path": regression.get("fixture_path"),
            "test_run": regression.get("test_run"),
            "reason": regression.get("reason"),
        }
    preflight_health = payload.get("preflight_repair_health") if isinstance(payload.get("preflight_repair_health"), dict) else None
    if preflight_health:
        summary["preflight_repair_health"] = {
            "risk_level": preflight_health.get("risk_level"),
            "reliability_score": preflight_health.get("reliability_score"),
            "analyzed_entries": preflight_health.get("analyzed_entries"),
            "rollback_count": preflight_health.get("rollback_count"),
            "failed_verification_count": preflight_health.get("failed_verification_count"),
            "recommendation": preflight_health.get("recommendation"),
        }
    summary.update(
        {
            "truncated": True,
            "within_budget": True,
            "truncation_reason": "MCP tool response exceeded the 2000-token response budget.",
            "available_keys": sorted(str(key) for key in payload.keys()),
        }
    )
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    if len(summary_text) <= response_max_chars:
        return summary

    raw_review = payload.get("task_review") if isinstance(payload.get("task_review"), dict) else {}
    raw_user_report = (
        raw_review.get("user_report") if isinstance(raw_review.get("user_report"), dict) else {}
    )
    raw_outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
    essential = {
        "schema_version": payload.get("schema_version", 1),
        "kind": payload.get("kind"),
        "status": payload.get("status"),
        "launch_id": payload.get("launch_id"),
        "run_id": payload.get("run_id"),
        "five_pillars_active": payload.get("five_pillars_active"),
        "five_pillars_assessment": _compact_five_pillars_assessment(
            payload.get("five_pillars_assessment")
        ),
        "pillars": _compact_pillars(payload.get("pillars")),
        "evidence_integrity": payload.get("evidence_integrity"),
        "acceptance_adequacy": payload.get("acceptance_adequacy"),
        "product_verdict": payload.get("product_verdict"),
        "acceptance_assessment": _compact_acceptance_assessment(
            payload.get("acceptance_assessment")
        ),
        "outcome": {
            "outcome_status": raw_outcome.get("outcome_status"),
            "evidence_level": raw_outcome.get("evidence_level"),
            "batch_run_id": raw_outcome.get("batch_run_id"),
        },
        "task_review": {
            "valid": raw_review.get("valid"),
            "verdict": raw_review.get("verdict"),
            "trust": raw_review.get("trust"),
            "evidence_integrity": raw_review.get("evidence_integrity"),
            "acceptance_adequacy": raw_review.get("acceptance_adequacy"),
            "product_verdict": raw_review.get("product_verdict"),
            "acceptance_assessment": _compact_acceptance_assessment(
                raw_review.get("acceptance_assessment")
            ),
            "user_report": {
                "headline": str(raw_user_report.get("headline") or "")[:500],
                "can_trust": raw_user_report.get("can_trust"),
                "evidence_integrity": raw_user_report.get("evidence_integrity"),
                "acceptance_adequacy": raw_user_report.get("acceptance_adequacy"),
                "product_verdict": raw_user_report.get("product_verdict"),
                "next_action": str(raw_user_report.get("next_action") or "")[:500],
            },
        },
        "truncated": True,
        "within_budget": True,
        "truncation_reason": "MCP tool response exceeded the 2000-token response budget.",
    }
    return essential


def _compact_memory_entry(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (
            str(value.get(key) or "")[:500]
            if key in {"goal", "objective", "summary"}
            else value.get(key)
        )
        for key in (
            "memory_id",
            "mission_id",
            "batch_run_id",
            "recorded_at",
            "goal",
            "objective",
            "summary",
            "status",
            "evidence_level",
            "relevance_score",
            "match_reasons",
        )
        if key in value
    }


def _compact_pillars(value: Any) -> dict[str, dict[str, Any]]:
    pillars = value if isinstance(value, dict) else {}
    compact: dict[str, dict[str, Any]] = {}
    for name in ("routing", "memory", "managed", "acceptance", "dogfood"):
        item = pillars.get(name) if isinstance(pillars.get(name), dict) else {}
        if not item:
            continue
        row: dict[str, Any] = {
            "active": bool(item.get("active")),
            "state": str(item.get("state") or ""),
        }
        for key in (
            "effective_hit",
            "mimo_used",
            "evidence_integrity",
            "acceptance_adequacy",
            "product_verdict",
        ):
            if key in item:
                row[key] = item[key]
        assessment = item.get("assessment")
        if isinstance(assessment, dict):
            row["assessment"] = _compact_pillar_assessment(assessment)
        compact[name] = row
    return compact


def _compact_pillar_assessment(value: Any) -> dict[str, Any]:
    assessment = value if isinstance(value, dict) else {}
    return {
        "schema_version": assessment.get("schema_version"),
        "status": str(assessment.get("status") or "indeterminate"),
        "passed": bool(assessment.get("passed")),
        "available": bool(assessment.get("available")),
        "adequacy": str(assessment.get("adequacy") or "unknown"),
        "reason_codes": [str(item)[:120] for item in (assessment.get("reason_codes") or [])[:12]],
    }


def _compact_five_pillars_assessment(value: Any) -> dict[str, Any]:
    assessment = value if isinstance(value, dict) else {}
    if not assessment:
        return {}
    raw_pillars = assessment.get("pillars") if isinstance(assessment.get("pillars"), dict) else {}
    raw_counts = assessment.get("counts") if isinstance(assessment.get("counts"), dict) else {}
    return {
        "schema_version": assessment.get("schema_version"),
        "status": str(assessment.get("status") or "indeterminate"),
        "passed": bool(assessment.get("passed")),
        "counts": {
            status: int(raw_counts.get(status) or 0)
            for status in ("passed", "failed", "partial", "indeterminate")
        },
        "pillars": {
            name: _compact_pillar_assessment(raw_pillars[name])
            for name in ("routing", "memory", "managed", "acceptance", "dogfood")
            if isinstance(raw_pillars.get(name), dict)
        },
    }


def _compact_acceptance_assessment(value: Any) -> dict[str, Any]:
    assessment = value if isinstance(value, dict) else {}
    if not assessment:
        return {}
    return {
        "schema_version": assessment.get("schema_version"),
        "standard_source": str(assessment.get("standard_source") or "unknown"),
        "standard_digest": str(assessment.get("standard_digest") or "")[:128],
        "digest_verified": bool(assessment.get("digest_verified")),
        "adequacy": str(assessment.get("adequacy") or "insufficient"),
        "final_phase": bool(assessment.get("final_phase")),
        "required_step_classes": [
            str(item)[:80] for item in (assessment.get("required_step_classes") or [])[:10]
        ],
        "observed_step_classes": [
            str(item)[:80] for item in (assessment.get("observed_step_classes") or [])[:10]
        ],
        "missing_step_classes": [
            str(item)[:80] for item in (assessment.get("missing_step_classes") or [])[:10]
        ],
        "missing_commands": [
            str(item)[:300] for item in (assessment.get("missing_commands") or [])[:10]
        ],
        "reason_codes": [str(item)[:120] for item in (assessment.get("reason_codes") or [])[:12]],
    }


def mcp_error_payload(message: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "error": message,
        "hint": "Check workspace_root and workflow name. Use list_workflows to see available workflows.",
    }
