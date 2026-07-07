from __future__ import annotations

import json
from typing import Any

from .mcp_common import MCP_RESPONSE_MAX_CHARS


def budget_mcp_payload(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(text) <= MCP_RESPONSE_MAX_CHARS:
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
    ):
        if key in payload:
            summary[key] = payload[key]
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
    if len(summary_text) <= MCP_RESPONSE_MAX_CHARS:
        return summary

    return {
        "schema_version": payload.get("schema_version", 1),
        "truncated": True,
        "within_budget": True,
        "truncation_reason": "MCP tool response exceeded the 2000-token response budget.",
    }


def mcp_error_payload(message: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "error": message,
        "hint": "Check workspace_root and workflow name. Use list_workflows to see available workflows.",
    }
