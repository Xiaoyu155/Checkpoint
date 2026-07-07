from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from .dom import normalize_text
from .models import Observation


ERROR_KEYWORDS = (
    "error",
    "failed",
    "failure",
    "exception",
    "timeout",
    "not found",
    "unauthorized",
    "forbidden",
    "错误",
    "失败",
    "异常",
    "超时",
    "未找到",
    "无权限",
    "请求失败",
    "网络异常",
)
LOADING_KEYWORDS = ("loading", "加载中", "请稍候", "处理中", "提交中")
EMPTY_KEYWORDS = ("empty", "no data", "暂无数据", "无数据", "还没有")
DIALOG_ROLES = {"dialog", "alertdialog", "modal", "alert"}
ACTION_ROLES = {"button", "link", "menuitem", "tab"}
INPUT_ROLES = {"textbox", "input", "textarea", "combobox", "searchbox"}
DEFAULT_TEMPLATE_PHRASES = (
    "作为一个ai",
    "我是一个ai",
    "无法提供",
    "请咨询专业人士",
    "很抱歉",
    "抱歉",
    "我不清楚",
)


@dataclass(frozen=True)
class ProductContractResult:
    passed: bool
    missing_sections: tuple[str, ...]
    missing_actions: tuple[str, ...]
    forbidden_entries: tuple[str, ...]
    errors: tuple[str, ...]
    state: dict[str, Any]


@dataclass(frozen=True)
class BrowserReadinessResult:
    passed: bool
    issues: tuple[str, ...]
    state: dict[str, Any]
    failed_requests: tuple[dict[str, Any], ...]
    console_errors: tuple[dict[str, Any], ...]
    page_errors: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AIResponseQualityResult:
    passed: bool
    issues: tuple[str, ...]
    text_length: int
    context_references: tuple[str, ...]
    question_references: tuple[str, ...]
    ai_source: str = "unknown"


# Open convention (x-ai-source response header). Order is worst-first: one
# fallback response taints the whole run's AI path.
AI_SOURCE_REAL = "real"
AI_SOURCE_DEGRADED = "degraded"
AI_SOURCE_FALLBACK = "fallback"
AI_SOURCE_UNKNOWN = "unknown"
_AI_SOURCE_SEVERITY = (AI_SOURCE_FALLBACK, AI_SOURCE_DEGRADED, AI_SOURCE_REAL)


def classify_ai_source(network_events: list[dict[str, Any]] | None, *, url_contains: str = "") -> dict[str, Any]:
    """Classify the AI path of a run from x-ai-source response headers.

    Returns the worst source seen (fallback > degraded > real) plus the
    labeled events. ``unknown`` means the app does not implement the
    convention — callers must not treat unknown as real.
    """
    labeled = [
        event
        for event in (network_events or [])
        if isinstance(event, dict)
        and event.get("ai_source")
        and (not url_contains or url_contains in str(event.get("url") or ""))
    ]
    sources = {str(event.get("ai_source")) for event in labeled}
    worst = next((name for name in _AI_SOURCE_SEVERITY if name in sources), None)
    if worst is None and sources:
        worst = sorted(sources)[0]  # unrecognized labels surface as-is
    return {
        "source": worst or AI_SOURCE_UNKNOWN,
        "labeled_event_count": len(labeled),
        "events": [
            {"url": str(event.get("url") or ""), "ai_source": str(event.get("ai_source") or "")}
            for event in labeled[:10]
        ],
    }


