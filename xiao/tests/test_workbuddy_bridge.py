from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

from visual_agent.workbuddy_bridge import (
    WorkBuddyGatewayClient,
    WorkBuddyGatewayInfo,
    invoke_workbuddy,
)


def test_workbuddy_gateway_info() -> None:
    info = WorkBuddyGatewayInfo(port=61386, password="test-password", pid=1234)
    assert info.port == 61386
    assert info.password == "test-password"
    assert info.base_url == "http://127.0.0.1:61386"
    assert info.pid == 1234


def test_workbuddy_check_health_not_available() -> None:
    with patch("visual_agent.workbuddy_bridge.get_gateway_info", return_value=None):
        client = WorkBuddyGatewayClient(info=None)
        assert client.is_available is False
        res = client.check_health()
        assert res["available"] is False


def test_workbuddy_check_health_success() -> None:
    info = WorkBuddyGatewayInfo(port=61386, password="pwd")
    client = WorkBuddyGatewayClient(info=info)
    fake_body = json.dumps({"busy": False, "status": "idle"}).encode("utf-8")

    fake_resp = MagicMock()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.read.return_value = fake_body

    with patch("urllib.request.urlopen", return_value=fake_resp):
        res = client.check_health()
        assert res["available"] is True
        assert res["status"]["status"] == "idle"
        assert res["port"] == 61386


def test_workbuddy_create_run_and_stream() -> None:
    info = WorkBuddyGatewayInfo(port=61386, password="pwd")
    client = WorkBuddyGatewayClient(info=info)

    # First call: POST /api/v1/runs returns runId
    post_resp = MagicMock()
    post_resp.__enter__.return_value = post_resp
    post_resp.read.return_value = json.dumps({"runId": "run-456", "sessionId": "s-1"}).encode("utf-8")

    # Second call: GET /api/v1/runs/{id}/stream returns SSE lines
    sse_lines = [
        b"event: delta\n",
        b'data: {"delta": "def foo():"}\n',
        b"\n",
        b"event: delta\n",
        b'data: {"delta": " return 42"}\n',
        b"\n",
        b"event: done\n",
        b"data: [DONE]\n",
    ]
    stream_resp = MagicMock()
    stream_resp.__enter__.return_value = stream_resp
    stream_resp.__iter__.return_value = iter(sse_lines)

    call_count = 0

    def fake_urlopen(req, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return post_resp
        return stream_resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        run_res = client.create_run("write foo function")
        assert run_res.success is True
        assert run_res.run_id == "run-456"
        assert run_res.response == "def foo(): return 42"
        assert len(run_res.raw_events) == 2


def test_invoke_workbuddy_helper() -> None:
    from visual_agent.workbuddy_bridge import WorkBuddyAccountPool
    WorkBuddyAccountPool.reset_instance()
    with patch("visual_agent.workbuddy_bridge.get_gateway_info", return_value=None), \
         patch("visual_agent.workbuddy_bridge.discover_all_workbuddy_gateways", return_value=[]):
        res = invoke_workbuddy("hello")
        assert res.success is False
        assert "not available" in res.error.lower() or "exhausted" in res.error.lower()
    WorkBuddyAccountPool.reset_instance()


def test_workbuddy_account_pool_rotation_and_quota() -> None:
    from visual_agent.workbuddy_bridge import WorkBuddyAccountPool

    gw1 = WorkBuddyGatewayInfo(port=10001, password="p1", account_id="acc1")
    gw2 = WorkBuddyGatewayInfo(port=10002, password="p2", account_id="acc2")

    pool = WorkBuddyAccountPool(gateways=[gw1, gw2])
    assert pool.get_active_gateway().port == 10001

    # Mark gw1 exhausted
    pool.mark_exhausted(10001, cooldown_seconds=60.0, reason="HTTP 429 quota exceeded")
    # Should rotate to gw2
    assert pool.get_active_gateway().port == 10002

    # Rotate manually
    pool.rotate()
    # gw1 is still exhausted, so get_active_gateway returns gw2
    assert pool.get_active_gateway().port == 10002


def test_workbuddy_is_ready_handling() -> None:
    info = WorkBuddyGatewayInfo(port=61386, password="pwd")
    client = WorkBuddyGatewayClient(info=info)

    # When busy is False
    with patch.object(client, "check_health", return_value={"available": True, "status": {"data": {"busy": False}}}):
        assert client.is_ready is True

    # When busy is True
    with patch.object(client, "check_health", return_value={"available": True, "status": {"data": {"busy": True}}}):
        assert client.is_ready is False

    # When not available
    with patch.object(client, "check_health", return_value={"available": False}):
        assert client.is_ready is False

