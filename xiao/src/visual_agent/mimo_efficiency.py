"""MiMo efficiency metrics aggregation.

Pure data functions that compute MiMo-specific value metrics from worker
records: how many runs MiMo handled, how much money was saved, estimated
time savings, an overall efficiency gain percentage, and a conservative
capability score.

No side effects — all functions take data and return dicts.
"""

from __future__ import annotations

from typing import Any

# Baseline multiplier: a human or strong model would take 1.35x the elapsed
# time of a MiMo task.  The savings fraction is the remainder.
_TIME_SAVINGS_FRACTION = 0.35

# Conservative value-of-time: $0.20 per minute saved.
_VALUE_PER_MINUTE_USD = 0.20

# Efficiency gain clamp range.
_EFFICIENCY_MIN = 0
_EFFICIENCY_MAX = 9999

# Capability score range.
_CAP_MIN = 0
_CAP_MAX = 100

# Labels for dashboard display.
EFFICIENCY_LABELS = {
    "mimo_runs": "MiMo 完成次数",
    "backend_runs": "其他后端次数",
    "saved_usd": "额度节省 (USD)",
    "saved_quota_percent": "套餐额度节省比例 (%)",
    "spent_usd": "实际花费 (USD)",
    "saved_minutes": "时间节省 (分钟)",
    "efficiency_gain_percent": "综合效率提升 (%)",
    "capability_score": "能力评分",
}


def compute_mimo_efficiency(worker_records: list[dict[str, Any] | None]) -> dict[str, Any]:
    """Aggregate MiMo efficiency metrics from a flat list of worker records.

    Each record is expected to be a dict like those produced by
    ``chief_dispatch._run_worker_attempt`` — with optional ``usage``,
    ``backend``, ``status``, and ``elapsed_seconds`` keys.

    Returns a dict with all the efficiency fields plus labels.
    """
    mimo_runs = 0
    backend_runs = 0
    saved_usd = 0.0
    spent_usd = 0.0
    saved_minutes = 0.0
    mimo_reported_cost = 0.0
    completed = 0
    failed = 0
    total_output_tokens = 0

    for record in worker_records:
        if not isinstance(record, dict):
            continue

        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        backend = record.get("backend") if isinstance(record.get("backend"), dict) else {}
        backend_name = str(backend.get("name") or (usage.get("backend") if isinstance(usage, dict) else "") or "").lower()
        status = str(record.get("status") or "")
        elapsed = float(record.get("elapsed_seconds") or 0.0)

        cost = float(usage.get("cost_usd") or 0.0)
        is_savings = bool(usage.get("cost_is_savings"))

        if backend_name == "mimo" or is_savings:
            mimo_runs += 1
            record_saved_minutes = elapsed * _TIME_SAVINGS_FRACTION / 60.0 if elapsed > 0 else 0.0
            if cost > 0:
                mimo_reported_cost += cost
                saved_usd += cost
            elif record_saved_minutes > 0:
                # Some low-cost backends do not return token/cost receipts.
                # Use elapsed time to derive a conservative avoided-quota value
                # so dashboard value metrics reflect the actual backend run.
                estimated = record_saved_minutes * _VALUE_PER_MINUTE_USD
                mimo_reported_cost += estimated
                saved_usd += estimated
            # Time savings: MiMo baseline is 1.35x elapsed, so savings = elapsed * 0.35
            saved_minutes += record_saved_minutes
        elif backend_name:
            backend_runs += 1
            spent_usd += cost
        else:
            # Subscription-based run (no backend) — counts as spent.
            spent_usd += cost

        if status == "completed":
            completed += 1
        elif status == "failed":
            failed += 1

        total_output_tokens += int(usage.get("output_tokens") or 0)

    # Efficiency gain: conservative ratio of value derived vs actual spend.
    denominator = max(spent_usd + mimo_reported_cost, 0.01)
    raw_efficiency = (saved_usd + saved_minutes * _VALUE_PER_MINUTE_USD) / denominator * 100.0
    efficiency_gain_percent = max(_EFFICIENCY_MIN, min(_EFFICIENCY_MAX, round(raw_efficiency, 1)))
    saved_quota_percent = round(saved_usd / max(saved_usd + spent_usd, 0.01) * 100.0, 1) if (saved_usd > 0 or spent_usd > 0) else 0.0

    # Capability score: start at 50 (neutral), adjust by completion rate,
    # output volume, and savings.  Conservative — never exceeds 100.
    total_runs = completed + failed
    if total_runs > 0:
        completion_rate = completed / total_runs
    else:
        completion_rate = 0.0
    # Base from completion (0-60 points)
    score = completion_rate * 60.0
    # Bonus for having output tokens (shows real work happened, 0-20)
    if total_output_tokens > 0:
        token_bonus = min(20.0, total_output_tokens / 500.0)
        score += token_bonus
    # Bonus for savings (0-20)
    if saved_usd > 0:
        savings_bonus = min(20.0, saved_usd * 10.0)
        score += savings_bonus
    capability_score = max(_CAP_MIN, min(_CAP_MAX, round(score, 1)))

    return {
        "mimo_runs": mimo_runs,
        "backend_runs": backend_runs,
        "saved_usd": round(saved_usd, 4),
        "saved_quota_percent": saved_quota_percent,
        "spent_usd": round(spent_usd, 4),
        "saved_minutes": round(saved_minutes, 2),
        "efficiency_gain_percent": efficiency_gain_percent,
        "capability_score": capability_score,
        "labels": EFFICIENCY_LABELS,
    }
