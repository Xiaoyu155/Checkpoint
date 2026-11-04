from __future__ import annotations

from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import queue
import threading
import time
import urllib.error
import urllib.request

import pytest

from visual_agent import model_admin, pacer_model as model


@contextmanager
def gateway(tmp_path, monkeypatch, *, engine=None, run_timeout_seconds=30.0):
    monkeypatch.setattr(model, "legacy_execution_running", lambda _root: False)
    server = model.PacerModelServer(
        ("127.0.0.1", 0),
        {
            "workspace_root": str(tmp_path),
            "allowed_roots": [str(tmp_path)],
            "auth_token": "test-key",
            "runs_dir": str(tmp_path / "runs"),
            "run_timeout_seconds": run_timeout_seconds,
        },
        engine=engine or (lambda _prompt, _root, _progress: ("done", {"success": True})),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def post(url, path="/v1/messages", *, prompt="Fix example.py", key="test-key", **extra):
    body = {
        "model": "pacer",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
        **extra,
    }
    request = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": key, "Content-Type": "application/json"},
    )
    return urllib.request.urlopen(request, timeout=3)


def test_stream_marks_connection_aborted_as_disconnected():
    disconnected = threading.Event()

    class Events:
        def get(self, *args, **kwargs):
            pytest.fail("The initial SSE write should fail before reading events")

    class AbortedWriter:
        def write(self, _payload):
            raise ConnectionAbortedError("client connection aborted")

        def flush(self):
            return None

    handler = object.__new__(model.PacerModelHandler)
    handler.wfile = AbortedWriter()
    def abort_headers(*_args, **_kwargs):
        raise ConnectionAbortedError("client disconnected while sending headers")

    handler.send_response = abort_headers
    handler.send_header = lambda *_args, **_kwargs: None
    handler.end_headers = lambda: None

    handler._stream(Events(), "pm-aborted", disconnected=disconnected)

    assert disconnected.is_set()


def test_publish_result_stops_when_full_queue_hits_deadline():
    events = queue.Queue(maxsize=1)
    events.put_nowait(("text", "occupy queue"))
    disconnected = threading.Event()
    started = time.monotonic()

    published = model.PacerModelHandler._publish_result(
        events,
        "final result",
        disconnected,
        started + 0.05,
    )

    assert published is False
    assert time.monotonic() - started < 0.5


def test_executor_submit_failure_returns_503_and_releases_run_lock(tmp_path, monkeypatch):
    class RejectedExecutor:
        def submit(self, _function):
            raise RuntimeError("executor is shut down")

        def shutdown(self, **_kwargs):
            return None

    with gateway(tmp_path, monkeypatch) as (url, server):
        server.executor.shutdown(wait=True)
        server.executor = RejectedExecutor()

        with pytest.raises(urllib.error.HTTPError) as error:
            post(url)

        assert error.value.code == 503
        assert not server.run_lock.locked()
        record = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
        assert record["status"] == "failed"
        assert "executor rejected" in record["error"]


def test_unauthorized_admin_post_is_rejected_before_admin_handler(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(model_admin, "handle", lambda *_args: calls.append(True) or True)
    with gateway(tmp_path, monkeypatch) as (url, _server):
        with pytest.raises(urllib.error.HTTPError) as error:
            post(url, "/api/model-config", key="wrong-key", prompt="{}")

    assert error.value.code == 401
    assert calls == []


def test_non_streaming_request_returns_at_wall_clock_deadline(tmp_path, monkeypatch):
    release = threading.Event()

    def engine(_prompt, _root, _progress):
        release.wait(2)
        return "late", {"success": True}

    with gateway(tmp_path, monkeypatch, engine=engine, run_timeout_seconds=0.05) as (url, server):
        try:
            with pytest.raises(urllib.error.HTTPError) as error:
                post(url)
            assert error.value.code == 504
        finally:
            release.set()
        deadline = time.monotonic() + 2
        while server.run_lock.locked() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not server.run_lock.locked()


def test_primary_exception_marks_queued_amendments_failed(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def engine(prompt, _root, _progress):
        if prompt == "Fix example.py":
            started.set()
            release.wait(2)
            raise RuntimeError("primary task failed")
        pytest.fail("Queued amendment must not run after primary failure")

    with gateway(tmp_path, monkeypatch, engine=engine) as (url, _server):
        first = post(url, prompt="Fix example.py", stream=True)
        try:
            assert started.wait(1)
            with post(url, prompt="Follow-up: update example.py") as response:
                assert "已进入当前 Pacer" in json.load(response)["content"][0]["text"]
        finally:
            release.set()
        stream_text = first.read().decode("utf-8")
        first.close()

    assert "Follow-up: update example.py" in stream_text
    assert "明确失败" in stream_text
    record = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
    assert record["failed_amendments"] == [
        {"prompt": "Follow-up: update example.py", "status": "failed", "error": "主运行异常终止，未执行"}
    ]
