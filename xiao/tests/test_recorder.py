from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from visual_agent.cli import main
from visual_agent.recorder import (
    BrowserRecordingError,
    archive_recording_failure,
    inferred_assert_text,
    compact_recorded_events,
    recorded_recovery_hints,
    recorded_auth_state_path,
    recorded_events_to_workflow,
    recorded_result_ok,
    recorded_result_to_dict,
    recorded_result_to_markdown,
    recording_sensitive_values,
    recorded_workflow_path,
    recording_failure_to_markdown,
    record_browser_session,
    save_recorded_workflow,
    preview_recorded_workflow,
    selector_quality_report,
    target_from_recorded_event,
)
from visual_agent.validation import validate_workflow
from visual_agent.workflow import workflow_from_dict, parse_workflow_file
from visual_agent.workspace import init_workspace
from visual_agent.scheduler import list_queue_tasks


def test_recorded_events_generate_valid_workflow_and_sensitive_input_template() -> None:
    workflow, inputs = recorded_events_to_workflow(
        [
            {
                "type": "click",
                "selector": "#login",
                "text": "Login",
                "role": "button",
            },
            {
                "type": "input",
                "selector": "input[name=\"password\"]",
                "text": "Password",
                "role": "textbox",
                "name": "password",
                "input_type": "password",
                "value": "secret-value",
            },
        ],
        workflow_name="recorded_login",
        initial_url="https://example.test/login",
        assert_text="Dashboard",
        save_auth_state="seller session",
    )

    result = validate_workflow(workflow_from_dict(workflow))
    text = json.dumps(workflow, ensure_ascii=False)

    assert result.valid is True
    assert workflow["steps"][0]["action"] == "observe_browser"
    assert workflow["steps"][1]["action"] == "click"
    assert workflow["steps"][2]["action"] == "type"
    assert workflow["steps"][2]["value_from"] == "input.password"
    assert workflow["steps"][2]["sensitive"] is True
    assert workflow["steps"][-2] == {"id": "assert_recorded_result", "action": "assert_text", "text": "Dashboard"}
    assert workflow["steps"][-1] == {
        "id": "save_recorded_auth_state",
        "action": "save_storage_state",
        "path": ".agent-auth/seller-session.json",
        "require_confirm": True,
    }
    assert inputs == {"password": ""}
    assert "secret-value" not in text


