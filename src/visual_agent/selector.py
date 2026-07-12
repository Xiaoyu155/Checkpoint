from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Protocol

from .dom import element_accessible_name, element_bounds, normalize_text
from .models import LocationEvidence, Observation, Point, ProviderKind, ResolvedTarget, Target
from .uia import (
    element_accessible_name as uia_accessible_name,
    element_bounds as uia_bounds,
    normalize_control_type,
)


class SelectorStrategy(Protocol):
    provider: ProviderKind

    def locate(self, target: Target, observation: Observation) -> LocationEvidence | None:
        """Return location evidence when this strategy can resolve the target."""


@dataclass(frozen=True)
class MockSelectorStrategy:
    provider: ProviderKind = ProviderKind.MOCK

    def locate(self, target: Target, observation: Observation) -> LocationEvidence | None:
        if observation.width is None or observation.height is None:
            return None
        return LocationEvidence(
            provider=self.provider,
            confidence=0.1,
            reason=f"mock selected screen center for {target.display_name}",
            point=Point(x=observation.width // 2, y=observation.height // 2),
        )


@dataclass(frozen=True)
class DomSelectorStrategy:
    provider: ProviderKind = ProviderKind.DOM

    def locate(self, target: Target, observation: Observation) -> LocationEvidence | None:
        if observation.provider != self.provider:
            return None
        if not observation.elements:
            return None

        expected_text = normalize_text(target.text)
        expected_label = normalize_text(target.label)
        expected_role = normalize_text(target.role)
        expected_selector = str(target.selector or "").strip()
        expected_test_id = str(target.test_id or "").strip()
        expected_contains_text = normalize_text(target.contains_text)
        expected_row_text = normalize_text(target.row_text)
        expected_row_contains_text = normalize_text(target.row_contains_text)
        expected_column_header = normalize_text(target.column_header)
        expected_column_contains_text = normalize_text(target.column_contains_text)
        expected_near_text = normalize_text(target.near_text)
        expected_near_contains_text = normalize_text(target.near_contains_text)
        expected_scope_role = normalize_text(target.scope_role)
        expected_scope_text = normalize_text(target.scope_text)
        expected_scope_contains_text = normalize_text(target.scope_contains_text)
        best: tuple[float, dict] | None = None

        for element in observation.elements:
            score = self._score_element(
                element,
                observation,
                expected_text,
                expected_label,
                expected_role,
                expected_selector,
                expected_test_id,
                expected_contains_text,
                target.text_regex,
                expected_row_text,
                expected_row_contains_text,
                target.row_text_regex,
                expected_column_header,
                expected_column_contains_text,
                target.column_text_regex,
                expected_near_text,
                expected_near_contains_text,
                target.near_text_regex,
                expected_scope_role,
                expected_scope_text,
                expected_scope_contains_text,
            )
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, element)

        if best is None:
            return None

        score, element = best
        bounds = element_bounds(element)
        if bounds is None:
            return None

        return LocationEvidence(
            provider=self.provider,
            confidence=min(score, 0.98),
            reason=f"DOM element matched {target.display_name}",
            bounds=bounds,
            handle=str(element.get("selector") or element.get("index") or ""),
            metadata={
                "element": element,
                "score": score,
                "source": observation.source,
            },
        )

    def _score_element(
        self,
        element: dict,
        observation: Observation,
        expected_text: str,
        expected_label: str,
        expected_role: str,
        expected_selector: str,
        expected_test_id: str,
        expected_contains_text: str,
        expected_text_regex: str | None,
        expected_row_text: str,
        expected_row_contains_text: str,
        expected_row_text_regex: str | None,
        expected_column_header: str,
        expected_column_contains_text: str,
        expected_column_text_regex: str | None,
        expected_near_text: str,
        expected_near_contains_text: str,
        expected_near_text_regex: str | None,
        expected_scope_role: str,
        expected_scope_text: str,
        expected_scope_contains_text: str,
    ) -> float:
        score = 0.0
        accessible_name = element_accessible_name(element)
        row_text = normalize_text(element.get("row_text"))
        column_header = normalize_text(element.get("column_header"))
        role = normalize_text(element.get("role") or element.get("tag"))
        selector = str(element.get("selector") or "")
        test_id = str(element.get("test_id") or "")

        if expected_selector:
            if selector == expected_selector:
                score += 1.0
            else:
                return 0.0

        if expected_test_id:
            if test_id == expected_test_id:
                score += 0.95
            else:
                return 0.0

        text_query = expected_text or expected_label
        if text_query:
            if accessible_name == text_query:
                score += 0.75
            elif text_query in accessible_name:
                score += 0.55

        if expected_contains_text:
            if expected_contains_text in accessible_name:
                score += 0.6
            else:
                return 0.0

        if expected_text_regex:
            try:
                if re.search(expected_text_regex, accessible_name, re.IGNORECASE):
                    score += 0.65
                else:
                    return 0.0
            except re.error:
                return 0.0

        if expected_row_text:
            if row_text == expected_row_text:
                score += 0.8
            elif expected_row_text in row_text:
                score += 0.6
            else:
                return 0.0

        if expected_row_contains_text:
            if expected_row_contains_text in row_text:
                score += 0.6
            else:
                return 0.0

        if expected_row_text_regex:
            try:
                if re.search(expected_row_text_regex, row_text, re.IGNORECASE):
                    score += 0.65
                else:
                    return 0.0
            except re.error:
                return 0.0

        if expected_column_header:
            if column_header == expected_column_header:
                score += 0.75
            elif expected_column_header in column_header:
                score += 0.55
            else:
                return 0.0

        if expected_column_contains_text:
            if expected_column_contains_text in column_header:
                score += 0.55
            else:
                return 0.0

        if expected_column_text_regex:
            try:
                if re.search(expected_column_text_regex, column_header, re.IGNORECASE):
                    score += 0.6
                else:
                    return 0.0
            except re.error:
                return 0.0

        if expected_scope_role or expected_scope_text or expected_scope_contains_text:
            if not element_matches_scope(
                element,
                observation,
                expected_scope_role=expected_scope_role,
                expected_scope_text=expected_scope_text,
                expected_scope_contains_text=expected_scope_contains_text,
            ):
                return 0.0
            score += 0.35

        if expected_near_text or expected_near_contains_text or expected_near_text_regex:
            near = nearest_matching_element(
                element,
                observation,
                expected_near_text=expected_near_text,
                expected_near_contains_text=expected_near_contains_text,
                expected_near_text_regex=expected_near_text_regex,
            )
            if near is None:
                return 0.0
            score += near_score(element, near)

        if expected_role:
            if role == expected_role:
                score += 0.25
            elif expected_role in role:
                score += 0.15

        return score


