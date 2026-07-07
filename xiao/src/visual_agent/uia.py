from __future__ import annotations

from dataclasses import dataclass
from time import sleep
from typing import Any

from .models import Bounds, Observation, ProviderKind


@dataclass(frozen=True)
class WindowPlacement:
    """Snapshot of a window's position, size, and show state (via WINDOWPLACEMENT)."""

    handle: int
    show_cmd: int
    min_pt: tuple[int, int]
    max_pt: tuple[int, int]
    normal_rect: tuple[int, int, int, int]


def get_foreground_window() -> int | None:
    """Return the HWND of the current foreground window, or None on failure."""
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetForegroundWindow()
        return int(hwnd) if hwnd else None
    except Exception:
        return None


def save_window_placement(handle: int) -> WindowPlacement | None:
    """Capture full placement state (position + show_cmd) for later restore."""
    try:
        import ctypes
        from ctypes import wintypes

        class _PT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class _RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class _WP(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint),
                ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint),
                ("ptMinPosition", _PT),
                ("ptMaxPosition", _PT),
                ("rcNormalPosition", _RECT),
            ]

        wp = _WP()
        wp.length = ctypes.sizeof(_WP)
        if not ctypes.windll.user32.GetWindowPlacement(wintypes.HWND(handle), ctypes.byref(wp)):
            return None
        return WindowPlacement(
            handle=handle,
            show_cmd=wp.showCmd,
            min_pt=(wp.ptMinPosition.x, wp.ptMinPosition.y),
            max_pt=(wp.ptMaxPosition.x, wp.ptMaxPosition.y),
            normal_rect=(
                wp.rcNormalPosition.left,
                wp.rcNormalPosition.top,
                wp.rcNormalPosition.right,
                wp.rcNormalPosition.bottom,
            ),
        )
    except Exception:
        return None


def restore_window_placement(placement: WindowPlacement) -> bool:
    """Restore a window to the state captured by save_window_placement."""
    try:
        import ctypes
        from ctypes import wintypes

        class _PT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class _RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class _WP(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint),
                ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint),
                ("ptMinPosition", _PT),
                ("ptMaxPosition", _PT),
                ("rcNormalPosition", _RECT),
            ]

        wp = _WP()
        wp.length = ctypes.sizeof(_WP)
        wp.showCmd = placement.show_cmd
        wp.ptMinPosition.x, wp.ptMinPosition.y = placement.min_pt
        wp.ptMaxPosition.x, wp.ptMaxPosition.y = placement.max_pt
        (
            wp.rcNormalPosition.left,
            wp.rcNormalPosition.top,
            wp.rcNormalPosition.right,
            wp.rcNormalPosition.bottom,
        ) = placement.normal_rect
        return bool(ctypes.windll.user32.SetWindowPlacement(wintypes.HWND(placement.handle), ctypes.byref(wp)))
    except Exception:
        return False


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


@dataclass(frozen=True)
class UIAWindowMatch:
    bounds: Bounds
    native_window_handle: int | None = None
    name: str | None = None


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


def element_bounds(element: dict[str, Any], *, bring_to_front: bool = False) -> Bounds | None:
    handle_bounds = hwnd_bounds(element, bring_to_front=bring_to_front)
    if handle_bounds is not None:
        return handle_bounds
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


def hwnd_bounds(element: dict[str, Any], *, bring_to_front: bool = False) -> Bounds | None:
    handle = element_native_window_handle(element)
    if handle is None:
        return None
    return hwnd_bounds_from_handle(handle, bring_to_front=bring_to_front)


def element_native_window_handle(element: dict[str, Any]) -> int | None:
    raw_handle = str(element.get("native_window_handle") or "").strip()
    if not raw_handle:
        return None
    try:
        handle = int(raw_handle)
    except ValueError:
        return None
    if handle <= 0:
        return None
    return handle