def observation_to_state(observation: Observation, *, max_text_items: int = 80) -> dict[str, Any]:
    text_items: list[str] = []
    buttons: list[str] = []
    inputs: list[str] = []
    dialogs: list[str] = []
    errors: list[str] = []
    loading: list[str] = []
    empty: list[str] = []

    for element in observation.elements:
        if not isinstance(element, dict):
            text = clean_text(str(element))
            role = ""
            tag = ""
        else:
            text = element_text(element)
            role = str(element.get("role") or "").lower()
            tag = str(element.get("tag") or element.get("tag_name") or "").lower()
        if text:
            append_unique(text_items, text, limit=max_text_items)
        lowered = text.lower()
        if role in ACTION_ROLES or tag in {"button", "a"}:
            append_unique(buttons, text or role or tag, limit=40)
        if role in INPUT_ROLES or tag in {"input", "textarea", "select"}:
            append_unique(inputs, text or role or tag, limit=40)
        if role in DIALOG_ROLES:
            append_unique(dialogs, text or role, limit=20)
        if contains_any(lowered, ERROR_KEYWORDS):
            append_unique(errors, text, limit=20)
        if contains_any(lowered, LOADING_KEYWORDS):
            append_unique(loading, text, limit=20)
        if contains_any(lowered, EMPTY_KEYWORDS):
            append_unique(empty, text, limit=20)

    metadata_text = metadata_visible_text(observation.metadata)
    for text in metadata_text:
        append_unique(text_items, text, limit=max_text_items)
        lowered = text.lower()
        if contains_any(lowered, ERROR_KEYWORDS):
            append_unique(errors, text, limit=20)
        if contains_any(lowered, LOADING_KEYWORDS):
            append_unique(loading, text, limit=20)
        if contains_any(lowered, EMPTY_KEYWORDS):
            append_unique(empty, text, limit=20)

    title = str(observation.metadata.get("title") or observation.metadata.get("window_title") or "").strip()
    url = str(observation.metadata.get("url") or observation.source or "").strip()
    return {
        "title": title or None,
        "url": url or None,
        "source": observation.source,
        "provider": observation.provider.value,
        "visible_text": tuple(text_items),
        "buttons": tuple(buttons),
        "inputs": tuple(inputs),
        "dialogs": tuple(dialogs),
        "errors": tuple(errors),
        "loading": tuple(loading),
        "empty_states": tuple(empty),
        "primary_actions": tuple(buttons[:8]),
        "text_count": len(text_items),
        "button_count": len(buttons),
        "input_count": len(inputs),
        "has_error": bool(errors),
        "is_loading": bool(loading),
        "is_empty": bool(empty),
    }


