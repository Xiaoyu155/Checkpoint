from visual_agent.models import Observation, ProviderKind, Target
from visual_agent.selector import OCRSelectorStrategy, SelectorResolver, VisionSelectorStrategy


def test_selector_resolver_uses_mock_as_fallback() -> None:
    observation = Observation(
        provider=ProviderKind.SCREEN,
        source="test",
        width=1280,
        height=720,
    )

    resolved = SelectorResolver().resolve(Target.from_text("登录"), observation)

    assert resolved.click_point.x == 640
    assert resolved.click_point.y == 360
    assert resolved.evidence.provider == ProviderKind.MOCK
    resolution = resolved.evidence.metadata["selector_resolution"]
    assert resolution["fallback_path"] == ["dom", "uia", "ocr", "vision", "mock"]
    assert resolution["used_fallback"] is True
    assert resolution["confidence_level"] == "low"


def test_selector_resolver_reports_unresolved_target() -> None:
    observation = Observation(provider=ProviderKind.SCREEN, source="test")

    try:
        SelectorResolver().resolve(Target.from_text("登录"), observation)
    except LookupError as exc:
        assert "Unable to resolve target" in str(exc)
    else:
        raise AssertionError("Expected unresolved target to raise LookupError.")


def test_selector_resolution_metadata_marks_stable_dom_test_id() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        width=800,
        height=600,
        elements=(
            {
                "text": "Export",
                "role": "button",
                "test_id": "export-orders",
                "selector": '[data-testid="export-orders"]',
                "bounds": {"left": 20, "top": 30, "width": 100, "height": 40},
            },
        ),
    )

    resolved = SelectorResolver().resolve(Target(test_id="export-orders", preferred=(ProviderKind.DOM,)), observation)
    resolution = resolved.evidence.metadata["selector_resolution"]

    assert resolved.evidence.confidence == 0.95
    assert resolution["selected_provider"] == "dom"
    assert resolution["fallback_path"] == ["dom"]
    assert resolution["used_fallback"] is False
    assert resolution["confidence_level"] == "high"
    assert resolution["stability"]["level"] == "stable"
    assert resolution["stability"]["signals"] == ["test_id"]


def test_selector_resolution_metadata_records_dom_to_mock_fallback_path() -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="https://example.test",
        width=800,
        height=600,
        elements=(
            {
                "text": "Cancel",
                "role": "button",
                "selector": "#cancel",
                "bounds": {"left": 20, "top": 30, "width": 100, "height": 40},
            },
        ),
    )

    resolved = SelectorResolver().resolve(Target(text="Submit", preferred=(ProviderKind.DOM, ProviderKind.MOCK)), observation)
    resolution = resolved.evidence.metadata["selector_resolution"]

    assert resolved.evidence.provider == ProviderKind.MOCK
    assert resolution["fallback_path"] == ["dom", "mock"]
    assert [attempt["status"] for attempt in resolution["attempts"]] == ["no_match", "success"]
    assert resolution["stability"]["level"] == "fallback"


def test_ocr_selector_matches_text_bounds() -> None:
    observation = Observation(
        provider=ProviderKind.OCR,
        source="screen.png",
        width=800,
        height=600,
        elements=(
            {
                "text": "登录成功",
                "confidence": 0.9,
                "bounds": {"left": 20, "top": 30, "width": 100, "height": 40},
            },
        ),
    )

    evidence = OCRSelectorStrategy().locate(Target(contains_text="登录"), observation)

    assert evidence is not None
    assert evidence.provider == ProviderKind.OCR
    assert evidence.click_point is not None
    assert evidence.click_point.x == 70


def test_vision_selector_matches_description() -> None:
    observation = Observation(
        provider=ProviderKind.VISION,
        source="screen.png",
        width=800,
        height=600,
        elements=(
            {
                "text": "页面显示已登录状态",
                "status": "success",
                "confidence": 0.8,
                "bounds": {"left": 0, "top": 0, "width": 800, "height": 600},
            },
        ),
    )

    evidence = VisionSelectorStrategy().locate(Target(contains_text="已登录"), observation)

    assert evidence is not None
    assert evidence.provider == ProviderKind.VISION
    assert evidence.click_point is not None
    assert evidence.click_point.x == 400


def test_vision_selector_prefers_structured_candidate_over_description() -> None:
    observation = Observation(
        provider=ProviderKind.VISION,
        source="screen.png",
        width=800,
        height=600,
        elements=(
            {
                "text": "页面上有登录按钮和注册链接",
                "role": "vision_description",
                "status": "success",
                "confidence": 0.8,
                "bounds": {"left": 0, "top": 0, "width": 800, "height": 600},
            },
            {
                "text": "登录",
                "label": "登录",
                "role": "vision_candidate",
                "target_role": "button",
                "status": "success",
                "confidence": 0.68,
                "bounds": {"left": 0, "top": 0, "width": 800, "height": 600},
            },
        ),
    )

    evidence = VisionSelectorStrategy().locate(Target(text="登录"), observation)

    assert evidence is not None
    assert evidence.metadata["element"]["role"] == "vision_candidate"
