from __future__ import annotations

from .helpers import json_output, run_cli


def test_doctor_reports_dom_workflows_ready() -> None:
    result = run_cli("doctor")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json_output(result)

    perception = payload.get("perception") or {}
    assert perception.get("dom_browser") is True, perception
    assert perception.get("ready_for_dom_workflows") is True, perception


def test_doctor_perception_warnings_are_actionable() -> None:
    result = run_cli("doctor")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json_output(result)

    warnings = payload.get("perception", {}).get("warnings") or []
    action_words = ("install", "set", "configure", "run", "add", "pip")
    for warning in warnings:
        assert any(word in str(warning).lower() for word in action_words), warning


def test_demo_workspace_check_and_dashboard_load(e2e_workspace) -> None:
    demo = run_cli("demo-workspace-check", "--root", str(e2e_workspace), "--overwrite")
    assert demo.returncode == 0, demo.stdout + demo.stderr
    demo_payload = json_output(demo)
    assert demo_payload["status"] == "success"
    assert demo_payload["validation_ok"] is True
    assert demo_payload["run_id"]

    dashboard = run_cli("workspace-dashboard", "--root", str(e2e_workspace), "--format", "markdown")
    assert dashboard.returncode == 0, dashboard.stdout + dashboard.stderr
    assert "Workspace Dashboard" in dashboard.stdout
    assert "Workflows" in dashboard.stdout
