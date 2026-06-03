import json
import pytest
from pathlib import Path
from threading import Thread
from time import sleep as sleep_seconds

from visual_agent.dispatcher import ActionDispatcher
from visual_agent.locks import RunLock
from visual_agent.models import ActionResult, ActionStatus, Observation, ProviderKind
from visual_agent.providers import ProviderRegistry
from visual_agent.state import StateStore
from visual_agent.workflow import (
    Workflow,
    WorkflowRuntime,
    WorkflowStep,
    close_context_resources,
    file_metadata,
    find_network_response,
    network_assertion_label,
    normalize_extension,
    observation_contains_text,
    parse_workflow_file,
    retry_config,
    resolve_output_path,
    sanitize_filename,
    target_from_config,
    url_matches_condition,
    wait_condition_from_dict,
    wait_for_conditions,
    workflow_from_dict,
)
from visual_agent.dispatcher import read_path, resolve_step_value
from visual_agent.workflow_types import WorkflowContext


def test_workflow_from_dict_parses_steps() -> None:
    workflow = workflow_from_dict(
        {
            "name": "demo",
            "version": 1,
            "steps": [
                {"id": "observe", "action": "observe_screen"},
                {"id": "resolve", "action": "resolve", "target": "登录"},
            ],
        }
    )

    assert workflow.name == "demo"
    assert workflow.steps[1].params["target"] == "登录"


def test_url_matches_condition_returns_false_for_invalid_regex() -> None:
    assert url_matches_condition("https://example.test/orders", {"url_regex": "["}) is False


def test_url_matches_condition_returns_false_when_no_url_keys() -> None:
    assert url_matches_condition("https://example.test/orders", {"condition": "url"}) is False


def test_url_matches_condition_matches_url_contains() -> None:
    assert url_matches_condition("https://example.test/orders/123", {"url_contains": "/orders/"}) is True


def test_wait_for_conditions_list_form_parsed_correctly() -> None:
    conditions = wait_for_conditions(
        {
            "conditions": [
                {"condition": "text", "text": "Ready"},
                {"type": "url", "url_contains": "/checkout"},
            ],
            "timeout_seconds": 3,
            "match": "any",
            "observation": "observe",
        }
    )

    assert conditions == [
        {"condition": "text", "text": "Ready", "observation": "observe"},
        {"type": "url", "url_contains": "/checkout", "condition": "url", "observation": "observe"},
    ]


def test_wait_condition_from_dict_raises_on_missing_condition_field() -> None:
    with pytest.raises(ValueError, match="missing condition"):
        wait_condition_from_dict({"text": "Ready"}, {})


def test_close_context_resources_is_silent_on_no_close_method(tmp_path) -> None:
    context = WorkflowContext(run_id="run", run_dir=tmp_path, resources={"playwright_page": object()})

    close_context_resources(context)


def test_workflow_from_dict_parses_schema_metadata() -> None:
    workflow = workflow_from_dict(
        {
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "name": "demo",
            "version": 1,
            "steps": [{"id": "observe", "action": "observe_screen"}],
        }
    )

    assert workflow.schema_version == 1
    assert workflow.min_runtime_version == "0.1.0"


def test_target_from_config_parses_preferred_providers() -> None:
    target = target_from_config({"text": "登录", "preferred": ["mock"]})

    assert target.display_name == "登录"
    assert [provider.value for provider in target.preferred] == ["mock"]


def test_target_from_config_parses_structured_selector_fields() -> None:
    target = target_from_config(
        {
            "selector": "#submit",
            "test_id": "submit-order",
            "contains_text": "订单",
            "text_regex": r"order-\d+",
            "row_contains_text": "A1001",
            "row_text_regex": r"A100\d",
            "column_header": "操作",
            "column_text_regex": "操.*",
            "near_text": "客户名称",
            "near_contains_text": "A1001",
            "near_text_regex": r"客户\s+名称",
            "scope_role": "dialog",
            "scope_text": "编辑客户",
            "scope_contains_text": "编辑",
            "preferred": ["dom"],
        }
    )

    assert target.display_name == "#submit"
    assert target.selector == "#submit"
    assert target.test_id == "submit-order"
    assert target.contains_text == "订单"
    assert target.text_regex == r"order-\d+"
    assert target.row_contains_text == "A1001"
    assert target.row_text_regex == r"A100\d"
    assert target.column_header == "操作"
    assert target.column_text_regex == "操.*"
    assert target.near_text == "客户名称"
    assert target.near_contains_text == "A1001"
    assert target.near_text_regex == r"客户\s+名称"
    assert target.scope_role == "dialog"
    assert target.scope_text == "编辑客户"
    assert target.scope_contains_text == "编辑"