def element_matches_scope(
    element: dict,
    observation: Observation,
    *,
    expected_scope_role: str,
    expected_scope_text: str,
    expected_scope_contains_text: str,
) -> bool:
    scope_selector = str(element.get("scope_selector") or element.get("dialog_selector") or "")
    scope_text = normalize_text(element.get("scope_text") or element.get("dialog_text"))
    scope_role = normalize_text(element.get("scope_role") or element.get("dialog_role"))
    if expected_scope_role and scope_role:
        if scope_role != expected_scope_role and expected_scope_role not in scope_role:
            return False
    elif expected_scope_role and scope_selector:
        scoped = find_element_by_selector(observation, scope_selector)
        scoped_role = normalize_text(scoped.get("role") if scoped else "")
        if scoped_role != expected_scope_role and expected_scope_role not in scoped_role:
            return False
    elif expected_scope_role:
        return False

    if expected_scope_text or expected_scope_contains_text:
        haystack = scope_text
        if not haystack and scope_selector:
            scoped = find_element_by_selector(observation, scope_selector)
            haystack = element_accessible_name(scoped) if scoped else ""
        if expected_scope_text and haystack != expected_scope_text:
            return False
        if expected_scope_contains_text and expected_scope_contains_text not in haystack:
            return False
    return True


def find_element_by_selector(observation: Observation, selector: str) -> dict | None:
    for element in observation.elements:
        if str(element.get("selector") or "") == selector:
            return element
    return None


