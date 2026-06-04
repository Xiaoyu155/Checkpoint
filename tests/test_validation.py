from visual_agent.validation import validate_workflow, validate_workflow_file
from visual_agent.workflow import workflow_from_dict


def test_validate_workflow_file_accepts_local_html_demo() -> None:
    result = validate_workflow_file("examples/local_html_form_workflow.yaml")

    assert result.valid
    assert result.issues == ()


def test_validate_workflow_rejects_missing_required_params() -> None:
    workflow = workflow_from_dict(
        {
            "name": "bad",
            "steps": [
                {"id": "observe", "action": "observe_html"},
                {"id": "fill", "action": "paste", "target": "用户名"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert not result.valid
    assert any("Missing required parameter: path" in issue.message for issue in result.issues)
    assert any("value or value_from" in issue.message for issue in result.issues)


def test_validate_workflow_rejects_action_without_target_or_resolve() -> None:
    workflow = workflow_from_dict({"name": "bad", "steps": [{"id": "click", "action": "click"}]})

    result = validate_workflow(workflow)

    assert not result.valid
    assert any("requires a target" in issue.message for issue in result.issues)


def test_validate_workflow_accepts_press_key_with_target_and_keys() -> None:
    workflow = workflow_from_dict(
        {
            "name": "press-key",
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "examples/web/login_demo.html"},
                {"id": "submit", "action": "press_key", "target": "登录", "keys": "enter"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid


def test_validate_workflow_accepts_press_key_without_target() -> None:
    workflow = workflow_from_dict(
        {
            "name": "press-key-global",
            "steps": [
                {"id": "submit", "action": "press_key", "keys": "enter"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid


def test_validate_workflow_accepts_press_key_key_alias() -> None:
    workflow = workflow_from_dict(
        {
            "name": "press-key-alias",
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "examples/web/login_demo.html"},
                {"id": "submit", "action": "press_key", "target": "登录", "key": "enter"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid


def test_validate_workflow_rejects_press_key_without_keys() -> None:
    workflow = workflow_from_dict(
        {
            "name": "press-key-bad",
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "examples/web/login_demo.html"},
                {"id": "submit", "action": "press_key", "target": "登录"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert not result.valid
    assert any("Missing required parameter: keys or key" in issue.message for issue in result.issues)


def test_validate_workflow_accepts_post_action_observe() -> None:
    workflow = workflow_from_dict(
        {
            "name": "post-action-observe",
            "steps": [
                {
                    "id": "submit",
                    "action": "press_key",
                    "keys": "enter",
                    "post_action_observe": {"wait_seconds": 0, "assert_text": "提交成功", "mock_text": "提交成功"},
                },
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid


def test_validate_workflow_rejects_invalid_post_action_observe() -> None:
    workflow = workflow_from_dict(
        {
            "name": "post-action-observe-bad",
            "steps": [
                {
                    "id": "submit",
                    "action": "press_key",
                    "keys": "enter",
                    "post_action_observe": {"wait_seconds": "later", "assert_text": 123},
                },
            ],
        }
    )

    result = validate_workflow(workflow)

    assert not result.valid
    assert any("post_action_observe.wait_seconds" in issue.message for issue in result.issues)
    assert any("post_action_observe.assert_text" in issue.message for issue in result.issues)


def test_validate_workflow_accepts_click_text_and_wait_for_text() -> None:
    workflow = workflow_from_dict(
        {
            "name": "ocr-actions",
            "steps": [
                {"id": "buy", "action": "click_text", "text": "购买服务", "mock_text": "购买服务"},
                {"id": "wait", "action": "wait_for_text", "contains_text": "支付", "mock_text": "支付成功"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid


def test_validate_workflow_accepts_product_state_and_contract_actions() -> None:
    workflow = workflow_from_dict(
        {
            "name": "product-contract",
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "examples/web/checkout_verification_demo.html"},
                {"id": "state", "action": "observe_state"},
                {
                    "id": "contract",
                    "action": "assert_product_contract",
                    "required_sections": ["Premium Widget"],
                    "must_have_actions": ["Proceed to Checkout"],
                    "no_error_state": True,
                },
                {"id": "no_error", "action": "assert_no_error"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid


def test_validate_workflow_rejects_empty_product_contract() -> None:
    workflow = workflow_from_dict(
        {
            "name": "bad-contract",
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "examples/web/checkout_verification_demo.html"},
                {"id": "contract", "action": "assert_product_contract"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert not result.valid
    assert any("assert_product_contract requires" in issue.message for issue in result.issues)


def test_validate_workflow_accepts_ai_quality_with_direct_text_without_observation_warning() -> None:
    workflow = workflow_from_dict(
        {
            "name": "ai-quality",
            "steps": [
                {
                    "id": "quality",
                    "action": "assert_ai_response_quality",
                    "response": "建议先确认登录状态，再重试购买服务。",
                    "question": "购买服务失败怎么办",
                }
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid
    assert not any(issue.level == "warning" and "previous observation" in issue.message for issue in result.issues)


def test_validate_workflow_accepts_request_api_and_rejects_bad_method() -> None:
    valid = validate_workflow(
        workflow_from_dict(
            {
                "name": "api",
                "steps": [
                    {"id": "api", "action": "request_api", "url": "https://example.test/health", "method": "GET"},
                    {"id": "assert", "action": "assert_response", "url_contains": "/health", "status": 200},
                ],
            }
        )
    )
    invalid = validate_workflow(
        workflow_from_dict(
            {
                "name": "api-bad",
                "steps": [{"id": "api", "action": "request_api", "url": "https://example.test/health", "method": "TRACE"}],
            }
        )
    )

    assert valid.valid
    assert not invalid.valid
    assert any("Unsupported request_api method" in issue.message for issue in invalid.issues)


def test_validate_workflow_rejects_text_actions_without_text() -> None:
    workflow = workflow_from_dict(
        {
            "name": "ocr-actions-bad",
            "steps": [
                {"id": "buy", "action": "click_text", "mock_text": "购买服务"},
                {"id": "wait", "action": "wait_for_text", "mock_text": "支付成功"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert not result.valid
    assert any("click_text requires" in issue.message for issue in result.issues)
    assert any("wait_for_text requires" in issue.message for issue in result.issues)


def test_validate_workflow_flags_invalid_wait_condition() -> None:
    workflow = workflow_from_dict(
        {
            "name": "bad",
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "examples/web/login_demo.html"},
                {"id": "wait", "action": "wait_for", "condition": "image"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert not result.valid
    assert any("Unsupported wait_for condition" in issue.message for issue in result.issues)


def test_validate_workflow_accepts_wait_for_condition_list() -> None:
    workflow = workflow_from_dict(
        {
            "name": "wait-list",
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "examples/web/login_demo.html"},
                {
                    "id": "wait",
                    "action": "wait_for",
                    "conditions": [
                        {"condition": "text", "text": "登录"},
                        {"condition": "selector", "selector": "#login"},
                        {"condition": "url", "url_contains": "login_demo.html"},
                    ],
                },
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid


def test_validate_workflow_rejects_invalid_wait_for_condition_list() -> None:
    workflow = workflow_from_dict(
        {
            "name": "wait-list-bad",
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "examples/web/login_demo.html"},
                {"id": "wait", "action": "wait_for", "conditions": [{"condition": "selector"}], "match": "sometimes"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert not result.valid
    assert any("selector requires selector" in issue.message for issue in result.issues)
    assert any("Unsupported wait_for match mode" in issue.message for issue in result.issues)


def test_validate_workflow_warns_retry_on_mutating_action() -> None:
    workflow = workflow_from_dict(
        {
            "name": "retry-warning",
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "examples/web/login_demo.html"},
                {"id": "click", "action": "click", "target": "登录", "retry": {"count": 1}},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid
    assert any("Automatic retry is disabled" in issue.message for issue in result.issues)


def test_validate_workflow_accepts_ocr_observation() -> None:
    workflow = workflow_from_dict(
        {
            "name": "ocr",
            "steps": [
                {"id": "observe", "action": "observe_ocr", "mock_text": "登录"},
                {"id": "assert", "action": "assert_text", "text": "登录"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid


def test_validate_workflow_accepts_text_contract_assertion() -> None:
    workflow = workflow_from_dict(
        {
            "name": "text-contract",
            "steps": [
                {"id": "observe", "action": "observe_ocr", "mock_text": "我遇到了什么"},
                {"id": "assert", "action": "assert_text_contract", "required_all": ["我遇到了什么"]},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid


def test_validate_workflow_rejects_empty_text_contract() -> None:
    workflow = workflow_from_dict(
        {
            "name": "empty-text-contract",
            "steps": [
                {"id": "observe", "action": "observe_ocr", "mock_text": "ready"},
                {"id": "assert", "action": "assert_text_contract"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid is False
    assert any(issue.message.startswith("assert_text_contract requires") for issue in result.issues)


def test_validate_workflow_accepts_vision_observation() -> None:
    workflow = workflow_from_dict(
        {
            "name": "vision",
            "steps": [
                {"id": "observe", "action": "observe_vision", "mock_description": "页面显示已登录"},
                {"id": "assert", "action": "assert_text", "text": "已登录"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid


def test_strict_validation_requires_assertion() -> None:
    workflow = workflow_from_dict(
        {
            "name": "strict-bad",
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "examples/web/login_demo.html"},
                {"id": "resolve", "action": "resolve", "target": "登录"},
            ],
        }
    )

    result = validate_workflow(workflow, strict=True)

    assert not result.valid
    assert any("verification assertion" in issue.message for issue in result.issues)


def test_strict_validation_requires_sensitive_flag_for_secret_inputs() -> None:
    workflow = workflow_from_dict(
        {
            "name": "strict-sensitive",
            "steps": [
                {"id": "observe", "action": "observe_html", "path": "examples/web/login_demo.html"},
                {"id": "fill_password", "action": "paste", "target": "密码", "value_from": "input.password"},
                {"id": "assert", "action": "assert_text", "text": "登录"},
            ],
        }
    )

    result = validate_workflow(workflow, strict=True)

    assert not result.valid
    assert any("sensitive: true" in issue.message for issue in result.issues)


def test_strict_validation_blocks_high_risk_without_confirm() -> None:
    workflow = workflow_from_dict(
        {
            "name": "strict-high-risk",
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "steps": [
                {"id": "observe", "action": "observe_browser", "url": "https://example.com"},
                {"id": "assert", "action": "assert_text", "text": "Example"},
                {"id": "save_auth", "action": "save_storage_state", "path": ".agent-auth/state.json"},
            ],
        }
    )

    result = validate_workflow(workflow, strict=True)

    assert not result.valid
    assert any("high-risk action" in issue.message for issue in result.issues)


def test_strict_validation_allows_high_risk_with_confirm() -> None:
    workflow = workflow_from_dict(
        {
            "name": "strict-high-risk",
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "steps": [
                {"id": "observe", "action": "observe_browser", "url": "https://example.com"},
                {"id": "assert", "action": "assert_text", "text": "Example"},
                {
                    "id": "save_auth",
                    "action": "save_storage_state",
                    "path": ".agent-auth/state.json",
                    "require_confirm": True,
                },
            ],
        }
    )

    result = validate_workflow(workflow, strict=True)

    assert result.valid


def test_observe_browser_reuse_page_does_not_require_url() -> None:
    workflow = workflow_from_dict(
        {
            "name": "browser-reuse",
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "steps": [
                {"id": "observe", "action": "observe_browser", "url": "https://example.com"},
                {"id": "reobserve", "action": "observe_browser", "reuse_page": True},
                {"id": "assert", "action": "assert_text", "text": "Example"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid


def test_validation_accepts_readonly_probe_input_references() -> None:
    workflow = workflow_from_dict(
        {
            "name": "readonly-probe",
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "steps": [
                {"id": "observe", "action": "observe_browser", "url_from": "input.url"},
                {"id": "assert", "action": "assert_text", "text_from": "input.assert_text"},
                {"id": "wait", "action": "wait_for", "condition": "text", "text_from": "input.assert_text"},
            ],
        }
    )

    result = validate_workflow(workflow)

    assert result.valid


def test_strict_validation_file_accepts_local_html_demo() -> None:
    workflow = workflow_from_dict(
        {
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "name": "strict-schema",
            "steps": [
                {
                    "id": "observe",
                    "action": "observe_fixture",
                    "path": "examples/fixtures/login_page_observation.json",
                },
                {"id": "assert", "action": "assert_text", "text": "客户管理系统"},
            ],
        }
    )

    result = validate_workflow(workflow, strict=True)

    assert result.valid


def test_validation_rejects_future_schema_version() -> None:
    workflow = workflow_from_dict(
        {
            "schema_version": 999,
            "name": "future",
            "steps": [{"id": "observe", "action": "observe_screen"}],
        }
    )

    result = validate_workflow(workflow)

    assert not result.valid
    assert any("Unsupported workflow schema_version" in issue.message for issue in result.issues)


def test_validation_rejects_future_runtime_requirement() -> None:
    workflow = workflow_from_dict(
        {
            "schema_version": 1,
            "min_runtime_version": "999.0.0",
            "name": "future-runtime",
            "steps": [{"id": "observe", "action": "observe_screen"}],
        }
    )

    result = validate_workflow(workflow)

    assert not result.valid
    assert any("requires runtime" in issue.message for issue in result.issues)
