from __future__ import annotations

import json
from os import utime

import pytest

from visual_agent.cli import build_parser, main
from visual_agent.gui import (
    build_console_window_model,
    build_gui_action_plan,
    batch_report_detail_markdown,
    build_gui_action_history_index,
    build_gui_action_history_report,
    build_gui_action_history_risk_summary,
    build_gui_error_detail,
    console_model_detail_markdown,
    console_window_button_states,
    console_window_selection_state,
    execute_gui_action,
    gui_action_audit_path,
    gui_action_event_markdown,
    gui_action_history_index_to_markdown,
    gui_action_history_report_to_markdown,
    gui_action_feedback,
    gui_action_risk_event_options,
    gui_action_history_remediation_items,
    gui_action_history_risk_policy,
    gui_action_history_risk_to_markdown,
    gui_action_history_risk_trend,
    gui_error_detail_to_markdown,
    is_long_running_gui_action,
    list_gui_action_events,
    readiness_to_markdown,
    recording_result_to_markdown,
    report_option_label,
    risk_policy_plan_to_markdown,
    run_gui_action_async,
    safe_execute_gui_action,
)
from visual_agent.recorder import BrowserRecordingError, archive_recording_failure, save_recorded_workflow
from visual_agent.scheduler import list_queue_tasks, submit_queue_task
from visual_agent.workspace import init_workspace, run_workspace_workflow