def evaluate_no_error_state(observation: Observation, *, network_events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    state = observation_to_state(observation)
    failed_requests = [
        event
        for event in (network_events or [])
        if isinstance(event, dict)
        and (event.get("type") == "request_failed" or event.get("ok") is False or int(event.get("status") or 200) >= 400)
    ]
    return {
        "passed": not state["errors"] and not failed_requests,
        "errors": state["errors"],
        "failed_requests": tuple(compact_network_event(event) for event in failed_requests[:10]),
        "state": state,
    }


def evaluate_browser_readiness(
    observation: Observation,
    params: dict[str, Any] | None = None,
    *,
    network_events: list[dict[str, Any]] | None = None,
) -> BrowserReadinessResult:
    params = params or {}
    state = observation_to_state(observation)
    visible_text_length = int(observation.metadata.get("visible_text_length") or len(" ".join(state["visible_text"])))
    interactive_count = int(observation.metadata.get("interactive_count") or len(observation.elements))
    min_text_length = int(params.get("min_text_length", 1) or 0)
    min_interactive = int(params.get("min_interactive", 0) or 0)
    require_title = bool(params.get("require_title", False))
    allow_blank = bool(params.get("allow_blank", False))
    check_network = bool(params.get("check_network", True))
    check_console = bool(params.get("check_console", True))
    issues: list[str] = []
    if not allow_blank and visible_text_length < min_text_length and interactive_count <= 0:
        issues.append("blank or empty browser page")
    if visible_text_length < min_text_length:
        issues.append(f"visible text shorter than {min_text_length}")
    if interactive_count < min_interactive:
        issues.append(f"interactive elements below {min_interactive}")
    if require_title and not state["title"]:
        issues.append("missing page title")
    if state["errors"]:
        issues.append("visible error text: " + "; ".join(state["errors"][:5]))
    failed_requests = tuple(
        compact_network_event(event)
        for event in (network_events or [])
        if isinstance(event, dict)
        and (event.get("type") == "request_failed" or event.get("ok") is False or int(event.get("status") or 200) >= 400)
    )
    if check_network and failed_requests:
        issues.append("failed network requests: " + str(len(failed_requests)))
    console_errors = tuple(observation.metadata.get("console_errors") or ())
    page_errors = tuple(observation.metadata.get("page_errors") or ())
    if check_console and console_errors:
        issues.append("browser console errors: " + str(len(console_errors)))
    if page_errors:
        issues.append("browser page errors: " + str(len(page_errors)))
    return BrowserReadinessResult(
        passed=not issues,
        issues=tuple(issues),
        state=state,
        failed_requests=failed_requests[:10],
        console_errors=console_errors[:10],
        page_errors=page_errors[:10],
    )


def evaluate_product_contract(
    observation: Observation,
    params: dict[str, Any],
    *,
    network_events: list[dict[str, Any]] | None = None,
) -> ProductContractResult:
    state = observation_to_state(observation)
    haystack = normalized_state_text(state)
    required_sections = tuple_param(params, "required_sections")
    required_actions = tuple_param(params, "must_have_actions")
    forbidden = tuple_param(params, "forbidden_entries") + tuple_param(params, "forbidden_any")

    missing_sections = tuple(item for item in required_sections if normalize_text(item) not in haystack)
    actions_text = normalize_text(" ".join(state["buttons"]))
    missing_actions = tuple(item for item in required_actions if normalize_text(item) not in actions_text and normalize_text(item) not in haystack)
    forbidden_hits = tuple(item for item in forbidden if normalize_text(item) in haystack)
    no_error = evaluate_no_error_state(observation, network_events=network_events) if bool(params.get("no_error_state", False)) else {"passed": True, "errors": ()}
    min_actions = int(params.get("min_primary_actions", 0) or 0)
    min_action_error = ("primary actions below minimum",) if len(state["primary_actions"]) < min_actions else ()
    errors = tuple(str(item) for item in no_error.get("errors", ())) + min_action_error
    return ProductContractResult(
        passed=not missing_sections and not missing_actions and not forbidden_hits and not errors,
        missing_sections=missing_sections,
        missing_actions=missing_actions,
        forbidden_entries=forbidden_hits,
        errors=errors,
        state=state,
    )


def product_contract_failure_message(result: ProductContractResult) -> str:
    parts = []
    if result.missing_sections:
        parts.append("missing sections: " + ", ".join(result.missing_sections))
    if result.missing_actions:
        parts.append("missing actions: " + ", ".join(result.missing_actions))
    if result.forbidden_entries:
        parts.append("forbidden entries: " + ", ".join(result.forbidden_entries))
    if result.errors:
        parts.append("errors: " + ", ".join(result.errors))
    return "product contract failed (" + "; ".join(parts) + ")"


def browser_readiness_failure_message(result: BrowserReadinessResult) -> str:
    return "browser readiness failed (" + "; ".join(result.issues) + ")"


def evaluate_ai_response_quality(
    params: dict[str, Any],
    observation: Observation | None = None,
    network_events: list[dict[str, Any]] | None = None,
) -> AIResponseQualityResult:
    text = str(params.get("text") or params.get("response") or "").strip()
    if not text and observation is not None:
        text = "\n".join(observation_to_state(observation)["visible_text"])
    normalized = normalize_text(text)
    issues: list[str] = []
    if not normalized:
        issues.append("empty response")
    min_length = int(params.get("min_length", 8) or 0)
    if len(text.strip()) < min_length:
        issues.append(f"response shorter than {min_length} characters")
    forbidden = tuple_param(params, "forbidden_phrases") or DEFAULT_TEMPLATE_PHRASES
    hits = [phrase for phrase in forbidden if normalize_text(phrase) and normalize_text(phrase) in normalized]
    if hits:
        issues.append("template or forbidden phrase: " + ", ".join(hits[:5]))
    if repetition_ratio(text) > float(params.get("max_repetition_ratio", 0.45)):
        issues.append("response is too repetitive")

    question_refs = reference_hits(text, str(params.get("question") or ""))
    if params.get("question") and bool(params.get("require_answer_relevance", True)) and not question_refs:
        issues.append("response does not reference the user question")
    context_refs = reference_hits(text, str(params.get("previous_context") or params.get("context") or ""))
    if bool(params.get("require_context_reference", False)) and not context_refs:
        issues.append("response does not reference previous context")
    if bool(params.get("require_specific_advice", False)) and not contains_any(normalized, ("建议", "步骤", "可以", "应该", "需要", "first", "step", "recommend")):
        issues.append("response lacks specific advice")

    classification = classify_ai_source(network_events, url_contains=str(params.get("ai_url_contains") or ""))
    ai_source = str(classification["source"])
    if bool(params.get("require_real_ai", False)):
        if ai_source in (AI_SOURCE_DEGRADED, AI_SOURCE_FALLBACK):
            issues.append(
                f"AI path is '{ai_source}', not the real model — a degraded response must not be reported as a commercial-path pass"
            )
        elif ai_source == AI_SOURCE_UNKNOWN:
            issues.append(
                "require_real_ai is set but no response carried the x-ai-source header; "
                "the app must implement the convention before real-AI verification can pass"
            )
    return AIResponseQualityResult(
        passed=not issues,
        issues=tuple(issues),
        text_length=len(text),
        context_references=tuple(context_refs),
        question_references=tuple(question_refs),
        ai_source=ai_source,
    )


def ai_quality_failure_message(result: AIResponseQualityResult) -> str:
    return "AI response quality failed (" + "; ".join(result.issues) + ")"


def element_text(element: dict[str, Any]) -> str:
    values = []
    for key in ("text", "label", "name", "value", "aria_label", "title", "placeholder", "selector"):
        value = element.get(key)
        if value not in (None, ""):
            values.append(str(value))
    return clean_text(" ".join(values))


def metadata_visible_text(metadata: dict[str, Any]) -> tuple[str, ...]:
    raw = metadata.get("visible_text")
    if isinstance(raw, str):
        return (clean_text(raw),)
    if isinstance(raw, (list, tuple)):
        return tuple(clean_text(str(item)) for item in raw if clean_text(str(item)))
    return ()


def normalized_state_text(state: dict[str, Any]) -> str:
    parts = []
    for key in ("title", "url", "visible_text", "buttons", "inputs", "dialogs", "errors", "empty_states"):
        value = state.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    return normalize_text(" ".join(parts))


def tuple_param(params: dict[str, Any], name: str) -> tuple[str, ...]:
    value = params.get(name)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return (str(value),)


def clean_text(value: str) -> str:
    return " ".join(str(value).split())


def append_unique(items: list[str], value: str, *, limit: int) -> None:
    text = clean_text(value)
    if text and text not in items and len(items) < limit:
        items.append(text)


def contains_any(value: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword.lower() in value for keyword in keywords)


def compact_network_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": event.get("type"),
        "url": event.get("url"),
        "method": event.get("method"),
        "status": event.get("status"),
        "failure": event.get("failure"),
    }


