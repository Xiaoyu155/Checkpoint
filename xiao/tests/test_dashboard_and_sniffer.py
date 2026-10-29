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
from visual_agent.dashboard import PacerDashboardHandler
from visual_agent.pacer_trace import compute_aggregate_savings


def test_channel_sniffer_structure() -> None:
    data = sniff_all_ai_capabilities()
    assert data["status"] == "ok"
    assert "cli_tools" in data
    assert "desktop_tools" in data
    assert "local_servers" in data
    assert "channels" in data
    assert data["total_capabilities"] >= 0


def test_compute_aggregate_savings() -> None:
    fake_traces = [
        {
            "trace_id": "tr-1",
            "status": "success",
            "elapsed_seconds": 3.2,
            "patched_files": ["foo.py"],
            "verification_command": "pytest",
            "verification_verdict": "pass",
        },
        {
            "trace_id": "tr-2",
            "status": "success",
            "elapsed_seconds": 4.1,
            "patched_files": ["bar.py"],
            "verification_command": "pytest",
            "verification_verdict": "pass",
        },
    ]
    metrics = compute_aggregate_savings(fake_traces)
    assert metrics["total_tasks"] == 2
    assert metrics["successful_tasks"] == 2
    assert metrics["success_rate_pct"] == 100.0
    assert metrics["total_tokens_saved"] > 0
    assert metrics["savings_pct"] >= 80.0
    assert metrics["pillars"]["pillar_1_ast_index"]["count"] == 2


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