def hwnd_bounds_from_handle(handle: int, *, bring_to_front: bool = False) -> Bounds | None:
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if bring_to_front:
            ctypes.windll.user32.ShowWindow(wintypes.HWND(handle), 9)
            ctypes.windll.user32.SetForegroundWindow(wintypes.HWND(handle))
            sleep(0.2)
        ok = ctypes.windll.user32.GetWindowRect(wintypes.HWND(handle), ctypes.byref(rect))
        if not ok:
            return None
        if int(rect.left) < -10000 or int(rect.top) < -10000 or int(rect.right - rect.left) < 200 or int(rect.bottom - rect.top) < 100:
            ctypes.windll.user32.ShowWindow(wintypes.HWND(handle), 9)
            ctypes.windll.user32.SetForegroundWindow(wintypes.HWND(handle))
            sleep(0.2)
            ok = ctypes.windll.user32.GetWindowRect(wintypes.HWND(handle), ctypes.byref(rect))
            if not ok:
                return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None
        return Bounds(left=int(rect.left), top=int(rect.top), width=width, height=height)
    except Exception:
        return None


def minimize_window(handle: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        return bool(ctypes.windll.user32.ShowWindow(wintypes.HWND(handle), 6))
    except Exception:
        return False


def find_uia_element_bounds(
    params: dict[str, Any],
    *,
    max_depth: int = 3,
    max_elements: int = 800,
) -> Bounds:
    return find_uia_window_match(params, max_depth=max_depth, max_elements=max_elements).bounds


def find_uia_window_match(
    params: dict[str, Any],
    *,
    max_depth: int = 3,
    max_elements: int = 800,
) -> UIAWindowMatch:
    provider = UIAutomationProvider(max_depth=max_depth, max_elements=max_elements)
    observation = provider.observe_desktop()
    title_contains = normalized_match_text(params.get("window_title_contains") or params.get("title_contains"))
    title_contains_any = tuple(
        item
        for item in (
            normalized_match_text(value)
            for value in list_param(params.get("window_title_contains_any") or params.get("title_contains_any"))
        )
        if item
    )
    name_contains = normalized_match_text(params.get("uia_name_contains") or params.get("name_contains"))
    class_contains = normalized_match_text(params.get("uia_class_name_contains") or params.get("class_name_contains"))
    control_type = normalized_match_text(params.get("uia_control_type") or params.get("control_type"))
    bring_to_front = bool(params.get("bring_to_front") or params.get("foreground"))

    title_candidates = (title_contains,) if title_contains else title_contains_any or (None,)
    for title_candidate in title_candidates:
        matches = matching_uia_bounds(
            observation.elements,
            title_contains=title_candidate,
            name_contains=name_contains,
            class_contains=class_contains,
            control_type=control_type,
            bring_to_front=bring_to_front,
        )
        if matches:
            return max(matches, key=lambda item: item.bounds.width * item.bounds.height)

    label = title_contains or ", ".join(title_contains_any) or name_contains or class_contains or control_type or "matching UIA element"
    raise RuntimeError(f"Could not find UIA bounds for {label}.")


def matching_uia_bounds(
    elements: tuple[dict[str, Any], ...],
    *,
    title_contains: str | None,
    name_contains: str,
    class_contains: str,
    control_type: str,
    bring_to_front: bool = False,
) -> list[UIAWindowMatch]:
    matches: list[UIAWindowMatch] = []
    for element in elements:
        if control_type and control_type not in normalize_control_type(element.get("control_type")):
            continue
        name = str(element.get("name") or "").lower()
        class_name = str(element.get("class_name") or "").lower()
        accessible = element_accessible_name(element)
        if title_contains and title_contains not in accessible:
            continue
        if name_contains and name_contains not in name:
            continue
        if class_contains and class_contains not in class_name:
            continue
        bounds = element_bounds(element, bring_to_front=bring_to_front)
        if bounds is not None:
            matches.append(
                UIAWindowMatch(
                    bounds=bounds,
                    native_window_handle=element_native_window_handle(element),
                    name=str(element.get("name") or "") or None,
                )
            )
    return matches


def normalized_match_text(value: Any) -> str:
    return str(value or "").strip().lower()


def list_param(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def element_accessible_name(element: dict[str, Any]) -> str:
    candidates = [
        element.get("name"),
        element.get("automation_id"),
        element.get("class_name"),
    ]
    return " ".join(str(item).strip().lower() for item in candidates if item)