def repetition_ratio(text: str) -> float:
    tokens = [token for token in normalize_text(text).replace("，", " ").replace("。", " ").split() if token]
    if len(tokens) < 6:
        return 0.0
    counts = Counter(tokens)
    repeated = sum(count for count in counts.values() if count > 1)
    return repeated / max(1, len(tokens))


def reference_hits(response: str, source: str) -> list[str]:
    source_text = clean_text(source)
    if not source_text:
        return []
    response_norm = normalize_text(response)
    hits = []
    for token in reference_tokens(source_text):
        if normalize_text(token) in response_norm:
            append_unique(hits, token, limit=10)
    return hits


def reference_tokens(text: str) -> tuple[str, ...]:
    spaced = text.replace("，", " ").replace("。", " ").replace("？", " ").replace("?", " ")
    tokens = [item.strip() for item in spaced.split() if len(item.strip()) >= 2]
    compact = clean_text(text)
    if tokens and not (len(tokens) == 1 and tokens[0] == compact and contains_cjk(compact)):
        return tuple(tokens[:20])
    if contains_cjk(compact):
        return tuple(
            compact[index : index + 2]
            for index in range(0, min(len(compact), 30) - 1)
            if len(compact[index : index + 2]) == 2
        )
    return tuple(compact[index : index + 2] for index in range(0, min(len(compact), 20), 2) if len(compact[index : index + 2]) == 2)


def contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)
