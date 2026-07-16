from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from visual_agent.mcp_pacer_contracts import (
    PACER_TYPED_TOOL_NAMES,
    pacer_tool_input_schema,
    pacer_tool_output_schema,
    validate_pacer_tool_input,
    validate_pacer_tool_output,
)
from visual_agent.mcp_server import call_tool_payload, mcp_tools


def test_typed_pacer_tool_schemas_are_generated_and_fully_inlined() -> None:
    tools = {tool.name: tool for tool in mcp_tools()}

    assert PACER_TYPED_TOOL_NAMES <= tools.keys()
    for name in PACER_TYPED_TOOL_NAMES:
        assert "$defs" not in tools[name].inputSchema
        assert "$defs" not in tools[name].outputSchema
        assert tools[name].inputSchema == pacer_tool_input_schema(name)
        assert tools[name].outputSchema == pacer_tool_output_schema(name)
        assert tools[name].inputSchema["additionalProperties"] is False


def test_typed_inputs_reject_extra_fields_and_unbound_memory_use() -> None:
    with pytest.raises(ValidationError, match="memory_ids_used requires known_memory_receipt"):
        validate_pacer_tool_input(
            "get_pacer_memory",
            {
                "workspace_root": ".agent-workspace",
                "repo_root": ".",
                "memory_ids_used": ["memory-1"],
            },
        )

    with pytest.raises(ValidationError, match="extra_forbidden"):
        validate_pacer_tool_input(
            "run_pacer_verification",
            {
                "workspace_root": ".agent-workspace",
                "repo_root": ".",
                "steps": [{"name": "tests", "argv": ["pytest"], "command": "pytest"}],
            },
        )


def test_typed_outputs_fail_closed_when_success_shape_is_incomplete() -> None:
    with pytest.raises(ValidationError, match="verification output requires"):
        validate_pacer_tool_output(
            "run_pacer_verification",
            {"schema_version": 1, "status": "passed"},
        )

    error = validate_pacer_tool_output(
        "run_pacer_verification",
        {"schema_version": 1, "error": "validation failed"},
    )
    assert error == {"schema_version": 1, "error": "validation failed"}


def test_dispatch_rejects_invalid_typed_input_before_handler_execution(tmp_path) -> None:
    result = asyncio.run(
        call_tool_payload(
            "run_pacer_verification",
            {
                "workspace_root": str(tmp_path / ".agent-workspace"),
                "repo_root": str(tmp_path),
                "steps": [{"name": "tests", "argv": ["pytest"], "command": "pytest"}],
            },
        )
    )

    assert result["schema_version"] == 1
    assert "error" in result
    assert not (tmp_path / ".agent-workspace" / "pacer_native" / "commands").exists()


def test_dispatch_validates_typed_handler_output_without_running_commands(tmp_path, monkeypatch) -> None:
    from visual_agent import mcp_server

    monkeypatch.setattr(
        mcp_server,
        "run_pacer_verification_payload",
        lambda _args: {
            "schema_version": 1,
            "status": "passed",
            "run_id": "run-1",
            "records": [],
        },
    )
    arguments = {
        "workspace_root": str(tmp_path / ".agent-workspace"),
        "repo_root": str(tmp_path),
        "steps": [{"name": "tests", "argv": ["pytest"]}],
    }

    passed = asyncio.run(call_tool_payload("run_pacer_verification", arguments))
    assert passed["run_id"] == "run-1"

    monkeypatch.setattr(
        mcp_server,
        "run_pacer_verification_payload",
        lambda _args: {"schema_version": 1, "status": "passed"},
    )
    rejected = asyncio.run(call_tool_payload("run_pacer_verification", arguments))
    assert "verification output requires" in rejected["error"]
