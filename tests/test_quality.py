import json
from os import utime
from pathlib import Path

from visual_agent.quality import (
    QualityGateStep,
    QualityGateResult,
    apply_quality_gate_strict_policy,
    build_coding_agent_brief,
    build_install_check_plan,
    build_mcp_client_config,
    build_release_check_plan,
    coding_agent_brief_to_markdown,
    build_quality_gate_index,
    build_quality_gate_plan,
    build_quality_gate_risk_summary,
    demo_workspace_check_to_markdown,
    install_check_plan_to_markdown,
    list_quality_gate_reports,
    load_quality_gate_index,
    mcp_client_config_to_markdown,
    mcp_smoke_check_to_markdown,
    quality_gate_index_to_markdown,
    quality_gate_reports_to_markdown,
    quality_gate_to_junit_xml,
    quality_gate_to_step_summary,
    release_check_plan_to_markdown,
    release_trial_to_markdown,
    run_demo_workspace_check,
    run_mcp_smoke_check,
    run_release_trial,
    quality_gate_status,
    quality_gate_to_markdown,
    run_quality_gate,
    scan_workspace_secret_artifacts,
    write_quality_gate_index,
)
from visual_agent.cli import main
from visual_agent.gui import build_gui_action_plan, execute_gui_action, safe_execute_gui_action
from visual_agent.scheduler import submit_queue_task
from visual_agent.workspace import init_workspace


def test_quality_gate_plan_includes_core_tests() -> None:
    steps = build_quality_gate_plan("local")

    assert steps[0].name == "core_tests"
    assert steps[0].command[2] == "pytest"
    assert "--ignore=tests/e2e" in steps[0].command


def test_release_check_plan_lists_required_commands() -> None:
    plan = build_release_check_plan(workspace_root=".agent-workspace")
    markdown = release_check_plan_to_markdown(plan)

    assert plan["check_count"] >= 6
    assert any(check["id"] == "install_check" for check in plan["checks"])
    assert any(check["id"] == "mcp_smoke_cli" for check in plan["checks"])
    assert any(check["id"] == "quality_gate" for check in plan["checks"])
    assert any(check["id"] == "release_trial" for check in plan["checks"])
    assert "demo-workspace-check" in markdown
    assert "release-trial" in markdown
    assert "pytest tests\\test_mcp_server.py" in markdown
    assert "docs/release_checklist.md" in markdown


def test_install_check_plan_lists_dependency_steps() -> None:
    plan = build_install_check_plan()
    markdown = install_check_plan_to_markdown(plan)

    ids = {check["id"] for check in plan["checks"]}
    assert {"python", "editable_install_web", "editable_install_mcp", "doctor"} <= ids
    assert "playwright install chromium" in markdown


def test_mcp_client_config_generates_local_python_server() -> None:
    payload = build_mcp_client_config(workspace_root=".agent-workspace", client="cursor", python="python", repo_root=".")
    markdown = mcp_client_config_to_markdown(payload)

    server = payload["config"]["mcpServers"]["visual-agent"]
    assert payload["target_filename"] == "cursor_mcp.json"
    assert server["command"] == "python"
    assert "visual_agent.mcp_server" in server["args"]
    assert "PYTHONPATH" in server["env"]
    assert "approved run_profile" in markdown


def test_mcp_client_config_generates_vscode_shape() -> None:
    payload = build_mcp_client_config(workspace_root=".agent-workspace", client="vscode", python="python", repo_root=".")

    assert payload["target_filename"] == ".vscode/mcp.json"
    assert payload["config"]["servers"]["visual-agent"]["command"] == "python"


def test_coding_agent_brief_targets_codex_claude_code_and_cursor(tmp_path) -> None:
    for client in ("codex", "claude-code", "cursor", "vscode"):
        brief = build_coding_agent_brief(
            workspace_root=".agent-workspace",
            client=client,
            python="python",
            repo_root=tmp_path,
        )
        markdown = coding_agent_brief_to_markdown(brief)

        assert brief["client"] == client
        assert brief["mcp"]["server_name"] == "visual-agent"
        assert "list_workflows" in {tool["name"] for tool in brief["tools"]}
        assert "get_workspace_dashboard" in {tool["name"] for tool in brief["tools"]}
        assert "get_latest_failure" in {tool["name"] for tool in brief["tools"]}
        assert any("dry-run" in rule for rule in brief["rules"])
        assert "Coding Agent Brief" in markdown
        assert "run_workflow" in markdown
        assert "Never request approved run_profile" in markdown