def test_console_window_model_handles_empty_workspace(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    model = build_console_window_model(workspace)

    assert model["title"].endswith("agent-workspace")
    assert model["dashboard"]["health"]["status"] == "ok"
    assert model["report_options"] == []
    assert model["selected_run_id"] is None
    assert model["selected_report"] is None
    assert model["selected_report_markdown"] == ""
    assert [card["id"] for card in model["summary_cards"]] == [
        "health",
        "workflows",
        "reports",
        "quality",
        "risk_policy",
        "auto_repair",
        "queue",
        "external_samples",
        "gui_action_risk",
    ]
    assert {option["name"] for option in model["workflow_options"]} == {
        "browser_form_workflow",
        "checkout_verification",
        "local_html_form_workflow",
        "pacer_gateway_billing_acceptance",
        "pacer_workbench_static_acceptance",
    }
    assert [button["id"] for button in model["action_buttons"]] == [
        "run_workflow",
        "queue_run_next",
        "cancel_queue_task",
        "retry_queue_task",
        "open_artifact",
        "inspect_auth_state",
        "refresh_readiness",
        "plan_external_sample_run",
        "submit_external_sample_batch",
        "external_sample_summary",
        "plan_risk_policy_patch",
        "apply_risk_policy_patch",
        "preview_planner_draft_save",
        "generate_planner_draft_preview",
        "save_generated_planner_draft",
        "record_browser_workflow",
        "delete_auth_state",
        "read_input_template",
        "save_input_template",
        "install_check",
        "release_check",
        "demo_workspace_check",
        "mcp_smoke_check",
        "show_strict_policy_failures",
        "external_sample_batch_report",
        "plan_external_sample_batch_reruns",
        "submit_external_sample_batch_reruns",
        "plan_external_sample_reruns",
        "submit_external_sample_reruns",
    ]
    assert model["external_sample_readiness"]["ready_samples"] == 1
    assert model["external_sample_readiness"]["blocked_samples"] == 3
    assert model["external_sample_readiness"]["missing_storage_state_files"] == 3
    assert model["readiness_options"][0]["label"].startswith("BLOCKED")
    assert "missing_storage_state_file" in model["readiness_markdown"]
    assert model["gui_action_history_risk"]["risk_level"] == "ok"
    risk_policy_card = next(card for card in model["summary_cards"] if card["id"] == "risk_policy")
    assert risk_policy_card["value"] == "warning"
    assert risk_policy_card["detail"] == "0 errors, 2 warnings"
    auto_repair_card = next(card for card in model["summary_cards"] if card["id"] == "auto_repair")
    assert auto_repair_card["value"] == "medium"
    assert "min 0.75" in auto_repair_card["detail"]
    assert model["summary_cards"][-1]["id"] == "gui_action_risk"
    assert model["summary_cards"][-1]["value"] == "ok"
    assert [column["id"] for column in model["primary_columns"]] == ["workflows", "runs", "queue"]
    assert model["primary_columns"][0]["option_count"] == 5
    assert model["primary_columns"][1]["option_count"] == 0
    assert model["primary_columns"][2]["empty_state"] == "No queue tasks."
    assert "run_workflow" in model["primary_columns"][0]["primary_actions"]


def test_gui_release_and_install_check_actions_return_markdown(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    install = execute_gui_action(workspace, build_gui_action_plan("install_check"))
    release = execute_gui_action(workspace, build_gui_action_plan("release_check"))

    assert install["status"] == "success"
    assert gui_action_feedback(install).startswith("# Install Check Plan")
    assert release["status"] == "success"
    assert "mcp-smoke" in gui_action_feedback(release)


def test_gui_mcp_smoke_action_runs_local_demo(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = execute_gui_action(workspace, build_gui_action_plan("mcp_smoke_check"))
    feedback = gui_action_feedback(result)

    assert result["status"] == "success"
    assert result["mcp_smoke"]["run_id"]
    assert feedback.startswith("# MCP Smoke Check")


def test_console_window_model_selects_latest_report(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    result = run_workspace_workflow(
        workspace,
        "local_html_form_workflow",
        inputs={"username": "demo_user", "password": "secret"},
        dry_run=True,
    )

    model = build_console_window_model(workspace)

    assert model["selected_run_id"] == result.run_id
    assert model["selected_report"]["workflow_name"] == "local_html_form_workflow"
    assert model["report_options"][0]["run_id"] == result.run_id
    assert "Report Detail: local_html_form_workflow" in model["selected_report_markdown"]
    assert model["summary_cards"][2]["id"] == "reports"
    assert model["summary_cards"][2]["value"] == "1"
    assert model["primary_columns"][1]["option_count"] == 1
    assert model["primary_columns"][1]["options"][0]["run_id"] == result.run_id
    assert any(option["path"].endswith(f"reports/{result.run_id}.json") for option in model["artifact_options"])


def test_console_window_model_quality_card_shows_risk_trend(tmp_path) -> None:
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
                            "direction": "worsening",
                            "error_rate_delta": 0.5,
                            "remediation_count_delta": 1,
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

    model = build_console_window_model(workspace)
    quality_card = next(card for card in model["summary_cards"] if card["id"] == "quality")

    assert model["dashboard"]["quality_gates"]["latest_risk_trend_direction"] == "worsening"
    assert quality_card["detail"] == "0 failed, risk worsening, 2 warnings, strict 0 failed"


def test_console_window_model_quality_card_shows_strict_policy_gate(tmp_path) -> None:
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
                    "strict_policy_gate": {
                        "enabled": True,
                        "failed": True,
                        "risk_policy_error_count": 1,
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    utime(report_path, (200, 200))

    model = build_console_window_model(workspace)
    quality_card = next(card for card in model["summary_cards"] if card["id"] == "quality")

    assert model["dashboard"]["quality_gates"]["strict_policy_gate_failed"] == 1
    assert model["dashboard"]["quality_gates"]["latest_strict_policy_gate_failed"] is True
    assert "strict_policy_gate_failed" in model["dashboard"]["health"]["issues"]
    assert quality_card["detail"] == "1 failed, risk unknown, 0 warnings, strict 1 failed"
    assert "| strict-policy | ci | failed | True | 1 | strict-policy.json |" in model["strict_policy_failed_markdown"]


def test_console_window_model_exposes_strict_policy_failure_detail(tmp_path) -> None:
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

    model = build_console_window_model(workspace)
    states = console_window_button_states(model)

    assert states["show_strict_policy_failures"]["enabled"] is True
    assert model["dashboard"]["quality_gates"]["strict_policy_failed_reports"][0]["run_id"] == "strict-policy"
    assert model["strict_policy_failed_markdown"].startswith("# Quality Gate Index")
    assert "- strict_policy_failed: `True`" in model["strict_policy_failed_markdown"]
    assert "| strict-policy | ci | failed | True | 2 | strict-policy.json |" in model["strict_policy_failed_markdown"]


def test_console_window_model_risk_policy_card_shows_invalid_policy(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality"] = {"gui_action_history": {"history_limit": 0}}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    model = build_console_window_model(workspace)
    risk_policy_card = next(card for card in model["summary_cards"] if card["id"] == "risk_policy")

    assert model["dashboard"]["health"]["status"] == "attention"
    assert "workspace_risk_policy_invalid" in model["dashboard"]["health"]["issues"]
    assert "## Risk Policy Check" in model["risk_policy_check_markdown"]
    assert "quality.gui_action_history.history_limit" in model["risk_policy_check_markdown"]
    assert risk_policy_card["value"] == "error"
    assert risk_policy_card["detail"] == "1 errors, 1 warnings"


def test_console_model_detail_markdown_can_show_risk_policy_check(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality"] = {"gui_action_history": {"history_limit": 0}}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    model = build_console_window_model(workspace)
    detail = console_model_detail_markdown(model)

    assert detail.startswith("## Risk Policy Check")
    assert "risk_policy_int_out_of_range" in detail


def test_console_window_model_can_select_specific_report(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    first = run_workspace_workflow(
        workspace,
        "local_html_form_workflow",
        inputs={"username": "first", "password": "secret"},
        dry_run=True,
    )
    second = run_workspace_workflow(
        workspace,
        "local_html_form_workflow",
        inputs={"username": "second", "password": "secret"},
        dry_run=True,
    )

    model = build_console_window_model(workspace, selected_run_id=first.run_id)

    assert second.run_id != first.run_id
    assert model["selected_run_id"] == first.run_id
    assert model["selected_report"]["run_id"] == first.run_id
    assert [option["run_id"] for option in model["report_options"]][:2] == [second.run_id, first.run_id]


def test_console_window_selection_state_retains_selected_report(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    first = run_workspace_workflow(
        workspace,
        "local_html_form_workflow",
        inputs={"username": "first", "password": "secret"},
        dry_run=True,
    )
    run_workspace_workflow(
        workspace,
        "local_html_form_workflow",
        inputs={"username": "second", "password": "secret"},
        dry_run=True,
    )

    model = build_console_window_model(workspace, selected_run_id=first.run_id)
    state = console_window_selection_state(model)

    assert state["report"]["by_label"][state["report"]["selected_label"]]["run_id"] == first.run_id
    assert len(state["report"]["labels"]) == 2
    assert console_model_detail_markdown(model).startswith("# Report Detail: local_html_form_workflow")


def test_console_window_button_states_disable_missing_selections(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    model = build_console_window_model(workspace)
    states = console_window_button_states(model)

    assert states["run_workflow"]["enabled"] is True
    assert states["queue_run_next"]["enabled"] is False
    assert states["cancel_queue_task"]["enabled"] is False
    assert states["retry_queue_task"]["enabled"] is False
    assert states["open_artifact"]["enabled"] is False
    assert states["inspect_auth_state"]["enabled"] is bool(model["auth_state_options"])
    assert states["submit_external_sample_batch"]["enabled"] is True
    assert states["open_batch_report"]["enabled"] is False
    assert states["show_strict_policy_failures"]["enabled"] is False
    assert states["submit_external_sample_reruns"]["enabled"] is False


def test_console_window_button_states_follow_selected_queue_task_status(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow", inputs_file="demo_login.json")

    pending_model = build_console_window_model(workspace)
    pending_states = console_window_button_states(pending_model)

    assert pending_states["queue_run_next"]["enabled"] is True
    assert pending_states["cancel_queue_task"]["enabled"] is True
    assert pending_states["retry_queue_task"]["enabled"] is False

    canceled = execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    canceled_states = console_window_button_states(canceled["refreshed_model"])

    assert canceled_states["queue_run_next"]["enabled"] is False
    assert canceled_states["cancel_queue_task"]["enabled"] is False
    assert canceled_states["retry_queue_task"]["enabled"] is True


def test_report_option_label_is_compact() -> None:
    label = report_option_label(
        {
            "run_id": "20260524-123456-abcdef",
            "workflow_name": "order_entry",
            "status": "failed",
        }
    )

    assert label == "FAILED  order_entry  20260524-123456-abc"


def test_gui_action_plan_defaults_workflow_run_to_dry_run() -> None:
    plan = build_gui_action_plan("run_workflow", workflow="local_html_form_workflow")

    assert plan["action"] == "run_workflow"
    assert plan["workflow"] == "local_html_form_workflow"
    assert plan["dry_run"] is True
    assert plan["run_profile"] == "dry-run"
    assert plan["requires_confirmation"] is False


def test_gui_action_plan_supports_risk_policy_patch_overwrite() -> None:
    plan = build_gui_action_plan("plan_risk_policy_patch", overwrite=True)

    assert plan["action"] == "plan_risk_policy_patch"
    assert plan["overwrite"] is True
    assert plan["dry_run"] is True


def test_gui_action_plan_marks_real_click_for_confirmation() -> None:
    plan = build_gui_action_plan("run_workflow", workflow="local_html_form_workflow", allow_click=True)

    assert plan["dry_run"] is False
    assert plan["run_profile"] == "approved"
    assert plan["requires_confirmation"] is True


def test_gui_action_plan_requires_record_browser_url_and_save_name() -> None:
    with pytest.raises(ValueError):
        build_gui_action_plan("record_browser_workflow", save_as="recorded/login")

    plan = build_gui_action_plan(
        "record_browser_workflow",
        url="https://example.test/login",
        save_as="recorded/login",
        assert_text="Dashboard",
        save_auth_state="seller",
        preview_run=True,
    )

    assert plan["url"] == "https://example.test/login"
    assert plan["save_as"] == "recorded/login"
    assert plan["assert_text"] == "Dashboard"
    assert plan["save_auth_state"] == "seller"
    assert plan["preview_run"] is True


def test_execute_gui_action_runs_workflow_as_dry_run(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    plan = build_gui_action_plan("run_workflow", workflow="local_html_form_workflow", inputs_file="demo_login.json")

    result = execute_gui_action(workspace, plan)

    assert result["status"] == "success"
    assert result["run_id"]
    assert result["refreshed_model"]["selected_run_id"] == result["run_id"]
    assert result["refreshed_model"]["dashboard"]["reports"]["total"] == 1
    assert build_console_window_model(workspace)["dashboard"]["reports"]["total"] == 1


def test_long_running_gui_actions_are_classified() -> None:
    assert is_long_running_gui_action("record_browser_workflow") is True
    assert is_long_running_gui_action("generate_planner_draft_preview") is True
    assert is_long_running_gui_action("cancel_queue_task") is False


def test_run_gui_action_async_executes_in_background(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    completed = []

    job = run_gui_action_async(
        workspace,
        build_gui_action_plan("run_workflow", workflow="local_html_form_workflow", inputs_file="demo_login.json"),
        on_done=lambda finished: completed.append(finished.job_id),
    )

    assert job.status == "running"
    assert job.thread is not None
    job.thread.join(timeout=10)

    assert job.done is True
    assert job.status == "success"
    assert job.result["run_id"]
    assert completed == [job.job_id]
    assert build_console_window_model(workspace)["dashboard"]["reports"]["total"] == 1


def test_execute_gui_action_records_browser_workflow(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    def fake_record_browser_session(workspace, **kwargs):
        return save_recorded_workflow(
            workspace,
            [{"type": "click", "selector": "#login", "text": "Login", "role": "button"}],
            save_as=kwargs["save_as"],
            initial_url=kwargs["url"],
            assert_text=kwargs.get("assert_text"),
            save_auth_state=kwargs.get("save_auth_state"),
            overwrite=kwargs.get("overwrite", False),
            queue_run=kwargs.get("queue_run", False),
            preview_run=False,
        )

    monkeypatch.setattr("visual_agent.gui.record_browser_session", fake_record_browser_session)

    result = execute_gui_action(
        workspace,
        build_gui_action_plan(
            "record_browser_workflow",
            url="https://example.test/login",
            save_as="recorded/login",
            assert_text="Dashboard",
            save_auth_state="seller",
            preview_run=True,
            overwrite=True,
            queue_run=True,
        ),
    )

    workflow_path = workspace.workflows_dir / "recorded" / "login.yaml"

    assert result["status"] == "success"
    assert result["recording"]["workflow_path"] == str(workflow_path)
    assert result["recording"]["event_count"] == 1
    assert result["recording"]["queue"]["status"] == "submitted"
    assert result["recording"]["save"]["path"] == "workflows/recorded/login.yaml"
    assert "## Save Diff" in gui_action_feedback(result)
    assert workflow_path.exists()
    assert result["refreshed_model"]["dashboard"]["workspace"]["workflow_count"] >= 1
    assert gui_action_feedback(result).startswith("# Browser Recording")


def test_recording_result_markdown_summarizes_validation_preflight_and_preview(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    result = save_recorded_workflow(
        workspace,
        [{"type": "click", "selector": "#login", "text": "Login", "role": "button"}],
        save_as="recorded/summary",
        initial_url="https://example.test/login",
        check=False,
    )
    payload = {
        "workflow_path": str(result.workflow_path),
        "inputs_path": None,
        "input_keys": ["password"],
        "empty_input_keys": ["password"],
        "event_count": 1,
        "validation": {"valid": True, "issues": []},
        "preflight": {"ok": True, "missing_required_capabilities": [], "unavailable_used_capabilities": []},
        "preview": {"status": "skipped", "run_id": "", "reason": "input_template_has_empty_values"},
        "suggested_run": {"workflow": "workflows/recorded/summary.yaml", "inputs_file": "summary_inputs.json"},
        "recovery_hints": ["Fill the generated inputs template before running the recorded workflow with real credentials."],
        "save": {
            "status": "saved",
            "path": "workflows/recorded/summary.yaml",
            "diff": "--- /dev/null\n+++ b/workflows/recorded/summary.yaml\n+name: summary",
        },
        "queue": {"status": "blocked", "message": "Fill the generated inputs template before queueing the recorded workflow.", "task": None},
    }

    markdown = recording_result_to_markdown(payload)

    assert markdown.startswith("# Browser Recording")
    assert str(result.workflow_path) in markdown
    assert "Fill before real run: `password`" in markdown
    assert "## Recovery" in markdown
    assert "Fill the generated inputs template" in markdown
    assert "- OK: `True`" in markdown
    assert "- Reason: `input_template_has_empty_values`" in markdown
    assert "workspace-run --root <workspace> --workflow workflows/recorded/summary.yaml --inputs-file summary_inputs.json" in markdown
    assert "## Queue" in markdown
    assert "before queueing" in markdown
    assert "## Save Diff" in markdown
    assert "+++ b/workflows/recorded/summary.yaml" in markdown


def test_gui_action_recovery_hint_for_record_browser_existing_file(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    def fake_record_browser_session(*_args, **_kwargs):
        raise FileExistsError("Recorded workflow already exists: login.yaml")

    monkeypatch.setattr("visual_agent.gui.record_browser_session", fake_record_browser_session)

    result = safe_execute_gui_action(
        workspace,
        build_gui_action_plan("record_browser_workflow", url="https://example.test", save_as="recorded/login"),
    )

    assert result["status"] == "error"
    assert "enable overwrite" in result["recovery_hint"]


def test_safe_execute_gui_action_includes_recording_failure_report(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    report = archive_recording_failure(
        workspace,
        url="https://example.test",
        save_as="recorded/gui_failure",
        error=RuntimeError("browser unavailable"),
    )

    def fake_record_browser_session(*_args, **_kwargs):
        raise BrowserRecordingError("Browser recording failed: browser unavailable", report)

    monkeypatch.setattr("visual_agent.gui.record_browser_session", fake_record_browser_session)

    result = safe_execute_gui_action(
        workspace,
        build_gui_action_plan("record_browser_workflow", url="https://example.test", save_as="recorded/gui_failure"),
    )
    feedback = gui_action_feedback(result)

    assert result["status"] == "error"
    assert result["failure_report"]["json_report"] == report["json_report"]
    assert "Failure report" in feedback
    assert result["action_event"]["result"]["failure_report"]["report_id"] == report["report_id"]


def test_execute_gui_action_runs_next_queue_task(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow", inputs_file="demo_login.json")

    result = execute_gui_action(workspace, build_gui_action_plan("queue_run_next"))
    queue = list_queue_tasks(workspace)

    assert result["status"] == "success"
    assert result["result"]["task"]["task_id"] == task.task_id
    assert result["refreshed_model"]["selected_run_id"] == result["result"]["result"]["run_id"]
    assert result["refreshed_model"]["dashboard"]["queue"]["pending"] == 0
    assert queue["entries"][0]["status"] == "success"


def test_execute_gui_action_cancels_and_retries_queue_task(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")

    canceled = execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    retried = execute_gui_action(workspace, build_gui_action_plan("retry_queue_task", task_id=task.task_id))
    queue = list_queue_tasks(workspace)

    assert canceled["status"] == "success"
    assert retried["status"] == "success"
    assert queue["entries"][0]["status"] == "pending"


def test_safe_execute_gui_action_returns_structured_error_and_refreshed_model(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    with pytest.raises(RuntimeError):
        execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    result = safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    assert result["status"] == "error"
    assert result["error"]["type"] == "RuntimeError"
    assert "Only pending tasks can be canceled" in result["error"]["message"]
    assert "Refresh the queue state" in result["recovery_hint"]
    assert result["refreshed_model"]["dashboard"]["queue"]["pending"] == 0
    assert result["refreshed_model"]["queue_options"][0]["status"] == "canceled"


def test_safe_execute_gui_action_writes_success_audit_event(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = safe_execute_gui_action(workspace, build_gui_action_plan("refresh_readiness"))
    events = list_gui_action_events(workspace)
    audit_text = gui_action_audit_path(workspace).read_text(encoding="utf-8")

    assert result["status"] == "success"
    assert result["action_event"]["event_id"]
    assert events[0]["event_id"] == result["action_event"]["event_id"]
    assert events[0]["action"] == "refresh_readiness"
    assert events[0]["status"] == "success"
    assert events[0]["plan"]["action"] == "refresh_readiness"
    assert events[0]["result"]["message"] == "External samples: 1/4 ready."
    assert result["refreshed_model"]["gui_action_events"][0]["event_id"] == result["action_event"]["event_id"]
    assert "refreshed_model" not in audit_text


def test_safe_execute_gui_action_writes_compact_risk_policy_plan_event(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = safe_execute_gui_action(workspace, build_gui_action_plan("plan_risk_policy_patch"))
    event = result["action_event"]
    policy_plan = event["result"]["policy_plan"]
    markdown = gui_action_event_markdown({"event": event})

    assert event["action"] == "plan_risk_policy_patch"
    assert policy_plan["applied"] is False
    assert "quality.gui_action_history" in policy_plan["changed_paths"]
    assert policy_plan["validation_after"]["status"] == "ok"
    assert "patch" not in policy_plan
    assert "## Risk Policy Patch" in markdown
    assert "Validation after: `ok`" in markdown


def test_safe_execute_gui_action_writes_error_audit_event(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    result = safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    events = list_gui_action_events(workspace)

    assert result["status"] == "error"
    assert result["action_event"]["status"] == "error"
    assert events[0]["status"] == "error"
    assert events[0]["action"] == "cancel_queue_task"
    assert events[0]["result"]["error"]["type"] == "RuntimeError"
    assert "Only pending tasks can be canceled" in events[0]["result"]["error"]["message"]
    assert result["refreshed_model"]["gui_action_events"][0]["event_id"] == result["action_event"]["event_id"]


def test_console_window_model_exposes_gui_action_history_options(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    first = safe_execute_gui_action(workspace, build_gui_action_plan("refresh_readiness"))
    second = safe_execute_gui_action(workspace, build_gui_action_plan("external_sample_summary"))

    model = build_console_window_model(workspace)
    state = console_window_selection_state(model)
    states = console_window_button_states(model, state)

    assert model["gui_action_events"][0]["event_id"] == second["action_event"]["event_id"]
    assert model["gui_action_event_options"][0]["event_id"] == second["action_event"]["event_id"]
    assert "EXTERNAL_SAMPLE_SUMMARY" not in model["gui_action_event_options"][0]["label"]
    assert model["gui_action_event_options"][0]["label"].startswith("SUCCESS  external_sample_summary")
    assert state["action_history"]["by_label"][state["action_history"]["selected_label"]]["event_id"] == second["action_event"]["event_id"]
    assert states["show_action_history"]["enabled"] is True
    assert first["action_event"]["event_id"] in {event["event_id"] for event in model["gui_action_events"]}
    assert model["gui_action_history_index"]["total_events"] == 2
    assert model["gui_action_history_index"]["success_events"] == 2


def test_list_gui_action_events_filters_by_action_and_status(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    safe_execute_gui_action(workspace, build_gui_action_plan("refresh_readiness"))
    safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    success = list_gui_action_events(workspace, status="success")
    errors = list_gui_action_events(workspace, status="error")
    cancel_errors = list_gui_action_events(workspace, action="cancel_queue_task", status="error")

    assert [event["action"] for event in success] == ["refresh_readiness"]
    assert errors[0]["action"] == "cancel_queue_task"
    assert cancel_errors[0]["status"] == "error"
    assert cancel_errors[0]["result"]["error"]["type"] == "RuntimeError"


def test_gui_action_event_markdown_renders_success_and_error_details(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    success = safe_execute_gui_action(workspace, build_gui_action_plan("refresh_readiness"))
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    error = safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    success_markdown = gui_action_event_markdown({"event": success["action_event"]})
    error_markdown = gui_action_event_markdown({"event": error["action_event"]})

    assert success_markdown.startswith("# GUI Action Event")
    assert "- Action: `refresh_readiness`" in success_markdown
    assert "## Plan" in success_markdown
    assert "## Result" in success_markdown
    assert "## Error" not in success_markdown
    assert "## Error" in error_markdown
    assert "Only pending tasks can be canceled" in error_markdown


def test_gui_action_history_report_filters_and_renders_markdown(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    safe_execute_gui_action(workspace, build_gui_action_plan("refresh_readiness"))
    safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    report = build_gui_action_history_report(workspace, status="error")
    markdown = gui_action_history_report_to_markdown(report)

    assert report["total_events"] == 1
    assert report["error_events"] == 1
    assert report["filters"]["status"] == "error"
    assert report["events"][0]["action"] == "cancel_queue_task"
    assert report["options"][0]["label"].startswith("ERROR  cancel_queue_task")
    assert markdown.startswith("# GUI Action History")
    assert "| error | cancel_queue_task |" in markdown
    assert "Only pending tasks can be canceled" in markdown


def test_gui_action_history_index_summarizes_errors_by_action(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    safe_execute_gui_action(workspace, build_gui_action_plan("refresh_readiness"))
    safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    index = build_gui_action_history_index(workspace)
    by_action = {item["action"]: item for item in index["actions"]}

    assert index["total_events"] == 2
    assert index["success_events"] == 1
    assert index["error_events"] == 1
    assert index["error_rate"] == 0.5
    assert index["status_counts"] == {"error": 1, "success": 1}
    assert by_action["cancel_queue_task"]["error"] == 1
    assert by_action["cancel_queue_task"]["error_rate"] == 1.0
    assert index["failed_actions"][0]["action"] == "cancel_queue_task"
    assert index["recent_errors"][0]["action"] == "cancel_queue_task"
    assert index["recent_errors"][0]["error_type"] == "RuntimeError"
    assert "Only pending tasks can be canceled" in index["recent_errors"][0]["error_message"]


def test_gui_action_history_index_renders_markdown(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    markdown = gui_action_history_index_to_markdown(build_gui_action_history_index(workspace))

    assert markdown.startswith("# GUI Action History Index")
    assert "| cancel_queue_task | 1 | 0 | 1 | 100.00% |" in markdown
    assert "Only pending tasks can be canceled" in markdown


def test_gui_action_history_risk_policy_merges_profile_overrides() -> None:
    policy = gui_action_history_risk_policy(
        {
            "history_limit": 50,
            "error_rate_threshold": 0.5,
            "failed_action_limit": 2,
            "profiles": {
                "ci": {
                    "error_rate_threshold": 0.1,
                    "failed_action_limit": 1,
                }
            },
        },
        profile="ci",
    )

    assert policy == {
        "history_limit": 50,
        "error_rate_threshold": 0.1,
        "failed_action_limit": 1,
    }


def test_gui_action_history_risk_policy_ignores_invalid_values() -> None:
    policy = gui_action_history_risk_policy(
        {
            "history_limit": "many",
            "error_rate_threshold": "high",
            "failed_action_limit": None,
        }
    )

    assert policy == {
        "history_limit": 100,
        "error_rate_threshold": 0.2,
        "failed_action_limit": 5,
    }


def test_gui_action_history_risk_summary_respects_threshold_config(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    safe_execute_gui_action(workspace, build_gui_action_plan("refresh_readiness"))
    safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    summary = build_gui_action_history_risk_summary(workspace, config={"error_rate_threshold": 0.9})

    assert summary["error_rate"] == 0.5
    assert summary["risk_level"] == "warning"
    assert [warning["code"] for warning in summary["warnings"]] == ["gui_action_failed_action"]
    assert summary["policy"]["error_rate_threshold"] == 0.9


def test_gui_action_history_risk_markdown_renders_warnings_and_errors(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    markdown = gui_action_history_risk_to_markdown(build_gui_action_history_risk_summary(workspace))

    assert markdown.startswith("# GUI Action Risk")
    assert "## Trend" in markdown
    assert "- Direction: `insufficient_history`" in markdown
    assert "## Remediation Checklist" in markdown
    assert "[1x] `cancel_queue_task` `RuntimeError`" in markdown
    assert "`gui_action_error_rate`" in markdown
    assert "| cancel_queue_task | 1 | 1 | 100.00% |" in markdown
    assert "Only pending tasks can be canceled" in markdown


def test_gui_action_history_risk_trend_compares_newest_and_older_windows() -> None:
    events = [
        {"status": "success", "action": "refresh_readiness"},
        {"status": "success", "action": "external_sample_summary"},
        {
            "event_id": "older-error",
            "status": "error",
            "action": "cancel_queue_task",
            "result": {
                "error": {"type": "RuntimeError", "message": "Only pending tasks can be canceled"},
                "recovery_hint": "Refresh the queue state, then select a pending task before retrying.",
            },
        },
        {
            "event_id": "older-error-2",
            "status": "error",
            "action": "cancel_queue_task",
            "result": {
                "error": {"type": "RuntimeError", "message": "Only pending tasks can be canceled"},
                "recovery_hint": "Refresh the queue state, then select a pending task before retrying.",
            },
        },
    ]

    trend = gui_action_history_risk_trend(events, window_size=2)

    assert trend["direction"] == "improving"
    assert trend["newest"]["error_rate"] == 0.0
    assert trend["older"]["error_rate"] == 1.0
    assert trend["error_rate_delta"] == -1.0
    assert trend["remediation_count_delta"] == -2


def test_gui_action_history_risk_summary_includes_trend(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    safe_execute_gui_action(workspace, build_gui_action_plan("refresh_readiness"))
    safe_execute_gui_action(workspace, build_gui_action_plan("external_sample_summary"))

    summary = build_gui_action_history_risk_summary(workspace, failed_action_limit=2)

    assert summary["trend"]["direction"] == "improving"
    assert summary["trend"]["window_size"] == 2
    assert summary["trend"]["newest"]["error_events"] == 0
    assert summary["trend"]["older"]["error_events"] == 1


def test_gui_action_history_remediation_items_deduplicate_by_action_error_and_hint(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    first = safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    second = safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    summary = build_gui_action_history_risk_summary(workspace)
    items = gui_action_history_remediation_items(summary["recent_errors"])

    assert len(items) == 1
    assert items[0]["action"] == "cancel_queue_task"
    assert items[0]["error_type"] == "RuntimeError"
    assert items[0]["count"] == 2
    assert items[0]["event_ids"] == [second["action_event"]["event_id"], first["action_event"]["event_id"]]
    assert items[0]["latest_event_id"] == second["action_event"]["event_id"]
    assert summary["remediation_items"] == items


def test_gui_action_risk_event_options_map_recent_errors_to_history_options(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    result = safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    model = result["refreshed_model"]
    options = gui_action_risk_event_options(model["gui_action_history_risk"], model["gui_action_event_options"])

    assert options[0]["event_id"] == result["action_event"]["event_id"]
    assert options[0]["label"] == model["gui_action_event_options"][0]["label"]
    assert options[0]["risk_label"].startswith("RISK  cancel_queue_task")


def test_console_window_model_exposes_gui_action_risk_summary_card(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    result = safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    model = result["refreshed_model"]
    risk_card = next(card for card in model["summary_cards"] if card["id"] == "gui_action_risk")

    assert model["gui_action_history_risk"]["risk_level"] == "warning"
    assert model["gui_action_history_risk"]["warning_count"] == 2
    assert model["gui_action_history_risk_markdown"].startswith("# GUI Action Risk")
    assert model["gui_action_risk_event_options"][0]["event_id"] == result["action_event"]["event_id"]
    assert model["selected_gui_action_risk_event"]["event_id"] == result["action_event"]["event_id"]
    assert risk_card["value"] == "warning"
    assert "2 warnings" in risk_card["detail"]


def test_console_window_button_states_enable_action_risk_when_failed_event_exists(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    empty_model = build_console_window_model(workspace)
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    result = safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    assert console_window_button_states(empty_model)["show_action_risk"]["enabled"] is False
    assert console_window_button_states(result["refreshed_model"])["show_action_risk"]["enabled"] is True


def test_execute_gui_action_open_artifact_resolves_workspace_path(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    result = run_workspace_workflow(
        workspace,
        "local_html_form_workflow",
        inputs={"username": "demo_user", "password": "secret"},
        dry_run=True,
    )
    path = f"reports/{result.run_id}.json"

    opened = execute_gui_action(workspace, build_gui_action_plan("open_artifact", path=path))

    assert opened["status"] == "success"
    assert opened["path"].endswith(f"reports\\{result.run_id}.json") or opened["path"].endswith(f"reports/{result.run_id}.json")


def test_execute_gui_action_open_artifact_rejects_outside_path(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    with pytest.raises(ValueError):
        execute_gui_action(workspace, build_gui_action_plan("open_artifact", path=str(tmp_path / "outside.txt")))


def test_console_window_model_lists_auth_state_options(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    auth_dir = workspace.root / ".agent-auth"
    auth_dir.mkdir()
    (auth_dir / "seller.json").write_text(
        json.dumps({"cookies": [{"name": "session", "value": "secret", "domain": ".seller.example.com"}], "origins": []}),
        encoding="utf-8",
    )

    model = build_console_window_model(workspace)

    assert model["auth_state_options"][0]["path"].endswith("seller.json")
    assert model["auth_state_options"][0]["metadata"]["domains"] == ["seller.example.com"]
    assert model["auth_state_options"][0]["status"] == "ready"
    assert model["auth_state_options"][0]["label"].startswith("READY")
    assert "secret" not in json.dumps(model["auth_state_options"])


def test_console_window_model_flags_expired_auth_state(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    auth_dir = workspace.root / ".agent-auth"
    auth_dir.mkdir()
    (auth_dir / "expired.json").write_text(
        json.dumps({"cookies": [{"name": "session", "value": "secret", "domain": ".seller.example.com", "expires": 1}], "origins": []}),
        encoding="utf-8",
    )

    option = build_console_window_model(workspace)["auth_state_options"][0]

    assert option["status"] == "expired"
    assert option["label"].startswith("EXPIRED")
    assert "re-import" in option["warning"]
    assert "secret" not in json.dumps(option)


def test_execute_gui_action_inspects_auth_state(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    auth_dir = workspace.root / ".agent-auth"
    auth_dir.mkdir()
    state = auth_dir / "seller.json"
    state.write_text(
        json.dumps({"cookies": [{"name": "session", "value": "secret", "domain": ".seller.example.com"}], "origins": []}),
        encoding="utf-8",
    )

    result = execute_gui_action(workspace, build_gui_action_plan("inspect_auth_state", path=str(state)))

    assert result["status"] == "success"
    assert result["metadata"]["cookie_count"] == 1
    assert "secret" not in json.dumps(result)


def test_execute_gui_action_deletes_auth_state_and_manifest(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    auth_dir = workspace.root / ".agent-auth"
    auth_dir.mkdir()
    state = auth_dir / "seller.json"
    manifest = auth_dir / "seller.json.manifest.json"
    state.write_text(
        json.dumps({"cookies": [{"name": "session", "value": "secret", "domain": ".seller.example.com"}], "origins": []}),
        encoding="utf-8",
    )
    manifest.write_text("{}", encoding="utf-8")

    result = safe_execute_gui_action(workspace, build_gui_action_plan("delete_auth_state", path=str(state)))

    assert result["status"] == "success"
    assert result["manifest_deleted"] is True
    assert not state.exists()
    assert not manifest.exists()
    assert result["refreshed_model"]["auth_state_options"] == []
    assert "seller.example.com" in json.dumps(result, ensure_ascii=False)
    assert '"value": "secret"' not in json.dumps(result, ensure_ascii=False)


def test_console_model_lists_input_templates(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    model = build_console_window_model(workspace)

    assert model["input_template_options"][0]["path"] == "inputs/demo_login.json"
    assert model["input_template_options"][0]["valid"] is True
    assert set(model["input_template_options"][0]["keys"]) == {"password", "username"}


def test_execute_gui_action_reads_and_saves_input_template(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    read = execute_gui_action(workspace, build_gui_action_plan("read_input_template", path="inputs/demo_login.json"))
    saved = safe_execute_gui_action(
        workspace,
        build_gui_action_plan(
            "save_input_template",
            path="edited.json",
            input_text=json.dumps({"username": "demo", "password": "secret"}),
        ),
    )
    events = list_gui_action_events(workspace, action="save_input_template")

    assert read["status"] == "success"
    assert read["input_template"]["path"] == "inputs/demo_login.json"
    assert "username" in read["input_template"]["text"]
    assert saved["status"] == "success"
    assert saved["input_template"]["path"] == "inputs/edited.json"
    assert json.loads((workspace.inputs_dir / "edited.json").read_text(encoding="utf-8"))["username"] == "demo"
    assert "secret" not in json.dumps(events, ensure_ascii=False)
    assert events[0]["result"]["input_template"]["keys"] == ["password", "username"]


def test_input_template_editor_rejects_path_traversal(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    with pytest.raises(ValueError):
        execute_gui_action(
            workspace,
            build_gui_action_plan("save_input_template", path="../outside.json", input_text="{}"),
        )


def test_execute_gui_action_imports_auth_state(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps({"cookies": [{"name": "session", "value": "secret", "domain": ".seller.example.com"}], "origins": []}),
        encoding="utf-8",
    )

    result = execute_gui_action(
        workspace,
        build_gui_action_plan("import_auth_state", source=str(source), auth_name="seller"),
    )

    assert result["status"] == "success"
    assert (workspace.root / ".agent-auth" / "seller.json").exists()
    assert '"value": "secret"' not in json.dumps(result)


def test_console_window_model_marks_external_sample_ready_after_auth_state_exists(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    auth_dir = workspace.root / ".agent-auth"
    auth_dir.mkdir()
    (auth_dir / "seller-sandbox-state.json").write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    (auth_dir / "inventory-sandbox-state.json").write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    (auth_dir / "finance-sandbox-state.json").write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    model = build_console_window_model(workspace)

    readiness = model["external_sample_readiness"]
    assert readiness["ready_samples"] == 4
    assert readiness["blocked_samples"] == 0
    assert readiness["missing_storage_state_files"] == 0
    assert readiness["auth_blocked_samples"] == 3
    ecommerce = next(entry for entry in readiness["entries"] if entry["sample_id"] == "external_ecommerce_orders_readonly")
    assert ecommerce["storage_state_files"][0]["exists"] is True
    assert ecommerce["storage_state_files"][0]["allowed"] is True
    assert ecommerce["storage_state_files"][0]["status"] == "empty"
    assert model["readiness_options"][0]["label"].startswith("READY")


def test_execute_gui_action_refreshes_external_sample_readiness(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    blocked = execute_gui_action(workspace, build_gui_action_plan("refresh_readiness"))

    assert blocked["status"] == "success"
    assert blocked["readiness"]["ready_samples"] == 1
    assert blocked["readiness"]["blocked_samples"] == 3
    assert blocked["message"] == "External samples: 1/4 ready."


def test_execute_gui_action_plans_risk_policy_patch_without_writing(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    manifest_path = workspace.root / "workspace.json"
    original = json.loads(manifest_path.read_text(encoding="utf-8"))

    result = execute_gui_action(workspace, build_gui_action_plan("plan_risk_policy_patch"))
    current = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert result["status"] == "success"
    assert result["policy_plan"]["applied"] is False
    assert "quality.gui_action_history" in result["policy_plan"]["changed_paths"]
    assert gui_action_feedback(result).startswith("# Risk Policy Patch")
    assert current == original


def test_execute_gui_action_applies_risk_policy_patch_and_refreshes_model(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = execute_gui_action(workspace, build_gui_action_plan("apply_risk_policy_patch"))
    model = result["refreshed_model"]
    risk_policy_card = next(card for card in model["summary_cards"] if card["id"] == "risk_policy")

    assert result["status"] == "success"
    assert result["policy_plan"]["applied"] is True
    assert model["dashboard"]["risk_policy_check"]["status"] == "ok"
    assert risk_policy_card["value"] == "ok"


def test_risk_policy_plan_to_markdown_shows_changed_paths_and_validation(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    result = execute_gui_action(workspace, build_gui_action_plan("plan_risk_policy_patch"))

    markdown = risk_policy_plan_to_markdown(result["policy_plan"])

    assert markdown.startswith("# Risk Policy Patch")
    assert "| before | `warning` | 0 | 2 |" in markdown
    assert "| after | `ok` | 0 | 0 |" in markdown
    assert "- `quality.gui_action_history`" in markdown
    assert "- `auto_repair.min_confidence`" in markdown


def test_readiness_markdown_includes_status_summary_and_remediation(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    model = build_console_window_model(workspace)

    markdown = readiness_to_markdown(model["external_sample_readiness"])

    assert "## Status Summary" in markdown
    assert "- Ready samples: `external_support_tickets_triage`" in markdown
    assert "`external_ecommerce_orders_readonly`" in markdown
    assert "- Auth-blocked samples: 3" in markdown
    assert "## Blocked Remediation" in markdown
    assert "Import the required storage_state with auth-state-import" in markdown
    assert "auth=missing" in markdown


def test_execute_gui_action_plans_external_sample_run(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    blocked = execute_gui_action(
        workspace,
        build_gui_action_plan("plan_external_sample_run", sample_id="external_ecommerce_orders_readonly"),
    )

    assert blocked["status"] == "blocked"
    assert "missing_storage_state_file" in blocked["plan"]["blockers"]

    auth_dir = workspace.root / ".agent-auth"
    auth_dir.mkdir()
    (auth_dir / "seller-sandbox-state.json").write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    (auth_dir / "inventory-sandbox-state.json").write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    (auth_dir / "finance-sandbox-state.json").write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    ready = execute_gui_action(
        workspace,
        build_gui_action_plan("plan_external_sample_run", sample_id="external_ecommerce_orders_readonly"),
    )

    assert ready["status"] == "success"
    assert ready["plan"]["ready"] is True
    assert ready["plan"]["run_profile"] == "dry-run"


def test_execute_gui_action_submits_external_sample_batch(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = execute_gui_action(workspace, build_gui_action_plan("submit_external_sample_batch"))
    queue = list_queue_tasks(workspace)

    assert result["status"] == "success"
    assert result["result"]["submitted_count"] == 1
    assert result["result"]["skipped_count"] == 3
    assert result["refreshed_model"]["dashboard"]["queue"]["pending"] == 1
    assert result["refreshed_model"]["external_sample_summary"]["queued_samples"] == 1
    assert queue["pending_tasks"] == 1


def test_refreshed_model_updates_console_window_queue_selection_state(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = execute_gui_action(workspace, build_gui_action_plan("submit_external_sample_batch"))
    state = console_window_selection_state(result["refreshed_model"])

    assert result["status"] == "success"
    assert state["queue"]["labels"]
    assert state["queue"]["by_label"][state["queue"]["selected_label"]]["status"] == "pending"
    assert result["refreshed_model"]["dashboard"]["queue"]["pending"] == 1


def test_execute_gui_action_shows_external_sample_summary(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    execute_gui_action(workspace, build_gui_action_plan("submit_external_sample_batch"))

    result = execute_gui_action(workspace, build_gui_action_plan("external_sample_summary"))

    assert result["status"] == "success"
    assert result["summary"]["total_samples"] == 4
    assert result["summary"]["queued_samples"] == 1
    assert result["message"] == "External samples: 0 with reports, 1 queued."


def test_execute_gui_action_previews_planner_draft_save_without_writing(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = execute_gui_action(
        workspace,
        build_gui_action_plan(
            "preview_planner_draft_save",
            workflow="local_html_form_workflow",
            save_as="planner_preview/local_html_form_workflow",
        ),
    )
    feedback = gui_action_feedback(result)

    assert result["status"] == "success"
    assert result["planner_draft"]["save"]["status"] == "previewed"
    assert result["planner_draft"]["save"]["path"] == "workflows/planner_preview/local_html_form_workflow.yaml"
    assert "## Save Diff" in feedback
    assert not (workspace.workflows_dir / "planner_preview" / "local_html_form_workflow.yaml").exists()


def test_execute_gui_action_generates_planner_draft_preview_without_writing(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    key_file = tmp_path / "keys.txt"
    key_file.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": """
schema_version: 1
min_runtime_version: "0.1.0"
name: gui_generated_login_check
version: 1
steps:
  - name: observe login
    action: observe_html
    params:
      path: fixtures/login_demo.html
  - name: assert login
    action: assert_text
    params:
      text: 登录
"""
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr("visual_agent.planner_generate.urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())

    result = execute_gui_action(
        workspace,
        build_gui_action_plan(
            "generate_planner_draft_preview",
            instruction="检查登录页",
            source=str(key_file),
            preferred="xiaomimimo",
        ),
    )
    feedback = gui_action_feedback(result)

    assert result["status"] == "success"
    assert result["planner_draft"]["status"] == "valid"
    assert result["planner_draft"]["save"]["status"] == "previewed"
    assert result["planner_draft"]["save"]["path"] == "workflows/planner_generated/gui_generated_login_check.yaml"
    assert "## Save Diff" in feedback
    assert not (workspace.workflows_dir / "planner_generated" / "gui_generated_login_check.yaml").exists()
    assert "sk-xiaomi-secret-value-abcdef" not in json.dumps(result, ensure_ascii=False)
    assert "sk-xiaomi-secret-value-abcdef" not in feedback


def test_execute_gui_action_saves_generated_planner_draft_after_confirmation_plan(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    key_file = tmp_path / "keys.txt"
    key_file.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": """
schema_version: 1
min_runtime_version: "0.1.0"
name: gui_saved_login_check
version: 1
steps:
  - id: observe_login
    action: observe_html
    path: fixtures/login_demo.html
  - id: assert_login
    action: assert_text
    text: 登录
"""
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr("visual_agent.planner_generate.urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())

    result = execute_gui_action(
        workspace,
        build_gui_action_plan(
            "save_generated_planner_draft",
            instruction="检查登录页",
            source=str(key_file),
            preferred="xiaomimimo",
            save_as="planner_generated/gui_saved_login_check",
        ),
    )
    target = workspace.workflows_dir / "planner_generated" / "gui_saved_login_check.yaml"

    assert result["status"] == "success"
    assert result["planner_draft"]["save"]["status"] == "saved"
    assert result["planner_draft"]["save"]["path"] == "workflows/planner_generated/gui_saved_login_check.yaml"
    assert result["planner_draft"]["preflight"]["ok"] is True
    assert target.exists()
    assert "gui_saved_login_check" in target.read_text(encoding="utf-8")
    assert result["refreshed_model"]["dashboard"]["workspace"]["workflow_count"] >= 2
    assert "sk-xiaomi-secret-value-abcdef" not in json.dumps(result, ensure_ascii=False)


def test_safe_execute_gui_action_audits_planner_draft_save_without_draft_text(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    key_file = tmp_path / "keys.txt"
    key_file.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": """
schema_version: 1
min_runtime_version: "0.1.0"
name: audited_gui_saved_login_check
version: 1
steps:
  - id: observe_login
    action: observe_html
    path: fixtures/login_demo.html
  - id: assert_login
    action: assert_text
    text: 登录
"""
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr("visual_agent.planner_generate.urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())

    result = safe_execute_gui_action(
        workspace,
        build_gui_action_plan(
            "save_generated_planner_draft",
            instruction="检查登录页",
            source=str(key_file),
            preferred="xiaomimimo",
            save_as="planner_generated/audited_gui_saved_login_check",
        ),
    )
    event = result["action_event"]
    planner_draft = event["result"]["planner_draft"]
    markdown = gui_action_event_markdown({"event": event})

    assert event["action"] == "save_generated_planner_draft"
    assert event["status"] == "success"
    assert event["plan"]["save_as"] == "planner_generated/audited_gui_saved_login_check"
    assert event["plan"]["preferred"] == "xiaomimimo"
    assert planner_draft["save"]["status"] == "saved"
    assert planner_draft["save"]["path"] == "workflows/planner_generated/audited_gui_saved_login_check.yaml"
    assert planner_draft["preflight"]["ok"] is True
    assert planner_draft["preflight"]["missing_required_count"] == 0
    assert planner_draft["workflow"] == {"name": "audited_gui_saved_login_check", "step_count": 2}
    assert "draft_text" not in json.dumps(event, ensure_ascii=False)
    assert "sk-xiaomi-secret-value-abcdef" not in json.dumps(event, ensure_ascii=False)
    assert "## Planner Draft" in markdown
    assert "Save status: `saved`" in markdown
    assert "Preflight OK: `True`" in markdown


def test_safe_execute_gui_action_audits_planner_draft_target_exists_block(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    key_file = tmp_path / "keys.txt"
    key_file.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": """
schema_version: 1
min_runtime_version: "0.1.0"
name: audited_existing_login_check
version: 1
steps:
  - id: observe_login
    action: observe_html
    path: fixtures/login_demo.html
  - id: assert_login
    action: assert_text
    text: 登录
"""
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr("visual_agent.planner_generate.urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())
    plan = build_gui_action_plan(
        "save_generated_planner_draft",
        instruction="检查登录页",
        source=str(key_file),
        preferred="xiaomimimo",
        save_as="planner_generated/audited_existing_login_check",
    )

    first = safe_execute_gui_action(workspace, plan)
    second = safe_execute_gui_action(workspace, plan)
    event = second["action_event"]
    planner_draft = event["result"]["planner_draft"]

    assert first["status"] == "success"
    assert second["status"] == "blocked"
    assert event["status"] == "blocked"
    assert planner_draft["save"]["status"] == "blocked"
    assert planner_draft["save"]["reason"] == "target_exists"
    assert planner_draft["save"]["target_exists"] is True
    assert "draft_text" not in json.dumps(event, ensure_ascii=False)


def test_safe_execute_gui_action_audits_planner_draft_recovery_suggestions(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    key_file = tmp_path / "keys.txt"
    key_file.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": """
schema_version: 1
min_runtime_version: "0.1.0"
name: unsafe_gui_draft
version: 1
steps:
  - id: observe_login
    action: observe_html
    path: fixtures/login_demo.html
  - id: save_auth
    action: save_storage_state
    path: auth/real_state.json
  - id: assert_login
    action: assert_text
    text: 登录
"""
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr("visual_agent.planner_generate.urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())

    result = safe_execute_gui_action(
        workspace,
        build_gui_action_plan(
            "save_generated_planner_draft",
            instruction="生成并保存登录态",
            source=str(key_file),
            preferred="xiaomimimo",
            save_as="planner_generated/unsafe_gui_draft",
        ),
    )
    event = result["action_event"]
    planner_draft = event["result"]["planner_draft"]
    feedback = gui_action_feedback(result)

    assert result["status"] == "blocked"
    assert result["planner_draft"]["status"] == "invalid"
    assert result["planner_draft"]["save"]["reason"] == "draft_not_valid"
    assert any("Remove high-risk actions" in item for item in planner_draft["recovery_suggestions"])
    assert "## Recovery Suggestions" in feedback
    assert "Remove high-risk actions" in feedback
    assert "draft_text" not in json.dumps(event, ensure_ascii=False)
    assert "sk-xiaomi-secret-value-abcdef" not in json.dumps(event, ensure_ascii=False)


def test_gui_action_plan_requires_save_generated_planner_draft_fields() -> None:
    with pytest.raises(ValueError):
        build_gui_action_plan("save_generated_planner_draft", instruction="检查登录页")

    with pytest.raises(ValueError):
        build_gui_action_plan("save_generated_planner_draft", save_as="planner_generated/login")


def test_console_window_model_exposes_planner_draft_preview_action(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    model = build_console_window_model(workspace)
    state = console_window_selection_state(model)
    buttons = console_window_button_states(model, state)

    assert any(button["id"] == "preview_planner_draft_save" for button in model["action_buttons"])
    assert any(button["id"] == "generate_planner_draft_preview" for button in model["action_buttons"])
    assert any(button["id"] == "save_generated_planner_draft" for button in model["action_buttons"])
    assert buttons["preview_planner_draft_save"]["enabled"] is True
    assert buttons["generate_planner_draft_preview"]["enabled"] is True
    assert buttons["save_generated_planner_draft"]["enabled"] is True


def test_execute_gui_action_exports_external_sample_batch_report(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    execute_gui_action(workspace, build_gui_action_plan("submit_external_sample_batch"))

    result = execute_gui_action(workspace, build_gui_action_plan("external_sample_batch_report"))
    model = build_console_window_model(workspace)

    assert result["status"] == "success"
    assert result["result"]["summary"]["queued_samples"] == 1
    assert result["result"]["json_report"].endswith(".json")
    assert result["result"]["markdown_report"].endswith(".md")
    assert model["external_sample_batch_report_index"]["total_reports"] == 1
    assert model["batch_report_options"][0]["report_id"] == result["result"]["report_id"]
    assert model["batch_report_options"][0]["path"].endswith(".md")
    assert model["selected_batch_report_id"] == result["result"]["report_id"]
    assert "# External Sample Batch Report" in model["selected_batch_report_markdown"]
    assert "## Samples" in model["selected_batch_report_markdown"]
    assert result["refreshed_model"]["selected_batch_report_id"] == result["result"]["report_id"]
    assert result["refreshed_model"]["batch_report_options"][0]["report_id"] == result["result"]["report_id"]
    assert "# External Sample Batch Report" in result["refreshed_model"]["selected_batch_report_markdown"]


def test_console_window_model_can_preview_selected_batch_report(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    first = execute_gui_action(workspace, build_gui_action_plan("external_sample_batch_report"))["result"]
    execute_gui_action(workspace, build_gui_action_plan("submit_external_sample_batch"))
    second = execute_gui_action(workspace, build_gui_action_plan("external_sample_batch_report"))["result"]

    model = build_console_window_model(workspace, selected_batch_report_id=first["report_id"])

    assert first["report_id"] != second["report_id"]
    assert model["selected_batch_report_id"] == first["report_id"]
    assert model["selected_batch_report"]["report_id"] == first["report_id"]
    assert first["report_id"] in model["selected_batch_report_markdown"]
    assert second["report_id"] not in model["selected_batch_report_markdown"]


def test_refreshed_model_updates_console_window_batch_selection_state(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    execute_gui_action(workspace, build_gui_action_plan("submit_external_sample_batch"))

    result = execute_gui_action(workspace, build_gui_action_plan("external_sample_batch_report"))
    state = console_window_selection_state(result["refreshed_model"])

    assert state["batch_report"]["by_label"][state["batch_report"]["selected_label"]]["report_id"] == result["result"]["report_id"]
    detail = console_model_detail_markdown(result["refreshed_model"])
    assert detail.startswith("# GUI Batch Status Summary")
    assert "# External Sample Batch Report" in detail


def test_batch_report_detail_markdown_adds_gui_status_summary(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    workspace.reports_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "run_id": "failed-support",
        "workflow_name": "external_support_tickets_triage",
        "status": "failed",
        "run_profile": "dry-run",
        "total_steps": 1,
        "succeeded_steps": 0,
        "failed_step": "observe",
        "dry_run_actions": 0,
        "elapsed_seconds": 0.1,
        "artifacts": {},
        "downloads": [],
        "steps": [],
        "external_sample": {"sample_id": "external_support_tickets_triage"},
    }
    (workspace.reports_dir / "failed-support.json").write_text(json.dumps(report), encoding="utf-8")
    result = execute_gui_action(workspace, build_gui_action_plan("external_sample_batch_report"))
    option = result["refreshed_model"]["selected_batch_report"]

    markdown = batch_report_detail_markdown(workspace, option)

    assert markdown.startswith("# GUI Batch Status Summary")
    assert "- Failed: 1" in markdown
    assert "- Ready rerun candidates: 1" in markdown
    assert "## Ready Rerun Candidates" in markdown
    assert "`external_support_tickets_triage`" in markdown
    assert "## Blocked Samples" in markdown
    assert "---\n\n# External Sample Batch Report" in markdown


def test_console_window_button_states_enable_batch_and_ready_failed_reruns(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    workspace.reports_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "run_id": "failed-support",
        "workflow_name": "external_support_tickets_triage",
        "status": "failed",
        "run_profile": "dry-run",
        "total_steps": 1,
        "succeeded_steps": 0,
        "failed_step": "observe",
        "dry_run_actions": 0,
        "elapsed_seconds": 0.1,
        "artifacts": {},
        "downloads": [],
        "steps": [],
        "external_sample": {"sample_id": "external_support_tickets_triage"},
    }
    (workspace.reports_dir / "failed-support.json").write_text(json.dumps(report), encoding="utf-8")
    batch = execute_gui_action(workspace, build_gui_action_plan("external_sample_batch_report"))

    states = console_window_button_states(batch["refreshed_model"])

    assert states["open_batch_report"]["enabled"] is True
    assert states["plan_external_sample_batch_reruns"]["enabled"] is True
    assert states["submit_external_sample_batch_reruns"]["enabled"] is True
    assert states["plan_external_sample_reruns"]["enabled"] is True
    assert states["submit_external_sample_reruns"]["enabled"] is True


def test_gui_action_feedback_prefers_explicit_text_then_message() -> None:
    assert gui_action_feedback({"action": "run_workflow", "status": "success", "message": "done"}) == "done"
    assert gui_action_feedback({"action": "run_workflow", "status": "success", "message": "done"}, "details") == "details"
    assert gui_action_feedback({"action": "unknown", "status": "empty"}) == "unknown: empty"


def test_gui_action_feedback_formats_policy_plan_before_preferred_payload(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    result = execute_gui_action(workspace, build_gui_action_plan("plan_risk_policy_patch"))

    feedback = gui_action_feedback(result, "raw payload")

    assert feedback.startswith("# Risk Policy Patch")
    assert "## Changed Paths" in feedback


def test_gui_action_feedback_formats_errors_before_preferred_payload() -> None:
    result = {
        "action": "cancel_queue_task",
        "status": "error",
        "error": {"type": "RuntimeError", "message": "Only pending tasks can be canceled"},
        "recovery_hint": "Refresh the queue state and select a task whose status allows this action.",
    }

    feedback = gui_action_feedback(result, "raw payload")

    assert feedback.startswith("# Action Failed: cancel_queue_task")
    assert "RuntimeError" in feedback
    assert "Only pending tasks can be canceled" in feedback
    assert "Refresh the queue state" in feedback
    assert "raw payload" not in feedback


def test_gui_error_detail_unifies_action_preflight_and_quality() -> None:
    detail = build_gui_error_detail(
        {
            "action": "run_workflow",
            "status": "error",
            "error": {"type": "RuntimeError", "message": "failed"},
            "recovery_hint": "Inspect report.",
            "preflight": {"ok": False, "missing_required_capabilities": ["observe_browser"]},
            "quality": {
                "risk_level": "warning",
                "warning_count": 1,
                "strict_policy_gate": {"failed": True},
                "secret_scan": {"finding_count": 2},
            },
            "failure_report": {"json_report": "reports/failure.json"},
        }
    )
    markdown = gui_error_detail_to_markdown(detail)

    assert detail["source"] == "gui_action"
    assert detail["error_type"] == "RuntimeError"
    assert markdown.startswith("# Action Failed: run_workflow")
    assert "## Preflight" in markdown
    assert "observe_browser" in markdown
    assert "## Quality" in markdown
    assert "Secret findings: 2" in markdown
    assert "reports/failure.json" in markdown


def test_execute_gui_action_plans_and_submits_external_sample_reruns(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    workspace.reports_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "run_id": "failed-support",
        "workflow_name": "external_support_tickets_triage",
        "status": "failed",
        "run_profile": "dry-run",
        "total_steps": 1,
        "succeeded_steps": 0,
        "failed_step": "observe",
        "dry_run_actions": 0,
        "elapsed_seconds": 0.1,
        "artifacts": {},
        "downloads": [],
        "steps": [],
        "external_sample": {"sample_id": "external_support_tickets_triage"},
    }
    (workspace.reports_dir / "failed-support.json").write_text(json.dumps(report), encoding="utf-8")

    plan = execute_gui_action(workspace, build_gui_action_plan("plan_external_sample_reruns"))
    submitted = execute_gui_action(workspace, build_gui_action_plan("submit_external_sample_reruns"))
    queue = list_queue_tasks(workspace)

    assert plan["status"] == "success"
    assert plan["plan"]["candidate_count"] == 1
    assert plan["refreshed_model"]["selected_run_id"] == "failed-support"
    assert submitted["status"] == "success"
    assert submitted["result"]["submitted_count"] == 1
    assert submitted["refreshed_model"]["dashboard"]["queue"]["pending"] == 1
    assert queue["pending_tasks"] == 1


def test_execute_gui_action_plans_and_submits_selected_batch_reruns(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    workspace.reports_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "run_id": "failed-support",
        "workflow_name": "external_support_tickets_triage",
        "status": "failed",
        "run_profile": "dry-run",
        "total_steps": 1,
        "succeeded_steps": 0,
        "failed_step": "observe",
        "dry_run_actions": 0,
        "elapsed_seconds": 0.1,
        "artifacts": {},
        "downloads": [],
        "steps": [],
        "external_sample": {"sample_id": "external_support_tickets_triage"},
    }
    (workspace.reports_dir / "failed-support.json").write_text(json.dumps(report), encoding="utf-8")
    batch = execute_gui_action(workspace, build_gui_action_plan("external_sample_batch_report"))["result"]

    plan = execute_gui_action(
        workspace,
        build_gui_action_plan("plan_external_sample_batch_reruns", batch_report_id=batch["report_id"]),
    )
    submitted = execute_gui_action(
        workspace,
        build_gui_action_plan("submit_external_sample_batch_reruns", batch_report_id=batch["report_id"]),
    )
    queue = list_queue_tasks(workspace)

    assert plan["status"] == "success"
    assert plan["plan"]["candidate_count"] == 1
    assert plan["refreshed_model"]["selected_batch_report_id"] == batch["report_id"]
    assert submitted["status"] == "success"
    assert submitted["result"]["submitted_count"] == 1
    assert submitted["refreshed_model"]["selected_batch_report_id"] == batch["report_id"]
    assert submitted["refreshed_model"]["dashboard"]["queue"]["pending"] == 1
    assert queue["pending_tasks"] == 1


def test_workspace_gui_cli_parser_accepts_root_and_run_id() -> None:
    args = build_parser().parse_args(
        [
            "workspace-gui",
            "--root",
            ".agent-workspace",
            "--run-id",
            "run-1",
            "--limit",
            "3",
        ]
    )

    assert args.command == "workspace-gui"
    assert args.root == ".agent-workspace"
    assert args.run_id == "run-1"
    assert args.limit == 3


def test_workspace_gui_actions_cli_parser_accepts_filters_and_format() -> None:
    args = build_parser().parse_args(
        [
            "workspace-gui-actions",
            "--root",
            ".agent-workspace",
            "--format",
            "markdown",
            "--limit",
            "7",
            "--action",
            "refresh_readiness",
            "--status",
            "success",
        ]
    )

    assert args.command == "workspace-gui-actions"
    assert args.root == ".agent-workspace"
    assert args.format == "markdown"
    assert args.limit == 7
    assert args.action == "refresh_readiness"
    assert args.status == "success"


def test_workspace_gui_action_index_cli_parser_accepts_limits_and_format() -> None:
    args = build_parser().parse_args(
        [
            "workspace-gui-action-index",
            "--root",
            ".agent-workspace",
            "--format",
            "markdown",
            "--limit",
            "30",
            "--recent-error-limit",
            "4",
        ]
    )

    assert args.command == "workspace-gui-action-index"
    assert args.root == ".agent-workspace"
    assert args.format == "markdown"
    assert args.limit == 30
    assert args.recent_error_limit == 4


def test_workspace_gui_action_index_cli_exports_risk_json_and_markdown(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    json_code = main(["workspace-gui-action-index", "--root", str(workspace.root), "--risk"])
    json_output = capsys.readouterr().out
    markdown_code = main(["workspace-gui-action-index", "--root", str(workspace.root), "--risk", "--format", "markdown"])
    markdown_output = capsys.readouterr().out
    payload = json.loads(json_output)

    assert json_code == 0
    assert markdown_code == 0
    assert payload["remediation_items"][0]["action"] == "cancel_queue_task"
    assert payload["remediation_items"][0]["count"] == 1
    assert markdown_output.startswith("# GUI Action Risk")
    assert "## Remediation Checklist" in markdown_output
    assert "[1x] `cancel_queue_task` `RuntimeError`" in markdown_output


def test_external_sample_run_cli_parser_accepts_guarded_options() -> None:
    args = build_parser().parse_args(
        [
            "external-sample-run",
            "--workspace-root",
            ".agent-workspace",
            "--sample-id",
            "external_ecommerce_orders_readonly",
            "--run-profile",
            "supervised",
        ]
    )

    assert args.command == "external-sample-run"
    assert args.workspace_root == ".agent-workspace"
    assert args.sample_id == "external_ecommerce_orders_readonly"
    assert args.run_profile == "supervised"


def test_external_sample_batch_submit_cli_parser_accepts_queue_options() -> None:
    args = build_parser().parse_args(
        [
            "external-sample-batch-submit",
            "--workspace-root",
            ".agent-workspace",
            "--run-profile",
            "dry-run",
            "--priority",
            "3",
            "--max-retries",
            "1",
        ]
    )

    assert args.command == "external-sample-batch-submit"
    assert args.workspace_root == ".agent-workspace"
    assert args.priority == 3
    assert args.max_retries == 1


def test_external_sample_summary_cli_parser_accepts_workspace_root() -> None:
    args = build_parser().parse_args(
        [
            "external-sample-summary",
            "--workspace-root",
            ".agent-workspace",
        ]
    )

    assert args.command == "external-sample-summary"
    assert args.workspace_root == ".agent-workspace"


def test_external_sample_batch_report_cli_parser_accepts_workspace_root() -> None:
    args = build_parser().parse_args(
        [
            "external-sample-batch-report",
            "--workspace-root",
            ".agent-workspace",
        ]
    )

    assert args.command == "external-sample-batch-report"
    assert args.workspace_root == ".agent-workspace"


def test_external_sample_dry_run_report_cli_parser_accepts_safety_flags() -> None:
    args = build_parser().parse_args(
        [
            "external-sample-dry-run-report",
            "--workspace-root",
            ".agent-workspace",
            "--require-live-auth",
            "--skip-preflight",
        ]
    )

    assert args.command == "external-sample-dry-run-report"
    assert args.workspace_root == ".agent-workspace"
    assert args.require_live_auth is True
    assert args.skip_preflight is True


def test_external_sample_live_placeholder_cli_parser_accepts_workspace_root() -> None:
    args = build_parser().parse_args(
        [
            "external-sample-live-placeholder",
            "--workspace-root",
            ".agent-workspace",
            "--no-require-live-auth",
        ]
    )

    assert args.command == "external-sample-live-placeholder"
    assert args.workspace_root == ".agent-workspace"
    assert args.no_require_live_auth is True


def test_external_sample_batch_report_index_cli_parser_accepts_filters() -> None:
    args = build_parser().parse_args(
        [
            "external-sample-batch-report-index",
            "--workspace-root",
            ".agent-workspace",
            "--rebuild",
            "--status",
            "failed",
            "--sample-id",
            "external_support_tickets_triage",
        ]
    )
    list_args = build_parser().parse_args(
        [
            "external-sample-batch-reports",
            "--workspace-root",
            ".agent-workspace",
            "--status",
            "failed",
        ]
    )

    assert args.command == "external-sample-batch-report-index"
    assert args.rebuild is True
    assert args.status == "failed"
    assert args.sample_id == "external_support_tickets_triage"
    assert list_args.command == "external-sample-batch-reports"


def test_external_sample_batch_rerun_cli_parser_accepts_report_id_and_queue_options() -> None:
    failures = build_parser().parse_args(
        [
            "external-sample-batch-failures",
            "--workspace-root",
            ".agent-workspace",
            "--report-id",
            "external-samples-1",
        ]
    )
    plan = build_parser().parse_args(
        [
            "external-sample-batch-rerun-plan",
            "--workspace-root",
            ".agent-workspace",
            "--report-id",
            "external-samples-1",
            "--run-profile",
            "dry-run",
        ]
    )
    submit = build_parser().parse_args(
        [
            "external-sample-batch-rerun-submit",
            "--workspace-root",
            ".agent-workspace",
            "--report-id",
            "external-samples-1",
            "--priority",
            "4",
            "--max-retries",
            "2",
        ]
    )

    assert failures.command == "external-sample-batch-failures"
    assert failures.report_id == "external-samples-1"
    assert plan.command == "external-sample-batch-rerun-plan"
    assert submit.command == "external-sample-batch-rerun-submit"
    assert submit.priority == 4
    assert submit.max_retries == 2


def test_external_sample_rerun_submit_cli_parser_accepts_queue_options() -> None:
    args = build_parser().parse_args(
        [
            "external-sample-rerun-submit",
            "--workspace-root",
            ".agent-workspace",
            "--priority",
            "8",
            "--max-retries",
            "2",
        ]
    )

    assert args.command == "external-sample-rerun-submit"
    assert args.priority == 8
    assert args.max_retries == 2
