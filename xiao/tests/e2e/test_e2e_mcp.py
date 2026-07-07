from __future__ import annotations

import asyncio
import json
from pathlib import Path

from visual_agent.mcp_server import call_tool


def call_mcp(name: str, args: dict[str, object]) -> dict[str, object]:
    result = asyncio.run(call_tool(name, args))
    return json.loads(result[0].text)


def test_mcp_client_can_discover_validate_run_and_read_report(e2e_workspace: Path) -> None:
    workspace_arg = {"workspace_root": str(e2e_workspace)}

    listed = call_mcp("list_workflows", workspace_arg)
    assert listed["workflow_count"] >= 1
    assert any(item["name"] == "local_html_form_workflow" for item in listed["workflows"])

    validated = call_mcp("validate_workflow", workspace_arg | {"workflow_name": "local_html_form_workflow"})
    assert validated["valid"] is True
    assert validated["preflight"]["ok"] is True

    run = call_mcp(
        "run_workflow",
        workspace_arg | {"workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"},
    )
    assert run["status"] == "success"
    assert run["run_profile"] == "dry-run"
    assert run["run_id"]

    report = call_mcp("get_run_report", workspace_arg | {"run_id": run["run_id"], "format": "markdown"})
    content = str(report["content"])
    assert "Report Detail: local_html_form_workflow" in content
    assert report["within_budget"] is True
    assert len(content) <= 7000
    assert "demo_password" not in content

    artifacts = call_mcp("list_run_artifacts", workspace_arg | {"run_id": run["run_id"]})
    assert artifacts["artifact_count"] >= 3
    for artifact in artifacts["artifacts"]:
        path = Path(str(artifact["path"])).resolve()
        path.relative_to(e2e_workspace.resolve())

    dashboard = call_mcp("get_workspace_dashboard", workspace_arg | {"format": "markdown"})
    assert "Workspace Dashboard" in str(dashboard["content"])

    latest_failure = call_mcp("get_latest_failure", workspace_arg | {"format": "json"})
    assert latest_failure["status"] in {"none", "found"}

    session_context = call_mcp("get_session_context", workspace_arg)
    assert session_context["within_budget"] is True
    assert len(str(session_context["snapshot"])) <= 2000

    failure_summary = call_mcp("summarize_latest_failure", workspace_arg)
    assert failure_summary["status"] in {"no_failure", "found"}
    assert len(json.dumps(failure_summary, ensure_ascii=False)) <= 2000

    verification = call_mcp("run_verification", workspace_arg)
    assert verification["within_budget"] is True
    assert len(str(verification["content"])) <= 3200
    assert "Verification Report" in str(verification["content"])


def test_mcp_returns_structured_errors_for_agent_recovery(e2e_workspace: Path) -> None:
    missing = call_mcp(
        "validate_workflow",
        {"workspace_root": str(e2e_workspace), "workflow_name": "missing_workflow"},
    )
    assert "error" in missing
    assert "hint" in missing
    assert "list_workflows" in str(missing["hint"])


def test_mcp_refuses_unapproved_real_execution(e2e_workspace: Path) -> None:
    result = call_mcp(
        "run_workflow",
        {
            "workspace_root": str(e2e_workspace),
            "workflow_name": "local_html_form_workflow",
            "inputs_file": "demo_login.json",
            "run_profile": "approved",
        },
    )

    assert "error" in result
    assert "approved_workflows" in str(result["error"])


def test_ai_context_tools_do_not_leak_secrets(e2e_workspace: Path) -> None:
    workspace_arg = {"workspace_root": str(e2e_workspace)}
    call_mcp(
        "run_workflow",
        workspace_arg | {"workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"},
    )

    context = call_mcp("get_session_context", workspace_arg)
    summary = call_mcp("summarize_latest_failure", workspace_arg)
    combined = json.dumps(context, ensure_ascii=False) + json.dumps(summary, ensure_ascii=False)

    for keyword in ("password", "cookie", "Bearer ", "demo_password"):
        assert keyword not in combined
