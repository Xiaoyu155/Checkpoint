from __future__ import annotations

import json

from visual_agent.mcp_common import MCP_RESPONSE_MAX_CHARS
from visual_agent.mcp_response import budget_mcp_payload


def test_pacer_evidence_survives_response_budget_truncation() -> None:
    payload = {
        "schema_version": 2,
        "launch_id": "launch-1",
        "effective_memory": {"hit": True, "native_history_entries": 1},
        "five_pillars_active": False,
        "pillars": {"memory": {"active": True}},
        "runtime": {"provider": "custom", "model": "gpt-test"},
        "context_control": {"auto_compact_token_limit": 96000, "uncached_input_tokens": 100},
        "task_review": {
            "verdict": "approved",
            "trust": "with_limits",
            "user_report": {"headline": "审查通过，但存在证据边界。"},
        },
        "entries": [{"summary": "x" * 20_000}],
    }
    result = budget_mcp_payload(payload)
    assert result["truncated"] is True
    assert result["launch_id"] == "launch-1"
    assert result["effective_memory"]["hit"] is True
    assert result["context_control"]["auto_compact_token_limit"] == 96000
    assert result["task_review"]["trust"] == "with_limits"


def test_large_task_review_fallback_keeps_terminal_verdict_and_identity() -> None:
    long_items = ["x" * 1000 for _ in range(20)]
    payload = {
        "schema_version": 2,
        "kind": "pacer_task_completion",
        "status": "completed",
        "launch_id": "launch-large-review",
        "run_id": "20260714-120000-large",
        "outcome": {
            "outcome_status": "completed",
            "evidence_level": "verified_batch",
            "batch_run_id": "20260714-120000-large",
        },
        "task_review": {
            "valid": True,
            "verdict": "approved",
            "trust": "with_limits",
            "errors": long_items,
            "warnings": long_items,
            "user_report": {
                "headline": "审查通过，但存在证据边界。",
                "completed": long_items,
                "not_completed": long_items,
                "evidence": long_items,
                "blocking_issues": long_items,
                "risks": long_items,
                "can_trust": "with_limits",
                "next_action": "仅按机械证据交付。",
            },
            "user_report_markdown": "y" * 20_000,
        },
        "entries": [{"summary": "z" * 20_000}],
    }

    result = budget_mcp_payload(payload)

    assert result["truncated"] is True
    assert result["status"] == "completed"
    assert result["launch_id"] == "launch-large-review"
    assert result["run_id"] == "20260714-120000-large"
    assert result["outcome"]["evidence_level"] == "verified_batch"
    assert result["task_review"]["verdict"] == "approved"
    assert result["task_review"]["trust"] == "with_limits"
    assert result["task_review"]["user_report"]["can_trust"] == "with_limits"
    assert len(json.dumps(result, ensure_ascii=False, indent=2)) <= MCP_RESPONSE_MAX_CHARS
