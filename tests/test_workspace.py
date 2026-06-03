import json
from pathlib import Path
from threading import Thread
from time import sleep as sleep_seconds

import pytest

from visual_agent.cli import main
from visual_agent.locks import RunLock
from visual_agent.gui import build_gui_action_plan, execute_gui_action, safe_execute_gui_action
from visual_agent.models import ActionStatus
from visual_agent.scheduler import submit_queue_task
from visual_agent.session import load_agent_session
from visual_agent.workspace import (
    build_workspace_risk_policy_template,
    build_workspace_risk_policy_apply_plan,
    discover_workflows,
    export_regression_fixture,
    export_workspace_run_report,
    find_workflow,
    init_workspace,
    list_regression_tests,
    list_workspace_reports,
    load_workspace_report_index,
    load_workspace_report_tags,
    load_workspace_inputs,
    planner_context,
    promote_regression_fixture,
    run_workspace_regression_tests,
    run_workspace_workflow,
    tag_workspace_report,
    validate_workflow_inputs,
    validate_workspace,
    validate_workspace_risk_policy,
    workspace_run_summaries,
    workspace_status,
)


def test_init_workspace_creates_dirs_and_demo(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    assert workspace.workflows_dir.exists()
    assert workspace.inputs_dir.exists()
    assert workspace.fixtures_dir.exists()
    assert (workspace.root / "workspace.json").exists()
    assert (workspace.inputs_dir / "miniprogram_default.json").exists()
    assert {workflow.name for workflow in discover_workflows(workspace)} == {
        "checkout_verification",
        "local_html_form_workflow",
        "miniprogram_simulator_capture",
        "miniprogram_visual_text_contract",
        "wechat_devtools_shell",
    }


def test_find_workflow_accepts_name_and_relative_path(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    by_name = find_workflow(workspace, "local_html_form_workflow")
    by_path = find_workflow(workspace, "workflows/local_html_form_workflow.yaml")

    assert by_name.path == by_path.path


def test_validate_workspace_accepts_demo(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    results = validate_workspace(workspace)

    assert len(results) == 5
    assert all(result.valid for result in results)


def test_load_workspace_inputs_reads_inputs_dir(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    inputs = load_workspace_inputs(workspace, None, "demo_login.json")

    assert inputs["username"] == "demo_user"


def test_run_workspace_workflow_writes_to_workspace_runs(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    inputs = load_workspace_inputs(workspace, None, "demo_login.json")

    result = run_workspace_workflow(workspace, "local_html_form_workflow", inputs=inputs, dry_run=True)

    assert result.run_dir.parent == workspace.runs_dir
    assert result.steps[-1].status == ActionStatus.DRY_RUN
    assert workspace_run_summaries(workspace)[0].run_id == result.run_id
    assert (workspace.reports_dir / f"{result.run_id}.json").exists()
    assert (workspace.reports_dir / f"{result.run_id}.md").exists()
    assert (workspace.reports_dir / "index.json").exists()


def test_validate_workflow_inputs_blocks_empty_sensitive_template(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    workflow = find_workflow(workspace, "local_html_form_workflow")
    from visual_agent.workflow import parse_workflow_file

    check = validate_workflow_inputs(
        parse_workflow_file(workflow.path),
        {"username": "demo_user", "password": ""},
    )

    assert check["ok"] is False
    assert check["empty_sensitive_inputs"][0]["path"] == "password"
    assert "Fill the inputs template" in check["message"]


def test_run_workspace_workflow_blocks_empty_sensitive_template(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    with pytest.raises(ValueError) as exc:
        run_workspace_workflow(
            workspace,
            "local_html_form_workflow",
            inputs={"username": "demo_user", "password": ""},
            dry_run=True,
        )

    assert "empty: password" in str(exc.value)


def test_workspace_run_cli_blocks_empty_sensitive_template(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    (workspace.inputs_dir / "empty_password.json").write_text(
        json.dumps({"username": "demo_user", "password": ""}),
        encoding="utf-8",
    )

    code = main(
        [
            "workspace-run",
            "--root",
            str(workspace.root),
            "--workflow",
            "local_html_form_workflow",
            "--inputs-file",
            "empty_password.json",
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 1
    assert payload["status"] == "blocked"
    assert payload["input_check"]["empty_sensitive_inputs"][0]["path"] == "password"


def test_run_workspace_workflow_can_queue_on_active_lock(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    inputs = load_workspace_inputs(workspace, None, "demo_login.json")
    lock = RunLock(workspace.runs_dir)
    lock.acquire(owner="external")

    def release_later() -> None:
        sleep_seconds(0.2)
        lock.release()

    releaser = Thread(target=release_later)
    releaser.start()
    try:
        result = run_workspace_workflow(
            workspace,
            "local_html_form_workflow",
            inputs=inputs,
            dry_run=True,
            queue_when_locked=True,
            lock_wait_seconds=1.0,
            lock_poll_seconds=0.05,
        )
    finally:
        releaser.join(timeout=1.0)

    assert result.run_queue is not None
    assert result.run_queue["attempts"] > 1


def test_export_workspace_run_report_can_be_called_explicitly(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    inputs = load_workspace_inputs(workspace, None, "demo_login.json")
    result = run_workspace_workflow(
        workspace,
        "local_html_form_workflow",
        inputs=inputs,
        dry_run=True,
        export_report=False,
    )

    exported = export_workspace_run_report(workspace, result.run_dir)
    reports = list_workspace_reports(workspace)
    index = load_workspace_report_index(workspace)

    assert exported.json_path is not None
    assert exported.markdown_path is not None
    assert exported.index_path is not None
    assert exported.json_path.exists()
    assert exported.markdown_path.exists()
    assert reports[0]["name"].startswith(result.run_id)
    assert all(report["name"] != "index.json" for report in reports)
    assert index["total_reports"] == 1
    assert index["entries"][0]["run_id"] == result.run_id
    assert index["entries"][0]["status"] == "success"


def test_workspace_report_index_filters_failed_reports(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    failing_workflow = workspace.workflows_dir / "failing_report.yaml"
    failing_workflow.write_text(
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

    result = run_workspace_workflow(workspace, "failing_report", dry_run=True, preflight=False)
    index = load_workspace_report_index(workspace, rebuild=True, failed_only=True)

    assert result.steps[-1].status == ActionStatus.FAILED
    assert index["total_reports"] == 1
    assert index["failed_reports"] == 1
    assert index["entries"][0]["workflow_name"] == "failing_report"
    assert index["entries"][0]["failed_step"] == "assert_missing"


def test_workspace_report_tags_are_merged_into_index(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    failing_workflow = workspace.workflows_dir / "failing_report.yaml"
    failing_workflow.write_text(
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
    result = run_workspace_workflow(workspace, "failing_report", dry_run=True, preflight=False)

    annotation = tag_workspace_report(
        workspace,
        result.run_id,
        review_status="needs_fix",
        tags=("selector", "assertion"),
        note="需要调整断言文本",
        regression_candidate=True,
    )
    tags = load_workspace_report_tags(workspace)
    index = load_workspace_report_index(workspace, rebuild=True, failed_only=True)

    assert annotation["review_status"] == "needs_fix"
    assert tags[result.run_id]["regression_candidate"] is True
    assert index["entries"][0]["annotation"]["note"] == "需要调整断言文本"
    assert index["entries"][0]["annotation"]["tags"] == ["assertion", "selector"]


def test_export_regression_fixture_creates_fixture_and_test_draft(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    failing_workflow = workspace.workflows_dir / "failing_report.yaml"
    failing_workflow.write_text(
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
    result = run_workspace_workflow(workspace, "failing_report", dry_run=True, preflight=False)

    exported = export_regression_fixture(workspace, result.run_id)
    tags = load_workspace_report_tags(workspace)
    index = load_workspace_report_index(workspace, rebuild=True, failed_only=True)

    assert exported.fixture_path.exists()
    assert exported.test_draft_path.exists()
    assert exported.manifest_path.exists()
    assert "observe_html" in exported.test_draft_path.read_text(encoding="utf-8") or "Failed step" in exported.test_draft_path.read_text(encoding="utf-8")
    assert tags[result.run_id]["review_status"] == "regression_ready"
    assert tags[result.run_id]["regression_candidate"] is True
    manifest = exported.manifest_path.read_text(encoding="utf-8")
    assert "regression_ready" in manifest
    assert index["entries"][0]["annotation"]["review_status"] == "regression_ready"


def test_promote_regression_fixture_creates_workspace_regression_test(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    failing_workflow = workspace.workflows_dir / "failing_report.yaml"
    failing_workflow.write_text(
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
    result = run_workspace_workflow(workspace, "failing_report", dry_run=True, preflight=False)
    export_regression_fixture(workspace, result.run_id)

    promoted = promote_regression_fixture(workspace, result.run_id)
    tests_index = list_regression_tests(workspace)
    tags = load_workspace_report_tags(workspace)

    assert promoted.test_path.exists()
    assert promoted.index_path.exists()
    assert tests_index["total_tests"] == 1
    assert tests_index["entries"][0]["name"].startswith("test_")
    assert "promoted" in tags[result.run_id]["tags"]


def test_run_workspace_regression_tests_writes_report(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    failing_workflow = workspace.workflows_dir / "failing_report.yaml"
    failing_workflow.write_text(
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
    result = run_workspace_workflow(workspace, "failing_report", dry_run=True, preflight=False)
    export_regression_fixture(workspace, result.run_id)
    promote_regression_fixture(workspace, result.run_id)

    regression_run = run_workspace_regression_tests(workspace, timeout_seconds=30)

    assert regression_run.status == "success"
    assert regression_run.exit_code == 0
    assert regression_run.report_path.exists()
    assert regression_run.markdown_path.exists()
    assert regression_run.passed_tests == 1


def test_workspace_status_reports_counts(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    status = workspace_status(workspace)

    assert status["workflow_count"] == 5
    assert status["report_count"] == 0
    assert status["regression_test_count"] == 0
    assert status["valid_workflows"] == 5
    assert status["invalid_workflows"] == 0


def test_planner_context_exposes_safe_workspace_summary(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    inputs = load_workspace_inputs(workspace, None, "demo_login.json")
    run_workspace_workflow(workspace, "local_html_form_workflow", inputs=inputs, dry_run=True)

    context = planner_context(workspace)
    capability_names = {capability["name"] for capability in context["capabilities"]}

    assert context["workspace"]["name"] == "agent-workspace"
    local_workflow = next(workflow for workflow in context["workflows"] if workflow["name"] == "local_html_form_workflow")
    assert local_workflow["valid"] is True
    assert context["inputs"][0]["name"] == "demo_login.json"
    assert "username" not in str(context["inputs"])
    assert "password" not in str(context["inputs"])
    assert context["recent_runs"][0]["workflow_name"] == "local_html_form_workflow"
    assert context["reports"]
    assert "regression_tests" in context
    assert "click" in capability_names
    assert "assert_response" in capability_names
    assert context["gui_action_history"]["risk_level"] == "ok"


def test_planner_context_exposes_gui_action_history_risks(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    context = planner_context(workspace)

    assert context["gui_action_history"]["risk_level"] == "warning"
    assert context["gui_action_history"]["error_events"] == 1
    assert context["gui_action_history"]["failed_actions"][0]["action"] == "cancel_queue_task"
    assert {warning["code"] for warning in context["gui_action_history"]["warnings"]} == {
        "gui_action_error_rate",
        "gui_action_failed_action",
    }


def test_planner_context_uses_workspace_gui_action_history_thresholds(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality"] = {
        "gui_action_history": {
            "error_rate_threshold": 0.9,
            "profiles": {
                "planner": {
                    "failed_action_limit": 0,
                }
            },
        }
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    task = submit_queue_task(workspace, "local_html_form_workflow")
    execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))
    safe_execute_gui_action(workspace, build_gui_action_plan("refresh_readiness"))
    safe_execute_gui_action(workspace, build_gui_action_plan("cancel_queue_task", task_id=task.task_id))

    context = planner_context(workspace)

    assert context["gui_action_history"]["risk_level"] == "ok"
    assert context["gui_action_history"]["policy"]["error_rate_threshold"] == 0.9
    assert context["gui_action_history"]["policy"]["failed_action_limit"] == 0
    assert context["gui_action_history"]["warnings"] == []


def test_workspace_risk_policy_template_is_copyable_quality_fragment() -> None:
    template = build_workspace_risk_policy_template()

    config = template["quality"]["gui_action_history"]
    assert config["error_rate_threshold"] == 0.25
    assert config["history_limit"] == 50
    assert config["failed_action_limit"] == 2
    assert config["profiles"]["ci"]["error_rate_threshold"] == 0.15
    assert config["profiles"]["ci"]["failed_action_limit"] == 1
    assert config["health"]["attention_trend_directions"] == ["worsening"]


def test_validate_workspace_risk_policy_accepts_template(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(build_workspace_risk_policy_template())
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    result = validate_workspace_risk_policy(workspace)

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["issues"] == []


def test_validate_workspace_risk_policy_reports_invalid_values(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality"] = {
        "gui_action_history": {
            "error_rate_threshold": 1.2,
            "history_limit": 0,
            "failed_action_limit": True,
            "profiles": {
                "ci": {"error_rate_threshold": "low"},
                "staging": {"failed_action_limit": 1},
            },
            "health": {"attention_trend_directions": ["worsening", "bad_direction", "worsening"]},
        }
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    result = validate_workspace_risk_policy(workspace)
    codes = {issue["code"] for issue in result["issues"]}
    paths = {issue["path"] for issue in result["issues"]}

    assert result["ok"] is False
    assert result["status"] == "error"
    assert "risk_policy_float_out_of_range" in codes
    assert "risk_policy_int_out_of_range" in codes
    assert "risk_policy_int_invalid" in codes
    assert "risk_policy_float_invalid" in codes
    assert "risk_policy_unknown_profile" in codes
    assert "risk_policy_attention_trend_unsupported" in codes
    assert "risk_policy_attention_trend_duplicate" in codes
    assert "quality.gui_action_history.profiles.ci.error_rate_threshold" in paths


def test_workspace_risk_policy_apply_plan_fills_missing_policy_without_writing(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    manifest_path = workspace.root / "workspace.json"
    original = json.loads(manifest_path.read_text(encoding="utf-8"))

    plan = build_workspace_risk_policy_apply_plan(workspace)
    after = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert plan["mode"] == "fill_missing"
    assert plan["applied"] is False
    assert plan["changed"] is True
    assert "quality.gui_action_history" in plan["changed_paths"]
    assert plan["patch"]["quality"]["gui_action_history"]["profiles"]["ci"]["failed_action_limit"] == 1
    assert plan["validation_after"]["status"] == "ok"
    assert after == original


def test_workspace_risk_policy_apply_plan_preserves_existing_values_by_default(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality"] = {"gui_action_history": {"error_rate_threshold": 0.9}}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    plan = build_workspace_risk_policy_apply_plan(workspace)

    assert plan["patch"]["quality"]["gui_action_history"]["error_rate_threshold"] == 0.9
    assert plan["patch"]["quality"]["gui_action_history"]["history_limit"] == 50


def test_workspace_risk_policy_apply_plan_can_apply_overwrite(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["quality"] = {"gui_action_history": {"error_rate_threshold": 0.9}}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    plan = build_workspace_risk_policy_apply_plan(workspace, overwrite=True, apply=True)
    updated = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert plan["mode"] == "overwrite"
    assert plan["applied"] is True
    assert updated["quality"]["gui_action_history"]["error_rate_threshold"] == 0.25
    assert updated["quality"]["gui_action_history"]["profiles"]["planner"]["history_limit"] == 50


def test_run_workspace_workflow_updates_session_file_on_pass(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = run_workspace_workflow(workspace, "local_html_form_workflow", inputs=load_workspace_inputs(workspace, None, "demo_login.json"))
    session = load_agent_session(workspace.root)

    assert result.workflow_name == "local_html_form_workflow"
    assert session is not None
    assert "local_html_form_workflow" in session.passing_workflows
    assert session.latest_failure is None


def test_run_workspace_workflow_updates_session_file_on_fail(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace", with_demo=False)
    (workspace.workflows_dir / "failure.yaml").write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str((Path(__file__).resolve().parent.parent / 'examples' / 'fixtures' / 'login_page_observation.json')).replace(chr(92), '/')}\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )

    result = run_workspace_workflow(workspace, "failure")
    session = load_agent_session(workspace.root)

    assert result.steps[-1].status == ActionStatus.FAILED
    assert session is not None
    assert "failure" in session.failing_workflows
    assert session.latest_failure is not None
    assert session.latest_failure.step_id == "assert_missing"


def test_run_workspace_workflow_does_not_raise_when_session_update_fails(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    def fail_update(*args, **kwargs):
        raise RuntimeError("session write failed")

    monkeypatch.setattr("visual_agent.session.update_agent_session", fail_update)

    result = run_workspace_workflow(workspace, "local_html_form_workflow", inputs=load_workspace_inputs(workspace, None, "demo_login.json"))

    assert result.workflow_name == "local_html_form_workflow"
