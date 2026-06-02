from pathlib import Path

from PIL import Image

from visual_agent.vision import MockVisionLocator, build_locator


def test_mock_locator_returns_image_center() -> None:
    image = Image.new("RGB", (800, 600))
    location = MockVisionLocator().locate(image, Path("screen.png"), "登录")

    assert location.x == 400
    assert location.y == 300
    assert location.target == "登录"


def test_build_locator_supports_mock() -> None:
    assert isinstance(build_locator("mock"), MockVisionLocator)

