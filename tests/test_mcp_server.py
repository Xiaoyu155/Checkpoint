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


ROOT = Path(__file__).resolve().parent.parent


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
        "summarize_latest_failure",
        "get_session_context",
        "run_verification",
    }


def test_mcp_list_workflows_returns_workspace_workflows(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = list_workflows_payload({"workspace_root": str(workspace.root)})

    assert result["workflow_count"] >= 1
    assert any(item["name"] == "local_html_form_workflow" for item in result["workflows"])


def test_mcp_list_workflows_truncates_large_response(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    for index in range(250):
        (workspace.workflows_dir / f"workflow_{index:03d}_with_long_name_for_budget.yaml").write_text(
            "schema_version: 1\n"
            "min_runtime_version: '0.1.0'\n"
            f"name: workflow_{index:03d}_with_long_name_for_budget\n"
            "version: 1\n"
            "steps:\n"
            "  - id: observe\n"
            "    action: observe_screen\n",
            encoding="utf-8",
        )

    result = asyncio.run(call_tool("list_workflows", {"workspace_root": str(workspace.root)}))
    payload = content_payload(result)

    assert len(result[0].text) <= 8000
    assert payload["workflow_count"] == 250
    assert payload["truncated"] is True
    assert payload["omitted_count"] > 0


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


def test_mcp_get_run_report_is_budgeted_when_report_is_large(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})
    report_path = workspace.reports_dir / f"{run['run_id']}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["steps"] = [
        {
            "id": f"step_{index}",
            "action": "assert_text",
            "status": "success",
            "message": "large report line " + ("x" * 500),
            "elapsed_seconds": 0.01,
        }
        for index in range(80)
    ]
    report["total_steps"] = 80
    report["succeeded_steps"] = 80
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    markdown_result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "markdown"}))
    markdown_payload = content_payload(markdown_result)
    json_result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "json"}))
    json_payload = content_payload(json_result)

    assert markdown_payload["truncated"] is True
    assert markdown_payload["within_budget"] is True
    assert len(markdown_result[0].text) <= 8000
    assert json_payload["truncated"] is True
    assert json_payload["within_budget"] is True
    assert len(json_result[0].text) <= 8000


def test_mcp_list_run_artifacts_paths_stay_inside_workspace(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})

    result = list_run_artifacts_payload({"workspace_root": str(workspace.root), "run_id": run["run_id"]})

    assert result["artifact_count"] > 0
    for artifact in result["artifacts"]:
        assert str(artifact["path"]).startswith(str(workspace.root))
        assert ".." not in artifact["relative_path"]


def test_mcp_list_run_artifacts_truncates_large_response(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})
    artifact_dir = workspace.runs_dir / run["run_id"] / "many"
    artifact_dir.mkdir(parents=True)
    for index in range(300):
        (artifact_dir / f"artifact_{index:03d}_with_long_name_for_budget.txt").write_text("x", encoding="utf-8")

    result = asyncio.run(call_tool("list_run_artifacts", {"workspace_root": str(workspace.root), "run_id": run["run_id"]}))
    payload = content_payload(result)

    assert len(result[0].text) <= 8000
    assert payload["artifact_count"] >= 300
    assert payload["truncated"] is True
    assert payload["omitted_count"] > 0


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
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
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


def test_mcp_session_context_and_failure_summary_stay_within_budget(tmp_path) -> None:
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
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )
    run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "failure"})

    context = content_payload(asyncio.run(call_tool("get_session_context", {"workspace_root": str(workspace.root)})))
    summary = content_payload(asyncio.run(call_tool("summarize_latest_failure", {"workspace_root": str(workspace.root)})))
    combined = json.dumps(context, ensure_ascii=False) + json.dumps(summary, ensure_ascii=False)

    assert context["within_budget"] is True
    assert len(context["snapshot"]) <= 2000
    assert summary["status"] == "found"
    assert len(json.dumps(summary, ensure_ascii=False)) <= 2000
    for keyword in ("password", "cookie", "Bearer ", "demo123"):
        assert keyword not in combined


def test_mcp_run_verification_returns_ai_ready_report(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow = workspace.workflows_dir / "verification.yaml"
    workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: verification\n"
        "version: 1\n"
        "tags:\n"
        "  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n",
        encoding="utf-8",
    )

    payload = content_payload(asyncio.run(call_tool("run_verification", {"workspace_root": str(workspace.root)})))

    assert payload["total"] == 1
    assert payload["passed"] == 1
    assert payload["failed"] == 0
    assert payload["within_budget"] is True
    assert "Verification Report" in payload["content"]


def test_mcp_run_verification_can_target_one_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    for name in ("slow_visual_contract", "fast_smoke_contract"):
        (workspace.workflows_dir / f"{name}.yaml").write_text(
            "schema_version: 1\n"
            "min_runtime_version: '0.1.0'\n"
            f"name: {name}\n"
            "version: 1\n"
            "tags:\n"
            "  - verification\n"
            "steps:\n"
            "  - id: observe\n"
            "    action: observe_fixture\n"
            f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n",
            encoding="utf-8",
        )

    payload = content_payload(
        asyncio.run(
            call_tool(
                "run_verification",
                {
                    "workspace_root": str(workspace.root),
                    "workflow": ["fast_smoke_contract"],
                    "max_workflows": 1,
                },
            )
        )
    )

    assert payload["total"] == 1
    assert "fast_smoke_contract" in payload["content"]
    assert "slow_visual_contract" not in payload["content"]


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


def assert_mcp_tool_audited(workspace, tool_name: str, args: dict[str, object]) -> None:
    asyncio.run(call_tool(tool_name, {"workspace_root": str(workspace.root), **args}))
    audit_path = workspace.root / "gui" / "actions.jsonl"
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

    assert events[-2]["action"] == f"mcp:{tool_name}"
    assert events[-2]["status"] == "started"
    assert events[-1]["action"] == f"mcp:{tool_name}"
    assert events[-1]["status"] in {"success", "none", "found", "no_failure"}


def test_mcp_get_session_context_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "get_session_context", {})


def test_mcp_summarize_latest_failure_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "summarize_latest_failure", {})


def test_mcp_run_verification_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "run_verification", {})
