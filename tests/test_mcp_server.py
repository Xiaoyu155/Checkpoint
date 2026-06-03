from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from visual_agent.mcp_server import (
    call_tool,
    list_run_artifacts_payload,
    list_workflows_payload,
    mcp_tools,
    mcp_workspace_root_allowed,
    require_workspace,
    get_latest_failure_payload,
    get_workspace_dashboard_payload,
    run_workflow_payload,
    validate_workflow_payload,
)
from visual_agent.workspace import init_workspace


def content_payload(result):
    return json.loads(result[0].text)


def test_mcp_tools_include_expected_names() -> None:
    names = {tool.name for tool in mcp_tools()}

    assert names == {
        "list_workflows",
        "validate_workflow",
        "run_workflow",
        "get_run_report",
        "list_run_artifacts",
        "get_workspace_dashboard",
        "get_latest_failure",
    }


def test_mcp_list_workflows_returns_workspace_workflows(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = list_workflows_payload({"workspace_root": str(workspace.root)})

    assert result["workflow_count"] >= 1
    assert any(item["name"] == "local_html_form_workflow" for item in result["workflows"])


def test_mcp_validate_workflow_returns_validation_and_preflight(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = validate_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow"})

    assert result["valid"] is True
    assert result["preflight"]["ok"] is True


def test_mcp_validate_missing_observation_workflow_returns_not_valid(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.workflows_dir / "bad.yaml").write_text(
        "schema_version: 1\nmin_runtime_version: '0.1.0'\nname: bad\nversion: 1\nsteps:\n  - id: click\n    action: click\n",
        encoding="utf-8",
    )

    result = validate_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "bad"})

    assert result["valid"] is False


def test_mcp_run_workflow_defaults_to_dry_run_and_audits(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    args = {"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"}
    result = run_workflow_payload(args)
    audit = workspace.root / "gui" / "actions.jsonl"

    assert result["status"] == "success"
    assert result["run_profile"] == "dry-run"
    assert result["run_id"]
    assert not audit.exists()

    async_result = asyncio.run(call_tool("run_workflow", args))
    payload = content_payload(async_result)

    assert payload["status"] == "success"
    assert audit.exists()
    assert "mcp:run_workflow" in audit.read_text(encoding="utf-8")


def test_mcp_run_workflow_rejects_approved_outside_whitelist(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    with pytest.raises(ValueError):
        run_workflow_payload(
            {
                "workspace_root": str(workspace.root),
                "workflow_name": "local_html_form_workflow",
                "run_profile": "approved",
                "inputs_file": "demo_login.json",
            }
        )


def test_mcp_run_workflow_rejects_approved_when_whitelist_empty_and_when_outside(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    manifest = workspace.root / "workspace.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["mcp"]["max_run_profile"] = "approved"
    data["mcp"]["approved_workflows"] = ["other_workflow"]
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(ValueError):
        run_workflow_payload(
            {
                "workspace_root": str(workspace.root),
                "workflow_name": "local_html_form_workflow",
                "run_profile": "approved",
                "inputs_file": "demo_login.json",
            }
        )


def test_mcp_run_workflow_allows_approved_when_whitelisted_and_max_profile_allows(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    manifest = workspace.root / "workspace.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["mcp"]["max_run_profile"] = "approved"
    data["mcp"]["approved_workflows"] = ["local_html_form_workflow"]
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_workflow_payload(
        {
            "workspace_root": str(workspace.root),
            "workflow_name": "local_html_form_workflow",
            "run_profile": "approved",
            "inputs_file": "demo_login.json",
        }
    )

    assert result["run_profile"] == "approved"


def test_mcp_run_workflow_downgrades_approved_to_max_profile_when_whitelisted(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    manifest = workspace.root / "workspace.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["mcp"]["max_run_profile"] = "supervised"
    data["mcp"]["approved_workflows"] = ["local_html_form_workflow"]
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_workflow_payload(
        {
            "workspace_root": str(workspace.root),
            "workflow_name": "local_html_form_workflow",
            "run_profile": "approved",
            "inputs_file": "demo_login.json",
        }
    )

    assert result["requested_run_profile"] == "approved"
    assert result["run_profile"] == "supervised"


def test_mcp_get_run_report_markdown_is_redacted(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})

    result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "markdown"}))
    payload = content_payload(result)
    text = payload["content"]

    assert "Report Detail" in text
    assert "secret" not in text.lower()
    assert "cookie" not in text.lower()


def test_mcp_get_run_report_scrubs_sensitive_field_names_and_values(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})
    report_path = workspace.reports_dir / f"{run['run_id']}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["artifacts"] = {
        "password": "plain-password",
        "token": "plain-token",
        "cookie": "session-cookie",
        "api_key": "sk-testsecret123456",
        "authorization": "Bearer abcdefghijklmnop",
        "bearer": "abcdefghijklmnop",
        "message": "token=abc12345",
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    json_result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "json"}))
    json_payload = content_payload(json_result)
    markdown_result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "markdown"}))
    markdown_payload = content_payload(markdown_result)
    combined = json.dumps(json_payload, ensure_ascii=False) + markdown_payload["content"]

    assert "plain-password" not in combined
    assert "plain-token" not in combined
    assert "session-cookie" not in combined
    assert "sk-testsecret123456" not in combined
    assert "abcdefghijklmnop" not in combined
    assert "[REDACTED]" in combined or '"redacted": true' in combined.lower()


