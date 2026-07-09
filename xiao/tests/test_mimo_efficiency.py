"""Tests for mimo_efficiency aggregation module."""

from __future__ import annotations

from visual_agent.mimo_efficiency import compute_mimo_efficiency


def test_compute_mimo_efficiency_empty_records():
    result = compute_mimo_efficiency([])
    assert result["mimo_runs"] == 0
    assert result["backend_runs"] == 0
    assert result["saved_usd"] == 0.0
    assert result["saved_quota_percent"] == 0.0
    assert result["spent_usd"] == 0.0
    assert result["saved_minutes"] == 0.0
    assert result["efficiency_gain_percent"] == 0
    assert result["capability_score"] == 0.0


def test_compute_mimo_efficiency_none_records_skipped():
    result = compute_mimo_efficiency([None, {"status": "completed"}])
    assert result["mimo_runs"] == 0


def test_compute_mimo_efficiency_mimo_backend_is_savings():
    records = [
        {
            "status": "completed",
            "elapsed_seconds": 120.0,
            "usage": {"cost_usd": 0.05, "cost_is_savings": True, "output_tokens": 500},
            "backend": {"name": "mimo"},
        },
    ]
    result = compute_mimo_efficiency(records)
    assert result["mimo_runs"] == 1
    assert result["saved_usd"] == 0.05
    assert result["saved_quota_percent"] == 100.0
    assert result["spent_usd"] == 0.0
    # 120s * 0.35 / 60 = 0.7 minutes
    assert abs(result["saved_minutes"] - 0.7) < 0.01


def test_compute_mimo_efficiency_estimates_savings_without_cost_receipt():
    records = [
        {
            "status": "failed",
            "elapsed_seconds": 120,
            "backend": {"name": "mimo"},
            "usage": {"cost_is_savings": True, "backend": "mimo"},
        }
    ]

    result = compute_mimo_efficiency(records)

    assert result["mimo_runs"] == 1
    assert result["saved_minutes"] == 0.7
    assert result["saved_usd"] > 0
    assert result["efficiency_gain_percent"] > 0
    assert result["capability_score"] > 0


def test_compute_mimo_efficiency_subscription_run_is_spent():
    records = [
        {
            "status": "completed",
            "elapsed_seconds": 60.0,
            "usage": {"cost_usd": 0.10, "output_tokens": 200},
            # No backend -> subscription spent
        },
    ]
    result = compute_mimo_efficiency(records)
    assert result["mimo_runs"] == 0
    assert result["backend_runs"] == 0
    assert result["spent_usd"] == 0.10
    assert result["saved_usd"] == 0.0
    assert result["saved_quota_percent"] == 0.0


def test_compute_mimo_efficiency_other_backend_is_spent():
    records = [
        {
            "status": "completed",
            "elapsed_seconds": 90.0,
            "usage": {"cost_usd": 0.08, "output_tokens": 300},
            "backend": {"name": "openai"},
        },
    ]
    result = compute_mimo_efficiency(records)
    assert result["backend_runs"] == 1
    assert result["spent_usd"] == 0.08


def test_compute_mimo_efficiency_capability_score_completed_tasks():
    # 3 completed, 1 failed -> completion rate 0.75 -> score ~45 base + tokens
    records = [
        {"status": "completed", "usage": {"output_tokens": 1000}},
        {"status": "completed", "usage": {"output_tokens": 1000}},
        {"status": "completed", "usage": {"output_tokens": 1000}},
        {"status": "failed", "usage": {"output_tokens": 100}},
    ]
    result = compute_mimo_efficiency(records)
    assert 50 <= result["capability_score"] <= 100


def test_compute_mimo_efficiency_capability_score_no_records():
    result = compute_mimo_efficiency([])
    assert result["capability_score"] == 0.0


def test_compute_mimo_efficiency_efficiency_capped():
    # Extreme savings, tiny spend — should be capped at 9999
    records = [
        {
            "status": "completed",
            "elapsed_seconds": 3600.0,
            "usage": {"cost_usd": 100.0, "cost_is_savings": True, "output_tokens": 50000},
            "backend": {"name": "mimo"},
        },
        {
            "status": "completed",
            "elapsed_seconds": 10.0,
            "usage": {"cost_usd": 0.001, "output_tokens": 10},
        },
    ]
    result = compute_mimo_efficiency(records)
    assert result["efficiency_gain_percent"] <= 9999


def test_compute_mimo_efficiency_mimo_run_via_cost_is_savings_flag():
    # A record with cost_is_savings=True but no backend name should still count as MiMo
    records = [
        {
            "status": "completed",
            "elapsed_seconds": 60.0,
            "usage": {"cost_usd": 0.03, "cost_is_savings": True, "output_tokens": 200},
        },
    ]
    result = compute_mimo_efficiency(records)
    assert result["mimo_runs"] == 1
    assert result["saved_usd"] == 0.03


def test_compute_mimo_efficiency_labels_present():
    result = compute_mimo_efficiency([])
    labels = result["labels"]
    assert "mimo_runs" in labels
    assert "saved_usd" in labels
    assert "saved_quota_percent" in labels
    assert "saved_minutes" in labels
    assert "efficiency_gain_percent" in labels
    assert "capability_score" in labels
    # All labels should be Chinese
    assert "MiMo" in labels["mimo_runs"]
    assert "额度" in labels["saved_usd"]
    assert "套餐额度" in labels["saved_quota_percent"]
    assert "时间" in labels["saved_minutes"]
    assert "效率" in labels["efficiency_gain_percent"]
