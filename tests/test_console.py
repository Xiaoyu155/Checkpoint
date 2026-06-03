import json
from os import utime

from visual_agent.cli import main
from visual_agent.console import (
    build_report_detail,
    build_workspace_dashboard,
    dashboard_to_markdown,
    report_detail_to_markdown,
    risk_policy_check_to_markdown,
)
from visual_agent.scheduler import submit_queue_task
from visual_agent.workspace import init_workspace, run_workspace_workflow, tag_workspace_report


def test_workspace_dashboard_summarizes_empty_workspace(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    dashboard = build_workspace_dashboard(workspace)

    assert dashboard["health"]["status"] == "ok"
    assert dashboard["risk_policy_check"]["status"] == "warning"
    assert dashboard["risk_policy_check"]["warning_count"] == 1
    assert dashboard["workspace"]["workflow_count"] == 1
    assert dashboard["reports"]["total"] == 0
    assert dashboard["quality_gates"]["total"] == 0
    assert dashboard["queue"]["total"] == 0


def test_doctor_includes_vlm_summary_without_secret(monkeypatch, capsys) -> None:
    monkeypatch.setenv("VISUAL_AGENT_VLM_PROVIDER", "openai")
    monkeypatch.setenv("VISUAL_AGENT_VLM_API_KEY", "sk-test-secret-value-123456")
    monkeypatch.setenv("VISUAL_AGENT_VLM_BASE_URL", "https://api.openai.test/v1")
    monkeypatch.setenv("VISUAL_AGENT_VLM_MODEL", "gpt-4o")

    exit_code = main(["doctor"])
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["vlm"]["doctor_summary"]["recommended_engine"] == "cloud"
    assert payload["vlm"]["doctor_summary"]["cloud"]["api_key_configured"] is True
    assert payload["vlm"]["doctor_summary"]["cloud"]["base_url"] == "https://api.openai.test/v1"
    assert payload["perception"]["vlm"] is True
    assert "ready_for_dom_workflows" in payload["perception"]
    assert "sk-test-secret-value-123456" not in output


def test_doctor_recommendations_are_prioritized(monkeypatch, capsys) -> None:
    import visual_agent.capabilities as capabilities
    import visual_agent.cli as cli

    original = capabilities.module_available

    def fake_module_available(name):
        if name == "playwright":
            return False
        return original(name)

    monkeypatch.setattr(capabilities, "module_available", fake_module_available)
    monkeypatch.setattr(cli, "module_available", fake_module_available, raising=False)

    exit_code = main(["doctor"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["recommendations"]
    assert output["recommendations"][0]["priority"] == "P0"
    assert output["recommendations"][0]["name"] in {"observe_browser", "observe_dom", "playwright"}


def test_release_check_cli_outputs_markdown(capsys) -> None:
    exit_code = main(["release-check", "--workspace-root", ".agent-workspace", "--format", "markdown"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("# Release Check Plan")
    assert "install-check" in output
    assert "mcp-smoke" in output
    assert "quality-gate --profile ci" in output


def test_install_check_cli_outputs_markdown(capsys) -> None:
    exit_code = main(["install-check", "--format", "markdown"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("# Install Check Plan")
    assert "pip install -e .[web]" in output


def test_mcp_client_config_cli_outputs_json(capsys) -> None:
    exit_code = main(["mcp-client-config", "--workspace-root", ".agent-workspace", "--client", "cursor"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["client"] == "cursor"
    assert output["config"]["mcpServers"]["visual-agent"]["args"][1] == "visual_agent.mcp_server"


def test_demo_workspace_check_cli_outputs_markdown(tmp_path, capsys) -> None:
    exit_code = main(["demo-workspace-check", "--root", str(tmp_path / "workspace"), "--overwrite", "--format", "markdown"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("# Demo Workspace Check")
    assert "Status: `success`" in output


def test_mcp_smoke_cli_outputs_markdown(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    exit_code = main(["mcp-smoke", "--workspace-root", str(workspace.root), "--format", "markdown"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("# MCP Smoke Check")
    assert "run_workflow" in output


def test_queue_migration_cli_round_trip(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    main(["workspace-queue-submit", "--root", str(workspace.root), "--workflow", "local_html_form_workflow"])
    capsys.readouterr()

    migrate_code = main(["workspace-queue-migrate-sqlite", "--root", str(workspace.root), "--no-backup"])
    migrate_output = json.loads(capsys.readouterr().out)
    rollback_code = main(["workspace-queue-rollback-json", "--root", str(workspace.root), "--no-backup"])
    rollback_output = json.loads(capsys.readouterr().out)

    assert migrate_code == 0
    assert migrate_output["status"] == "migrated"
    assert migrate_output["task_count"] == 1
    assert rollback_code == 0
    assert rollback_output["status"] == "rolled_back"
    assert rollback_output["task_count"] == 1


def test_queue_worker_cli_runs_once(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    submit_queue_task(workspace, "local_html_form_workflow", inputs_file="demo_login.json")

    exit_code = main(["workspace-queue-worker", "--root", str(workspace.root), "--once", "--poll-seconds", "0"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["status"] == "once_completed"
    assert output["tasks_run"] == 1
    assert output["runs"][0]["task"]["status"] == "success"


def test_workspace_dashboard_flags_invalid_risk_policy(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality"] = {
        "gui_action_history": {
            "error_rate_threshold": 1.5,
            "health": {"attention_trend_directions": "worsening"},
        }
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    dashboard = build_workspace_dashboard(workspace)
    markdown = dashboard_to_markdown(dashboard)

    assert dashboard["health"]["status"] == "attention"
    assert "workspace_risk_policy_invalid" in dashboard["health"]["issues"]
    assert dashboard["risk_policy_check"]["error_count"] == 2
    assert "Risk policy check: `error` (2 errors, 0 warnings)" in markdown
    assert "## Risk Policy Check" in markdown
    assert "risk_policy_float_out_of_range" in markdown
    assert "quality.gui_action_history.error_rate_threshold" in markdown


def test_risk_policy_check_markdown_lists_issue_suggestions() -> None:
    markdown = risk_policy_check_to_markdown(
        {
            "status": "error",
            "error_count": 1,
            "warning_count": 0,
            "issues": [
                {
                    "level": "error",
                    "code": "risk_policy_float_out_of_range",
                    "path": "quality.gui_action_history.error_rate_threshold",
                    "message": "error_rate_threshold must be between 0 and 1.",
                    "suggestion": "Set error_rate_threshold within the supported range.",
                }
            ],
        }
    )

    assert markdown.startswith("## Risk Policy Check")
    assert "| level | code | path | message | suggestion |" in markdown
    assert "risk_policy_float_out_of_range" in markdown
    assert "Set error_rate_threshold within the supported range." in markdown


def test_workspace_dashboard_includes_queue_and_recent_runs(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    submit_queue_task(workspace, "local_html_form_workflow", inputs_file="demo_login.json", priority=5)
    result = run_workspace_workflow(
        workspace,
        "local_html_form_workflow",
        inputs={"username": "demo_user", "password": "secret"},
        dry_run=True,
    )

    dashboard = build_workspace_dashboard(workspace)

    assert dashboard["queue"]["pending"] == 1
    assert dashboard["queue"]["recent"][0]["priority"] == 5
    assert dashboard["runs"]["recent"][0]["run_id"] == result.run_id
    assert dashboard["reports"]["recent"][0]["run_id"] == result.run_id


def test_workspace_dashboard_flags_failed_quality_gate(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    quality_root = workspace.reports_dir / "quality_gates"
    quality_root.mkdir(parents=True)
    report_path = quality_root / "failed.json"
    report_path.write_text(
        json.dumps(
            {
                "run_id": "failed",
                "profile": "ci",
                "status": "failed",
                "elapsed_seconds": 1.0,
                "steps": [{"name": "core_tests", "status": "failed"}],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    utime(report_path, (200, 200))

    dashboard = build_workspace_dashboard(workspace)

    assert dashboard["health"]["status"] == "attention"
    assert "failed_quality_gate" in dashboard["health"]["issues"]
    assert dashboard["quality_gates"]["latest"]["run_id"] == "failed"
    assert dashboard["quality_gates"]["latest_risk_trend_direction"] == "unknown"


def test_workspace_dashboard_includes_quality_gate_risk_trend(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    quality_root = workspace.reports_dir / "quality_gates"
    quality_root.mkdir(parents=True)
    report_path = quality_root / "risk-trend.json"
    report_path.write_text(
        json.dumps(
            {
                "run_id": "risk-trend",
                "profile": "ci",
                "status": "success",
                "elapsed_seconds": 1.0,
                "steps": [{"name": "core_tests", "status": "success"}],
                "risk_summary": {
                    "risk_level": "warning",
                    "warning_count": 2,
                    "remediation_items": [{"action": "cancel_queue_task"}],
                    "gui_action_history": {
                        "trend": {
                            "direction": "improving",
                            "error_rate_delta": -0.5,
                            "remediation_count_delta": -1,
                            "window_size": 2,
                        }
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    utime(report_path, (200, 200))

    dashboard = build_workspace_dashboard(workspace)
    markdown = dashboard_to_markdown(dashboard)

    assert dashboard["quality_gates"]["risk_warnings"] == 2
    assert dashboard["quality_gates"]["risk_trends"]["improving"] == 1
    assert dashboard["quality_gates"]["latest_risk_trend_direction"] == "improving"
    assert "Quality risk warnings: 2" in markdown
    assert "Latest quality risk trend: `improving`" in markdown


def test_workspace_dashboard_includes_strict_policy_gate_summary(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    quality_root = workspace.reports_dir / "quality_gates"
    quality_root.mkdir(parents=True)
    report_path = quality_root / "strict-policy.json"
    report_path.write_text(
        json.dumps(
            {
                "run_id": "strict-policy",
                "profile": "ci",
                "status": "failed",
                "elapsed_seconds": 1.0,
                "steps": [{"name": "core_tests", "status": "success"}],
                "risk_summary": {
                    "risk_level": "warning",
                    "warning_count": 1,
                    "strict_policy_gate": {
                        "enabled": True,
                        "failed": True,
                        "risk_policy_error_count": 2,
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    utime(report_path, (200, 200))

    dashboard = build_workspace_dashboard(workspace)
    markdown = dashboard_to_markdown(dashboard)

    assert dashboard["health"]["status"] == "attention"
    assert "strict_policy_gate_failed" in dashboard["health"]["issues"]
    assert dashboard["quality_gates"]["strict_policy_gate_enabled"] == 1
    assert dashboard["quality_gates"]["strict_policy_gate_failed"] == 1
    assert dashboard["quality_gates"]["strict_policy_gate_policy_errors"] == 2
    assert dashboard["quality_gates"]["latest_strict_policy_gate_failed"] is True
    assert "Strict policy gate failures: 1" in markdown
    assert "Latest strict policy gate failed: True" in markdown
    assert "Strict policy gate: enabled=True, failed=True, policy_errors=2" in markdown


def test_workspace_dashboard_includes_strict_policy_failed_history(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    quality_root = workspace.reports_dir / "quality_gates"
    quality_root.mkdir(parents=True)
    write_quality_strict_policy_report(quality_root, run_id="strict", failed=True, mtime=200)
    write_quality_strict_policy_report(quality_root, run_id="clean", failed=False, mtime=100)

    dashboard = build_workspace_dashboard(workspace)
    quality = dashboard["quality_gates"]
    markdown = dashboard_to_markdown(dashboard)

    assert [report["run_id"] for report in quality["strict_policy_failed_reports"]] == ["strict"]
    assert "- strict_policy_failed: `True`" in quality["strict_policy_failed_markdown"]
    assert "| strict | ci | failed | True | 1 | strict.json |" in quality["strict_policy_failed_markdown"]
    assert "## Strict Policy Failure History" in markdown
    assert "| strict | ci | failed | 1 | strict.json |" in markdown


def test_workspace_dashboard_flags_worsening_quality_risk_trend(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    write_quality_trend_report(workspace, direction="worsening")

    dashboard = build_workspace_dashboard(workspace)
    markdown = dashboard_to_markdown(dashboard)

    assert dashboard["health"]["status"] == "attention"
    assert "gui_action_risk_worsening" in dashboard["health"]["issues"]
    assert dashboard["quality_gates"]["risk_health_policy"]["attention_trend_directions"] == ["worsening"]
    assert "gui_action_risk_worsening" in markdown
    assert "Risk health attention trends: `worsening`" in markdown


def test_workspace_dashboard_does_not_flag_improving_quality_risk_trend(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    write_quality_trend_report(workspace, direction="improving")

    dashboard = build_workspace_dashboard(workspace)

    assert dashboard["health"]["status"] == "ok"
    assert "gui_action_risk_worsening" not in dashboard["health"]["issues"]


def test_workspace_dashboard_default_policy_ignores_mixed_quality_risk_trend(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    write_quality_trend_report(workspace, direction="mixed")

    dashboard = build_workspace_dashboard(workspace)

    assert dashboard["health"]["status"] == "ok"
    assert "gui_action_risk_mixed" not in dashboard["health"]["issues"]


def test_workspace_dashboard_uses_configured_attention_risk_trends(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    write_workspace_gui_action_risk_health_config(workspace, directions=["mixed", "worsening"])
    write_quality_trend_report(workspace, direction="mixed")

    dashboard = build_workspace_dashboard(workspace)
    markdown = dashboard_to_markdown(dashboard)

    assert dashboard["health"]["status"] == "attention"
    assert "gui_action_risk_mixed" in dashboard["health"]["issues"]
    assert dashboard["quality_gates"]["risk_health_policy"]["attention_trend_directions"] == ["mixed", "worsening"]
    assert "Risk health attention trends: `mixed`, `worsening`" in markdown


def test_workspace_dashboard_markdown_is_human_readable(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    submit_queue_task(workspace, "local_html_form_workflow")

    markdown = dashboard_to_markdown(build_workspace_dashboard(workspace))

    assert "# Workspace Dashboard" in markdown
    assert "Queue:" in markdown
    assert "local_html_form_workflow" in markdown


def test_report_detail_summarizes_successful_run(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    result = run_workspace_workflow(
        workspace,
        "local_html_form_workflow",
        inputs={"username": "demo_user", "password": "secret"},
        dry_run=True,
    )

    detail = build_report_detail(workspace, result.run_id)

    assert detail["run_id"] == result.run_id
    assert detail["workflow_name"] == "local_html_form_workflow"
    assert detail["status"] == "success"
    assert detail["summary"]["total_steps"] == 6
    assert detail["summary"]["failed_step"] is None
    assert detail["paths"]["json_report"] == f"reports/{result.run_id}.json"
    assert detail["paths"]["markdown_report"] == f"reports/{result.run_id}.md"
    assert detail["artifacts"]["workflow_result"]
    assert detail["steps"][-1]["id"] == "click_login"
    assert detail["steps"][-1]["has_failure_diagnosis"] is False


def test_report_detail_includes_annotation_and_failure_diagnosis(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    write_failing_workflow(workspace)
    result = run_workspace_workflow(workspace, "failing_report", dry_run=True, preflight=False)
    tag_workspace_report(
        workspace,
        result.run_id,
        review_status="needs_fix",
        tags=("assertion",),
        note="需要调整断言文本",
    )

    detail = build_report_detail(workspace, result.run_id)

    assert detail["status"] == "failed"
    assert detail["summary"]["failed_step"] == "assert_missing"
    assert detail["annotation"]["review_status"] == "needs_fix"
    assert detail["failure"]["failed_step"] == "assert_missing"
    assert detail["failure"]["expected"] == "expected text: 不存在的文本"
    assert detail["failure"]["recovery_suggestions"]
    assert detail["steps"][-1]["has_failure_diagnosis"] is True


def test_report_detail_markdown_is_human_readable(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    write_failing_workflow(workspace)
    result = run_workspace_workflow(workspace, "failing_report", dry_run=True, preflight=False)

    markdown = report_detail_to_markdown(build_report_detail(workspace, result.run_id))

    assert "# Report Detail: failing_report" in markdown
    assert "## Failure Diagnosis" in markdown
    assert "`assert_missing`" in markdown
    assert "## Artifacts" in markdown


def test_report_detail_cli_outputs_markdown(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    result = run_workspace_workflow(
        workspace,
        "local_html_form_workflow",
        inputs={"username": "demo_user", "password": "secret"},
        dry_run=True,
    )

    exit_code = main(
        [
            "workspace-report-detail",
            "--root",
            str(workspace.root),
            "--run-id",
            result.run_id,
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "# Report Detail: local_html_form_workflow" in output
    assert result.run_id in output


def test_workspace_risk_policy_template_cli_outputs_json(capsys) -> None:
    exit_code = main(["workspace-risk-policy-template"])
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    config = payload["quality"]["gui_action_history"]
    assert config["profiles"]["planner"]["history_limit"] == 50
    assert config["health"]["attention_trend_directions"] == ["worsening"]


def test_workspace_risk_policy_check_cli_reports_invalid_policy(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality"] = {
        "gui_action_history": {
            "error_rate_threshold": -0.1,
            "health": {"attention_trend_directions": "worsening"},
        }
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    exit_code = main(["workspace-risk-policy-check", "--root", str(workspace.root)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ok"] is False
    assert payload["error_count"] == 2
    assert {issue["code"] for issue in payload["issues"]} == {
        "risk_policy_float_out_of_range",
        "risk_policy_attention_trends_not_list",
    }


def test_workspace_risk_policy_plan_cli_outputs_patch_preview(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    exit_code = main(["workspace-risk-policy-plan", "--root", str(workspace.root)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["applied"] is False
    assert payload["changed"] is True
    assert "quality.gui_action_history" in payload["changed_paths"]
    assert payload["patch"]["quality"]["gui_action_history"]["health"]["attention_trend_directions"] == ["worsening"]
    assert payload["validation_after"]["status"] == "ok"


def test_quality_gate_index_cli_filters_strict_policy_failed(tmp_path, capsys) -> None:
    write_quality_strict_policy_report(tmp_path, run_id="strict", failed=True, mtime=200)
    write_quality_strict_policy_report(tmp_path, run_id="clean", failed=False, mtime=100)

    exit_code = main(
        [
            "quality-gate-index",
            "--report-root",
            str(tmp_path),
            "--strict-policy-failed",
            "true",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["filters"]["strict_policy_failed"] is True
    assert payload["total_reports"] == 1
    assert payload["latest"]["run_id"] == "strict"


def test_quality_gate_reports_cli_filters_strict_policy_failed_false(tmp_path, capsys) -> None:
    write_quality_strict_policy_report(tmp_path, run_id="strict", failed=True, mtime=200)
    write_quality_strict_policy_report(tmp_path, run_id="clean", failed=False, mtime=100)

    exit_code = main(
        [
            "quality-gate-reports",
            "--report-root",
            str(tmp_path),
            "--strict-policy-failed",
            "false",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert [report["run_id"] for report in payload] == ["clean"]


def test_quality_gate_index_cli_outputs_markdown(tmp_path, capsys) -> None:
    write_quality_strict_policy_report(tmp_path, run_id="strict", failed=True, mtime=200)

    exit_code = main(
        [
            "quality-gate-index",
            "--report-root",
            str(tmp_path),
            "--strict-policy-failed",
            "true",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("# Quality Gate Index")
    assert "- strict_policy_failed: `True`" in output
    assert "| strict | ci | failed | True | 1 | strict.json |" in output


def test_quality_gate_reports_cli_outputs_markdown(tmp_path, capsys) -> None:
    write_quality_strict_policy_report(tmp_path, run_id="strict", failed=True, mtime=200)

    exit_code = main(
        [
            "quality-gate-reports",
            "--report-root",
            str(tmp_path),
            "--strict-policy-failed",
            "true",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("# Quality Gate Reports")
    assert "- Strict policy failures: 1" in output
    assert "| strict | ci | failed | True | 1 | strict.json |" in output


def write_failing_workflow(workspace) -> None:
    (workspace.workflows_dir / "failing_report.yaml").write_text(
        """
schema_version: 1
min_runtime_version: "0.1.0"
name: failing_report
version: 1
steps:
  - id: observe_html
    action: observe_html
    path: fixtures/login_demo.html
  - id: assert_missing
    action: assert_text
    text: 不存在的文本
""".strip(),
        encoding="utf-8",
    )


def write_quality_trend_report(workspace, *, direction: str) -> None:
    quality_root = workspace.reports_dir / "quality_gates"
    quality_root.mkdir(parents=True)
    report_path = quality_root / f"{direction}.json"
    report_path.write_text(
        json.dumps(
            {
                "run_id": direction,
                "profile": "ci",
                "status": "success",
                "elapsed_seconds": 1.0,
                "steps": [{"name": "core_tests", "status": "success"}],
                "risk_summary": {
                    "risk_level": "warning" if direction == "worsening" else "ok",
                    "warning_count": 1 if direction == "worsening" else 0,
                    "remediation_items": [],
                    "gui_action_history": {
                        "trend": {
                            "direction": direction,
                            "error_rate_delta": 0.5 if direction == "worsening" else -0.5,
                            "remediation_count_delta": 1 if direction == "worsening" else -1,
                            "window_size": 2,
                        }
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    utime(report_path, (200, 200))


def write_quality_strict_policy_report(root, *, run_id: str, failed: bool, mtime: int) -> None:
    report_path = root / f"{run_id}.json"
    report_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "profile": "ci",
                "status": "failed" if failed else "success",
                "elapsed_seconds": 1.0,
                "steps": [{"name": "core_tests", "status": "success"}],
                "risk_summary": {
                    "strict_policy_gate": {
                        "enabled": True,
                        "failed": failed,
                        "risk_policy_error_count": 1 if failed else 0,
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    utime(report_path, (mtime, mtime))


def write_workspace_gui_action_risk_health_config(workspace, *, directions: list[str]) -> None:
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality"] = {
        "gui_action_history": {
            "health": {
                "attention_trend_directions": directions,
            },
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
