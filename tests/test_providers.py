from pathlib import Path

from visual_agent.models import Observation, ProviderKind
import pytest

from visual_agent.providers import ProviderContext, ProviderRegistry, default_provider_registry, normalize_url, route_handler


def test_default_provider_registry_contains_current_observe_actions(tmp_path) -> None:
    registry = default_provider_registry()

    assert "observe_html" in registry.actions
    assert "observe_fixture" in registry.actions
    assert "observe_screen" in registry.actions
    assert "observe_browser" in registry.actions
    assert "observe_ocr" in registry.actions
    assert "observe_vision" in registry.actions


def test_provider_registry_can_register_custom_provider(tmp_path) -> None:
    registry = ProviderRegistry()
    registry.register(
        "observe_custom",
        lambda params, context: Observation(provider=ProviderKind.MOCK, source=params["source"]),
    )

    observation = registry.observe("observe_custom", {"source": "custom"}, ProviderContext(run_dir=tmp_path))

    assert observation.provider == ProviderKind.MOCK
    assert observation.source == "custom"


def test_ocr_provider_supports_deterministic_mock_text(tmp_path) -> None:
    registry = default_provider_registry()
    observation = registry.observe(
        "observe_ocr",
        {
            "mock_text": "登录成功",
            "mock_bounds": {"left": 10, "top": 20, "width": 120, "height": 30},
        },
        ProviderContext(run_dir=tmp_path, synthetic_on_capture_fail=True),
    )

    assert observation.provider == ProviderKind.OCR
    assert observation.metadata["engine"] == "mock"
    assert observation.screenshot_path == tmp_path / "ocr-mock.png"
    assert observation.elements[0]["text"] == "登录成功"
    assert observation.elements[0]["bounds"]["left"] == 10


def test_vision_provider_supports_deterministic_mock_description(tmp_path) -> None:
    registry = default_provider_registry()
    observation = registry.observe(
        "observe_vision",
        {
            "prompt": "页面是否登录成功？",
            "mock_description": "页面显示已登录状态",
            "mock_status": "success",
        },
        ProviderContext(run_dir=tmp_path, synthetic_on_capture_fail=True),
    )

    assert observation.provider == ProviderKind.VISION
    assert observation.metadata["engine"] == "mock"
    assert observation.metadata["engine_status"]["available"] is True
    assert observation.metadata["status"] == "success"
    assert observation.screenshot_path == tmp_path / "vision-mock.png"
    assert observation.elements[0]["text"] == "页面显示已登录状态"


def test_route_handler_rejects_absolute_system_path(tmp_path) -> None:
    handler = route_handler({"url": "**/*", "body_from_file": "C:/Windows/System32/drivers/etc/hosts"}, tmp_path)

    with pytest.raises((ValueError, FileNotFoundError)):
        handler(MockRoute())


def test_route_handler_allows_absolute_project_examples_fixture(tmp_path) -> None:
    fixture = (Path(__file__).parent.parent / "examples" / "external_samples" / "fixtures" / "support_tickets_demo.html").resolve()
    route = FulfilledRoute()
    handler = route_handler({"url": "**/*", "body_from_file": str(fixture)}, tmp_path)

    handler(route)

    assert route.kwargs["status"] == 200
    assert "Tickets" in route.kwargs["body"]


def test_normalize_url_rejects_out_of_project_path() -> None:
    with pytest.raises(ValueError):
        normalize_url("C:/Windows/System32/drivers/etc/hosts")


class MockRoute:
    def fulfill(self, **_kwargs) -> None:
        raise AssertionError("route should not be fulfilled for unsafe file paths")


class FulfilledRoute:
    def __init__(self) -> None:
        self.kwargs = {}

    def fulfill(self, **kwargs) -> None:
        self.kwargs = kwargs
