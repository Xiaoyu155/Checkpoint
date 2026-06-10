import json
import pytest
from pathlib import Path
from threading import Thread
from time import sleep as sleep_seconds
from typing import Any

from PIL import Image

from visual_agent.dispatcher import ActionDispatcher
from visual_agent.locks import RunLock
from visual_agent.models import ActionResult, ActionStatus, Observation, ProviderKind, to_jsonable
from visual_agent.providers import ProviderRegistry
from visual_agent.run_profile import normalize_run_profile, policy_for_profile
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


def test_run_profile_semi_auto_policy_allows_medium_risk_actions() -> None:
    policy = policy_for_profile("semi-auto")

    assert normalize_run_profile("semi-auto") == "semi-auto"
    assert policy.force_dry_run is False
    assert policy.allow_low_and_medium_risk is True
    assert policy.allow_high_risk is False


def test_semi_auto_prompts_before_mutating_action(tmp_path, monkeypatch, capsys) -> None:
    prompts: list[str] = []

    def fake_input() -> str:
        prompts.append("prompted")
        return ""

    monkeypatch.setattr("builtins.input", fake_input)
    dispatcher = ActionDispatcher()
    dispatcher.register(
        "refresh_browser",
        lambda target, params, context: ActionResult(
            action="refresh_browser",
            status=ActionStatus.SUCCESS,
            target=target.target.display_name,
            message="refreshed",
        ),
    )
    runtime = WorkflowRuntime(output_dir=tmp_path, dispatcher=dispatcher)
    workflow = Workflow(
        name="semi_auto",
        version=1,
        steps=(WorkflowStep(id="refresh", action="refresh_browser"),),
    )

    result = runtime.run(workflow, run_profile="semi-auto")

    assert result.steps[0].status == ActionStatus.SUCCESS
    assert prompts == ["prompted"]
    assert "[semi-auto] About to execute: refresh_browser on step refresh" in capsys.readouterr().out


def test_workflow_from_dict_parses_affects() -> None:
    workflow = workflow_from_dict(
        {
            "name": "checkout",
            "version": 1,
            "affects": ["src/payment/", "templates/checkout.html"],
            "steps": [{"id": "observe", "action": "observe_screen"}],
        }
    )

    assert workflow.affects == ("src/payment/", "templates/checkout.html")


def test_workflow_from_dict_parses_variables_fixtures_and_preconditions() -> None:
    workflow = workflow_from_dict(
        {
            "name": "control-flow",
            "version": 1,
            "variables": {"greeting": "Hello"},
            "fixtures": ["auth_standard"],
            "preconditions": ["fixture:auth_standard", {"type": "workflow", "workflow": "child"}],
            "steps": [{"id": "observe", "action": "observe_screen"}],
        }
    )

    assert workflow.variables == {"greeting": "Hello"}
    assert workflow.fixtures == ("auth_standard",)
    assert len(workflow.preconditions) == 2
    assert workflow.preconditions[0] == "fixture:auth_standard"


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


def product_page_observation(params, context) -> Observation:
    return Observation(
        provider=ProviderKind.DOM,
        source="product",
        elements=(
            {"role": "button", "text": "购买服务"},
            {"role": "button", "text": "联系客服"},
            {"text": "首页 会员权益 退款说明"},
        ),
        metadata={"title": "会员页"},
    )


def error_page_observation(params, context) -> Observation:
    return Observation(provider=ProviderKind.DOM, source="product", elements=({"text": "请求失败，请稍后重试"},))


def profile_uid_observation(params, context) -> Observation:
    return Observation(
        provider=ProviderKind.DOM,
        source="profile",
        elements=(
            {"selector": "#profile .uid", "text": "UID-42"},
            {"selector": "#profile .status", "text": "Child ready"},
        ),
        metadata={"url": "https://example.test/profile"},
    )


def test_workflow_observe_state_returns_structured_page_state(tmp_path) -> None:
    registry = ProviderRegistry()
    registry.register("observe_fixture", product_page_observation)
    runtime = WorkflowRuntime(tmp_path, providers=registry)
    workflow = Workflow(
        name="state",
        version=1,
        steps=(
            WorkflowStep("observe", "observe_fixture", {}),
            WorkflowStep("state", "observe_state", {"max_text_items": 5}),
        ),
    )

    result = runtime.run(workflow, dry_run=True)

    assert result.steps[-1].status == ActionStatus.SUCCESS
    state = result.steps[-1].metadata["state"]
    assert state["title"] == "会员页"
    assert state["buttons"] == ("购买服务", "联系客服")
    assert state["primary_actions"] == ("购买服务", "联系客服")


