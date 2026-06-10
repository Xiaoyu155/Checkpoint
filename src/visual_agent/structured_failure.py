from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .versioning import migrate_structured_failure_payload


STRUCTURED_FAILURE_SCHEMA_VERSION = 1
STRUCTURED_FAILURE_V1_FIELDS = (
    "schema_version",
    "step_id",
    "action",
    "provider",
    "expected",
    "actual_visible",
    "page_url",
    "page_state",
    "screenshot_path",
    "root_cause",
    "confidence",
    "suggested_fix",
    "related_files",
)

HYDRATION_MISMATCH_MARKERS = (
    "hydration-mismatch",
    "a tree hydrated but some attributes of the server rendered html didn't match the client properties",
    "hydration failed because the server rendered",
    "react hydration error",
    "react.dev/link/hydration-mismatch",
    "nextjs.org/docs/messages/react-hydration-error",
)


@dataclass(frozen=True)
class StructuredFailure:
    step_id: str
    action: str
    provider: str
    expected: str
    actual_visible: list[str]
    page_url: str
    page_state: str
    screenshot_path: str
    root_cause: str
    confidence: float
    suggested_fix: str
    related_files: list[str]


def structured_failure_to_dict(failure: StructuredFailure) -> dict[str, Any]:
    return {
        "schema_version": STRUCTURED_FAILURE_SCHEMA_VERSION,
        "step_id": failure.step_id,
        "action": failure.action,
        "provider": failure.provider,
        "expected": failure.expected,
        "actual_visible": list(failure.actual_visible),
        "page_url": failure.page_url,
        "page_state": failure.page_state,
        "screenshot_path": failure.screenshot_path,
        "root_cause": failure.root_cause,
        "confidence": failure.confidence,
        "suggested_fix": failure.suggested_fix,
        "related_files": list(failure.related_files),
    }


def structured_failure_from_dict(payload: dict[str, Any]) -> StructuredFailure:
    normalized = migrate_structured_failure_payload(payload)
    return StructuredFailure(
        step_id=str(normalized.get("step_id") or ""),
        action=str(normalized.get("action") or ""),
        provider=str(normalized.get("provider") or ""),
        expected=str(normalized.get("expected") or ""),
        actual_visible=[str(item) for item in normalized.get("actual_visible", []) if str(item)]
        if isinstance(normalized.get("actual_visible"), list)
        else [],
        page_url=str(normalized.get("page_url") or ""),
        page_state=str(normalized.get("page_state") or ""),
        screenshot_path=str(normalized.get("screenshot_path") or ""),
        root_cause=str(normalized.get("root_cause") or ""),
        confidence=float(normalized.get("confidence") or 0.0),
        suggested_fix=str(normalized.get("suggested_fix") or ""),
        related_files=[str(item) for item in normalized.get("related_files", []) if str(item)]
        if isinstance(normalized.get("related_files"), list)
        else [],
    )


def empty_structured_failure(*, message: str = "", suggested_fix: str = "") -> dict[str, Any]:
    return structured_failure_to_dict(
        StructuredFailure(
            step_id="",
            action="",
            provider="",
            expected=message,
            actual_visible=[],
            page_url="",
            page_state="unknown",
            screenshot_path="",
            root_cause="env_error" if message else "",
            confidence=0.0,
            suggested_fix=suggested_fix,
            related_files=[],
        )
    )


def structured_failure_from_diagnosis(
    diagnosis: dict[str, Any],
    *,
    project_root: Path | None = None,
    workflow_name: str = "",
    provider: str | None = None,
) -> StructuredFailure:
    observation = diagnosis.get("observation") if isinstance(diagnosis.get("observation"), dict) else {}
    artifacts = diagnosis.get("artifacts") if isinstance(diagnosis.get("artifacts"), dict) else {}
    visual_provider = provider or str(observation.get("provider") or diagnosis.get("provider") or "")
    actual_visible = [str(item) for item in observation.get("visible_text", []) if str(item)] if isinstance(observation.get("visible_text"), list) else []
    page_url = page_url_from_diagnosis(diagnosis)
    cause, confidence = classify_root_cause(diagnosis)
    suggested_fix = suggested_fix_for_cause(cause, diagnosis)
    return StructuredFailure(
        step_id=str(diagnosis.get("step_id") or ""),
        action=str(diagnosis.get("action") or ""),
        provider=str(visual_provider or ""),
        expected=str(diagnosis.get("expected") or ""),
        actual_visible=actual_visible[:20],
        page_url=page_url,
        page_state=classify_page_state(diagnosis, actual_visible),
        screenshot_path=str(artifacts.get("screenshot") or ""),
        root_cause=cause,
        confidence=confidence,
        suggested_fix=suggested_fix,
        related_files=related_files_for_url(page_url, project_root=project_root, workflow_name=workflow_name),
    )


