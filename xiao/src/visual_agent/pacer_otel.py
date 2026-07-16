"""Optional OpenTelemetry projection for durable Pacer events.

The local JSON event is always the source of truth. This module is loaded only
after that event has been persisted, and every SDK/configuration/export error is
contained here so observability cannot change a task result.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .security import contains_secret_text


PACER_OTEL_ENABLED_ENV = "PACER_OTEL_ENABLED"
OTEL_SDK_DISABLED_ENV = "OTEL_SDK_DISABLED"
OTEL_INSTRUMENTATION_NAME = "visual_agent.pacer"
MAX_ATTRIBUTE_COUNT = 24
MAX_ATTRIBUTE_STRING_LENGTH = 96
MAX_IDENTITIES_PER_KIND = 4
MAX_CORRELATION_CONTEXTS = 2048

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,127}$")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_IDENTIFIER_FIELDS: tuple[tuple[str, str], ...] = (
    ("launch", "launch_id"),
    ("run", "run_id"),
    ("mission", "mission_id"),
    ("verification", "verification_id"),
    ("verification", "verification_run_id"),
    ("verification", "verification_batch_id"),
    ("verification", "batch_run_id"),
    ("verification", "verification_digest"),
)
_SAFE_TEXT_DATA_FIELDS = {
    "status": "pacer.result.status",
    "kind": "pacer.result.kind",
    "lifecycle_status": "pacer.lifecycle.status",
    "liveness_state": "pacer.liveness.state",
    "evidence_level": "pacer.evidence.level",
    "cache_status": "pacer.cache.status",
    "task_review_verdict": "pacer.review.verdict",
    "product_verdict": "pacer.product.verdict",
    "verification_verdict": "pacer.verification.verdict",
    "stop_reason": "pacer.stop.reason",
    "reason_code": "pacer.reason.code",
}
_SAFE_NUMBER_DATA_FIELDS = {
    "exit_code": "pacer.process.exit_code",
    "elapsed_seconds": "pacer.elapsed_seconds",
    "requested_steps": "pacer.verification.requested_steps",
    "executed_steps": "pacer.verification.executed_steps",
    "failed_steps": "pacer.verification.failed_steps",
    "payload_chars": "pacer.payload_chars",
}
_ERROR_STATUSES = frozenset(
    {"blocked", "crashed", "error", "failed", "failure", "stopped", "timeout"}
)
_OK_STATUSES = frozenset({"completed", "ok", "passed", "success", "succeeded", "verified"})

_CONTEXT_LOCK = threading.Lock()
_CONTEXT_BY_IDENTITY: OrderedDict[str, Any] = OrderedDict()


@dataclass(frozen=True)
class _OtelRuntime:
    tracer: Any
    link_type: Any
    span_kind: Any
    status_type: Any
    status_code: Any


@dataclass(frozen=True)
class _Identity:
    kind: str
    digest: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.digest}"


def export_pacer_event(
    event: Mapping[str, Any],
    *,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Project one already-persisted Pacer event into a configured OTel SDK."""
    if not _export_enabled(enabled):
        return {"status": "disabled", "exported": False}
    try:
        runtime = _load_otel_runtime()
    except (ImportError, ModuleNotFoundError):
        return {"status": "unavailable", "exported": False, "reason": "sdk_unavailable"}
    except Exception:  # noqa: BLE001 - telemetry configuration must be non-fatal
        return {"status": "degraded", "exported": False, "reason": "sdk_configuration_failed"}

    identities = _event_identities(event)
    attributes = _span_attributes(event, identities)
    event_name = _event_name(event.get("type"))
    timestamp_ns = _timestamp_ns(event.get("timestamp"))
    try:
        links = _links_for_identities(runtime, identities)
        span_kwargs: dict[str, Any] = {
            "kind": runtime.span_kind.INTERNAL,
            "attributes": attributes,
            "links": links,
            "record_exception": False,
            "set_status_on_exception": False,
        }
        if timestamp_ns is not None:
            span_kwargs["start_time"] = timestamp_ns
        with runtime.tracer.start_as_current_span("pacer.event", **span_kwargs) as span:
            event_kwargs: dict[str, Any] = {"attributes": _event_attributes(attributes)}
            if timestamp_ns is not None:
                event_kwargs["timestamp"] = timestamp_ns
            span.add_event(event_name, **event_kwargs)
            _set_span_status(span, runtime, event)
            context = span.get_span_context()
        _remember_context(identities, context)
    except Exception:  # noqa: BLE001 - exporter/span processor failures are isolated
        return {"status": "degraded", "exported": False, "reason": "export_failed"}
    return {
        "status": "exported",
        "exported": True,
        "identifier_count": len(identities),
        "link_count": len(links),
    }