def test_workflow_assert_product_contract_passes_required_sections_and_actions(tmp_path) -> None:
    registry = ProviderRegistry()
    registry.register("observe_fixture", product_page_observation)
    runtime = WorkflowRuntime(tmp_path, providers=registry)
    workflow = Workflow(
        name="contract",
        version=1,
        steps=(
            WorkflowStep("observe", "observe_fixture", {}),
            WorkflowStep(
                "contract",
                "assert_product_contract",
                {
                    "required_sections": ["会员权益", "退款说明"],
                    "must_have_actions": ["购买服务"],
                    "forbidden_entries": ["旧功能入口"],
                    "no_error_state": True,
                    "min_primary_actions": 1,
                },
            ),
        ),
    )

    result = runtime.run(workflow, dry_run=True)

    assert result.steps[-1].status == ActionStatus.SUCCESS
    assert result.steps[-1].metadata["product_contract"].passed is True


def test_workflow_assert_no_error_fails_on_visible_error_state(tmp_path) -> None:
    registry = ProviderRegistry()
    registry.register("observe_fixture", error_page_observation)
    runtime = WorkflowRuntime(tmp_path, providers=registry)
    workflow = Workflow(
        name="error",
        version=1,
        steps=(
            WorkflowStep("observe", "observe_fixture", {}),
            WorkflowStep("assert", "assert_no_error", {}),
        ),
    )

    result = runtime.run(workflow, dry_run=True)

    assert result.steps[-1].status == ActionStatus.FAILED
    assert "error state detected" in result.steps[-1].message


def test_workflow_assert_ai_response_quality_fails_template_answer(tmp_path) -> None:
    runtime = WorkflowRuntime(tmp_path)
    workflow = Workflow(
        name="ai-quality",
        version=1,
        steps=(
            WorkflowStep(
                "quality",
                "assert_ai_response_quality",
                {"response": "很抱歉 很抱歉 很抱歉 很抱歉 很抱歉 很抱歉", "question": "怎么购买服务"},
            ),
        ),
    )

    result = runtime.run(workflow, dry_run=True)

    assert result.steps[-1].status == ActionStatus.FAILED
    assert "AI response quality failed" in result.steps[-1].message


