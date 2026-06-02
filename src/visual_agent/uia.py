from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import Bounds, Observation, ProviderKind


@dataclass(frozen=True)
class UIAutomationProvider:
    """Read structured Windows UI Automation controls.

    The `uiautomation` package is optional. Install with
    `pip install -e .[desktop]` when running this against real Windows apps.
    """

    max_depth: int = 4
    max_elements: int = 500

    def observe_desktop(self) -> Observation:
        try:
            import uiautomation as auto
        except ImportError as exc:
            raise RuntimeError("uiautomation is not installed. Run: pip install -e .[desktop]") from exc

        root = auto.GetRootControl()
        elements: list[dict[str, Any]] = []
        self._walk_control(root, elements, depth=0)
        return Observation(
            provider=ProviderKind.UIA,
            source="windows-desktop",
            elements=tuple(elements),
            metadata={"max_depth": self.max_depth},
        )

    def _walk_control(self, control: Any, elements: list[dict[str, Any]], depth: int) -> None:
        if len(elements) >= self.max_elements or depth > self.max_depth:
            return

        element = control_to_element(control, depth=depth)
        if element is not None:
            elements.append(element)

        try:
            children = control.GetChildren()
        except Exception:
            children = []

        for child in children:
            self._walk_control(child, elements, depth=depth + 1)


def normalize_control_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.endswith("control"):
        text = text[: -len("control")]
    return text.replace(" ", "")


def control_to_element(control: Any, *, depth: int) -> dict[str, Any] | None:
    try:
        rect = control.BoundingRectangle
        bounds = {
            "left": int(rect.left),
            "top": int(rect.top),
            "width": int(rect.right - rect.left),
            "height": int(rect.bottom - rect.top),
        }
    except Exception:
        bounds = None

    try:
        name = str(control.Name or "")
    except Exception:
        name = ""

    try:
        automation_id = str(control.AutomationId or "")
    except Exception:
        automation_id = ""

    try:
        control_type = str(control.ControlTypeName or control.ControlType or "")
    except Exception:
        control_type = ""

    if not any([name, automation_id, control_type, bounds]):
        return None

    return {
        "name": name,
        "automation_id": automation_id,
        "control_type": normalize_control_type(control_type),
        "class_name": safe_attr(control, "ClassName"),
        "native_window_handle": safe_attr(control, "NativeWindowHandle"),
        "depth": depth,
        "bounds": bounds,
    }


def safe_attr(control: Any, attr: str) -> str:
    try:
        value = getattr(control, attr)
    except Exception:
        return ""
    return str(value or "")


def element_bounds(element: dict[str, Any]) -> Bounds | None:
    raw = element.get("bounds")
    if not isinstance(raw, dict):
        return None
    try:
        width = int(raw.get("width", 0))
        height = int(raw.get("height", 0))
        if width <= 0 or height <= 0:
            return None
        return Bounds(
            left=int(raw.get("left", 0)),
            top=int(raw.get("top", 0)),
            width=width,
            height=height,
        )
    except (TypeError, ValueError):
        return None


def element_accessible_name(element: dict[str, Any]) -> str:
    candidates = [
        element.get("name"),
        element.get("automation_id"),
        element.get("class_name"),
    ]
    return " ".join(str(item).strip().lower() for item in candidates if item)