def test_coding_agent_brief_cli_renders_markdown(capsys) -> None:
    code = main(
        [
            "coding-agent-brief",
            "--client",
            "cursor",
            "--workspace-root",
            ".agent-workspace",
            "--python",
            "python",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "# Coding Agent Brief" in output
    assert "visual_agent.mcp_server" in output
    assert "mcp-smoke" in output


def test_demo_workspace_check_runs_local_demo(tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    result = run_demo_workspace_check(root=workspace_root, overwrite=True)
    markdown = demo_workspace_check_to_markdown(result)

    assert result["status"] == "success"
    assert result["workflow"] == "local_html_form_workflow"
    assert result["run_profile"] == "dry-run"
    assert result["run_id"]
    assert (workspace_root / "workflows" / "browser_form_workflow.yaml").exists()
    assert "Demo Workspace Check" in markdown


def test_demo_workspace_check_runs_supervised_browser_demo(tmp_path) -> None:
    result = run_demo_workspace_check(root=tmp_path / "workspace", overwrite=True, run_profile="supervised")

    assert result["status"] == "success"
    assert result["workflow"] == "browser_form_workflow"
    assert result["run_profile"] == "supervised"
    assert result["run_id"]
    assert result["failed_steps"] == []


def test_mcp_smoke_check_runs_tool_chain(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    result = run_mcp_smoke_check(workspace_root=workspace.root)
    markdown = mcp_smoke_check_to_markdown(result)

    assert result["status"] == "success"
    assert result["check_count"] == 5
    assert result["run_id"]
    assert "MCP Smoke Check" in markdown


def test_release_trial_runs_demo_mcp_and_cloud_bundle(tmp_path, monkeypatch) -> None:
    workspace_root = tmp_path / "workspace"
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_demo_workspace_check(**kwargs):
        calls.append(("demo", kwargs))
        assert kwargs["with_demo"] is True
        return {
            "schema_version": 1,
            "workspace_root": str(kwargs["root"]),
            "status": "success",
            "validation_ok": True,
            "workflow": "browser_form_workflow",
            "run_profile": kwargs["run_profile"],
            "run_id": "demo-run",
            "failed_steps": [],
            "report_index": str(Path(kwargs["root"]) / "reports" / "index.json"),
        }

    def fake_mcp_smoke_check(**kwargs):
        calls.append(("mcp", kwargs))
        return {
            "schema_version": 1,
            "workspace_root": str(kwargs["workspace_root"]),
            "workflow": "local_html_form_workflow",
            "inputs_file": "demo_login.json",
            "status": "success",
            "check_count": 5,
            "failed_count": 0,
            "run_id": "mcp-run",
            "checks": [],
        }

    class FakeServer:
        def __init__(self) -> None:
            self.server_port = 7891

        def serve_forever(self) -> None:
            calls.append(("serve", {}))

        def shutdown(self) -> None:
            calls.append(("shutdown", {}))

        def server_close(self) -> None:
            calls.append(("close", {}))

    def fake_create_cloud_server(**kwargs):
        calls.append(("server", kwargs))
        return FakeServer()

    def fake_build_http_cloud_transport(*, endpoint, api_key, org="", user_id="", timeout_seconds=30.0, max_retries=0, retry_backoff_seconds=0.0, opener=None):
        calls.append(("transport", {"endpoint": endpoint, "api_key": api_key, "org": org, "user_id": user_id}))
        return lambda request: {
            "schema_version": 1,
            "status": "success",
            "run_id": "cloud-run",
            "workflow_name": request.get("workflow_name") or "",
            "workflow_source": request.get("workflow_source") or "",
            "workflow_id": request.get("workflow_id") or "",
            "report_url": "/v1/run/cloud-run",
            "message": "Workflow completed.",
            "steps_total": 2,
            "steps_passed": 2,
        }

    def fake_execute_remote_workflow_plan(workflow_name, workspace_root, **kwargs):
        calls.append(("cloud", {"workflow_name": workflow_name, "workspace_root": str(workspace_root), **kwargs}))
        transport = kwargs.get("transport")
        result = transport(
            {
                "workflow_name": workflow_name,
                "workflow_source": "workspace",
                "workflow_id": "",
            }
        )
        return {
            "schema_version": 1,
            "workspace": str(workspace_root),
            "workflow_name": workflow_name,
            "execution_requested": True,
            "network_sent": True,
            "request": {"workflow_source": "workspace", "workflow_id": ""},
            "result": {**result, "usage_recorded": True},
        }

    appended: dict[str, object] = {}

    def fake_append_cloud_run_history(workspace_root, result):
        appended["workspace_root"] = str(workspace_root)
        appended["result"] = result
        return workspace_root / "run_history.jsonl"

    monkeypatch.setattr("visual_agent.quality.run_demo_workspace_check", fake_demo_workspace_check)
    monkeypatch.setattr("visual_agent.quality.run_mcp_smoke_check", fake_mcp_smoke_check)
    monkeypatch.setattr("visual_agent.cloud_server.create_cloud_server", fake_create_cloud_server)
    monkeypatch.setattr("visual_agent.cloud.build_http_cloud_transport", fake_build_http_cloud_transport)
    monkeypatch.setattr("visual_agent.cloud.execute_remote_workflow_plan", fake_execute_remote_workflow_plan)
    monkeypatch.setattr("visual_agent.visual_status.append_cloud_run_history", fake_append_cloud_run_history)

    result = run_release_trial(workspace_root=workspace_root, overwrite=True, run_profile="supervised")
    markdown = release_trial_to_markdown(result)

    assert result["status"] == "success"
    assert result["failed_count"] == 0
    assert result["checks"][0]["id"] == "demo_workspace_check"
    assert result["checks"][1]["id"] == "mcp_smoke"
    assert result["checks"][2]["id"] == "cloud_run"
    assert result["checks"][2]["workflow_source"] == "workspace"
    assert appended["workspace_root"] == str(workspace_root.resolve())
    assert appended["result"]["workflow_name"] == "browser_form_workflow"
    assert result["workspace_dashboard"]["health"]["status"] in {"ok", "attention"}
    assert result["run_history_report_path"].endswith("release_trial_report.html")
    assert result["release_trial_bundle"]["json"].endswith("release_trial_bundle.json")
    assert result["release_trial_bundle"]["markdown"].endswith("release_trial_bundle.md")
    assert Path(result["release_trial_bundle"]["json"]).exists()
    assert Path(result["release_trial_bundle"]["markdown"]).exists()
    assert "Release Trial" in markdown
    assert "Workspace Dashboard" in markdown
    assert "Run History Report" in markdown
    assert "Bundle" in markdown
    assert any(name == "server" for name, _ in calls)
    assert any(name == "transport" for name, _ in calls)


def test_release_trial_uses_existing_workspace_workflow_without_seed_demo(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False, overwrite=True)
    (workspace.fixtures_dir / "ready.html").write_text("<p>Ready</p>", encoding="utf-8")
    (workspace.workflows_dir / "ready.yaml").write_text(
        """
schema_version: 1
name: ready
version: 1
steps:
  - id: observe
    action: observe_html
    path: fixtures/ready.html
  - id: assert_ready
    action: assert_text
    text: Ready
""".strip(),
        encoding="utf-8",
    )
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_demo_workspace_check(**kwargs):
        calls.append(("demo", kwargs))
        assert kwargs["with_demo"] is False
        return {
            "schema_version": 1,
            "workspace_root": str(kwargs["root"]),
            "status": "success",
            "validation_ok": True,
            "workflow": kwargs["workflow_name"],
            "run_profile": kwargs["run_profile"],
            "run_id": "demo-run",
            "failed_steps": [],
            "report_index": str(Path(kwargs["root"]) / "reports" / "index.json"),
        }

    def fake_mcp_smoke_check(**kwargs):
        calls.append(("mcp", kwargs))
        return {
            "schema_version": 1,
            "workspace_root": str(kwargs["workspace_root"]),
            "workflow": kwargs["workflow"],
            "inputs_file": kwargs["inputs_file"],
            "status": "success",
            "check_count": 5,
            "failed_count": 0,
            "run_id": "mcp-run",
            "checks": [],
        }

    class FakeServer:
        def __init__(self) -> None:
            self.server_port = 7891

        def serve_forever(self) -> None:
            calls.append(("serve", {}))

        def shutdown(self) -> None:
            calls.append(("shutdown", {}))

        def server_close(self) -> None:
            calls.append(("close", {}))

    monkeypatch.setattr("visual_agent.quality.run_demo_workspace_check", fake_demo_workspace_check)
    monkeypatch.setattr("visual_agent.quality.run_mcp_smoke_check", fake_mcp_smoke_check)
    monkeypatch.setattr("visual_agent.cloud_server.create_cloud_server", lambda **kwargs: FakeServer())
    monkeypatch.setattr(
        "visual_agent.cloud.build_http_cloud_transport",
        lambda **kwargs: (lambda request: {"schema_version": 1, "status": "success", "run_id": "cloud-run", "workflow_name": request.get("workflow_name") or "", "workflow_source": request.get("workflow_source") or "", "report_url": "/v1/run/cloud-run", "message": "Workflow completed.", "steps_total": 2, "steps_passed": 2}),
    )
    monkeypatch.setattr(
        "visual_agent.cloud.execute_remote_workflow_plan",
        lambda workflow_name, workspace_root, **kwargs: {
            "schema_version": 1,
            "workspace": str(workspace_root),
            "workflow_name": workflow_name,
            "execution_requested": True,
            "network_sent": True,
            "request": {"workflow_source": "workspace", "workflow_id": ""},
            "result": {
                "schema_version": 1,
                "status": "success",
                "run_id": "cloud-run",
                "workflow_name": workflow_name,
                "workflow_source": "workspace",
                "report_url": "/v1/run/cloud-run",
                "message": "Workflow completed.",
                "steps_total": 2,
                "steps_passed": 2,
                "usage_recorded": True,
            },
        },
    )
    monkeypatch.setattr("visual_agent.visual_status.append_cloud_run_history", lambda *args, **kwargs: Path(args[0]) / "run_history.jsonl")

    result = run_release_trial(workspace_root=workspace.root, overwrite=False, run_profile="dry-run")

    assert result["status"] == "success"
    assert result["checks"][0]["workflow"] == "ready"
    assert result["checks"][1]["workflow"] == "ready"
    assert result["cloud_run"]["workflow_name"] == "ready"
    assert result["release_trial_bundle"]["json"].endswith("release_trial_bundle.json")
    assert any(name == "demo" for name, _ in calls)


def test_quality_gate_plan_includes_workspace_regression_when_present(tmp_path) -> None:
    regression_dir = tmp_path / "workspace" / "regression_tests"
    regression_dir.mkdir(parents=True)
    (regression_dir / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")

    steps = build_quality_gate_plan("ci", workspace_root=tmp_path / "workspace")
    names = [step.name for step in steps]

    assert "core_tests" in names
    assert "workflow_contracts" in names
    assert "workspace_regression_tests" in names


def test_quality_gate_dry_run_writes_no_report(tmp_path) -> None:
    result = run_quality_gate("local", execute=False, report_root=tmp_path)

    assert result.status == "planned"
    assert result.report_path is None
    assert not list(tmp_path.iterdir())
    assert result.risk_summary["risk_level"] == "ok"


def test_quality_gate_to_junit_xml_encodes_failures_and_skips() -> None:
    result = QualityGateResult(
        run_id="run-1",
        profile="ci",
        status="failed",
        report_path=None,
        markdown_path=None,
        steps=(
            QualityGateStep(
                name="core_tests",
                command=("python", "-m", "pytest"),
                required=True,
                status="failed",
                exit_code=1,
                elapsed_seconds=1.5,
                stdout="stdout api_key=secret",
                stderr="stderr token=secret",
            ),
            QualityGateStep(
                name="workflow_contracts",
                command=("python", "-m", "pytest", "tests/test_workflow_contracts.py"),
                required=True,
                status="planned",
            ),
        ),
        elapsed_seconds=2.0,
        risk_summary={
            "risk_level": "warning",
            "warning_count": 1,
            "strict_policy_gate": {"failed": True, "enabled": True, "risk_policy_error_count": 1, "secret_scan_finding_count": 0},
            "risk_policy_check": {"error_count": 1, "warning_count": 0},
            "secret_scan": {"finding_count": 0},
        },
    )

    xml = quality_gate_to_junit_xml(result)

    assert xml.startswith("<?xml")
    assert "testsuite" in xml
    assert "core_tests" in xml
    assert "workflow_contracts" in xml
    assert "QualityGateStepFailure" in xml
    assert "api_key=secret" not in xml
    assert "token=secret" not in xml


def test_quality_gate_markdown_includes_redacted_failed_step_output() -> None:
    result = QualityGateResult(
        run_id="run-1",
        profile="ci",
        status="failed",
        report_path=None,
        markdown_path=None,
        steps=(
            QualityGateStep(
                name="core_tests",
                command=("python", "-m", "pytest"),
                required=True,
                status="failed",
                exit_code=1,
                stdout="failed stdout api_key=secret",
                stderr="failed stderr token=secret",
            ),
        ),
        risk_summary={"risk_level": "ok", "warning_count": 0},
    )

    markdown = quality_gate_to_markdown(result)

    assert "#### stdout" in markdown
    assert "#### stderr" in markdown
    assert "failed stdout" in markdown
    assert "failed stderr" in markdown
    assert "api_key=secret" not in markdown
    assert "token=secret" not in markdown


def test_quality_gate_to_step_summary_includes_junit_path() -> None:
    result = QualityGateResult(
        run_id="run-1",
        profile="ci",
        status="failed",
        report_path=None,
        markdown_path=None,
        steps=(
            QualityGateStep(
                name="core_tests",
                command=("python", "-m", "pytest"),
                required=True,
                status="failed",
                exit_code=1,
                elapsed_seconds=1.5,
            ),
        ),
        elapsed_seconds=2.0,
        risk_summary={
            "risk_level": "warning",
            "warning_count": 1,
            "strict_policy_gate": {"failed": True, "enabled": True, "risk_policy_error_count": 1, "secret_scan_finding_count": 0},
        },
    )

    summary = quality_gate_to_step_summary(result, junit_output=".runs/quality_gates/junit.xml")

    assert "Checkpoint Quality Gate" in summary
    assert "JUnit: `.runs/quality_gates/junit.xml`" in summary
    assert "Strict Policy Gate" in summary


def test_quality_gate_risk_summary_includes_gui_action_history(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    summary = build_quality_gate_risk_summary(workspace_root=workspace.root)
    result = run_quality_gate("local", workspace_root=workspace.root, execute=False, report_root=tmp_path / "quality")
    markdown = quality_gate_to_markdown(result)

    assert summary["risk_level"] == "warning"
    assert summary["gui_action_history"]["failed_actions"][0]["action"] == "cancel_queue_task"
    assert summary["remediation_items"][0]["action"] == "cancel_queue_task"
    assert result.risk_summary["warning_count"] == 2
    assert "gui_action_error_rate" in markdown
    assert "cancel_queue_task" in markdown
    assert "### Remediation Checklist" in markdown
    assert "[1x] `cancel_queue_task` `RuntimeError`" in markdown


def test_quality_gate_risk_summary_uses_profile_thresholds(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality"] = {
        "gui_action_history": {
            "error_rate_threshold": 0.9,
            "failed_action_limit": 0,
            "profiles": {
                "ci": {
                    "error_rate_threshold": 0.1,
                    "failed_action_limit": 1,
                }
            },
        }
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    safe_execute_gui_action(workspace, build_gui_action_plan("refresh_readiness"))
    safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    local_summary = build_quality_gate_risk_summary(workspace_root=workspace.root, profile="local")
    ci_summary = build_quality_gate_risk_summary(workspace_root=workspace.root, profile="ci")

    assert local_summary["risk_level"] == "ok"
    assert local_summary["gui_action_history"]["policy"]["failed_action_limit"] == 0
    assert ci_summary["risk_level"] == "warning"
    assert ci_summary["profile"] == "ci"
    assert ci_summary["gui_action_history"]["policy"]["error_rate_threshold"] == 0.1
    assert [warning["code"] for warning in ci_summary["warnings"]] == [
        "gui_action_error_rate",
        "gui_action_failed_action",
    ]


def test_quality_gate_risk_summary_includes_risk_policy_check_errors(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality"] = {"gui_action_history": {"history_limit": 0}}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = build_quality_gate_risk_summary(workspace_root=workspace.root, profile="ci")
    result = run_quality_gate("ci", workspace_root=workspace.root, execute=False, report_root=tmp_path / "quality")
    markdown = quality_gate_to_markdown(result)

    assert summary["risk_level"] == "warning"
    assert summary["risk_policy_check"]["status"] == "error"
    assert summary["risk_policy_check"]["error_count"] == 1
    assert summary["warnings"][0]["code"] == "workspace_risk_policy_invalid"
    assert "### Risk Policy Check" in markdown
    assert "risk_policy_int_out_of_range" in markdown
    assert "quality.gui_action_history.history_limit" in markdown


def test_quality_gate_secret_scan_warns_without_leaking_plaintext(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    report_path = workspace.reports_dir / "leaky.json"
    report_path.write_text('{"password": "plain-secret-value"}', encoding="utf-8")

    scan = scan_workspace_secret_artifacts(workspace.root)
    summary = build_quality_gate_risk_summary(workspace_root=workspace.root, profile="ci")
    result = run_quality_gate("ci", workspace_root=workspace.root, execute=False, report_root=tmp_path / "quality")
    markdown = quality_gate_to_markdown(result)

    assert scan["finding_count"] == 1
    assert scan["findings"][0]["path"] == "reports/leaky.json"
    assert "plain-secret-value" not in json.dumps(scan, ensure_ascii=False)
    assert summary["risk_level"] == "warning"
    assert summary["warnings"][0]["code"] == "workspace_report_secret_leak"
    assert result.risk_summary["strict_policy_gate"]["failed"] is False
    assert "### Secret Scan" in markdown
    assert "plain-secret-value" not in markdown


def test_quality_gate_secret_scan_can_fail_strict_gate(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    (workspace.reports_dir / "leaky.md").write_text("token=plain-secret-token", encoding="utf-8")

    result = run_quality_gate(
        "ci",
        workspace_root=workspace.root,
        execute=False,
        report_root=tmp_path / "quality",
        fail_on_secret_leak=True,
    )
    status = quality_gate_status(
        [QualityGateStep(name="core_tests", command=("pytest",), status="success", exit_code=0)],
        risk_summary=result.risk_summary,
    )

    assert result.risk_summary["strict_policy_gate"]["enabled"] is True
    assert result.risk_summary["strict_policy_gate"]["failed"] is True
    assert result.risk_summary["strict_policy_gate"]["secret_scan_finding_count"] == 1
    assert status == "failed"


def test_quality_gate_policy_errors_are_warning_only_by_default(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality"] = {"gui_action_history": {"history_limit": 0}}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_quality_gate("ci", workspace_root=workspace.root, execute=False, report_root=tmp_path / "quality")
    status = quality_gate_status(
        [QualityGateStep(name="core_tests", command=("pytest",), status="success", exit_code=0)],
        risk_summary=result.risk_summary,
    )

    assert result.risk_summary["risk_policy_check"]["error_count"] == 1
    assert result.risk_summary["strict_policy_gate"]["enabled"] is False
    assert result.risk_summary["strict_policy_gate"]["failed"] is False
    assert result.risk_summary["strict_policy_gate"]["secret_scan_finding_count"] == 0
    assert status == "success"


def test_quality_gate_strict_policy_error_can_fail_status(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality"] = {"gui_action_history": {"history_limit": 0}}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_quality_gate(
        "ci",
        workspace_root=workspace.root,
        execute=False,
        report_root=tmp_path / "quality",
        fail_on_risk_policy_error=True,
    )
    status = quality_gate_status(
        [QualityGateStep(name="core_tests", command=("pytest",), status="success", exit_code=0)],
        risk_summary=result.risk_summary,
    )
    markdown = quality_gate_to_markdown(result)

    assert result.risk_summary["strict_policy_gate"]["enabled"] is True
    assert result.risk_summary["strict_policy_gate"]["failed"] is True
    assert result.risk_summary["strict_policy_gate"]["risk_policy_error_count"] == 1
    assert result.risk_summary["strict_policy_gate"]["secret_scan_finding_count"] == 0
    assert status == "failed"
    assert "### Strict Policy Gate" in markdown
    assert "- Failed: True" in markdown


def test_quality_gate_strict_policy_helper_handles_missing_policy_check() -> None:
    summary = apply_quality_gate_strict_policy({"risk_level": "ok"}, fail_on_risk_policy_error=True)

    assert summary["strict_policy_gate"]["enabled"] is True
    assert summary["strict_policy_gate"]["failed"] is False
    assert summary["strict_policy_gate"]["risk_policy_error_count"] == 0
    assert summary["strict_policy_gate"]["secret_scan_finding_count"] == 0


def test_quality_gate_index_summarizes_latest_status(tmp_path) -> None:
    write_quality_report(
        tmp_path / "older.json",
        run_id="older",
        profile="local",
        status="success",
        steps=[{"name": "core_tests", "status": "success"}],
        mtime=100,
    )
    write_quality_report(
        tmp_path / "newer.json",
        run_id="newer",
        profile="ci",
        status="failed",
        steps=[
            {"name": "core_tests", "status": "success"},
            {"name": "workflow_contracts", "status": "failed"},
        ],
        mtime=200,
    )

    index = build_quality_gate_index(report_root=tmp_path)

    assert index["total_reports"] == 2
    assert index["successful_reports"] == 1
    assert index["failed_reports"] == 1
    assert index["risk_warnings"] == 0
    assert index["risk_trends"]["unknown"] == 2
    assert index["risk_policy_errors"] == 0
    assert index["risk_policy_warnings"] == 0
    assert index["latest"]["run_id"] == "newer"
    assert index["latest"]["status"] == "failed"
    assert index["latest"]["failed_steps"] == ["workflow_contracts"]
    assert index["latest"]["risk_level"] == "ok"
    assert index["latest"]["risk_trend_direction"] == "unknown"


def test_quality_gate_index_exposes_risk_trend_summary(tmp_path) -> None:
    write_quality_report(
        tmp_path / "older.json",
        run_id="older",
        profile="local",
        status="success",
        risk_summary={
            "risk_level": "warning",
            "warning_count": 2,
            "remediation_items": [{"action": "cancel_queue_task"}],
            "gui_action_history": {
                "trend": {
                    "direction": "worsening",
                    "error_rate_delta": 0.5,
                    "remediation_count_delta": 1,
                    "window_size": 2,
                }
            },
        },
        mtime=100,
    )
    write_quality_report(
        tmp_path / "newer.json",
        run_id="newer",
        profile="ci",
        status="success",
        risk_summary={
            "risk_level": "ok",
            "warning_count": 0,
            "remediation_items": [],
            "gui_action_history": {
                "trend": {
                    "direction": "improving",
                    "error_rate_delta": -0.5,
                    "remediation_count_delta": -1,
                    "window_size": 2,
                }
            },
        },
        mtime=200,
    )

    index = build_quality_gate_index(report_root=tmp_path)

    assert index["risk_warnings"] == 2
    assert index["risk_trends"]["improving"] == 1
    assert index["risk_trends"]["worsening"] == 1
    assert index["latest"]["run_id"] == "newer"
    assert index["latest"]["risk_level"] == "ok"
    assert index["latest"]["risk_warning_count"] == 0
    assert index["latest"]["remediation_count"] == 0
    assert index["latest"]["risk_trend_direction"] == "improving"
    assert index["latest"]["risk_trend"]["error_rate_delta"] == -0.5


def test_quality_gate_index_summarizes_risk_policy_check(tmp_path) -> None:
    write_quality_report(
        tmp_path / "policy.json",
        run_id="policy",
        profile="ci",
        status="success",
        risk_summary={
            "risk_level": "warning",
            "warning_count": 1,
            "warnings": [{"code": "workspace_risk_policy_invalid"}],
            "risk_policy_check": {
                "status": "error",
                "error_count": 2,
                "warning_count": 1,
                "issues": [],
            },
        },
        mtime=100,
    )

    index = build_quality_gate_index(report_root=tmp_path)

    assert index["risk_policy_errors"] == 2
    assert index["risk_policy_warnings"] == 1
    assert index["latest"]["risk_policy_status"] == "error"
    assert index["latest"]["risk_policy_error_count"] == 2
    assert index["latest"]["risk_policy_warning_count"] == 1


def test_quality_gate_index_summarizes_strict_policy_gate(tmp_path) -> None:
    write_quality_report(
        tmp_path / "strict.json",
        run_id="strict",
        profile="ci",
        status="failed",
        risk_summary={
            "risk_level": "warning",
            "warning_count": 1,
            "strict_policy_gate": {
                "enabled": True,
                "failed": True,
                "risk_policy_error_count": 2,
            },
        },
        mtime=100,
    )

    index = build_quality_gate_index(report_root=tmp_path)

    assert index["strict_policy_gate_enabled"] == 1
    assert index["strict_policy_gate_failed"] == 1
    assert index["strict_policy_gate_policy_errors"] == 2
    assert index["latest"]["strict_policy_gate_enabled"] is True
    assert index["latest"]["strict_policy_gate_failed"] is True
    assert index["latest"]["strict_policy_gate_policy_errors"] == 2


def test_quality_gate_reports_filter_by_strict_policy_failed(tmp_path) -> None:
    write_quality_report(
        tmp_path / "strict.json",
        run_id="strict",
        profile="ci",
        status="failed",
        risk_summary={"strict_policy_gate": {"enabled": True, "failed": True, "risk_policy_error_count": 1}},
        mtime=200,
    )
    write_quality_report(
        tmp_path / "normal.json",
        run_id="normal",
        profile="ci",
        status="success",
        risk_summary={"strict_policy_gate": {"enabled": True, "failed": False, "risk_policy_error_count": 0}},
        mtime=100,
    )

    failed_reports = list_quality_gate_reports(report_root=tmp_path, strict_policy_failed=True)
    clean_reports = list_quality_gate_reports(report_root=tmp_path, strict_policy_failed=False)
    failed_index = build_quality_gate_index(report_root=tmp_path, strict_policy_failed=True)

    assert [report["run_id"] for report in failed_reports] == ["strict"]
    assert [report["run_id"] for report in clean_reports] == ["normal"]
    assert failed_index["filters"]["strict_policy_failed"] is True
    assert failed_index["total_reports"] == 1
    assert failed_index["strict_policy_gate_failed"] == 1
    assert failed_index["latest"]["run_id"] == "strict"


def test_quality_gate_index_markdown_renders_strict_policy_reports(tmp_path) -> None:
    write_quality_report(
        tmp_path / "strict.json",
        run_id="strict",
        profile="ci",
        status="failed",
        risk_summary={"strict_policy_gate": {"enabled": True, "failed": True, "risk_policy_error_count": 3}},
        mtime=100,
    )

    markdown = quality_gate_index_to_markdown(build_quality_gate_index(report_root=tmp_path, strict_policy_failed=True))

    assert markdown.startswith("# Quality Gate Index")
    assert "- strict_policy_failed: `True`" in markdown
    assert "Strict policy failures: 1" in markdown
    assert "| run_id | profile | status | strict_failed | policy_errors | json_report | markdown_report |" in markdown
    assert "| strict | ci | failed | True | 3 | strict.json |" in markdown


def test_quality_gate_reports_markdown_renders_empty_and_non_empty_reports(tmp_path) -> None:
    write_quality_report(
        tmp_path / "strict.json",
        run_id="strict",
        profile="ci",
        status="failed",
        risk_summary={"strict_policy_gate": {"enabled": True, "failed": True, "risk_policy_error_count": 1}},
        mtime=100,
    )

    reports = list_quality_gate_reports(report_root=tmp_path, strict_policy_failed=True)
    markdown = quality_gate_reports_to_markdown(reports)
    empty_markdown = quality_gate_reports_to_markdown(())

    assert markdown.startswith("# Quality Gate Reports")
    assert "- Strict policy failures: 1" in markdown
    assert "| strict | ci | failed | True | 1 | strict.json |" in markdown
    assert "No quality gate reports." in empty_markdown


def test_quality_gate_reports_filter_by_profile_and_status(tmp_path) -> None:
    write_quality_report(tmp_path / "local.json", run_id="local", profile="local", status="success", mtime=100)
    write_quality_report(tmp_path / "ci.json", run_id="ci", profile="ci", status="failed", mtime=200)
    write_quality_gate_index(report_root=tmp_path)

    reports = list_quality_gate_reports(report_root=tmp_path, profile="ci", status="failed")
    filtered_index = load_quality_gate_index(report_root=tmp_path, profile="ci")
    persisted_index = load_quality_gate_index(report_root=tmp_path)

    assert [report["run_id"] for report in reports] == ["ci"]
    assert filtered_index["total_reports"] == 1
    assert filtered_index["latest"]["profile"] == "ci"
    assert persisted_index["total_reports"] == 2


def write_quality_report(
    path,
    *,
    run_id: str,
    profile: str,
    status: str,
    steps: list[dict] | None = None,
    risk_summary: dict | None = None,
    mtime: int = 100,
) -> None:
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "profile": profile,
                "status": status,
                "elapsed_seconds": 1.25,
                "steps": steps or [{"name": "core_tests", "status": status}],
                "risk_summary": risk_summary or {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    utime(path, (mtime, mtime))