def test_save_recorded_workflow_writes_yaml_and_inputs(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = save_recorded_workflow(
        workspace,
        [
            {
                "type": "input",
                "selector": "#email",
                "text": "Email",
                "role": "textbox",
                "name": "email",
                "value": "demo@example.test",
            }
        ],
        save_as="recorded/login",
        initial_url="https://example.test/login",
        final_text="Welcome\nOrders",
    )

    workflow = parse_workflow_file(result.workflow_path)

    assert result.validation.valid is True
    assert result.workflow_path == workspace.workflows_dir / "recorded" / "login.yaml"
    assert result.inputs_path is None
    assert result.preflight is not None
    assert result.preflight.ok is True
    assert workflow.name == "recorded_login"
    assert workflow.steps[1].action == "type"
    assert workflow.steps[-1].action == "assert_text"


def test_recorded_events_can_disable_auto_assertion() -> None:
    workflow, _inputs = recorded_events_to_workflow(
        [{"type": "click", "selector": "#submit", "text": "Submit", "role": "button"}],
        workflow_name="no_assert",
        initial_url="https://example.test",
        final_text="Success",
        auto_assert=False,
    )

    assert all(step["action"] != "assert_text" for step in workflow["steps"])


def test_recorded_auth_state_path_stays_under_agent_auth() -> None:
    assert recorded_auth_state_path("seller session") == ".agent-auth/seller-session.json"

    with pytest.raises(ValueError):
        recorded_auth_state_path("../evil")


def test_target_from_recorded_event_prefers_test_id() -> None:
    target = target_from_recorded_event(
        {
            "selector": "body > div:nth-of-type(2) > button:nth-of-type(1)",
            "test_id": "submit-order",
            "text": "Submit",
            "role": "button",
        }
    )

    assert target == {"test_id": "submit-order", "role": "button"}


def test_target_from_recorded_event_prefers_role_text_over_brittle_selector() -> None:
    target = target_from_recorded_event(
        {
            "selector": "body > div:nth-of-type(2) > button:nth-of-type(1)",
            "text": "Submit order",
            "role": "button",
        }
    )

    assert target == {"role": "button", "contains_text": "Submit order"}


def test_target_from_recorded_event_uses_name_selector_for_inputs() -> None:
    target = target_from_recorded_event(
        {
            "selector": "body > form:nth-of-type(1) > input:nth-of-type(2)",
            "text": "Email",
            "role": "textbox",
            "tag": "input",
            "name": "email",
        }
    )

    assert target == {"selector": 'input[name="email"]', "role": "textbox"}


def test_selector_quality_report_scores_stable_and_fragile_targets() -> None:
    report = selector_quality_report(
        [
            {
                "type": "click",
                "selector": "body > div:nth-of-type(2) > button:nth-of-type(1)",
                "test_id": "submit-order",
                "text": "Submit",
                "role": "button",
            },
            {
                "type": "click",
                "selector": "body > main:nth-of-type(1) > button:nth-of-type(3)",
                "text": "",
                "role": "",
            },
            {
                "type": "click",
                "selector": "body > nav:nth-of-type(1) > a:nth-of-type(2)",
                "text": "Orders",
                "role": "link",
            },
        ]
    )

    assert report["total_targets"] == 3
    assert report["weakest_level"] == "fragile"
    assert report["entries"][0]["level"] == "excellent"
    assert report["entries"][1]["level"] == "fragile"
    assert report["entries"][2]["level"] == "ok"
    assert "data-testid" in report["entries"][1]["suggestion"]


def test_recorded_navigation_adds_reuse_page_observation() -> None:
    workflow, _inputs = recorded_events_to_workflow(
        [
            {"type": "navigate", "url": "https://example.test/start"},
            {"type": "click", "selector": "#orders", "text": "Orders", "role": "link", "url": "https://example.test/start"},
            {"type": "navigate", "url": "https://example.test/orders"},
            {"type": "input", "selector": "#query", "text": "Search", "role": "textbox", "value": "A100"},
        ],
        workflow_name="navigation_recording",
        initial_url="https://example.test/start",
        assert_text="Orders",
    )

    actions = [step["action"] for step in workflow["steps"]]
    navigation_step = next(step for step in workflow["steps"] if step["id"].startswith("observe_navigation"))

    assert actions == ["observe_browser", "click", "observe_browser", "type", "assert_text"]
    assert navigation_step["reuse_page"] is True
    assert "url" not in navigation_step


def test_compact_recorded_events_deduplicates_navigation() -> None:
    compacted = compact_recorded_events(
        [
            {"type": "navigate", "url": "https://example.test/a"},
            {"type": "navigate", "url": "https://example.test/a"},
            {"type": "navigate", "url": "https://example.test/b"},
        ]
    )

    assert compacted == [{"type": "navigate", "url": "https://example.test/b"}]


def test_inferred_assert_text_prefers_final_page_text_and_skips_sensitive() -> None:
    result = inferred_assert_text(
        [{"type": "click", "selector": "#submit", "text": "Submit", "role": "button"}],
        final_text="Password\nOrder completed\nOrder completed",
    )

    assert result == "Order completed"


def test_recorded_workflow_path_rejects_traversal(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    with pytest.raises(ValueError):
        recorded_workflow_path(workspace, "../outside")


def test_archive_recording_failure_writes_json_and_markdown(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    report = archive_recording_failure(
        workspace,
        url="https://example.test/login",
        save_as="recorded/login",
        error=RuntimeError("browser unavailable"),
        events=[{"type": "click", "selector": "#login", "text": "Login", "role": "button"}],
        options={"headed": False},
    )
    markdown = recording_failure_to_markdown(report)

    assert report["status"] == "failed"
    assert report["event_count"] == 1
    assert report["selector_report"]["total_targets"] == 1
    assert "browser unavailable" in Path(report["json_report"]).read_text(encoding="utf-8")
    assert Path(report["markdown_report"]).exists()
    assert markdown.startswith("# Browser Recording Failure")


def test_record_browser_session_archives_browser_start_failure(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    class FakeChromium:
        def launch(self, **_kwargs):
            raise RuntimeError("chromium missing")

    class FakePlaywright:
        chromium = FakeChromium()

    class FakeSyncPlaywright:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *_args):
            return False

    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: FakeSyncPlaywright()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    with pytest.raises(BrowserRecordingError) as exc:
        record_browser_session(
            workspace,
            url="https://example.test",
            save_as="recorded/failure",
            headed=False,
        )

    report = exc.value.failure_report
    assert report["error"]["message"] == "chromium missing"
    assert Path(report["json_report"]).exists()
    assert Path(report["markdown_report"]).exists()


def test_save_recorded_workflow_can_skip_preflight_check(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = save_recorded_workflow(
        workspace,
        [{"type": "click", "selector": "#submit", "text": "Submit", "role": "button"}],
        save_as="no_check",
        initial_url="https://example.test",
        check=False,
    )

    assert result.validation.valid is True
    assert result.preflight is None


def test_save_recorded_workflow_requires_overwrite_for_existing_file(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    save_recorded_workflow(
        workspace,
        [{"type": "click", "selector": "#first", "text": "First", "role": "button"}],
        save_as="recorded/existing",
        initial_url="https://example.test",
    )

    with pytest.raises(FileExistsError):
        save_recorded_workflow(
            workspace,
            [{"type": "click", "selector": "#second", "text": "Second", "role": "button"}],
            save_as="recorded/existing",
            initial_url="https://example.test",
        )

    result = save_recorded_workflow(
        workspace,
        [{"type": "click", "selector": "#second", "text": "Second", "role": "button"}],
        save_as="recorded/existing",
        initial_url="https://example.test",
        overwrite=True,
    )

    assert result.validation.valid is True
    assert "#second" in result.workflow_path.read_text(encoding="utf-8")


def test_save_recorded_workflow_can_run_preview(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    class Step:
        def __init__(self, id, action, status, message):
            self.id = id
            self.action = action
            self.status = status
            self.message = message

    class Status:
        value = "dry_run"

    class Result:
        run_id = "preview-run"
        run_dir = tmp_path / "agent-workspace" / "runs" / "preview-run"
        steps = (Step("observe_initial", "observe_browser", Status(), "ok"),)

    def fake_run(*_args, **_kwargs):
        return Result()

    monkeypatch.setattr("visual_agent.recorder.run_workspace_workflow", fake_run)

    result = save_recorded_workflow(
        workspace,
        [{"type": "click", "selector": "#submit", "text": "Submit", "role": "button"}],
        save_as="preview_recorded",
        initial_url="https://example.test",
        preview_run=True,
    )

    assert result.preview["ok"] is True
    assert result.preview["run_id"] == "preview-run"


def test_save_recorded_workflow_can_queue_recorded_dry_run(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = save_recorded_workflow(
        workspace,
        [{"type": "click", "selector": "#submit", "text": "Submit", "role": "button"}],
        save_as="recorded/queued",
        initial_url="https://example.test",
        queue_run=True,
        queue_priority=7,
        queue_max_retries=2,
    )
    payload = recorded_result_to_dict(result)
    queue = list_queue_tasks(workspace)

    assert result.queue_status == "submitted"
    assert result.queue_task["task_id"] == queue["entries"][0]["task_id"]
    assert queue["entries"][0]["workflow"] == "workflows/recorded/queued.yaml"
    assert queue["entries"][0]["run_profile"] == "dry-run"
    assert queue["entries"][0]["priority"] == 7
    assert queue["entries"][0]["max_retries"] == 2
    assert payload["queue"]["status"] == "submitted"


def test_save_recorded_workflow_blocks_queue_until_sensitive_inputs_are_filled(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = save_recorded_workflow(
        workspace,
        [
            {
                "type": "input",
                "selector": "input[name=\"password\"]",
                "text": "Password",
                "role": "textbox",
                "name": "password",
                "input_type": "password",
                "value": "secret-value",
            }
        ],
        save_as="recorded/blocked_queue",
        initial_url="https://example.test/login",
        queue_run=True,
    )

    assert result.queue_status == "blocked"
    assert result.queue_task is None
    assert "Fill the generated inputs template" in result.queue_message
    assert list_queue_tasks(workspace)["total_tasks"] == 0


def test_save_recorded_workflow_skips_preview_until_sensitive_inputs_are_filled(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("visual_agent.recorder.run_workspace_workflow", fake_run)

    result = save_recorded_workflow(
        workspace,
        [
            {
                "type": "input",
                "selector": "input[name=\"password\"]",
                "text": "Password",
                "role": "textbox",
                "name": "password",
                "input_type": "password",
                "value": "secret-value",
            }
        ],
        save_as="recorded/sensitive_preview",
        initial_url="https://example.test/login",
        preview_run=True,
    )
    payload = recorded_result_to_dict(result)

    assert called is False
    assert result.preview["status"] == "skipped"
    assert result.preview["reason"] == "input_template_has_empty_values"
    assert result.input_keys == ("password",)
    assert result.empty_input_keys == ("password",)
    assert payload["input_keys"] == ["password"]
    assert payload["empty_input_keys"] == ["password"]
    assert payload["recovery_hints"] == [
        "Fill the generated inputs template before running the recorded workflow with real credentials."
    ]
    assert payload["selector_report"]["entries"][0]["level"] == "good"
    assert payload["suggested_run"]["workflow"] == "workflows/recorded/sensitive_preview.yaml"
    assert payload["suggested_run"]["inputs_file"] == "sensitive_preview_inputs.json"
    assert payload["save"]["path"] == "workflows/recorded/sensitive_preview.yaml"
    assert "--- /dev/null" in payload["save"]["diff"]
    assert "sensitive_preview" in payload["save"]["diff"]


def test_recording_second_pass_redacts_sensitive_values_from_saved_outputs(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    secret = "super-secret-recorded-value"

    result = save_recorded_workflow(
        workspace,
        [
            {
                "type": "input",
                "selector": "input[name=\"password\"]",
                "text": f"Password {secret}",
                "role": "textbox",
                "name": "password",
                "input_type": "password",
                "value": secret,
            }
        ],
        save_as="recorded/second_pass",
        initial_url="https://example.test/login",
    )
    payload = recorded_result_to_dict(result)
    markdown = recorded_result_to_markdown(payload)

    assert recording_sensitive_values([{"type": "input", "name": "password", "value": secret}]) == (secret,)
    assert secret not in result.workflow_path.read_text(encoding="utf-8")
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert secret not in markdown
    assert "[REDACTED]" in json.dumps(payload, ensure_ascii=False)


def test_recording_failure_archive_redacts_sensitive_events_and_errors(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    secret = "sk-recordedsecretvalue123456"

    report = archive_recording_failure(
        workspace,
        url="https://example.test/login",
        save_as="recorded/failure_secret",
        error=RuntimeError(f"browser failed with token={secret}"),
        events=[{"type": "click", "selector": "#token", "text": f"Token {secret}", "role": "button"}],
        options={"api_key": secret},
    )
    markdown = Path(report["markdown_report"]).read_text(encoding="utf-8")
    json_report = Path(report["json_report"]).read_text(encoding="utf-8")

    assert secret not in json_report
    assert secret not in markdown
    assert "[REDACTED]" in json_report


def test_preview_recorded_workflow_reports_errors(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    workflow_path = workspace.workflows_dir / "broken.yaml"
    workflow_path.write_text("schema_version: 1\nname: broken\nversion: 1\nsteps:\n- id: observe\n  action: observe_browser\n  url: https://example.test\n", encoding="utf-8")

    def fake_run(*_args, **_kwargs):
        raise RuntimeError("browser unavailable")

    monkeypatch.setattr("visual_agent.recorder.run_workspace_workflow", fake_run)

    preview = preview_recorded_workflow(workspace, workflow_path, inputs={})

    assert preview["ok"] is False
    assert preview["status"] == "error"
    assert preview["error"]["message"] == "browser unavailable"
    assert "Review the generated workflow selectors" in preview["recovery_hint"]


def test_recorded_recovery_hints_include_preflight_and_preview_failures() -> None:
    hints = recorded_recovery_hints(
        {
            "empty_input_keys": [],
            "validation": {"valid": True, "issues": []},
            "preflight": {
                "ok": False,
                "missing_required_capabilities": ["observe_browser"],
                "unavailable_used_capabilities": ["click"],
            },
            "preview": {"ok": False, "failed_step": "click_1"},
        }
    )

    assert hints[0] == "Install or enable the missing browser capabilities before previewing: observe_browser, click."
    assert hints[1] == "Inspect and adjust the selector or assertion for preview step `click_1`."


def test_recorded_result_ok_requires_validation_preflight_and_preview_success() -> None:
    assert recorded_result_ok({"validation": {"valid": True}, "preflight": None, "preview": None}) is True
    assert recorded_result_ok({"validation": {"valid": False}, "preflight": None, "preview": None}) is False
    assert recorded_result_ok({"validation": {"valid": True}, "preflight": {"ok": False}, "preview": None}) is False
    assert recorded_result_ok({"validation": {"valid": True}, "preflight": {"ok": True}, "preview": {"ok": False}}) is False
    assert recorded_result_ok({"validation": {"valid": True}, "preflight": {"ok": True}, "preview": {"ok": True, "status": "skipped"}}) is True


def test_workspace_record_browser_cli_uses_recorder_result(tmp_path, monkeypatch, capsys) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    saved = save_recorded_workflow(
        workspace,
        [{"type": "click", "selector": "#submit", "text": "Submit", "role": "button"}],
        save_as="cli_recorded",
        initial_url="https://example.test",
    )

    def fake_record(*_args, **kwargs):
        assert kwargs["queue_run"] is True
        assert kwargs["queue_priority"] == 4
        assert kwargs["queue_max_retries"] == 1
        return saved

    monkeypatch.setattr("visual_agent.cli.record_browser_session", fake_record)

    code = main(
        [
            "workspace-record-browser",
            "--root",
            str(workspace.root),
            "--url",
            "https://example.test",
            "--save-as",
            "cli_recorded",
            "--headless",
            "--assert-text",
            "Done",
            "--no-check",
            "--preview-run",
            "--overwrite",
            "--queue",
            "--queue-priority",
            "4",
            "--queue-max-retries",
            "1",
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 0
    assert payload["validation"]["valid"] is True
    assert "preflight" in payload
    assert payload["workflow_path"].endswith("cli_recorded.yaml")
    assert payload["queue"]["status"] == "not_requested"


def test_workspace_record_browser_cli_returns_failure_for_preview_failure(tmp_path, monkeypatch, capsys) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    saved = save_recorded_workflow(
        workspace,
        [{"type": "click", "selector": "#submit", "text": "Submit", "role": "button"}],
        save_as="cli_preview_failed",
        initial_url="https://example.test",
    )

    from dataclasses import replace

    def fake_record(*_args, **_kwargs):
        return replace(saved, preview={"status": "failed", "ok": False, "failed_step": "click_1"})

    monkeypatch.setattr("visual_agent.cli.record_browser_session", fake_record)

    code = main(
        [
            "workspace-record-browser",
            "--root",
            str(workspace.root),
            "--url",
            "https://example.test",
            "--save-as",
            "cli_preview_failed",
            "--headless",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 1
    assert output.startswith("# Browser Recording")
    assert "- Status: `blocked`" in output
    assert "Failed step: `click_1`" in output


def test_workspace_record_browser_cli_outputs_archived_failure(tmp_path, monkeypatch, capsys) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    report = archive_recording_failure(
        workspace,
        url="https://example.test",
        save_as="recorded/cli_failure",
        error=RuntimeError("browser unavailable"),
    )

    def fake_record(*_args, **_kwargs):
        raise BrowserRecordingError("Browser recording failed: browser unavailable", report)

    monkeypatch.setattr("visual_agent.cli.record_browser_session", fake_record)

    code = main(
        [
            "workspace-record-browser",
            "--root",
            str(workspace.root),
            "--url",
            "https://example.test",
            "--save-as",
            "recorded/cli_failure",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 1
    assert output.startswith("# Browser Recording Failure")
    assert "browser unavailable" in output
    assert Path(report["json_report"]).exists()


def test_recorded_result_to_markdown_marks_success() -> None:
    markdown = recorded_result_to_markdown(
        {
            "workflow_path": "workflows/login.yaml",
            "inputs_path": None,
            "event_count": 1,
            "validation": {"valid": True, "issues": []},
            "preflight": {"ok": True},
            "preview": {"ok": True, "status": "success", "run_id": "run-1"},
            "selector_report": {
                "total_targets": 1,
                "weakest_level": "fragile",
                "entries": [
                    {
                        "step_id": "click_1",
                        "level": "fragile",
                        "score": 20,
                        "target": {"selector": "body > button:nth-of-type(1)"},
                        "suggestion": "Add data-testid.",
                    }
                ],
            },
            "save": {
                "status": "saved",
                "path": "workflows/login.yaml",
                "diff": "--- /dev/null\n+++ b/workflows/login.yaml\n+name: login",
            },
            "queue": {"status": "submitted", "message": "Queued recorded workflow: task-1.", "task": {"task_id": "task-1", "workflow": "workflows/login.yaml"}},
        }
    )

    assert "- Status: `success`" in markdown
    assert "## Selector Quality" in markdown
    assert "`click_1`" in markdown
    assert "## Queue" in markdown
    assert "task-1" in markdown
    assert "## Save Diff" in markdown
    assert "+++ b/workflows/login.yaml" in markdown
