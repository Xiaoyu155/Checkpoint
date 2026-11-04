from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from visual_agent import mcp_server


def test_mcp_initialize() -> None:
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"},
    }
    resp = mcp_server.handle_jsonrpc_message(msg)
    assert resp is not None
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert resp["result"]["serverInfo"]["name"] == "pacer-mcp"


def test_mcp_ping() -> None:
    msg = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
    resp = mcp_server.handle_jsonrpc_message(msg)
    assert resp is not None
    assert resp["id"] == 2
    assert resp["result"] == {}


def test_mcp_tools_list() -> None:
    msg = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
    resp = mcp_server.handle_jsonrpc_message(msg)
    assert resp is not None
    tools = resp["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "pacer_auto" in tool_names
    assert "pacer_index" in tool_names
    assert "pacer_canvas" in tool_names
    assert "begin_pacer_task" in tool_names
    assert "get_pacer_memory" in tool_names
    assert "complete_pacer_task" in tool_names


def test_mcp_call_pacer_index(tmp_path: Path) -> None:
    sample_file = tmp_path / "hello.py"
    sample_file.write_text("def greet():\n    return 'hello'\n", encoding="utf-8")

    msg = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "pacer_index",
            "arguments": {
                "file_path": str(sample_file),
                "workspace_root": str(tmp_path),
            },
        },
    }
    resp = mcp_server.handle_jsonrpc_message(msg)
    assert resp is not None
    assert resp["id"] == 4
    result = resp["result"]
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert "def greet" in text


def test_mcp_call_pacer_canvas(tmp_path: Path) -> None:
    msg = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {
            "name": "pacer_canvas",
            "arguments": {"workspace_root": str(tmp_path)},
        },
    }
    resp = mcp_server.handle_jsonrpc_message(msg)
    assert resp is not None
    assert resp["result"]["isError"] is False
    text = resp["result"]["content"][0]["text"]
    canvas_json = json.loads(text)
    assert "version" in canvas_json


def test_mcp_call_begin_and_get_memory(tmp_path: Path) -> None:
    msg1 = {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {
            "name": "begin_pacer_task",
            "arguments": {"workspace_root": str(tmp_path), "goal": "Add feature X"},
        },
    }
    resp1 = mcp_server.handle_jsonrpc_message(msg1)
    assert resp1 is not None
    assert resp1["result"]["isError"] is False

    msg2 = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "get_pacer_memory",
            "arguments": {"workspace_root": str(tmp_path)},
        },
    }
    resp2 = mcp_server.handle_jsonrpc_message(msg2)
    assert resp2 is not None
    memory_data = json.loads(resp2["result"]["content"][0]["text"])
    assert memory_data.get("goal", {}).get("summary") == "Add feature X"


def test_mcp_notification_returns_none() -> None:
    notification = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    resp = mcp_server.handle_jsonrpc_message(notification)
    assert resp is None


def test_mcp_begin_task_records_launch_lifecycle(tmp_path: Path) -> None:
    from visual_agent.pacer_launch_context import write_active_launch, read_active_launch
    # Set up an active launch
    write_active_launch(
        tmp_path,
        {
            "schema_version": 1,
            "launch_id": "launch-test-1",
            "launch_goal": "Test launch goal",
            "task_contract": {"goal": "Test launch goal"},
        },
    )

    msg = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {
            "name": "begin_pacer_task",
            "arguments": {"workspace_root": str(tmp_path), "goal": "Test launch goal"},
        },
    }
    resp = mcp_server.handle_jsonrpc_message(msg)
    assert resp is not None
    assert resp["result"]["isError"] is False

    active = read_active_launch(tmp_path)
    assert active is not None
    assert active.get("task_trigger", {}).get("status") == "triggered"
    assert active.get("task_lifecycle", {}).get("status") == "running"


def test_mcp_complete_task_rejects_unearned_trust(tmp_path: Path, monkeypatch) -> None:
    from visual_agent.pacer_launch_context import write_active_launch
    write_active_launch(
        tmp_path,
        {
            "schema_version": 1,
            "launch_id": "launch-test-2",
            "launch_goal": "Deliver feature Y with tests",
            "task_contract": {"goal": "Deliver feature Y with tests"},
        },
    )

    # Mock command verification to pass
    monkeypatch.setattr(
        mcp_server,
        "run_command_verification",
        lambda **kwargs: {"verdict": "pass", "exit_code": 0, "stdout": "1 passed", "stderr": ""},
    )

    # Complete task without evidence
    msg = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {
            "name": "complete_pacer_task",
            "arguments": {
                "workspace_root": str(tmp_path),
                "goal": "Deliver feature Y with tests",
                "summary": "Completed with no evidence",
            },
        },
    }
    resp = mcp_server.handle_jsonrpc_message(msg)
    assert resp is not None
    payload = json.loads(resp["result"]["content"][0]["text"])
    # can_trust should not be granted blindly without valid evidence
    assert payload.get("can_trust") is False or payload.get("status") == "failed"


def test_mcp_pacer_index_path_traversal_rejected(tmp_path: Path) -> None:
    outside_file = tmp_path.parent / "secret.py"
    outside_file.write_text("SECRET = 123\n", encoding="utf-8")

    msg = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {
            "name": "pacer_index",
            "arguments": {
                "file_path": "../secret.py",
                "workspace_root": str(tmp_path),
            },
        },
    }
    resp = mcp_server.handle_jsonrpc_message(msg)
    assert resp is not None
    assert resp["result"]["isError"] is True
    err_text = resp["result"]["content"][0]["text"]
    assert "Path traversal rejected" in err_text


def test_mcp_pacer_auto_reports_locked_acceptance_tests(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from visual_agent import model_workflow

    plan = SimpleNamespace(
        planner_backend="gemini38",
        tasks=[SimpleNamespace(role="coder", backend="workbuddy", patched_files=["calc.py"])],
    )

    class FinishedOrchestrator:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self):
            return {
                "success": True,
                "elapsed_seconds": 1.0,
                "completed_tasks": 1,
                "total_tasks": 1,
                "acceptance_test_files": ["tests/test_calc.py"],
            }

    monkeypatch.setattr(model_workflow, "plan_with_gemini4", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(mcp_server, "SubagentOrchestrator", FinishedOrchestrator)

    result = mcp_server._handle_tool_call("pacer_auto", {"goal": "Improve calc", "workspace_root": str(tmp_path)})
    report = result["content"][0]["text"]
    assert "Patched Files: calc.py" in report
    assert "Locked Acceptance Tests: tests/test_calc.py" in report