def nearest_matching_element(
    element: dict,
    observation: Observation,
    *,
    expected_near_text: str,
    expected_near_contains_text: str,
    expected_near_text_regex: str | None,
) -> dict | None:
    element_box = element_bounds(element)
    if element_box is None:
        return None
    matches: list[tuple[float, dict]] = []
    for other in observation.elements:
        if other is element:
            continue
        text = element_accessible_name(other)
        if not text_matches(text, expected_near_text, expected_near_contains_text, expected_near_text_regex):
            continue
        other_box = element_bounds(other)
        if other_box is None:
            continue
        matches.append((bounds_distance(element_box, other_box), other))
    if not matches:
        return None
    return sorted(matches, key=lambda item: item[0])[0][1]


def text_matches(text: str, expected_text: str, expected_contains_text: str, expected_regex: str | None) -> bool:
    normalized = normalize_text(text)
    if expected_text and normalized != expected_text:
        return False
    if expected_contains_text and expected_contains_text not in normalized:
        return False
    if expected_regex:
        try:
            if not re.search(expected_regex, normalized, re.IGNORECASE):
                return False
        except re.error:
            return False
    return bool(expected_text or expected_contains_text or expected_regex)


def bounds_distance(a, b) -> float:
    ax = a.left + a.width / 2
    ay = a.top + a.height / 2
    bx = b.left + b.width / 2
    by = b.top + b.height / 2
    return abs(ax - bx) + abs(ay - by)


def near_score(element: dict, near: dict) -> float:
    element_box = element_bounds(element)
    near_box = element_bounds(near)
    if element_box is None or near_box is None:
        return 0.0
    center_y_gap = abs((element_box.top + element_box.height / 2) - (near_box.top + near_box.height / 2))
    center_x_gap = abs((element_box.left + element_box.width / 2) - (near_box.left + near_box.width / 2))
    if center_y_gap <= 40 and center_x_gap <= 320:
        return 0.65
    distance = bounds_distance(element_box, near_box)
    if distance <= 120:
        return 0.65
    if distance <= 300:
        return 0.45
    return 0.25


@dataclass(frozen=True)
class UIASelectorStrategy:
    provider: ProviderKind = ProviderKind.UIA

    def locate(self, target: Target, observation: Observation) -> LocationEvidence | None:
        if observation.provider != self.provider:
            return None
        if not observation.elements:
            return None

        expected_text = normalize_text(target.text)
        expected_label = normalize_text(target.label)
        expected_role = normalize_control_type(target.role)
        best: tuple[float, dict] | None = None

        for element in observation.elements:
            score = self._score_element(element, expected_text, expected_label, expected_role)
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, element)

        if best is None:
            return None

        score, element = best
        bounds = uia_bounds(element)
        if bounds is None:
            return None

        return LocationEvidence(
            provider=self.provider,
            confidence=min(score, 0.98),
            reason=f"UIA control matched {target.display_name}",
            bounds=bounds,
            handle=str(element.get("automation_id") or element.get("native_window_handle") or ""),
            metadata={
                "element": element,
                "score": score,
                "source": observation.source,
            },
        )

    def _score_element(
        self,
        element: dict,
        expected_text: str,
        expected_label: str,
        expected_role: str,
    ) -> float:
        score = 0.0
        accessible_name = uia_accessible_name(element)
        control_type = normalize_control_type(element.get("control_type"))

        text_query = expected_text or expected_label
        if text_query:
            if accessible_name == text_query:
                score += 0.75
            elif text_query in accessible_name:
                score += 0.55

        if expected_role:
            if control_type == expected_role:
                score += 0.25
            elif expected_role in control_type:
                score += 0.15

        return score


