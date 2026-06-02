from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image


@dataclass(frozen=True)
class ElementLocation:
    target: str
    x: int
    y: int
    confidence: float
    reason: str


class VisionLocator(Protocol):
    def locate(self, image: Image.Image, image_path: Path, target: str) -> ElementLocation:
        """Return the desktop coordinate to click for a named UI target."""


class MockVisionLocator:
    """Development locator that returns the screen center.

    This keeps the first MVP loop runnable before a real VLM is connected.
    """

    def locate(self, image: Image.Image, image_path: Path, target: str) -> ElementLocation:
        return ElementLocation(
            target=target,
            x=image.width // 2,
            y=image.height // 2,
            confidence=0.1,
            reason=f"mock locator selected center of {image_path.name}",
        )


def build_locator(provider: str) -> VisionLocator:
    normalized = provider.strip().lower()
    if normalized == "mock":
        return MockVisionLocator()
    raise ValueError(
        f"Unsupported provider '{provider}'. Currently available: mock. "
        "Add a Qwen2-VL or cloud provider in visual_agent.vision."
    )

