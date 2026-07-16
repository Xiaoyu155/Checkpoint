from pathlib import Path

from PIL import Image
from visual_agent.models import Bounds, Observation, ProviderKind
from visual_agent.uia import UIAWindowMatch
import pytest

from visual_agent.capture import capture_window_params, crop_image
from visual_agent.providers import (
    ProviderContext,
    ProviderRegistry,
    default_provider_registry,
    normalize_url,
    route_handler,
    wait_for_browser_page_ready,
)


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


def test_browser_page_readiness_uses_body_locator_wait() -> None:
    calls = []

    class BodyLocator:
        def wait_for(self, *, state, timeout):
            calls.append(("wait_for", state, timeout))

    class Page:
        def locator(self, selector):
            calls.append(("locator", selector))
            return BodyLocator()

    wait_for_browser_page_ready(Page(), timeout_ms=4321)

    assert calls == [("locator", "body"), ("wait_for", "visible", 4321)]


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


def test_ocr_provider_applies_relative_crop_to_image(tmp_path) -> None:
    registry = default_provider_registry()
    observation = registry.observe(
        "observe_ocr",
        {
            "mock_text": "填分数",
            "mock_width": 1000,
            "mock_height": 800,
            "simulator_crop": {
                "left_percent": 0.1,
                "top_percent": 0.25,
                "width_percent": 0.5,
                "height_percent": 0.5,
            },
        },
        ProviderContext(run_dir=tmp_path, synthetic_on_capture_fail=True),
    )

    assert observation.width == 500
    assert observation.height == 400
    assert observation.metadata["crop_region"] == {"left": 100, "top": 200, "width": 500, "height": 400}
    assert observation.screenshot_path == tmp_path / "ocr-mock-ocr-region.png"


def test_screen_provider_can_fallback_to_screen_when_uia_window_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "visual_agent.uia.find_uia_window_match",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("missing")),
    )
    registry = default_provider_registry()
    observation = registry.observe(
        "observe_screen",
        {
            "window": {"title_contains": "missing-window"},
            "fallback_to_screen": True,
            "simulator_crop": {"left_percent": 0.0, "top_percent": 0.0, "width_percent": 0.5, "height_percent": 1.0},
        },
        ProviderContext(run_dir=tmp_path, synthetic_on_capture_fail=True),
    )

    assert observation.width == observation.metadata["crop_region"]["width"]
    assert observation.metadata["crop_region"]["width"] > 0
    assert observation.metadata["uia_window_fallback"]["used"] is True


def test_screen_provider_foregrounds_before_capture_and_minimizes_after(tmp_path, monkeypatch) -> None:
    calls = {"find": 0, "minimize": [], "lock": []}

    class FakeVisualLock:
        def __enter__(self):
            calls["lock"].append("enter")
            return self

        def __exit__(self, exc_type, exc, traceback):
            calls["lock"].append("exit")

    def fake_match(*_args, **_kwargs):
        calls["find"] += 1
        return UIAWindowMatch(
            bounds=Bounds(left=0, top=0, width=500, height=400),
            native_window_handle=123,
            name="Target App",
        )

    monkeypatch.setattr("visual_agent.locks.VisualLock", FakeVisualLock)
    monkeypatch.setattr("visual_agent.uia.find_uia_window_match", fake_match)
    monkeypatch.setattr("visual_agent.uia.minimize_window", lambda handle: calls["minimize"].append(handle) or True)

    registry = default_provider_registry()
    observation = registry.observe(
        "observe_screen",
        {
            "window": {"title_contains": "Target App", "bring_to_front": True},
        },
        ProviderContext(run_dir=tmp_path, synthetic_on_capture_fail=True),
    )

    assert calls["lock"] == ["enter", "exit"]
    assert calls["find"] >= 2
    assert calls["minimize"] == [123]
    assert observation.metadata["uia_window_pre_capture"]["foregrounded"] is True
    assert observation.metadata["uia_window_post_capture"] == {"action": "minimize", "status": "success"}


def test_screen_provider_normalizes_top_level_window_title_candidates(tmp_path, monkeypatch) -> None:
    calls = {"titles": [], "minimize": []}

    def fake_match(params, *_args, **_kwargs):
        calls["titles"].append(tuple(params["title_contains_any"]))
        return UIAWindowMatch(
            bounds=Bounds(left=0, top=0, width=500, height=400),
            native_window_handle=456,
            name="微信开发者工具",
        )

    monkeypatch.setattr("visual_agent.uia.find_uia_window_match", fake_match)
    monkeypatch.setattr("visual_agent.uia.minimize_window", lambda handle: calls["minimize"].append(handle) or True)

    registry = default_provider_registry()
    observation = registry.observe(
        "observe_screen",
        {
            "window_title_candidates": ["微信开发者工具", "Chrome"],
            "fallback_to_screen": False,
        },
        ProviderContext(run_dir=tmp_path, synthetic_on_capture_fail=True),
    )

    assert calls["titles"]
    assert calls["titles"][0] == ("微信开发者工具", "Chrome")
    assert calls["minimize"] == [456]
    assert observation.metadata["uia_window_pre_capture"]["foregrounded"] is True
    assert observation.metadata["uia_window_post_capture"] == {"action": "minimize", "status": "success"}


def test_capture_window_params_defaults_to_foreground_and_minimize_for_title_aliases() -> None:
    params = capture_window_params({"window_title_contains": "Chrome"})

    assert params["title_contains"] == "Chrome"
    assert params["bring_to_front"] is True
    assert params["post_capture"] == "minimize"


def test_screen_provider_can_keep_foregrounded_window_open(tmp_path, monkeypatch) -> None:
    calls = {"minimize": []}

    def fake_match(*_args, **_kwargs):
        return UIAWindowMatch(
            bounds=Bounds(left=0, top=0, width=500, height=400),
            native_window_handle=123,
            name="Target App",
        )

    monkeypatch.setattr("visual_agent.uia.find_uia_window_match", fake_match)
    monkeypatch.setattr("visual_agent.uia.minimize_window", lambda handle: calls["minimize"].append(handle) or True)

    registry = default_provider_registry()
    observation = registry.observe(
        "observe_screen",
        {
            "window": {"title_contains": "Target App", "bring_to_front": True, "post_capture": "keep"},
        },
        ProviderContext(run_dir=tmp_path, synthetic_on_capture_fail=True),
    )

    assert calls["minimize"] == []
    assert "uia_window_post_capture" not in observation.metadata


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


def test_crop_image_clamps_region_to_image_bounds() -> None:
    image = Image.new("RGB", (200, 100), color=(255, 255, 255))

    cropped, region = crop_image(image, {"left": 150, "top": 80, "width": 200, "height": 50})

    assert cropped.size == (50, 20)
    assert region == {"left": 150, "top": 80, "width": 50, "height": 20}


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