@dataclass(frozen=True)
class OCRSelectorStrategy:
    provider: ProviderKind = ProviderKind.OCR

    def locate(self, target: Target, observation: Observation) -> LocationEvidence | None:
        if observation.provider != self.provider:
            return None
        if not observation.elements:
            return None

        expected_text = normalize_text(target.text)
        expected_label = normalize_text(target.label)
        expected_contains_text = normalize_text(target.contains_text)
        best: tuple[float, dict] | None = None

        for element in observation.elements:
            score = self._score_element(
                element,
                expected_text or expected_label,
                expected_contains_text,
                target.text_regex,
            )
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, element)

        if best is None:
            return None

        score, element = best
        bounds = element_bounds(element)
        if bounds is None:
            return None

        return LocationEvidence(
            provider=self.provider,
            confidence=min(score, 0.95),
            reason=f"OCR text matched {target.display_name}",
            bounds=bounds,
            handle=str(element.get("text") or ""),
            metadata={
                "element": element,
                "score": score,
                "source": observation.source,
            },
        )

    def _score_element(
        self,
        element: dict,
        expected_text: str,
        expected_contains_text: str,
        expected_text_regex: str | None,
    ) -> float:
        score = 0.0
        text = normalize_text(element.get("text"))
        confidence = float(element.get("confidence", 0.5))

        if expected_text:
            if text == expected_text:
                score += 0.75
            elif expected_text in text:
                score += 0.55

        if expected_contains_text:
            if expected_contains_text in text:
                score += 0.6
            else:
                return 0.0

        if expected_text_regex:
            try:
                if re.search(expected_text_regex, text, re.IGNORECASE):
                    score += 0.65
                else:
                    return 0.0
            except re.error:
                return 0.0

        if score > 0:
            score += min(max(confidence, 0.0), 1.0) * 0.2
        return score


@dataclass(frozen=True)
class VisionSelectorStrategy:
    provider: ProviderKind = ProviderKind.VISION

    def locate(self, target: Target, observation: Observation) -> LocationEvidence | None:
        if observation.provider != self.provider:
            return None
        if not observation.elements:
            return None

        expected_text = normalize_text(target.text)
        expected_label = normalize_text(target.label)
        expected_contains_text = normalize_text(target.contains_text)
        best: tuple[float, dict] | None = None
        for element in observation.elements:
            score = self._score_element(
                element,
                expected_text or expected_label,
                expected_contains_text,
                target.text_regex,
            )
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, element)

        if best is None:
            return None

        score, element = best
        bounds = element_bounds(element)
        if bounds is None:
            return None

        return LocationEvidence(
            provider=self.provider,
            confidence=min(score, 0.9),
            reason=f"Vision description matched {target.display_name}",
            bounds=bounds,
            handle=str(element.get("status") or element.get("text") or ""),
            metadata={
                "element": element,
                "score": score,
                "source": observation.source,
            },
        )

    def _score_element(
        self,
        element: dict,
        expected_text: str,
        expected_contains_text: str,
        expected_text_regex: str | None,
    ) -> float:
        text = normalize_text(element.get("text"))
        status = normalize_text(element.get("status"))
        haystack = normalize_text(f"{text} {status}")
        confidence = float(element.get("confidence", 0.5))
        score = 0.0

        if expected_text:
            if expected_text == text:
                score += 0.72
            elif expected_text == haystack or expected_text == status:
                score += 0.7
            elif expected_text in haystack:
                score += 0.5

        if expected_contains_text:
            if expected_contains_text in haystack:
                score += 0.6
            else:
                return 0.0

        if expected_text_regex:
            try:
                if re.search(expected_text_regex, haystack, re.IGNORECASE):
                    score += 0.6
                else:
                    return 0.0
            except re.error:
                return 0.0

        if score > 0:
            score += min(max(confidence, 0.0), 1.0) * 0.15
        return score


class SelectorResolver:
    def __init__(self, strategies: list[SelectorStrategy] | None = None) -> None:
        self.strategies = strategies or [
            DomSelectorStrategy(),
            UIASelectorStrategy(),
            OCRSelectorStrategy(),
            VisionSelectorStrategy(),
            MockSelectorStrategy(),
        ]

    def resolve(self, target: Target, observation: Observation) -> ResolvedTarget:
        by_provider = {strategy.provider: strategy for strategy in self.strategies}
        attempts: list[dict] = []

        for provider in target.preferred:
            strategy = by_provider.get(provider)
            if strategy is None:
                attempts.append({"provider": provider.value, "status": "unavailable", "reason": "strategy not registered"})
                continue
            attempts.append({"provider": provider.value, "status": "attempted"})
            evidence = strategy.locate(target, observation)
            if evidence is None:
                attempts[-1] = {"provider": provider.value, "status": "no_match"}
                continue
            if evidence.click_point is None:
                attempts[-1] = {
                    "provider": provider.value,
                    "status": "no_click_point",
                    "confidence": evidence.confidence,
                    "reason": evidence.reason,
                }
                continue
            attempts[-1] = {
                "provider": provider.value,
                "status": "success",
                "confidence": evidence.confidence,
                "reason": evidence.reason,
            }
            return ResolvedTarget(
                target=target,
                evidence=with_resolution_metadata(
                    evidence,
                    target=target,
                    observation=observation,
                    attempts=attempts,
                ),
            )

        attempted = ", ".join(str(item["provider"]) for item in attempts) if attempts else "none"
        raise LookupError(f"Unable to resolve target '{target.display_name}'. Attempted: {attempted}.")


