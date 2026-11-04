from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from visual_agent.channel_sniffer import (
    sniff_all_ai_capabilities,
    sniff_cli_tools,
    sniff_desktop_tools,
    sniff_local_llm_servers,
    sniff_configured_channels,
)
from visual_agent.dashboard import DASHBOARD_HTML_TEMPLATE, PacerDashboardHandler
from visual_agent.pacer_trace import compute_aggregate_savings


def test_channel_sniffer_structure() -> None:
    data = sniff_all_ai_capabilities()
    assert data["status"] == "ok"
    assert "cli_tools" in data
    assert "desktop_tools" in data
    assert "local_servers" in data
    assert "channels" in data
    assert data["total_capabilities"] >= 0


def test_compute_aggregate_savings_uses_measured_values_not_fixed_constants() -> None:
    """Aggregation must report measured usage only. There is no invented
    8,500/1,200 token baseline any more, and no fixed savings percentage."""
    fake_traces = [
        {
            "trace_id": "tr-1",
            "status": "success",
            "elapsed_seconds": 3.2,
            "patched_files": ["foo.py"],
            "verification_command": "pytest",
            "verification_verdict": "pass",
            "usage_measurement": "measured",
            "prompt_tokens": 1200,
            "model_calls": 2,
        },
        {
            "trace_id": "tr-2",
            "status": "success",
            "elapsed_seconds": 4.1,
            "patched_files": ["bar.py"],
            "verification_command": "pytest",
            "verification_verdict": "pass",
            "usage_measurement": "measured",
            "prompt_tokens": 800,
            "model_calls": 1,
        },
    ]
    metrics = compute_aggregate_savings(fake_traces)
    assert metrics["total_tasks"] == 2
    assert metrics["successful_tasks"] == 2
    assert metrics["success_rate_pct"] == 100.0
    # Real measured prompt volume, not 2 * 1200 from an invented constant.
    assert metrics["measured_prompt_tokens"] == 2000
    assert metrics["total_tokens_consumed"] == 2000
    assert metrics["model_calls"] == 3
    assert metrics["usage_measurement"] == "measured"
    # No traditional baseline was recorded, so no savings may be claimed.
    assert metrics["savings_pct"] is None
    assert metrics["total_tokens_saved"] is None
    assert metrics["estimated_usd_saved"] is None
    assert metrics["pillars"]["pillar_1_ast_index"]["count"] == 2
    # The invented "16.5 ms average" constant must not survive.
    assert metrics["pillars"]["pillar_1_ast_index"]["avg_latency_ms"] is None


def test_compute_aggregate_savings_reports_no_data_when_empty() -> None:
    """With no traces there is no data — the previous implementation
    reported a fabricated 94.5% savings and 100% success rate."""
    metrics = compute_aggregate_savings([])
    assert metrics["total_tasks"] == 0
    assert metrics["savings_pct"] is None
    assert metrics["estimated_usd_saved"] is None
    assert metrics["success_rate_pct"] is None
    assert metrics["total_time_saved_minutes"] is None
    assert metrics["measured_prompt_tokens"] == 0
    assert metrics["usage_measurement"] == "unknown"


def test_compute_aggregate_savings_counts_failed_tasks_without_claiming_savings() -> None:
    """A failed task is still a task. It must be counted, but must not be
    presented as a saving."""
    metrics = compute_aggregate_savings([
        {
            "trace_id": "tr-fail",
            "status": "failed",
            "elapsed_seconds": 2.0,
            "usage_measurement": "measured",
            "prompt_tokens": 3000,
            "model_calls": 4,
        }
    ])
    assert metrics["total_tasks"] == 1
    assert metrics["successful_tasks"] == 0
    assert metrics["success_rate_pct"] == 0.0
    assert metrics["measured_prompt_tokens"] == 3000
    assert metrics["model_calls"] == 4
    assert metrics["savings_pct"] is None


def test_compute_aggregate_savings_uses_recorded_baseline_when_present() -> None:
    """When a traditional baseline was actually measured alongside the run,
    savings may be computed from the two real numbers."""
    metrics = compute_aggregate_savings([
        {
            "trace_id": "tr-1",
            "status": "success",
            "elapsed_seconds": 3.0,
            "usage_measurement": "measured",
            "prompt_tokens": 1200,
            "model_calls": 1,
            "traditional_baseline_prompt_tokens": 8500,
            "traditional_baseline_seconds": 45.0,
        }
    ])
    assert metrics["savings_pct"] == round((7300 / 8500) * 100, 1)
    assert metrics["total_tokens_saved"] == 7300
    assert metrics["estimated_usd_saved"] == round((7300 / 1_000_000) * 3.0, 2)
    assert metrics["total_time_saved_sec"] is not None


def test_dashboard_does_not_inject_recorded_values_into_innerhtml() -> None:
    """Recorded goals, channel paths and probe details are attacker-
    controllable text. They must be rendered via textContent, never by
    string-interpolating them into innerHTML."""
    html = DASHBOARD_HTML_TEMPLATE
    # The trace goal must not be interpolated into an HTML template literal.
    assert "${t.goal}" not in html
    assert "title=\"${t.goal}\"" not in html
    assert "${m.details}" not in html
    assert "${item.name}" not in html
    assert "${pData.desc}" not in html
    # The rows must be built with the DOM API instead.
    assert "td.textContent = text;" in html
    assert "textContent = m.details || ''" in html


def test_dashboard_failed_task_is_not_shown_as_passed() -> None:
    """A failed run must not be rendered as PASSED just because a
    verification command inside it reported success."""
    html = DASHBOARD_HTML_TEMPLATE
    assert "t.verification_verdict === 'pass'" not in html
    assert "const isPass = t.status === 'success';" in html


def test_dashboard_does_not_invent_elapsed_seconds() -> None:
    """Without a recorded duration the dashboard must show a dash, not a
    fabricated number."""
    html = DASHBOARD_HTML_TEMPLATE
    assert "t.elapsed_seconds || 3.2" not in html
    assert "'—'" in html


def test_dashboard_handler_api() -> None:
    handler = PacerDashboardHandler.__new__(PacerDashboardHandler)
    handler.path = "/api/data"
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()

    handler.do_GET()
    handler.send_response.assert_called_with(200)
    handler.send_header.assert_called_with("Content-Type", "application/json; charset=utf-8")
    assert handler.wfile.write.called


def test_dashboard_handler_check_models_api() -> None:
    handler = PacerDashboardHandler.__new__(PacerDashboardHandler)
    handler.path = "/api/check_models"
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()

    with patch("visual_agent.dashboard.check_all_models_concurrently") as mock_check:
        mock_check.return_value = {
            "status": "ok",
            "online_count": 4,
            "models": {"codex": {"status": "online"}},
        }
        handler.do_GET()
        handler.send_response.assert_called_with(200)
        handler.send_header.assert_called_with("Content-Type", "application/json; charset=utf-8")
        assert handler.wfile.write.called
        written_bytes = handler.wfile.write.call_args[0][0]
        data = json.loads(written_bytes.decode("utf-8"))
        assert data["status"] == "ok"
        assert data["online_count"] == 4