def _load_otel_runtime() -> _OtelRuntime:
    # Importing the SDK as well as the API distinguishes a real optional install
    # from an API-only no-op provider. Provider/exporter setup remains owned by
    # the embedding application or standard OpenTelemetry configuration.
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider  # noqa: F401
    from opentelemetry.trace import Link, SpanKind, Status, StatusCode

    return _OtelRuntime(
        tracer=trace.get_tracer(OTEL_INSTRUMENTATION_NAME),
        link_type=Link,
        span_kind=SpanKind,
        status_type=Status,
        status_code=StatusCode,
    )


def _export_enabled(enabled: bool | None) -> bool:
    if _env_true(OTEL_SDK_DISABLED_ENV):
        return False
    if enabled is not None:
        return bool(enabled)
    return _env_true(PACER_OTEL_ENABLED_ENV)


def _env_true(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in _TRUE_VALUES


def _event_identities(event: Mapping[str, Any]) -> list[_Identity]:
    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    identities: list[_Identity] = []
    seen: set[str] = set()
    for kind, field in _IDENTIFIER_FIELDS:
        for source in (event, data):
            raw = _safe_identifier(source.get(field))
            if not raw:
                continue
            digest = hashlib.sha256(f"{kind}\0{raw}".encode("utf-8")).hexdigest()[:32]
            identity = _Identity(kind=kind, digest=digest)
            if identity.key not in seen:
                seen.add(identity.key)
                identities.append(identity)
    return identities


def _safe_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 128:
        return ""
    if contains_secret_text(text) or _looks_like_absolute_path(text):
        return ""
    return text if _SAFE_TOKEN.fullmatch(text) else ""


def _span_attributes(
    event: Mapping[str, Any],
    identities: list[_Identity],
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "pacer.event.type": _safe_enum(event.get("type")) or "unknown",
    }
    schema_version = _safe_int(event.get("schema_version"))
    sequence = _safe_int(event.get("sequence"))
    if schema_version is not None:
        attributes["pacer.schema_version"] = schema_version
    if sequence is not None:
        attributes["pacer.event.sequence"] = sequence
    event_id = _safe_identifier(event.get("event_id"))
    if event_id:
        attributes["pacer.event.id_hash"] = hashlib.sha256(
            event_id.encode("utf-8")
        ).hexdigest()[:32]

    by_kind: dict[str, list[str]] = {}
    for identity in identities:
        values = by_kind.setdefault(identity.kind, [])
        if len(values) < MAX_IDENTITIES_PER_KIND:
            values.append(identity.digest)
    for kind, values in by_kind.items():
        attributes[f"pacer.{kind}.id_hashes"] = tuple(values)

    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    for source, target in _SAFE_TEXT_DATA_FIELDS.items():
        value = _safe_enum(data.get(source))
        if value:
            attributes[target] = value
    for source, target in _SAFE_NUMBER_DATA_FIELDS.items():
        value = _safe_number(data.get(source))
        if value is not None:
            attributes[target] = value
    return _bounded_attributes(attributes)


def _event_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    allowed_prefixes = (
        "pacer.event.",
        "pacer.launch.",
        "pacer.run.",
        "pacer.mission.",
        "pacer.verification.",
        "pacer.result.",
    )
    return {
        key: value
        for key, value in attributes.items()
        if key.startswith(allowed_prefixes)
    }


def _links_for_identities(runtime: _OtelRuntime, identities: list[_Identity]) -> list[Any]:
    contexts: list[tuple[_Identity, Any]] = []
    seen_contexts: set[tuple[int, int] | int] = set()
    with _CONTEXT_LOCK:
        for identity in identities:
            context = _CONTEXT_BY_IDENTITY.get(identity.key)
            if not _valid_context(context):
                continue
            marker: tuple[int, int] | int
            trace_id = getattr(context, "trace_id", None)
            span_id = getattr(context, "span_id", None)
            marker = (
                (int(trace_id), int(span_id))
                if isinstance(trace_id, int) and isinstance(span_id, int)
                else id(context)
            )
            if marker in seen_contexts:
                continue
            seen_contexts.add(marker)
            _CONTEXT_BY_IDENTITY.move_to_end(identity.key)
            contexts.append((identity, context))
    return [
        runtime.link_type(
            context,
            attributes={
                "pacer.link.kind": identity.kind,
                "pacer.link.id_hash": identity.digest,
            },
        )
        for identity, context in contexts
    ]


def _remember_context(identities: list[_Identity], context: Any) -> None:
    if not _valid_context(context):
        return
    with _CONTEXT_LOCK:
        for identity in identities:
            _CONTEXT_BY_IDENTITY[identity.key] = context
            _CONTEXT_BY_IDENTITY.move_to_end(identity.key)
        while len(_CONTEXT_BY_IDENTITY) > MAX_CORRELATION_CONTEXTS:
            _CONTEXT_BY_IDENTITY.popitem(last=False)


def _valid_context(context: Any) -> bool:
    return context is not None and getattr(context, "is_valid", False) is True


def _set_span_status(span: Any, runtime: _OtelRuntime, event: Mapping[str, Any]) -> None:
    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    candidates = (
        data.get("status"),
        data.get("product_verdict"),
        data.get("task_review_verdict"),
    )
    statuses = {_safe_enum(value).lower() for value in candidates if _safe_enum(value)}
    if statuses & _ERROR_STATUSES:
        span.set_status(runtime.status_type(runtime.status_code.ERROR))
    elif statuses & _OK_STATUSES:
        span.set_status(runtime.status_type(runtime.status_code.OK))


def _event_name(value: Any) -> str:
    token = _safe_enum(value) or "unknown"
    return f"pacer.{token}"[:MAX_ATTRIBUTE_STRING_LENGTH]


def _safe_enum(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text or len(text) > MAX_ATTRIBUTE_STRING_LENGTH:
        return ""
    if contains_secret_text(text) or _looks_like_absolute_path(text):
        return ""
    return text if _SAFE_TOKEN.fullmatch(text) else ""


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(-(2**63), min(2**63 - 1, value))
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _safe_int(value: Any) -> int | None:
    number = _safe_number(value)
    return number if isinstance(number, int) else None


def _bounded_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    bounded: dict[str, Any] = {}
    for key, value in attributes.items():
        if len(bounded) >= MAX_ATTRIBUTE_COUNT:
            break
        clean_key = str(key)[:MAX_ATTRIBUTE_STRING_LENGTH]
        if isinstance(value, str):
            bounded[clean_key] = value[:MAX_ATTRIBUTE_STRING_LENGTH]
        elif isinstance(value, tuple):
            bounded[clean_key] = tuple(
                str(item)[:MAX_ATTRIBUTE_STRING_LENGTH]
                for item in value[:MAX_IDENTITIES_PER_KIND]
            )
        elif isinstance(value, (bool, int, float)):
            bounded[clean_key] = value
    return bounded


def _looks_like_absolute_path(value: str) -> bool:
    return value.startswith(("/", "\\\\")) or bool(_WINDOWS_ABSOLUTE_PATH.match(value))


def _timestamp_ns(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        timestamp = parsed.timestamp()
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(timestamp) or timestamp < 0:
        return None
    return int(timestamp * 1_000_000_000)


def _reset_context_registry() -> None:
    """Clear process-local correlation state for deterministic tests."""
    with _CONTEXT_LOCK:
        _CONTEXT_BY_IDENTITY.clear()
