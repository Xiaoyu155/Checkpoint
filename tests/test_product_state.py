from visual_agent.models import Observation, ProviderKind
from visual_agent.product_state import (
    evaluate_ai_response_quality,
    evaluate_no_error_state,
    evaluate_product_contract,
    observation_to_state,
    product_contract_failure_message,
)


def test_observation_to_state_extracts_actions_inputs_and_error_signals() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        elements=(
            {"role": "button", "text": "购买服务"},
            {"role": "textbox", "placeholder": "手机号"},
            {"role": "alert", "text": "请求失败，请重试"},
            {"text": "加载中"},
        ),
        metadata={"title": "Checkout", "visible_text": ["会员权益", "退款说明"]},
    )

    state = observation_to_state(observation)

    assert state["title"] == "Checkout"
    assert state["buttons"] == ("购买服务",)
    assert state["inputs"] == ("手机号",)
    assert state["dialogs"] == ("请求失败，请重试",)
    assert state["errors"] == ("请求失败，请重试",)
    assert state["loading"] == ("加载中",)
    assert state["has_error"] is True


def test_evaluate_no_error_state_fails_on_failed_network_event() -> None:
    observation = Observation(provider=ProviderKind.DOM, source="page", elements=({"text": "正常页面"},))

    result = evaluate_no_error_state(observation, network_events=[{"url": "/api/pay", "status": 500, "method": "POST"}])

    assert result["passed"] is False
    assert result["failed_requests"][0]["status"] == 500


def test_product_contract_checks_required_actions_forbidden_entries_and_errors() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="page",
        elements=(
            {"role": "button", "text": "购买服务"},
            {"text": "会员权益"},
            {"text": "旧功能入口"},
        ),
    )

    result = evaluate_product_contract(
        observation,
        {
            "required_sections": ["会员权益", "退款说明"],
            "must_have_actions": ["购买服务"],
            "forbidden_entries": ["旧功能入口"],
            "no_error_state": True,
        },
    )

    assert result.passed is False
    assert result.missing_sections == ("退款说明",)
    assert result.missing_actions == ()
    assert result.forbidden_entries == ("旧功能入口",)
    assert "missing sections: 退款说明" in product_contract_failure_message(result)


def test_ai_response_quality_requires_relevance_and_specific_advice() -> None:
    result = evaluate_ai_response_quality(
        {
            "response": "建议先检查购买服务按钮，再确认支付接口状态。",
            "question": "购买服务无法支付怎么办",
            "require_specific_advice": True,
        }
    )

    assert result.passed is True
    assert "购买服务无法支付怎么办" not in result.issues
    assert result.question_references


def test_ai_response_quality_rejects_template_and_repetitive_output() -> None:
    result = evaluate_ai_response_quality(
        {
            "response": "很抱歉 很抱歉 很抱歉 很抱歉 很抱歉 很抱歉",
            "question": "如何修复登录失败",
            "require_answer_relevance": True,
        }
    )

    assert result.passed is False
    assert any("template" in issue for issue in result.issues)
    assert any("repetitive" in issue for issue in result.issues)
