from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from visual_agent.pacer_events import append_pacer_event, list_pacer_events
from visual_agent.pacer_otel import (
    MAX_ATTRIBUTE_COUNT,
    MAX_ATTRIBUTE_STRING_LENGTH,
    _OtelRuntime,
    _reset_context_registry,
    export_pacer_event,
)


@dataclass(frozen=True)
class FakeContext:
    trace_id: int
    span_id: int
    is_valid: bool = True


class FakeLink:
    def __init__(self, context, attributes=None) -> None:
        self.context = context
        self.attributes = dict(attributes or {})


class FakeStatusCode:
    OK = "ok"
    ERROR = "error"


class FakeStatus:
    def __init__(self, status_code) -> None:
        self.status_code = status_code


class FakeSpanKind:
    INTERNAL = "internal"


class FakeSpan:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.events: list[dict[str, Any]] = []
        self.status = None

    def add_event(self, name, **kwargs) -> None:
        self.events.append({"name": name, **kwargs})

    def set_status(self, status) -> None:
        self.status = status

    def get_span_context(self) -> FakeContext:
        return self.context


class FakeTracer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    @contextmanager
    def start_as_current_span(self, name, **kwargs):
        if self.fail:
            raise RuntimeError("export failed")
        context = FakeContext(trace_id=100 + len(self.calls), span_id=200 + len(self.calls))
        span = FakeSpan(context)
        self.calls.append({"name": name, "span": span, **kwargs})
        yield span


def _runtime(tracer: FakeTracer) -> _OtelRuntime:
    return _OtelRuntime(
        tracer=tracer,
        link_type=FakeLink,
        span_kind=FakeSpanKind,
        status_type=FakeStatus,
        status_code=FakeStatusCode,
    )


def _event(event_type: str = "verification_batch_finished", **data: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_id": "event-1",
        "sequence": 7,
        "timestamp": "2026-07-15T08:30:00+00:00",
        "type": event_type,
        "launch_id": "launch-1",
        "data": data,
        "path": r"D:\private\workspace\event.json",
    }


def test_otel_is_disabled_without_loading_optional_sdk(monkeypatch) -> None:
    from visual_agent import pacer_otel

    monkeypatch.delenv("PACER_OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.setattr(
        pacer_otel,
        "_load_otel_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("SDK must not load")),
    )

    assert export_pacer_event(_event())["status"] == "disabled"


def test_missing_sdk_and_export_failure_are_non_fatal(monkeypatch) -> None:
    from visual_agent import pacer_otel

    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.setattr(
        pacer_otel,
        "_load_otel_runtime",
        lambda: (_ for _ in ()).throw(ModuleNotFoundError("opentelemetry.sdk")),
    )
    assert export_pacer_event(_event(), enabled=True) == {
        "status": "unavailable",
        "exported": False,
        "reason": "sdk_unavailable",
    }

    monkeypatch.setattr(pacer_otel, "_load_otel_runtime", lambda: _runtime(FakeTracer(fail=True)))
    assert export_pacer_event(_event(), enabled=True)["reason"] == "export_failed"


def test_local_event_remains_durable_when_otel_raises(tmp_path, monkeypatch) -> None:
    from visual_agent import pacer_otel

    monkeypatch.setattr(
        pacer_otel,
        "export_pacer_event",
        lambda _event: (_ for _ in ()).throw(RuntimeError("processor failed")),
    )
    workspace = tmp_path / ".agent-workspace"

    result = append_pacer_event(
        workspace,
        "launch_finished",
        launch_id="launch-1",
        data={"status": "completed"},
    )

    assert result["path"]
    assert [item["type"] for item in list_pacer_events(workspace)] == ["launch_finished"]


def test_projection_is_bounded_and_excludes_sensitive_or_path_data(monkeypatch) -> None:
    from visual_agent import pacer_otel

    _reset_context_registry()
    tracer = FakeTracer()
    monkeypatch.setattr(pacer_otel, "_load_otel_runtime", lambda: _runtime(tracer))
    event = _event(
        status="failed",
        run_id="run-1",
        mission_id="mission-1",
        batch_run_id="verify-1",
        prompt="explain every private source file",
        api_key="sk-super-secret-value",
        repo_root=r"D:\private\workspace",
        reason_code="x" * 500,
    )

    result = export_pacer_event(event, enabled=True)

    assert result["exported"] is True
    call = tracer.calls[0]
    serialized = json.dumps(call, default=lambda value: value.__dict__, ensure_ascii=False)
    assert "private" not in serialized
    assert "prompt" not in serialized
    assert "super-secret" not in serialized
    attributes = call["attributes"]
    assert len(attributes) <= MAX_ATTRIBUTE_COUNT
    assert all(
        len(item) <= MAX_ATTRIBUTE_STRING_LENGTH
        for value in attributes.values()
        for item in ((value,) if isinstance(value, str) else value if isinstance(value, tuple) else ())
    )
    assert attributes["pacer.launch.id_hashes"] != ("launch-1",)
    assert attributes["pacer.run.id_hashes"] != ("run-1",)


def test_events_create_status_and_real_context_links(monkeypatch) -> None:
    from visual_agent import pacer_otel

    _reset_context_registry()
    tracer = FakeTracer()
    monkeypatch.setattr(pacer_otel, "_load_otel_runtime", lambda: _runtime(tracer))

    first = export_pacer_event(_event("launch_started", status="running"), enabled=True)
    second = export_pacer_event(
        _event(
            "verification_batch_finished",
            status="passed",
            run_id="run-1",
            mission_id="mission-1",
            batch_run_id="verify-1",
        ),
        enabled=True,
    )

    assert first["link_count"] == 0
    assert second["identifier_count"] == 4
    assert second["link_count"] == 1
    assert tracer.calls[1]["links"][0].context == tracer.calls[0]["span"].context
    assert tracer.calls[1]["links"][0].attributes["pacer.link.kind"] == "launch"
    assert tracer.calls[1]["span"].events[0]["name"] == "pacer.verification_batch_finished"
    assert tracer.calls[1]["span"].status.status_code == FakeStatusCode.OK