def with_resolution_metadata(
    evidence: LocationEvidence,
    *,
    target: Target,
    observation: Observation,
    attempts: list[dict],
) -> LocationEvidence:
    selected_provider = evidence.provider.value
    fallback_path = [str(item["provider"]) for item in attempts]
    metadata = dict(evidence.metadata)
    metadata["selector_resolution"] = {
        "target": target.display_name,
        "observation_provider": observation.provider.value,
        "selected_provider": selected_provider,
        "attempted_providers": fallback_path,
        "fallback_path": fallback_path,
        "used_fallback": fallback_path[0] != selected_provider if fallback_path else False,
        "attempts": attempts,
        "confidence": evidence.confidence,
        "confidence_level": confidence_level(evidence.confidence),
        "stability": selector_stability(evidence, target),
    }
    return replace(evidence, metadata=metadata)


def confidence_level(confidence: float) -> str:
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.6:
        return "medium"
    return "low"


def selector_stability(evidence: LocationEvidence, target: Target) -> dict[str, object]:
    if evidence.provider != ProviderKind.DOM:
        return {
            "level": "fallback",
            "score": round(evidence.confidence, 3),
            "reason": f"{evidence.provider.value} evidence is a fallback provider.",
            "signals": [],
        }

    element = evidence.metadata.get("element") if isinstance(evidence.metadata.get("element"), dict) else {}
    selector = str(target.selector or element.get("selector") or evidence.handle or "")
    signals: list[str] = []

    if target.test_id or element.get("test_id"):
        signals.append("test_id")
        return {
            "level": "stable",
            "score": 0.95,
            "reason": "Matched by data-testid/test id.",
            "signals": signals,
        }
    if selector and is_stable_dom_selector(selector):
        signals.append("stable_selector")
        return {
            "level": "stable",
            "score": 0.85,
            "reason": "Matched by stable id/name/aria/data selector.",
            "signals": signals,
        }

    text_signal = bool(target.text or target.label or target.contains_text or target.text_regex)
    role_signal = bool(target.role or element.get("role"))
    row_or_column_signal = bool(
        target.row_text
        or target.row_contains_text
        or target.row_text_regex
        or target.column_header
        or target.column_contains_text
        or target.column_text_regex
    )
    scope_or_near_signal = bool(
        target.near_text
        or target.near_contains_text
        or target.near_text_regex
        or target.scope_role
        or target.scope_text
        or target.scope_contains_text
    )
    if text_signal:
        signals.append("text")
    if role_signal:
        signals.append("role")
    if row_or_column_signal:
        signals.append("row_or_column")
    if scope_or_near_signal:
        signals.append("scope_or_near")
    if selector:
        signals.append("selector")

    if row_or_column_signal or scope_or_near_signal or (text_signal and role_signal):
        return {
            "level": "moderate",
            "score": 0.7,
            "reason": "Matched by semantic constraints, but not by a stable selector.",
            "signals": signals,
        }
    if text_signal:
        return {
            "level": "moderate",
            "score": 0.55,
            "reason": "Matched by text only; duplicate labels can reduce stability.",
            "signals": signals,
        }
    return {
        "level": "fragile",
        "score": 0.35,
        "reason": "Matched through a structural selector or weak DOM evidence.",
        "signals": signals,
    }


def is_stable_dom_selector(selector: str) -> bool:
    value = str(selector or "").strip()
    if not value:
        return False
    lowered = value.lower()
    if any(token in lowered for token in ("[data-testid=", "[data-test=", "[aria-label=", "[name=")):
        return True
    if re.fullmatch(r"#[A-Za-z][\w:-]*", value):
        return True
    return False
