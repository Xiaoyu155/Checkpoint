from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from pathlib import Path
from time import time
from typing import Any


class ProviderKind(str, Enum):
    SCREEN = "screen"
    DOM = "dom"
    UIA = "uia"
    OCR = "ocr"
    VISION = "vision"
    MOCK = "mock"


class ActionStatus(str, Enum):
    SUCCESS = "success"
    DRY_RUN = "dry_run"
    FAILED = "failed"


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    width: int
    height: int

    @property
    def center(self) -> Point:
        return Point(x=self.left + self.width // 2, y=self.top + self.height // 2)


@dataclass(frozen=True)
class Target:
    text: str | None = None
    role: str | None = None
    label: str | None = None
    selector: str | None = None
    test_id: str | None = None
    contains_text: str | None = None
    text_regex: str | None = None
    row_text: str | None = None
    row_contains_text: str | None = None
    row_text_regex: str | None = None
    column_header: str | None = None
    column_contains_text: str | None = None
    column_text_regex: str | None = None
    near_text: str | None = None
    near_contains_text: str | None = None
    near_text_regex: str | None = None
    scope_role: str | None = None
    scope_text: str | None = None
    scope_contains_text: str | None = None
    window_title: str | None = None
    preferred: tuple[ProviderKind, ...] = (
        ProviderKind.DOM,
        ProviderKind.UIA,
        ProviderKind.OCR,
        ProviderKind.VISION,
        ProviderKind.MOCK,
    )

    @classmethod
    def from_text(cls, text: str) -> "Target":
        return cls(text=text)

    @property
    def display_name(self) -> str:
        return (
            self.selector
            or self.test_id
            or self.text
            or self.label
            or self.contains_text
            or self.text_regex
            or self.row_text
            or self.row_contains_text
            or self.row_text_regex
            or self.column_header
            or self.column_contains_text
            or self.column_text_regex
            or self.near_text
            or self.near_contains_text
            or self.near_text_regex
            or self.scope_text
            or self.scope_contains_text
            or self.scope_role
            or self.role
            or "unnamed-target"
        )


@dataclass(frozen=True)
class Observation:
    provider: ProviderKind
    source: str
    timestamp: float = field(default_factory=time)
    screenshot_path: Path | None = None
    width: int | None = None
    height: int | None = None
    elements: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LocationEvidence:
    provider: ProviderKind
    confidence: float
    reason: str
    point: Point | None = None
    bounds: Bounds | None = None
    handle: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def click_point(self) -> Point | None:
        if self.point is not None:
            return self.point
        if self.bounds is not None:
            return self.bounds.center
        return None


@dataclass(frozen=True)
class ResolvedTarget:
    target: Target
    evidence: LocationEvidence

    @property
    def click_point(self) -> Point:
        point = self.evidence.click_point
        if point is None:
            raise ValueError("Resolved target has no click point.")
        return point


@dataclass(frozen=True)
class ActionResult:
    action: str
    status: ActionStatus
    target: str
    point: Point | None = None
    provider: ProviderKind | None = None
    message: str = ""
    timestamp: float = field(default_factory=time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunResult:
    run_id: str
    run_dir: Path
    observation: Observation
    resolved_target: ResolvedTarget
    action: ActionResult


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value
