from __future__ import annotations

from datetime import datetime, timezone

from visual_agent.pacer_events import append_pacer_event, list_pacer_events


def test_event_files_are_atomic_and_ordered(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    first = append_pacer_event(workspace, "launch_started", launch_id="launch-1", data={"status": "running"})
    second = append_pacer_event(workspace, "launch_finished", launch_id="launch-1", data={"status": "completed"})
    assert first["path"]
    assert second["path"]
    events = list_pacer_events(workspace)
    assert [event["type"] for event in events] == ["launch_started", "launch_finished"]
    assert not list((workspace / "pacer_native" / "events").rglob("*.tmp"))


def test_event_payload_redacts_secrets(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    append_pacer_event(workspace, "test", data={"api_key": "sk-secret-value-123456"})
    event = list_pacer_events(workspace)[0]
    assert event["data"]["api_key"]["redacted"] is True
    assert "sk-secret" not in str(event)


def test_events_with_identical_timestamps_keep_append_order(tmp_path, monkeypatch) -> None:
    from visual_agent import pacer_events

    frozen = datetime(2026, 7, 14, 8, 30, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is not None else frozen.replace(tzinfo=None)

    monkeypatch.setattr(pacer_events, "datetime", FrozenDatetime)
    workspace = tmp_path / ".agent-workspace"
    first = append_pacer_event(workspace, "launch_started", launch_id="launch-1")
    append_pacer_event(workspace, "launch_started", launch_id="launch-2")
    last = append_pacer_event(workspace, "launch_finished", launch_id="launch-1")

    assert first["timestamp"] == last["timestamp"]
    assert first["sequence"] < last["sequence"]
    events = [
        event
        for event in list_pacer_events(workspace)
        if event["launch_id"] == "launch-1"
    ]
    assert [event["type"] for event in events] == ["launch_started", "launch_finished"]


def test_windows_access_denied_process_probe_means_process_exists(monkeypatch) -> None:
    import ctypes

    from visual_agent.pacer_events import _windows_process_exists

    class FakeFunction:
        argtypes = None
        restype = None

        def __init__(self, result):
            self.result = result

        def __call__(self, *_args):
            return self.result

    class FakeKernel32:
        OpenProcess = FakeFunction(0)
        CloseHandle = FakeFunction(True)

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: FakeKernel32(), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)

    assert _windows_process_exists(424242) is True