def test_workflow_runtime_supports_variables_branching_nested_workflow_and_preconditions(tmp_path) -> None:
    registry = ProviderRegistry()
    registry.register("observe_fixture", profile_uid_observation)
    (tmp_path / "fixtures").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workflows").mkdir(parents=True, exist_ok=True)
    (tmp_path / "fixtures" / "auth_standard.yaml").write_text(
        """
schema_version: 1
name: auth_standard
type: standard
page: /login
data:
  users: []
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "workflows" / "child.yaml").write_text(
        """
schema_version: 1
name: child
version: 1
steps:
  - id: observe_child
    action: observe_fixture
  - id: assert_child_ready
    action: assert_text
    text: Child ready
""".strip(),
        encoding="utf-8",
    )
    runtime = WorkflowRuntime(tmp_path, providers=registry)
    workflow = Workflow(
        name="parent",
        version=1,
        variables={"greeting": "Hello"},
        fixtures=("auth_standard",),
        preconditions=("fixture:auth_standard",),
        steps=(
            WorkflowStep("observe", "observe_fixture", {}),
            WorkflowStep("set_uid", "set_variable", {"name": "user_id", "from_text": "#profile .uid"}),
            WorkflowStep("branch", "if_text_exists", {"text": "UID-42", "then": "run_child", "else": "skip_failure"}),
            WorkflowStep("skip_failure", "assert_text", {"text": "should not run"}),
            WorkflowStep("run_child", "run_workflow", {"workflow": "child"}),
            WorkflowStep("final", "assert_text", {"text": "${user_id}"}),
        ),
    )

    result = runtime.run(workflow, dry_run=True, workspace_root=tmp_path)

    assert result.steps[0].action == "load_fixture"
    assert result.steps[1].action == "observe_fixture"
    assert result.steps[2].action == "set_variable"
    assert result.steps[2].metadata["value"] == "UID-42"
    assert result.steps[3].action == "if_text_exists"
    assert result.steps[3].metadata["jump_to"] == "run_child"
    assert all(step.id != "skip_failure" for step in result.steps)
    run_child = next(step for step in result.steps if step.action == "run_workflow")
    assert run_child.status == ActionStatus.SUCCESS
    assert run_child.metadata["nested_run"]["workflow_name"] == "child"
    assert result.steps[-1].status == ActionStatus.SUCCESS

    rerun = runtime.run(workflow, dry_run=True, workspace_root=tmp_path, from_step="run_child")

    assert rerun.steps[0].action == "load_fixture"
    assert rerun.steps[1].action == "run_workflow"
    assert rerun.steps[-1].action == "assert_text"
    assert all(step.action not in {"observe_fixture", "set_variable", "if_text_exists"} for step in rerun.steps[1:])


def test_workflow_request_api_dry_run_feeds_assert_response(tmp_path) -> None:
    runtime = WorkflowRuntime(tmp_path)
    workflow = Workflow(
        name="api-contract",
        version=1,
        steps=(
            WorkflowStep("api", "request_api", {"url": "https://example.test/api/orders", "method": "POST", "mock_status": 201}),
            WorkflowStep("assert", "assert_response", {"url_contains": "/api/orders", "method": "POST", "status": 201}),
        ),
    )

    result = runtime.run(workflow, dry_run=True)

    assert result.steps[0].status == ActionStatus.DRY_RUN
    assert result.steps[-1].status == ActionStatus.SUCCESS


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
    assert payload["workflow_schema_version"] == 1
    assert "run_lock" in payload
    assert not (tmp_path / "workflow.lock").exists()


def test_workflow_from_dict_missing_schema_version_is_upgraded() -> None:
    workflow = workflow_from_dict(
        {
            "name": "screen-demo",
            "steps": [{"id": "observe", "action": "observe_screen"}],
        }
    )

    assert workflow.schema_version == 1


def test_workflow_runtime_wraps_visual_steps_with_visual_lock(tmp_path, monkeypatch) -> None:
    calls = []
    providers = ProviderRegistry()

    class FakeVisualLock:
        def __enter__(self):
            calls.append("enter")
            return self

        def __exit__(self, exc_type, exc, traceback):
            calls.append("exit")

    def observe_uia(_params, _provider_context):
        calls.append("observe")
        return Observation(provider=ProviderKind.UIA, source="desktop")

    monkeypatch.setattr("visual_agent.workflow.VisualLock", FakeVisualLock)
    providers.register("observe_uia", observe_uia)
    workflow = workflow_from_dict({"name": "visual-lock", "steps": [{"id": "observe", "action": "observe_uia"}]})

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(workflow)

    assert result.steps[0].status == ActionStatus.SUCCESS
    assert calls == ["enter", "observe", "exit"]


def test_workflow_runtime_reuses_cached_observation_for_identical_observe_steps(tmp_path, monkeypatch) -> None:
    providers = ProviderRegistry()
    calls = {"count": 0}

    class FakeVisualLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr("visual_agent.workflow.VisualLock", FakeVisualLock)

    def observe_uia(params, _provider_context):
        calls["count"] += 1
        return Observation(
            provider=ProviderKind.UIA,
            source="fake-uia",
            elements=({"text": f"scan-{calls['count']}"},),
            metadata={"params": params},
        )

    providers.register("observe_uia", observe_uia)
    workflow = workflow_from_dict(
        {
            "name": "observe-cache",
            "steps": [
                {"id": "first", "action": "observe_uia", "window": {"title_contains": "Demo"}},
                {"id": "second", "action": "observe_uia", "window": {"title_contains": "Demo"}},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(workflow)

    assert calls["count"] == 1
    assert result.steps[0].metadata["observation_cache"] == "miss"
    assert result.steps[1].metadata["observation_cache"] == "hit"
    assert result.steps[1].observation is result.steps[0].observation


def test_workflow_runtime_invalidates_observation_cache_after_action(tmp_path, monkeypatch) -> None:
    providers = ProviderRegistry()
    dispatcher = ActionDispatcher()
    calls = {"count": 0}

    class FakeVisualLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr("visual_agent.workflow.VisualLock", FakeVisualLock)

    def observe_uia(params, _provider_context):
        calls["count"] += 1
        return Observation(
            provider=ProviderKind.UIA,
            source="fake-uia",
            elements=({"text": f"scan-{calls['count']}"},),
        )

    def fake_press_key(resolved, params, context):
        return ActionResult(
            action="press_key",
            status=ActionStatus.DRY_RUN if context.dry_run else ActionStatus.SUCCESS,
            target=resolved.target.display_name,
            provider=resolved.evidence.provider,
            message="fake press",
        )

    providers.register("observe_uia", observe_uia)
    dispatcher.register("press_key", fake_press_key)
    workflow = workflow_from_dict(
        {
            "name": "observe-cache-invalidated",
            "steps": [
                {"id": "before", "action": "observe_uia", "window": {"title_contains": "Demo"}},
                {"id": "press", "action": "press_key", "keys": "enter"},
                {"id": "after", "action": "observe_uia", "window": {"title_contains": "Demo"}},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers, dispatcher=dispatcher).run(
        workflow,
        run_profile="dry-run",
    )

    assert calls["count"] == 2
    assert result.steps[0].metadata["observation_cache"] == "miss"
    assert result.steps[2].metadata["observation_cache"] == "miss"
    assert result.steps[2].observation.elements[0]["text"] == "scan-2"


def test_workflow_runtime_observe_cache_can_be_disabled(tmp_path, monkeypatch) -> None:
    providers = ProviderRegistry()
    calls = {"count": 0}

    class FakeVisualLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr("visual_agent.workflow.VisualLock", FakeVisualLock)

    def observe_uia(params, _provider_context):
        calls["count"] += 1
        return Observation(provider=ProviderKind.UIA, source="fake-uia")

    providers.register("observe_uia", observe_uia)
    workflow = workflow_from_dict(
        {
            "name": "observe-cache-disabled",
            "steps": [
                {"id": "first", "action": "observe_uia", "cache": False},
                {"id": "second", "action": "observe_uia", "cache": False},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(workflow)

    assert calls["count"] == 2
    assert result.steps[0].metadata["observation_cache"] == "disabled"
    assert result.steps[1].metadata["observation_cache"] == "disabled"


def test_workflow_runtime_allows_press_key_without_target(tmp_path) -> None:
    dispatcher = ActionDispatcher()
    seen = {}

    def fake_press_key(resolved, params, context):
        seen["target"] = resolved.target.display_name
        seen["reason"] = resolved.evidence.reason
        return ActionResult(
            action="press_key",
            status=ActionStatus.DRY_RUN if context.dry_run else ActionStatus.SUCCESS,
            target=resolved.target.display_name,
            provider=resolved.evidence.provider,
            message="fake press",
        )

    dispatcher.register("press_key", fake_press_key)
    workflow = workflow_from_dict({"name": "press-key", "steps": [{"id": "press", "action": "press_key", "keys": "enter"}]})

    result = WorkflowRuntime(output_dir=tmp_path, dispatcher=dispatcher).run(workflow, run_profile="dry-run")

    assert result.steps[0].status == ActionStatus.DRY_RUN
    assert seen == {"target": "press_key", "reason": "global action does not require a target"}


class FakeBrowserLocator:
    def __init__(self, page):
        self.page = page

    def click(self):
        self.page.clicked = True
        self.page.text = "Dashboard Ready"

    def fill(self, value):
        self.page.text = str(value)


class FakeBrowserPage:
    url = "https://example.test/app"
    viewport_size = {"width": 1280, "height": 720}

    def __init__(self, *, text: str = "Login", elements: tuple[dict, ...] | None = None):
        self.text = text
        self.clicked = False
        self._elements = elements or ({"role": "button", "text": "Login", "selector": "#login", "bounds": {"left": 1, "top": 2, "width": 80, "height": 30}},)

    def evaluate(self, script, arg=None):
        if arg is not None:
            return list(self._elements if not self.clicked else ({"role": "button", "text": self.text, "selector": "#done", "bounds": {"left": 1, "top": 2, "width": 80, "height": 30}},))
        return self.text

    def title(self):
        return "Demo"

    def screenshot(self, *, path, full_page=True):
        Path(path).write_bytes(b"fake-png")

    def locator(self, selector):
        return FakeBrowserLocator(self)

    def wait_for_timeout(self, value):
        return None


def test_workflow_browser_action_auto_observes_after_click(tmp_path) -> None:
    providers = ProviderRegistry()

    def observe_browser(params, provider_context):
        page = FakeBrowserPage()
        provider_context.resources["playwright_page"] = page
        provider_context.resources["network_events"] = []
        provider_context.resources["console_events"] = []
        provider_context.resources["page_errors"] = []
        return Observation(
            provider=ProviderKind.DOM,
            source=page.url,
            elements=tuple(page.evaluate("collect", "selector")),
            metadata={"title": "Demo", "url": page.url, "visible_text": page.text, "visible_text_length": len(page.text), "interactive_count": 1},
        )

    providers.register("observe_browser", observe_browser)
    workflow = workflow_from_dict(
        {
            "name": "browser-click",
            "steps": [
                {"id": "observe", "action": "observe_browser", "url": "https://example.test/app"},
                {"id": "ready", "action": "assert_browser_ready", "min_text_length": 1, "min_interactive": 1},
                {"id": "click", "action": "click", "target": {"text": "Login", "role": "button"}},
                {"id": "assert", "action": "assert_text", "text": "Dashboard Ready"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(workflow, run_profile="supervised")

    assert [step.status for step in result.steps] == [ActionStatus.SUCCESS] * 4
    assert result.steps[2].metadata["browser_post_action_observe"]["status"] == "observed"
    assert result.steps[2].metadata["browser_post_action_observe"]["visible_text_length"] == len("Dashboard Ready")


def test_workflow_assert_browser_ready_fails_blank_page(tmp_path) -> None:
    providers = ProviderRegistry()
    providers.register(
        "observe_fixture",
        lambda _params, _context: Observation(
            provider=ProviderKind.DOM,
            source="about:blank",
            elements=(),
            metadata={"title": "", "url": "about:blank", "visible_text": "", "visible_text_length": 0, "interactive_count": 0},
        ),
    )
    workflow = workflow_from_dict(
        {
            "name": "blank-browser",
            "steps": [
                {"id": "observe", "action": "observe_fixture", "path": "unused"},
                {"id": "ready", "action": "assert_browser_ready", "min_text_length": 1},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(workflow, run_profile="dry-run")

    assert result.steps[-1].status == ActionStatus.FAILED
    assert "browser readiness failed" in result.steps[-1].message


def test_workflow_runtime_allows_refresh_browser_without_target(tmp_path) -> None:
    dispatcher = ActionDispatcher()
    seen = {}

    def fake_refresh(resolved, params, context):
        seen["target"] = resolved.target.display_name
        return ActionResult(
            action="refresh_browser",
            status=ActionStatus.DRY_RUN if context.dry_run else ActionStatus.SUCCESS,
            target=resolved.target.display_name,
            provider=resolved.evidence.provider,
            message="fake refresh",
        )

    dispatcher.register("refresh_browser", fake_refresh)
    workflow = workflow_from_dict({"name": "refresh", "steps": [{"id": "refresh", "action": "refresh_browser"}]})

    result = WorkflowRuntime(output_dir=tmp_path, dispatcher=dispatcher).run(workflow, run_profile="dry-run")

    assert result.steps[0].status == ActionStatus.DRY_RUN
    assert seen == {"target": "refresh_browser"}


def test_workflow_runtime_post_action_observe_asserts_text_after_action(tmp_path) -> None:
    dispatcher = ActionDispatcher()

    def fake_press_key(resolved, params, context):
        return ActionResult(
            action="press_key",
            status=ActionStatus.SUCCESS,
            target=resolved.target.display_name,
            provider=resolved.evidence.provider,
            message="fake press",
        )

    dispatcher.register("press_key", fake_press_key)
    workflow = workflow_from_dict(
        {
            "name": "post-action-observe",
            "steps": [
                {
                    "id": "submit",
                    "action": "press_key",
                    "keys": "enter",
                    "post_action_observe": {
                        "wait_seconds": 0,
                        "mock_text": "提交成功",
                        "assert_text": "提交成功",
                    },
                }
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, dispatcher=dispatcher).run(workflow, run_profile="supervised")

    step = result.steps[0]
    assert step.status == ActionStatus.SUCCESS
    assert step.metadata["post_action_observe"]["status"] == "observed"
    assert step.metadata["post_action_observe"]["assertion"] == "matched"
    assert step.metadata["post_action_observe"]["screenshot_path"].endswith("ocr-mock.png")


def test_workflow_runtime_post_action_observe_fails_when_assert_text_missing(tmp_path) -> None:
    dispatcher = ActionDispatcher()

    def fake_press_key(resolved, params, context):
        return ActionResult(
            action="press_key",
            status=ActionStatus.SUCCESS,
            target=resolved.target.display_name,
            provider=resolved.evidence.provider,
            message="fake press",
        )

    dispatcher.register("press_key", fake_press_key)
    workflow = workflow_from_dict(
        {
            "name": "post-action-observe-fail",
            "steps": [
                {
                    "id": "submit",
                    "action": "press_key",
                    "keys": "enter",
                    "post_action_observe": {
                        "wait_seconds": 0,
                        "mock_text": "仍在提交",
                        "assert_text": "提交成功",
                    },
                }
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, dispatcher=dispatcher).run(workflow, run_profile="supervised")

    assert result.steps[0].status == ActionStatus.FAILED
    assert "post_action_observe" in result.steps[0].message


def test_workflow_runtime_click_text_uses_ocr_bounds(tmp_path) -> None:
    workflow = workflow_from_dict(
        {
            "name": "click-text",
            "steps": [
                {
                    "id": "buy",
                    "action": "click_text",
                    "text": "购买服务",
                    "mock_text": "购买服务",
                    "mock_bounds": {"left": 20, "top": 30, "width": 100, "height": 40},
                },
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, run_profile="dry-run")

    step = result.steps[0]
    assert step.status == ActionStatus.DRY_RUN
    assert step.action_result is not None
    assert step.action_result.action == "click"
    assert step.action_result.point.x == 70
    assert step.action_result.point.y == 50


def test_workflow_runtime_wait_for_text_uses_ocr_bounds(tmp_path) -> None:
    workflow = workflow_from_dict(
        {
            "name": "wait-for-text",
            "steps": [
                {
                    "id": "wait",
                    "action": "wait_for_text",
                    "text": "支付成功",
                    "mock_text": "支付成功",
                    "mock_bounds": {"left": 10, "top": 10, "width": 80, "height": 30},
                    "timeout_seconds": 0.2,
                    "poll_seconds": 0.05,
                },
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, run_profile="dry-run")

    step = result.steps[0]
    assert step.status == ActionStatus.SUCCESS
    assert step.action_result is not None
    assert step.action_result.action == "wait_for_text"
    assert step.action_result.point.x == 50
    assert step.action_result.point.y == 25


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


def test_workflow_runtime_wait_for_url_uses_input_reference(tmp_path) -> None:
    providers = ProviderRegistry()

    def observe_browser(params, provider_context):
        provider_context.resources["playwright_page"] = type("Page", (), {"url": params["url"]})()
        return Observation(provider=ProviderKind.DOM, source=params["url"], metadata={"url": params["url"]})

    providers.register("observe_browser", observe_browser)
    workflow = workflow_from_dict(
        {
            "name": "url-fragment-from-input",
            "schema_version": 1,
            "steps": [
                {"id": "observe", "action": "observe_browser", "url_from": "input.url"},
                {"id": "wait_url", "action": "wait_for", "condition": "url", "url_contains_from": "input.fragment", "timeout_seconds": 0.1},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(
        workflow,
        inputs={"url": "https://example.test/dashboard?username=demo_user", "fragment": "demo_user"},
        dry_run=True,
    )

    assert result.steps[-1].status == ActionStatus.SUCCESS


def test_workflow_runtime_redacts_sensitive_input_values_from_results(tmp_path) -> None:
    providers = ProviderRegistry()

    def observe_browser(params, provider_context):
        provider_context.resources["playwright_page"] = type("Page", (), {"url": params["url"]})()
        return Observation(provider=ProviderKind.DOM, source=params["url"], metadata={"url": params["url"]})

    providers.register("observe_browser", observe_browser)
    workflow = workflow_from_dict(
        {
            "name": "redacted-url",
            "schema_version": 1,
            "steps": [
                {"id": "observe", "action": "observe_browser", "url": "https://example.test/callback?password=demo_password"},
                {"id": "wait_url", "action": "wait_for", "condition": "url", "url_contains_from": "input.password", "timeout_seconds": 0.1},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(
        workflow,
        inputs={"password": "demo_password"},
        sensitive_fields={"password"},
        dry_run=True,
    )
    raw = json.dumps(to_jsonable(result), ensure_ascii=False)
    step_text = (Path(result.run_dir) / "wait_url.json").read_text(encoding="utf-8")

    assert result.steps[-1].status == ActionStatus.SUCCESS
    assert "demo_password" not in raw
    assert "demo_password" not in step_text
    assert "[REDACTED]" in raw


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


def test_workflow_runtime_supports_click_visual_and_assert_visual_text(tmp_path, monkeypatch) -> None:
    class FakeLocator:
        def locate(self, image, image_path, target):
            return type("Location", (), {"x": 120, "y": 80, "confidence": 0.91, "reason": "fake match"})()

        def detect(self, image, image_path, target):
            return (
                {
                    "text": "Save button",
                    "label": "Save button",
                    "role": "button",
                    "confidence": 0.91,
                    "bounds": {"left": 100, "top": 60, "width": 40, "height": 40},
                },
            )

    monkeypatch.setattr("visual_agent.workflow.build_locator", lambda provider: FakeLocator())
    monkeypatch.setattr(
        "visual_agent.workflow.capture_visual_region",
        lambda params, output_dir, label, synthetic_on_capture_fail=False: (
            Image.new("RGB", (200, 200)),
            tmp_path / f"{label}.png",
            {"capture_label": label},
        ),
    )

    workflow = workflow_from_dict(
        {
            "name": "desktop_visual",
            "steps": [
                {"id": "click", "action": "click_visual", "description": "Save button", "provider": "omniparser"},
                {"id": "assert_text", "action": "assert_visual_text", "text": "Save button", "provider": "omniparser"},
            ],
        }
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)

    assert result.steps[0].action == "click_visual"
    assert result.steps[0].status == ActionStatus.DRY_RUN
    assert result.steps[1].action == "assert_visual_text"
    assert result.steps[1].status == ActionStatus.SUCCESS


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
    assert diagnosis["root_cause"] in {"assertion_wrong", "element_missing"}
    assert diagnosis["confidence"] > 0
    assert diagnosis["structured_failure"]["step_id"] == "wait_missing"
    assert diagnosis["structured_failure"]["suggested_fix"]


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
    assert diagnosis["structured_failure"]["root_cause"] in {"element_missing", "env_error", "assertion_wrong"}


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


def test_assert_text_contract_supports_required_any_all_and_forbidden(tmp_path) -> None:
    registry = ProviderRegistry()
    registry.register(
        "observe_contract",
        lambda _params, _context: Observation(
            provider=ProviderKind.OCR,
            source="screen.png",
            metadata={"engine_available": True},
            elements=(
                {"text": "录取数据", "role": "text", "confidence": 0.92},
                {"text": "填分数", "role": "text", "confidence": 0.88},
            ),
        ),
    )
    workflow = Workflow(
        name="text-contract",
        version=1,
        steps=(
            WorkflowStep("observe", "observe_contract", {}),
            WorkflowStep(
                "assert",
                "assert_text_contract",
                {
                    "required_all": ["录取数据"],
                    "required_any": ["填分数", "购买服务"],
                    "forbidden_any": ["终端输出"],
                },
            ),
        ),
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=registry).run(workflow, dry_run=True)

    assert result.steps[-1].status == ActionStatus.SUCCESS
    contract = result.steps[-1].metadata["text_contract"]
    assert contract["matched_required"] == ["录取数据"]
    assert contract["matched_any"] == ["填分数"]


def test_assert_text_contract_fails_on_forbidden_text(tmp_path) -> None:
    registry = ProviderRegistry()
    registry.register(
        "observe_contract",
        lambda _params, _context: Observation(
            provider=ProviderKind.OCR,
            source="screen.png",
            metadata={"engine_available": True},
            elements=({"text": "Terminal 录取数据", "role": "text", "confidence": 0.95},),
        ),
    )
    workflow = Workflow(
        name="text-contract-forbidden",
        version=1,
        steps=(
            WorkflowStep("observe", "observe_contract", {}),
            WorkflowStep("assert", "assert_text_contract", {"required_all": ["录取数据"], "forbidden_any": ["Terminal"]}),
        ),
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=registry).run(workflow, dry_run=True)

    assert result.steps[-1].status == ActionStatus.FAILED
    assert "forbidden text matched" in result.steps[-1].message


def test_assert_text_contract_filters_text_region(tmp_path) -> None:
    registry = ProviderRegistry()
    registry.register(
        "observe_contract",
        lambda _params, _context: Observation(
            provider=ProviderKind.OCR,
            source="screen.png",
            width=400,
            height=800,
            metadata={"engine_available": True},
            elements=(
                {"text": "终端里的录取数据", "role": "text", "confidence": 0.95, "bounds": {"left": 300, "top": 20, "width": 80, "height": 20}},
                {"text": "我遇到了什么", "role": "text", "confidence": 0.90, "bounds": {"left": 40, "top": 120, "width": 120, "height": 20}},
            ),
        ),
    )
    workflow = Workflow(
        name="text-contract-region",
        version=1,
        steps=(
            WorkflowStep("observe", "observe_contract", {}),
            WorkflowStep(
                "assert",
                "assert_text_contract",
                {
                    "required_all": ["我遇到了什么"],
                    "forbidden_any": ["终端"],
                    "text_region": {"left": 0, "top": 0, "width": 220, "height": 800},
                },
            ),
        ),
    )

    result = WorkflowRuntime(output_dir=tmp_path, providers=registry).run(workflow, dry_run=True)

    assert result.steps[-1].status == ActionStatus.SUCCESS


def test_assert_text_contract_matches_joined_ocr_fragments(tmp_path) -> None:
    registry = ProviderRegistry()
    registry.register(
        "observe_fragments",
        lambda _params, _context: Observation(
            provider=ProviderKind.OCR,
            source="screen.png",
            metadata={"engine_available": True},
            elements=tuple({"text": item, "role": "text", "confidence": 0.8} for item in ["我", "遇", "到", "了", "什", "么"]),
        ),
    )
    workflow = Workflow(
        name="text-contract-fragments",
        version=1,
        steps=(
            WorkflowStep("observe", "observe_fragments", {}),
            WorkflowStep("assert", "assert_text_contract", {"required_all": ["我遇到了什么"]}),
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


def test_soft_assert_continues_and_summarizes_failures(tmp_path: Path) -> None:
    fixture_path = tmp_path / "page_fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "provider": "dom",
                "source": "file:///page.html",
                "elements": [
                    {"selector": "#ok", "text": "Ready"},
                    {"selector": "p", "text": "Ready"},
                ],
                "metadata": {"url": "file:///page.html"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    workflow = Workflow(
        name="soft",
        version=1,
        steps=(
            WorkflowStep(id="observe", action="observe_fixture", params={"path": str(fixture_path)}),
            WorkflowStep(id="soft_url", action="assert_url_contains", params={"fragment": "missing-fragment", "soft_assert": True}),
            WorkflowStep(id="assert_ready", action="assert_text", params={"text": "Ready"}),
        ),
        schema_version=1,
        min_runtime_version="0.1.0",
    )

    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)

    assert result.steps[-1].action == "soft_assert_summary"
    assert any(step.id == "soft_url" and step.status == ActionStatus.FAILED for step in result.steps)
    assert any(step.id == "assert_ready" and step.status == ActionStatus.SUCCESS for step in result.steps)


def test_new_assertion_helpers_cover_element_url_count_attribute_and_overlap() -> None:
    runtime = WorkflowRuntime(output_dir=Path("runs"))

    class DummyLocator:
        def __init__(self, count: int, attrs: dict[str, Any]) -> None:
            self._count = count
            self._attrs = attrs

        def count(self) -> int:
            return self._count

        @property
        def first(self):  # type: ignore[override]
            return self

        def get_attribute(self, attr: str):
            return self._attrs.get(attr)

    class DummyPage:
        def __init__(self) -> None:
            self.url = "https://example.test/dashboard"

        def locator(self, selector: str) -> DummyLocator:
            counts = {"#submit": 2, ".item": 3}
            attrs = {"id": "submit", "disabled": "false"}
            return DummyLocator(counts.get(selector, 0), attrs)

        def evaluate(self, _script: str):
            return []

    page = DummyPage()
    context = WorkflowContext(
        run_id="run",
        run_dir=Path("runs"),
        resources={"playwright_page": page},
        observations={
            "observe": Observation(
                provider=ProviderKind.DOM,
                source=page.url,
                elements=(
                    {"selector": "#submit", "text": "Ready", "disabled": "false"},
                    {"selector": ".item", "text": "Item 1"},
                ),
            )
        },
    )

    assert runtime._assert_element_exists(WorkflowStep("exists", "assert_element_exists", {"selector": "#submit"}), context).status == ActionStatus.SUCCESS
    assert runtime._assert_url_contains(WorkflowStep("url", "assert_url_contains", {"fragment": "/dashboard"}), context).status == ActionStatus.SUCCESS
    assert runtime._assert_count(WorkflowStep("count", "assert_count", {"selector": ".item", "min": 1, "max": 5}), context).status == ActionStatus.SUCCESS
    assert runtime._assert_attribute(WorkflowStep("attr", "assert_attribute", {"selector": "#submit", "attr": "disabled", "value": "false"}), context).status == ActionStatus.SUCCESS
    assert runtime._assert_no_layout_overlap(WorkflowStep("overlap", "assert_no_layout_overlap", {}), context).status == ActionStatus.SUCCESS
