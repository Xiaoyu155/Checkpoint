from __future__ import annotations

from visual_agent.product_state import classify_ai_source, evaluate_ai_response_quality


GOOD_TEXT = "根据您的需求，建议第一天游览西湖，第二天前往灵隐寺，整体安排可以按上午下午分段进行。"


def ai_event(source: str, url: str = "https://app.test/api/ai/plan") -> dict:
    return {"type": "response", "url": url, "status": 200, "ok": True, "method": "POST", "ai_source": source}


def test_classify_unknown_when_no_labels() -> None:
    result = classify_ai_source([{"type": "response", "url": "/api/data", "status": 200}])

    assert result["source"] == "unknown"
    assert result["labeled_event_count"] == 0


def test_classify_worst_source_wins() -> None:
    events = [ai_event("real"), ai_event("fallback"), ai_event("degraded")]

    assert classify_ai_source(events)["source"] == "fallback"
    assert classify_ai_source([ai_event("real"), ai_event("degraded")])["source"] == "degraded"
    assert classify_ai_source([ai_event("real")])["source"] == "real"


def test_classify_scopes_by_url() -> None:
    events = [ai_event("fallback", url="https://app.test/api/other"), ai_event("real", url="https://app.test/api/ai/plan")]

    assert classify_ai_source(events, url_contains="/api/ai/")["source"] == "real"
    assert classify_ai_source(events)["source"] == "fallback"


def test_quality_result_always_reports_ai_source() -> None:
    result = evaluate_ai_response_quality({"text": GOOD_TEXT}, network_events=[ai_event("degraded")])

    # without require_real_ai a degraded pass is allowed, but it is labeled
    assert result.passed is True
    assert result.ai_source == "degraded"


def test_require_real_ai_rejects_degraded_and_fallback() -> None:
    for source in ("degraded", "fallback"):
        result = evaluate_ai_response_quality(
            {"text": GOOD_TEXT, "require_real_ai": True},
            network_events=[ai_event(source)],
        )

        assert result.passed is False
        assert any(source in issue for issue in result.issues)


def test_require_real_ai_rejects_unknown() -> None:
    result = evaluate_ai_response_quality({"text": GOOD_TEXT, "require_real_ai": True}, network_events=[])

    assert result.passed is False
    assert any("x-ai-source" in issue for issue in result.issues)


def test_require_real_ai_passes_on_real_path() -> None:
    result = evaluate_ai_response_quality(
        {"text": GOOD_TEXT, "require_real_ai": True, "ai_url_contains": "/api/ai/"},
        network_events=[ai_event("real")],
    )

    assert result.passed is True
    assert result.ai_source == "real"
