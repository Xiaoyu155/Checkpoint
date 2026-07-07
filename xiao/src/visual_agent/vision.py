from __future__ import annotations

from dataclasses import dataclass
import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from PIL import Image

from .env import env_get


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

    def detect(self, image: Image.Image, image_path: Path, target: str) -> tuple[dict[str, object], ...]:
        """Return structured visual elements for a screenshot."""


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

    def detect(self, image: Image.Image, image_path: Path, target: str) -> tuple[dict[str, object], ...]:
        return (
            {
                "text": target,
                "bounds": {"left": image.width // 4, "top": image.height // 4, "width": image.width // 2, "height": image.height // 2},
                "confidence": 0.1,
                "role": "mock",
            },
        )


@dataclass(frozen=True)
class OmniParserVisionLocator:
    endpoint: str
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> "OmniParserVisionLocator":
        endpoint = str(env_get("VISUAL_AGENT_OMNIPARSER_ENDPOINT", "") or "").strip()
        if not endpoint:
            raise RuntimeError(
                "VISUAL_AGENT_OMNIPARSER_ENDPOINT is not set. Configure an OmniParser-compatible HTTP endpoint "
                "that accepts {image_base64, target, mode} and returns structured elements."
            )
        return cls(endpoint=endpoint)

    def locate(self, image: Image.Image, image_path: Path, target: str) -> ElementLocation:
        elements = self.detect(image, image_path, target)
        best = self._best_element(elements, target)
        if best is None:
            raise LookupError(f"OmniParser could not locate target: {target}")
        bounds = best.get("bounds") if isinstance(best.get("bounds"), dict) else None
        if not isinstance(bounds, dict):
            raise LookupError(f"OmniParser returned a match without bounds for: {target}")
        left = int(bounds.get("left", 0))
        top = int(bounds.get("top", 0))
        width = max(1, int(bounds.get("width", 1)))
        height = max(1, int(bounds.get("height", 1)))
        return ElementLocation(
            target=target,
            x=left + width // 2,
            y=top + height // 2,
            confidence=float(best.get("confidence") or 0.0),
            reason=str(best.get("reason") or best.get("text") or "omniparser match"),
        )

    def detect(self, image: Image.Image, image_path: Path, target: str) -> tuple[dict[str, object], ...]:
        payload = {
            "mode": "detect",
            "target": target,
            "image_name": image_path.name,
            "image_base64": encode_image(image),
        }
        response = request_json(self.endpoint, payload, timeout_seconds=self.timeout_seconds)
        elements = response.get("elements") if isinstance(response, dict) else None
        if isinstance(elements, list):
            return tuple(item for item in elements if isinstance(item, dict))
        if isinstance(response, dict) and isinstance(response.get("boxes"), list):
            return tuple(item for item in response["boxes"] if isinstance(item, dict))
        return ()

    def _best_element(self, elements: tuple[dict[str, object], ...], target: str) -> dict[str, object] | None:
        normalized = target.strip().lower()
        best: dict[str, object] | None = None
        best_score = -1.0
        for element in elements:
            text = str(element.get("text") or element.get("label") or element.get("name") or "").strip().lower()
            role = str(element.get("role") or "").strip().lower()
            confidence = float(element.get("confidence") or 0.0)
            score = confidence
            if normalized and normalized == text:
                score += 1.0
            elif normalized and normalized in text:
                score += 0.6
            if normalized and normalized in role:
                score += 0.2
            if score > best_score:
                best_score = score
                best = element
        return best


def encode_image(image: Image.Image) -> str:
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def request_json(endpoint: str, payload: dict[str, object], *, timeout_seconds: float) -> dict[str, object]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OmniParser request failed: {exc.code} {exc.reason}") from exc
    except Exception as exc:
        raise RuntimeError(f"OmniParser request failed: {exc.__class__.__name__}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OmniParser returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("OmniParser response must be a JSON object.")
    return parsed


def build_locator(provider: str) -> VisionLocator:
    normalized = provider.strip().lower()
    if normalized == "mock":
        return MockVisionLocator()
    if normalized == "omniparser":
        return OmniParserVisionLocator.from_env()
    raise ValueError(
        f"Unsupported provider '{provider}'. Currently available: mock, omniparser. "
        "Set VISUAL_AGENT_OMNIPARSER_ENDPOINT for the OmniParser adapter."
    )