def test_workflow_runtime_runs_screen_resolve_click_dry_run(tmp_path) -> None:
    workflow = workflow_from_dict(
        {
            "name": "screen-demo",
            "steps": [
                {"id": "observe", "action": "observe_screen"},
                {"id": "resolve", "action": "resolve", "target": {"text": "登录", "preferred": ["mock"]}},
                {"id": "click", "action": "click"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(
        workflow,
        dry_run=True,
        synthetic_on_capture_fail=True,
    )

    payload = json.loads((result.run_dir / "workflow_result.json").read_text(encoding="utf-8"))

    assert len(result.steps) == 3
    assert result.steps[-1].status == ActionStatus.DRY_RUN
    assert (result.run_dir / "observe.json").exists()
    assert payload["steps"][-1]["action_result"]["status"] == "dry_run"
    assert payload["runtime_version"] == "0.1.0"
    assert payload["workflow_schema_version"] is None
    assert "run_lock" in payload
    assert not (tmp_path / "workflow.lock").exists()


def test_workflow_runtime_respects_active_run_lock(tmp_path) -> None:
    workflow = parse_workflow_file("examples/minimal_testable_workflow.yaml")
    lock = RunLock(tmp_path)
    lock.acquire(owner="external")

    try:
        WorkflowRuntime(output_dir=tmp_path).run(workflow)
    except RuntimeError as exc:
        assert "Run lock is active" in str(exc)
    else:
        raise AssertionError("Expected active run lock to block workflow.")
    finally:
        lock.release()


def test_workflow_runtime_queues_until_run_lock_released(tmp_path) -> None:
    workflow = parse_workflow_file("examples/minimal_testable_workflow.yaml")
    lock = RunLock(tmp_path)
    lock.acquire(owner="external")

    def release_later() -> None:
        sleep_seconds(0.05)
        lock.release()

    releaser = Thread(target=release_later)
    releaser.start()
    try:
        result = WorkflowRuntime(output_dir=tmp_path).run(
            workflow,
            queue_when_locked=True,
            lock_wait_seconds=1.0,
            lock_poll_seconds=0.01,
        )
    finally:
        releaser.join(timeout=1.0)

    assert result.run_queue is not None
    assert result.run_queue["enabled"] is True
    assert result.run_queue["attempts"] > 1


def test_workflow_run_profile_controls_action_dry_run(tmp_path) -> None:
    workflow = workflow_from_dict(
        {
            "name": "profile-demo",
            "steps": [
                {
                    "id": "observe",
                    "action": "observe_fixture",
                    "path": "examples/fixtures/login_page_observation.json",
                },
                {"id": "resolve", "action": "resolve", "target": {"text": "登录", "role": "button"}},
                {"id": "click", "action": "click"},
            ],
        }
    )
    dispatcher = ActionDispatcher()

    def fake_click(resolved, params, context):
        return ActionResult(
            action="click",
            status=ActionStatus.DRY_RUN if context.dry_run else ActionStatus.SUCCESS,
            target=resolved.target.display_name,
            provider=resolved.evidence.provider,
            message="fake",
        )

    dispatcher.register("click", fake_click)

    dry = WorkflowRuntime(output_dir=tmp_path / "dry", dispatcher=dispatcher).run(workflow, run_profile="dry-run")
    supervised = WorkflowRuntime(output_dir=tmp_path / "supervised", dispatcher=dispatcher).run(
        workflow,
        run_profile="supervised",
    )

    assert dry.steps[-1].status == ActionStatus.DRY_RUN
    assert dry.run_profile == "dry-run"
    assert supervised.steps[-1].status == ActionStatus.SUCCESS
    assert supervised.run_profile == "supervised"


def test_workflow_run_profile_blocks_high_risk_until_approved(tmp_path) -> None:
    providers = ProviderRegistry()

    class FakeBrowserContext:
        def __init__(self):
            self.called = False

        def storage_state(self, path):
            self.called = True
            Path(path).write_text("{}", encoding="utf-8")

    fake_context = FakeBrowserContext()

    def observe_browser(params, provider_context):
        assert provider_context.resources is not None
        provider_context.resources["playwright_context"] = fake_context
        return Observation(provider=ProviderKind.DOM, source="browser")

    providers.register("observe_browser", observe_browser)
    workflow = workflow_from_dict(
        {
            "name": "profile-high-risk",
            "steps": [
                {"id": "observe", "action": "observe_browser", "url": "https://example.test"},
                {"id": "save", "action": "save_storage_state", "path": "state.json"},
            ],
        }
    )

    dry = WorkflowRuntime(output_dir=tmp_path / "dry", providers=providers).run(workflow, run_profile="dry-run")
    supervised = WorkflowRuntime(output_dir=tmp_path / "supervised", providers=providers).run(
        workflow,
        run_profile="supervised",
    )
    approved_without_confirm = WorkflowRuntime(output_dir=tmp_path / "approved-no-confirm", providers=providers).run(
        workflow,
        run_profile="approved",
    )

    approved_workflow = workflow_from_dict(
        {
            "name": "profile-high-risk",
            "steps": [
                {"id": "observe", "action": "observe_browser", "url": "https://example.test"},
                {
                    "id": "save",
                    "action": "save_storage_state",
                    "path": "state.json",
                    "require_confirm": True,
                },
            ],
        }
    )
    approved = WorkflowRuntime(output_dir=tmp_path / "approved", providers=providers).run(
        approved_workflow,
        run_profile="approved",
    )

    assert dry.steps[-1].status == ActionStatus.DRY_RUN
    assert supervised.steps[-1].status == ActionStatus.FAILED
    assert "blocks high-risk action" in supervised.steps[-1].message
    assert approved_without_confirm.steps[-1].status == ActionStatus.FAILED
    assert "require_confirm" in approved_without_confirm.steps[-1].message
    assert approved.steps[-1].status == ActionStatus.SUCCESS
    assert fake_context.called is True


def test_workflow_runtime_runs_minimal_fixture_workflow(tmp_path) -> None:
    workflow = parse_workflow_file("examples/minimal_testable_workflow.yaml")

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)

    assert len(result.steps) == 4
    assert result.steps[0].observation is not None
    assert result.steps[2].resolved_target is not None
    assert result.steps[2].resolved_target.evidence.provider.value == "dom"
    assert result.steps[-1].status == ActionStatus.DRY_RUN


def test_workflow_runtime_resolves_readonly_probe_input_params(tmp_path) -> None:
    providers = ProviderRegistry()
    seen = {}

    def observe_browser(params, _provider_context):
        seen["url"] = params["url"]
        return Observation(
            provider=ProviderKind.DOM,
            source=params["url"],
            elements=({"text": "Readonly Probe is available"},),
        )

    providers.register("observe_browser", observe_browser)
    workflow = workflow_from_dict(
        {
            "name": "readonly-probe",
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "steps": [
                {"id": "observe", "action": "observe_browser", "url_from": "input.url"},
                {"id": "assert", "action": "assert_text", "text_from": "input.assert_text"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(
        workflow,
        inputs={"url": "https://readonly.sandbox.example.com/status", "assert_text": "Readonly Probe"},
        dry_run=True,
    )

    assert seen["url"] == "https://readonly.sandbox.example.com/status"
    assert result.steps[-1].status == ActionStatus.SUCCESS


def test_workflow_runtime_runs_minimal_form_workflow(tmp_path) -> None:
    workflow = parse_workflow_file("examples/minimal_form_workflow.yaml")

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)

    assert len(result.steps) == 5
    assert result.steps[2].action == "paste"
    assert result.steps[2].action_result is not None
    assert result.steps[2].action_result.metadata["text_preview"] == "dem***"
    assert result.steps[3].action_result is not None
    assert result.steps[3].resolved_target is not None
    assert result.steps[3].resolved_target.evidence.handle == "#password"
    assert result.steps[-1].status == ActionStatus.DRY_RUN


def test_workflow_runtime_runs_wait_retry_workflow(tmp_path) -> None:
    workflow = parse_workflow_file("examples/minimal_wait_retry_workflow.yaml")

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)

    assert len(result.steps) == 4
    assert result.steps[1].status == ActionStatus.SUCCESS
    assert result.steps[1].metadata["attempts"] == 1
    assert result.steps[2].resolved_target is not None
    assert result.steps[2].resolved_target.evidence.provider.value == "dom"
    assert result.steps[-1].metadata["run_attempts"] == 1
    assert result.steps[-1].metadata["retry_requested_attempts"] == 2
    assert result.steps[-1].metadata["retry_disabled"] is True


def test_workflow_runtime_retries_safe_observe_step(tmp_path) -> None:
    providers = ProviderRegistry()
    calls = {"count": 0}

    def flaky_observe(params, _provider_context):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary observe failure")
        return Observation(provider=ProviderKind.DOM, source="flaky", elements=({"text": "ready"},))

    providers.register("observe_flaky", flaky_observe)
    workflow = workflow_from_dict(
        {
            "name": "safe-retry-demo",
            "steps": [
                {"id": "observe", "action": "observe_flaky", "retry": {"count": 1, "delay_seconds": 0}},
                {"id": "assert", "action": "assert_text", "text": "ready"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(workflow, dry_run=True)

    assert calls["count"] == 2
    assert result.steps[0].status == ActionStatus.SUCCESS
    assert result.steps[0].metadata["run_attempt"] == 2
    assert result.steps[0].metadata["run_attempts"] == 2
    assert result.steps[0].metadata["retry_safe"] is True
    assert result.steps[0].metadata["retry_errors"][0]["error"].startswith("RuntimeError:")


def test_workflow_runtime_runs_local_html_form_workflow(tmp_path) -> None:
    workflow = parse_workflow_file("examples/local_html_form_workflow.yaml")

    result = WorkflowRuntime(output_dir=tmp_path).run(
        workflow,
        dry_run=True,
        inputs={"username": "demo_user", "password": "demo_password"},
    )

    assert len(result.steps) == 6
    assert result.steps[0].observation is not None
    assert result.steps[0].observation.metadata["provider"] == "html_file"
    assert result.steps[2].action_result is not None
    assert result.steps[2].resolved_target is not None
    assert result.steps[2].resolved_target.evidence.handle == "#username"
    assert result.steps[-1].status == ActionStatus.DRY_RUN


def test_workflow_runtime_runs_local_business_backend_workflow(tmp_path) -> None:
    workflow = parse_workflow_file("examples/local_business_backend_workflow.yaml")

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)
    by_id = {step.id: step for step in result.steps}

    assert len(result.steps) == 6
    assert by_id["fill_customer_filter"].resolved_target is not None
    assert by_id["fill_customer_filter"].resolved_target.evidence.handle == "#customer-filter"
    assert by_id["click_order_download"].resolved_target is not None
    assert by_id["click_order_download"].resolved_target.evidence.handle == '[data-testid="row-action"]'
    assert by_id["click_order_download"].resolved_target.evidence.metadata["element"]["row_index"] == 2
    assert by_id["click_order_download"].resolved_target.evidence.metadata["element"]["column_header"] == "下载"
    assert by_id["click_next_page"].resolved_target is not None
    assert by_id["click_next_page"].resolved_target.evidence.handle == "#next-page"
    assert by_id["dismiss_exception_dialog"].resolved_target is not None
    assert by_id["dismiss_exception_dialog"].resolved_target.evidence.handle == "#dismiss-error"
    assert result.steps[-1].status == ActionStatus.DRY_RUN


def test_workflow_runtime_runs_windows_notepad_demo_workflow(tmp_path) -> None:
    workflow = parse_workflow_file("examples/windows_notepad_demo_workflow.yaml")

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)
    by_id = {step.id: step for step in result.steps}

    assert len(result.steps) == 5
    assert by_id["fill_subject"].resolved_target is not None
    assert by_id["fill_subject"].resolved_target.evidence.provider == ProviderKind.UIA
    assert by_id["fill_subject"].resolved_target.evidence.handle == "subjectEdit"
    assert by_id["fill_body"].resolved_target is not None
    assert by_id["fill_body"].resolved_target.evidence.handle == "bodyEdit"
    assert by_id["click_save"].resolved_target is not None
    assert by_id["click_save"].resolved_target.evidence.handle == "saveButton"
    assert result.steps[-1].status == ActionStatus.DRY_RUN


def test_workflow_runtime_hashes_sensitive_field(tmp_path) -> None:
    workflow = parse_workflow_file("examples/local_html_form_workflow.yaml")

    result = WorkflowRuntime(output_dir=tmp_path).run(
        workflow,
        dry_run=True,
        inputs={"username": "demo_user", "password": "demo_password"},
    )

    password_step = result.steps[3]
    assert password_step.action_result is not None
    assert password_step.action_result.metadata["sensitive"] is True
    assert "text_length" not in password_step.action_result.metadata


def test_workflow_runtime_writes_checkpoint_state(tmp_path) -> None:
    workflow = parse_workflow_file("examples/minimal_testable_workflow.yaml")

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)
    state = StateStore(result.run_dir).load()

    assert state is not None
    assert state.workflow_name == "minimal_testable_dom_workflow"
    assert state.completed_steps[-1] == "click_login"


def test_workflow_runtime_resume_skips_completed_steps(tmp_path) -> None:
    workflow = parse_workflow_file("examples/minimal_testable_workflow.yaml")
    first = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)

    resumed = WorkflowRuntime(output_dir=tmp_path).run(
        workflow,
        dry_run=True,
        resume_from=first.run_dir,
    )

    assert resumed.run_dir == first.run_dir
    assert all(step.metadata.get("resumed") is True for step in resumed.steps)


def test_workflow_runtime_fails_when_input_missing(tmp_path) -> None:
    workflow = parse_workflow_file("examples/local_html_form_workflow.yaml")

    result = WorkflowRuntime(output_dir=tmp_path).run(
        workflow,
        dry_run=True,
        inputs={"username": "demo_user"},
    )

    assert result.steps[3].status == ActionStatus.FAILED
    assert "Input value not found: password" in result.steps[3].message


def test_workflow_wait_for_times_out(tmp_path) -> None:
    workflow = workflow_from_dict(
        {
            "name": "timeout-demo",
            "steps": [
                {
                    "id": "observe",
                    "action": "observe_fixture",
                    "path": "examples/fixtures/login_page_observation.json",
                },
                {
                    "id": "wait_missing",
                    "action": "wait_for",
                    "condition": "text",
                    "text": "不存在的文本",
                    "timeout_seconds": 0.01,
                    "interval_seconds": 0,
                },
                {"id": "never_runs", "action": "click"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow)

    assert len(result.steps) == 2
    assert result.steps[-1].status == ActionStatus.FAILED
    assert "wait_for timed out" in result.steps[-1].message
    diagnosis = result.steps[-1].metadata["failure_diagnosis"]
    assert diagnosis["expected"] == "expected wait_for text: 不存在的文本"
    assert diagnosis["observation"]["provider"] == "dom"
    assert diagnosis["dom_excerpt"]
    assert diagnosis["dom_excerpt"][0]["text"]
    assert "model_prompt" in diagnosis
    assert diagnosis["recovery_suggestions"]


def test_workflow_wait_for_combined_text_selector_and_url_conditions(tmp_path) -> None:
    workflow = workflow_from_dict(
        {
            "name": "wait-combined-demo",
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "examples/web/login_demo.html"},
                {
                    "id": "wait_combined",
                    "action": "wait_for",
                    "match": "all",
                    "conditions": [
                        {"condition": "text", "text": "登录"},
                        {"condition": "selector", "selector": "#login"},
                        {"condition": "url", "url_contains": "login_demo.html"},
                    ],
                    "timeout_seconds": 0.01,
                    "interval_seconds": 0,
                },
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)
    wait_step = result.steps[-1]

    assert wait_step.status == ActionStatus.SUCCESS
    assert wait_step.message == "wait_for all conditions matched"
    assert [item["condition"] for item in wait_step.metadata["conditions"]] == ["text", "selector", "url"]


def test_workflow_wait_for_response_condition_matches_network_events(tmp_path) -> None:
    context = WorkflowContext(
        run_id="run",
        run_dir=tmp_path,
        resources={
            "network_events": [
                {"type": "response", "url": "https://example.test/api/orders", "status": 201, "ok": True, "method": "POST"}
            ]
        },
    )
    step = WorkflowStep(
        "wait_response",
        "wait_for",
        {
            "conditions": [
                {"condition": "response", "url_contains": "/api/orders", "method": "POST", "status_min": 200, "status_max": 299}
            ],
            "timeout_seconds": 0.01,
            "interval_seconds": 0,
        },
    )

    result = WorkflowRuntime(output_dir=tmp_path)._wait_for(step, context)

    assert result.status == ActionStatus.SUCCESS
    assert result.metadata["conditions"][0]["event"]["status"] == 201


def test_workflow_failure_diagnosis_handles_missing_observation(tmp_path) -> None:
    workflow = workflow_from_dict(
        {
            "name": "failure-demo",
            "steps": [
                {"id": "bad_resolve", "action": "resolve", "target": "登录"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow)

    diagnosis = result.steps[0].metadata["failure_diagnosis"]
    assert diagnosis["actual"] == "no observation is available"
    assert diagnosis["observation"]["available"] is False
    assert "Add or fix an observe_* step" in diagnosis["recovery_suggestions"][0]
    assert diagnosis["evidence"]["ocr"]["available"] is False
    assert diagnosis["evidence"]["vision"]["available"] is False


def test_workflow_failure_diagnosis_runs_ocr_when_screenshot_exists(tmp_path) -> None:
    workflow = workflow_from_dict(
        {
            "name": "ocr-diagnosis-demo",
            "steps": [
                {
                    "id": "observe_ocr",
                    "action": "observe_ocr",
                    "mock_text": "当前页面",
                    "mock_bounds": {"left": 10, "top": 20, "width": 120, "height": 30},
                },
                {"id": "assert_missing", "action": "assert_text", "text": "不存在的文字"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow)

    diagnosis = result.steps[-1].metadata["failure_diagnosis"]
    evidence = diagnosis["evidence"]["ocr"]
    assert diagnosis["artifacts"]["screenshot"].endswith("ocr-mock.png")
    assert evidence["available"] is True
    assert evidence["source"].endswith("ocr-mock.png")
    assert "engine_available" in evidence
    assert diagnosis["evidence"]["vision"]["available"] is True
    assert diagnosis["evidence"]["vision"]["engine_available"] is False


def test_workflow_runtime_runs_vision_mock_workflow(tmp_path) -> None:
    workflow = workflow_from_dict(
        {
            "name": "vision-demo",
            "steps": [
                {
                    "id": "observe_vision",
                    "action": "observe_vision",
                    "prompt": "登录是否成功？",
                    "mock_description": "页面显示已登录状态",
                    "mock_status": "success",
                },
                {"id": "assert_status", "action": "assert_text", "text": "已登录"},
                {
                    "id": "resolve_status",
                    "action": "resolve",
                    "target": {"contains_text": "已登录", "preferred": ["vision", "mock"]},
                },
                {"id": "click_status_area", "action": "click"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)

    assert len(result.steps) == 4
    assert result.steps[0].observation is not None
    assert result.steps[0].observation.provider == ProviderKind.VISION
    assert result.steps[2].resolved_target is not None
    assert result.steps[2].resolved_target.evidence.provider == ProviderKind.VISION
    assert result.steps[-1].status == ActionStatus.DRY_RUN


def test_workflow_runtime_passes_screenshot_to_vision_step(tmp_path) -> None:
    workflow = workflow_from_dict(
        {
            "name": "vision-screenshot-demo",
            "steps": [
                {"id": "screen", "action": "observe_screen"},
                {
                    "id": "vision",
                    "action": "observe_vision",
                    "screenshot_from": "screen",
                    "mock_description": "screenshot shows dashboard",
                    "mock_status": "success",
                },
                {"id": "assert_vision", "action": "assert_text", "text": "dashboard"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(
        workflow,
        dry_run=True,
        synthetic_on_capture_fail=True,
    )
    screen = result.steps[0].observation
    vision = result.steps[1].observation

    assert screen is not None
    assert vision is not None
    assert result.steps[-1].status == ActionStatus.SUCCESS
    assert screen.screenshot_path == vision.screenshot_path
    assert vision.source == str(screen.screenshot_path)
    assert vision.metadata["description"] == "screenshot shows dashboard"


def test_workflow_resolves_nested_input_refs_with_default(tmp_path) -> None:
    workflow = Workflow(
        name="nested-input-default",
        version=1,
        steps=(
            WorkflowStep(
                "observe",
                "observe_ocr",
                {
                    "mock_text_from": "input.missing_text",
                    "mock_text_default": "填分数",
                    "mock_bounds": {"left_from": "input.left", "left_default": 12, "top": 0, "width": 100, "height": 20},
                },
            ),
            WorkflowStep("assert", "assert_text", {"text": "填分数"}),
        ),
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True, inputs={})

    assert result.steps[-1].status == ActionStatus.SUCCESS
    assert result.steps[0].observation.elements[0]["bounds"]["left"] == 12


def test_assert_text_reports_unavailable_ocr_engine(tmp_path) -> None:
    registry = ProviderRegistry()
    registry.register(
        "observe_ocr_unavailable",
        lambda _params, _context: Observation(
            provider=ProviderKind.OCR,
            source="screen.png",
            metadata={"engine_available": False, "install_hint": "Install Tesseract."},
        ),
    )
    workflow = Workflow(
        name="ocr-unavailable",
        version=1,
        steps=(
            WorkflowStep("observe", "observe_ocr_unavailable", {}),
            WorkflowStep("assert", "assert_text", {"text": "填分数"}),
        ),
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=registry).run(workflow, dry_run=True)

    assert result.steps[-1].status == ActionStatus.FAILED
    assert "OCR engine unavailable" in result.steps[-1].message
    assert result.steps[-1].metadata["failure_diagnosis"]["expected"] == "expected text: 填分数"


def test_assert_text_matches_joined_ocr_chinese_fragments(tmp_path) -> None:
    registry = ProviderRegistry()
    registry.register(
        "observe_ocr_fragments",
        lambda _params, _context: Observation(
            provider=ProviderKind.OCR,
            source="screen.png",
            metadata={"engine_available": True},
            elements=tuple({"text": item, "role": "text"} for item in ["我", "遇", "到", "了", "什", "么"]),
        ),
    )
    workflow = Workflow(
        name="ocr-fragments",
        version=1,
        steps=(
            WorkflowStep("observe", "observe_ocr_fragments", {}),
            WorkflowStep("assert", "assert_text", {"text": "我遇到了什么"}),
        ),
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=registry).run(workflow, dry_run=True)

    assert result.steps[-1].status == ActionStatus.SUCCESS


def test_workflow_runtime_stops_on_failed_step(tmp_path) -> None:
    workflow = workflow_from_dict(
        {
            "name": "failure-demo",
            "steps": [
                {"id": "bad_resolve", "action": "resolve", "target": "登录"},
                {"id": "never_runs", "action": "click"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow)

    assert len(result.steps) == 1
    assert result.steps[0].status == ActionStatus.FAILED


def test_action_target_existence_check_blocks_structured_miss_to_mock(tmp_path) -> None:
    workflow = workflow_from_dict(
        {
            "name": "target-existence-demo",
            "steps": [
                {
                    "id": "observe",
                    "action": "observe_html",
                    "path": "examples/web/login_demo.html",
                },
                {
                    "id": "click_missing",
                    "action": "click",
                    "target": {"text": "不存在的按钮", "preferred": ["dom", "mock"]},
                },
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)

    assert len(result.steps) == 2
    assert result.steps[-1].status == ActionStatus.FAILED
    assert "target existence check failed" in result.steps[-1].message
    assert "fallback_path=dom -> mock" in result.steps[-1].message
    assert result.steps[-1].metadata["failure_diagnosis"]["observation"]["provider"] == "dom"
    assert "登录" in result.steps[-1].metadata["failure_diagnosis"]["actual"]
    assert result.steps[-1].metadata["failure_diagnosis"]["selector_summary"]["target"]["text"] == "不存在的按钮"


def test_action_target_existence_check_allows_intentional_mock_target(tmp_path) -> None:
    workflow = workflow_from_dict(
        {
            "name": "target-existence-demo",
            "steps": [
                {
                    "id": "observe",
                    "action": "observe_html",
                    "path": "examples/web/login_demo.html",
                },
                {
                    "id": "click_missing",
                    "action": "click",
                    "target": {"text": "不存在的按钮", "preferred": ["dom", "mock"]},
                    "allow_mock_target": True,
                },
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)

    assert result.steps[-1].status == ActionStatus.DRY_RUN
    assert result.steps[-1].resolved_target is not None
    assert result.steps[-1].resolved_target.evidence.provider == ProviderKind.MOCK


def test_retry_config_accepts_integer_and_object() -> None:
    assert retry_config({"retry": 2}) == {"attempts": 3, "delay_seconds": 0.0}
    assert retry_config({"retry": {"count": 1, "delay_seconds": 0.5}}) == {
        "attempts": 2,
        "delay_seconds": 0.5,
    }


def test_read_path_reads_nested_input() -> None:
    assert read_path({"customer": {"name": "张三"}}, "customer.name") == "张三"


def test_resolve_step_value_reads_inputs() -> None:
    context = WorkflowContext(run_id="run", run_dir=".", inputs={"username": "demo_user"})

    assert resolve_step_value({"value_from": "input.username"}, context) == "demo_user"


def test_observation_contains_text_checks_elements() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        elements=(
            {
                "text": "保存成功",
                "role": "status",
            },
        ),
    )

    assert observation_contains_text(observation, "保存成功")


def test_find_network_response_matches_latest_success() -> None:
    events = [
        {"type": "response", "url": "https://example.test/api/orders", "status": 500, "ok": False, "method": "POST"},
        {"type": "response", "url": "https://example.test/api/orders", "status": 201, "ok": True, "method": "POST"},
    ]

    matched = find_network_response(
        events,
        {"url_contains": "/api/orders", "method": "POST", "status_min": 200, "status_max": 299, "ok": True},
    )

    assert matched is not None
    assert matched["status"] == 201


def test_find_network_response_ignores_failed_requests() -> None:
    events = [
        {"type": "request_failed", "url": "https://example.test/api/orders", "status": None, "ok": False, "method": "POST"}
    ]

    assert find_network_response(events, {"url_contains": "/api/orders"}) is None
    assert "url_contains=/api/orders" in network_assertion_label({"url_contains": "/api/orders"})


def test_file_metadata_and_filename_helpers(tmp_path) -> None:
    path = tmp_path / "orders.csv"
    path.write_text("id,total\nA1001,12.30\n", encoding="utf-8")

    metadata = file_metadata(path)

    assert metadata["filename"] == "orders.csv"
    assert metadata["extension"] == ".csv"
    assert metadata["size_bytes"] > 0
    assert sanitize_filename('bad:name?.csv') == "bad_name_.csv"
    assert normalize_extension("CSV") == ".csv"


def test_resolve_output_path_keeps_agent_auth_relative_to_project(tmp_path) -> None:
    assert resolve_output_path(".agent-auth/state.json", tmp_path) == Path(".agent-auth/state.json")
    assert resolve_output_path("state.json", tmp_path) == tmp_path / "state.json"


def test_browser_business_backend_workflow_runs_when_playwright_browser_available(tmp_path) -> None:
    pytest.importorskip("playwright")
    workflow = parse_workflow_file("examples/browser_business_backend_workflow.yaml")
    try:
        result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=False, run_profile="supervised")
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(str(exc))
        raise

    if result.steps and result.steps[0].status == ActionStatus.FAILED:
        message = result.steps[0].message
        if "Executable doesn't exist" in message or "playwright install" in message:
            pytest.skip(message)

    by_id = {step.id: step for step in result.steps}

    assert by_id["assert_process_response"].status == ActionStatus.SUCCESS
    assert by_id["assert_exception_dialog"].status == ActionStatus.SUCCESS
    assert by_id["assert_dialog_closed"].status == ActionStatus.SUCCESS
    assert by_id["assert_page_changed"].status == ActionStatus.SUCCESS
    assert by_id["download_exception_order"].status == ActionStatus.SUCCESS
    assert by_id["assert_downloaded_file"].status == ActionStatus.SUCCESS