def test_mcp_list_run_artifacts_paths_stay_inside_workspace(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})

    result = list_run_artifacts_payload({"workspace_root": str(workspace.root), "run_id": run["run_id"]})

    assert result["artifact_count"] > 0
    for artifact in result["artifacts"]:
        assert str(artifact["path"]).startswith(str(workspace.root))
        assert ".." not in artifact["relative_path"]


def test_mcp_list_run_artifacts_skips_symlink_that_escapes_workspace(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside", encoding="utf-8")
    link = workspace.runs_dir / run["run_id"] / "outside-link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this Windows environment.")

    result = list_run_artifacts_payload({"workspace_root": str(workspace.root), "run_id": run["run_id"]})

    assert all("outside-link.txt" not in artifact["relative_path"] for artifact in result["artifacts"])
    assert all(str(outside.resolve()) != artifact["path"] for artifact in result["artifacts"])


def test_mcp_workspace_dashboard_returns_agent_readable_health(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})

    result = get_workspace_dashboard_payload({"workspace_root": str(workspace.root), "format": "markdown"})

    assert result["format"] == "markdown"
    assert "Workspace Dashboard" in result["content"]
    assert "Workflows" in result["content"]


def test_mcp_latest_failure_returns_none_when_clean(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = get_latest_failure_payload({"workspace_root": str(workspace.root), "format": "json"})

    assert result["status"] == "none"
    assert result["report"] is None


def test_mcp_latest_failure_returns_failed_report_with_diagnosis(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    failure_workflow = workspace.workflows_dir / "failure.yaml"
    failure_workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        "    path: examples/fixtures/login_page_observation.json\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )
    run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "failure"})

    result = get_latest_failure_payload({"workspace_root": str(workspace.root), "format": "json"})

    assert result["status"] == "found"
    assert result["report"]["status"] == "failed"
    assert result["report"]["failure"]["diagnosis"]["expected"]


def test_mcp_workspace_root_rejects_path_traversal(tmp_path) -> None:
    with pytest.raises(ValueError):
        require_workspace({"workspace_root": str(tmp_path / ".." / "workspace")})


def test_mcp_workspace_root_rejects_system_path_with_structured_error() -> None:
    system_path = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"
    if not system_path.exists():
        pytest.skip("System32 path is unavailable on this environment.")

    assert mcp_workspace_root_allowed(system_path) is False
    result = asyncio.run(call_tool("list_workflows", {"workspace_root": str(system_path)}))
    payload = content_payload(result)

    assert "error" in payload
    assert "outside allowed MCP roots" in payload["error"]


def test_mcp_unknown_workflow_returns_structured_error(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = asyncio.run(call_tool("validate_workflow", {"workspace_root": str(workspace.root), "workflow_name": "missing"}))
    payload = content_payload(result)

    assert "error" in payload
    assert "Workflow not found" in payload["error"]


def test_mcp_call_audit_writes_entry_and_exit_events(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    asyncio.run(call_tool("list_workflows", {"workspace_root": str(workspace.root)}))
    audit_path = workspace.root / "gui" / "actions.jsonl"
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

    assert len(events) >= 2
    assert events[-2]["action"] == "mcp:list_workflows"
    assert events[-2]["status"] == "started"
    assert events[-1]["action"] == "mcp:list_workflows"
    assert events[-1]["status"] == "success"