def classify_root_cause(failure_context: dict[str, Any]) -> tuple[str, float]:
    action = str(failure_context.get("action") or "")
    error = str(failure_context.get("error") or "").lower()
    actual = str(failure_context.get("actual") or "").lower()
    expected = str(failure_context.get("expected") or "").lower()
    observation = failure_context.get("observation") if isinstance(failure_context.get("observation"), dict) else {}
    selector_summary = failure_context.get("selector_summary") if isinstance(failure_context.get("selector_summary"), dict) else {}
    visible_text = observation.get("visible_text") if isinstance(observation.get("visible_text"), list) else []
    visible_blob = " ".join(str(item).lower() for item in visible_text if str(item))
    context_blob = " ".join(
        part
        for part in (
            error,
            actual,
            expected,
            visible_blob,
            str(failure_context.get("dom_excerpt") or "").lower(),
        )
        if part
    )

    if any(marker in context_blob for marker in HYDRATION_MISMATCH_MARKERS):
        return "known_issue", 0.95

    if action in {"wait_for", "wait_for_text"} and ("text" in expected or "target" in expected or "url" in expected):
        return "assertion_wrong", 0.7
    if any(token in error + " " + actual for token in ("connection refused", "err_connection", "net::")):
        return "env_error", 0.82
    if any(token in error + " " + actual for token in ("timeout", "timed out")) and not action.startswith("assert") and action not in {"wait_for", "wait_for_text"}:
        return "env_error", 0.72
    if any(token in actual for token in ("404", "not found", "chunk", "asset", "route")):
        return "build_stale", 0.7
    if action in {"click", "type", "paste"} or "expected target" in expected:
        if selector_summary or "target" in failure_context:
            return "element_missing", 0.78
        return "element_missing", 0.62
    if action in {"assert_text", "wait_for", "wait_for_text", "assert_text_contract"}:
        visible = observation.get("visible_text") if isinstance(observation.get("visible_text"), list) else []
        if visible:
            return "assertion_wrong", 0.72
        return "element_missing", 0.58
    return "env_error" if not observation.get("available", True) else "assertion_wrong", 0.45


def classify_page_state(diagnosis: dict[str, Any], actual_visible: list[str]) -> str:
    text = " ".join(actual_visible).lower() + " " + str(diagnosis.get("actual") or "").lower()
    if any(token in text for token in ("loading", "加载")):
        return "loading"
    if any(token in text for token in ("error", "exception", "failed", "错误", "失败")):
        return "error"
    if any(token in text for token in ("login", "sign in", "登录")):
        return "authenticated" if any(token in text for token in ("logout", "account", "dashboard")) else "unauthenticated"
    if not actual_visible and "elements=0" in text:
        return "empty"
    return "unknown"


def page_url_from_diagnosis(diagnosis: dict[str, Any]) -> str:
    observation = diagnosis.get("observation") if isinstance(diagnosis.get("observation"), dict) else {}
    source = str(observation.get("source") or "")
    if source.startswith(("http://", "https://", "file://")):
        return source
    actual = str(diagnosis.get("actual") or "")
    marker = "source="
    if marker in actual:
        value = actual.split(marker, 1)[1].split(";", 1)[0].strip()
        if value.startswith(("http://", "https://", "file://")):
            return value
    return ""


def suggested_fix_for_cause(cause: str, diagnosis: dict[str, Any]) -> str:
    suggestions = diagnosis.get("recovery_suggestions") if isinstance(diagnosis.get("recovery_suggestions"), list) else []
    if suggestions:
        return str(suggestions[0])
    if cause == "known_issue":
        return (
            "This is a known Next.js hydration mismatch. "
            "Treat it as a demo/framework issue, not a workflow regression, unless the app is expected to be SSR-stable here."
        )
    if cause == "element_missing":
        return "Check that the target element is rendered, visible, and has a stable label, role, selector, or test id."
    if cause == "build_stale":
        return "Rebuild or restart the dev server, then verify the route and static assets exist."
    if cause == "assertion_wrong":
        return "Compare the expected assertion with the actual visible UI text and update either the app state or the workflow assertion."
    if cause == "env_error":
        return "Start the app or fix the test environment, then rerun the workflow."
    return "Inspect the failed step evidence and rerun after the smallest safe change."


def related_files_for_url(page_url: str, *, project_root: Path | None, workflow_name: str = "") -> list[str]:
    route = route_from_url(page_url)
    candidates = route_file_candidates(route, workflow_name=workflow_name)
    if project_root is None:
        return candidates[:8]
    existing: list[str] = []
    for candidate in candidates:
        path = project_root / candidate
        if path.exists():
            existing.append(str(path))
    return existing[:8] if existing else candidates[:8]


def route_from_url(page_url: str) -> str:
    from urllib.parse import urlparse

    if not page_url:
        return ""
    parsed = urlparse(page_url)
    path = parsed.path if parsed.scheme else page_url
    return path.strip("/")


def route_file_candidates(route: str, *, workflow_name: str = "") -> list[str]:
    route = route.strip("/") or "home"
    parts = [part for part in route.split("/") if part]
    pascal = "".join(part.replace("-", " ").replace("_", " ").title().replace(" ", "") for part in parts) or "Home"
    leaf = parts[-1] if parts else workflow_name or "home"
    candidates = [
        f"src/pages/{pascal}.tsx",
        f"src/pages/{pascal}.jsx",
        f"src/pages/{pascal}.vue",
        f"src/pages/{leaf}.tsx",
        f"src/pages/{leaf}.jsx",
        f"src/routes/{route}/+page.svelte",
        f"app/{route}/page.tsx",
        f"pages/{route}.tsx",
        f"pages/{route}.jsx",
    ]
    if workflow_name:
        candidates.append(f"workflows/{workflow_name}.yaml")
    return candidates
