from __future__ import annotations

from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
import urllib.request

from visual_agent import pacer_model


@contextmanager
def _gateway(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pacer_model, "legacy_execution_running", lambda _root: False)
    settings = {
        "workspace_root": str(tmp_path),
        "allowed_roots": [str(tmp_path)],
        "auth_token": "protocol-test-key",
        "runs_dir": str(tmp_path / "runs"),
    }
    server = pacer_model.PacerModelServer(
        ("127.0.0.1", 0),
        settings,
        engine=lambda _prompt, _root, _progress: ("protocol reply", {"success": True}),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _post(url: str, path: str, body: dict):
    req = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": "protocol-test-key", "Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=5)


def test_openai_chat_completion_returns_chat_schema(tmp_path, monkeypatch):
    body = {"model": "pacer", "messages": [{"role": "user", "content": "Fix sample.py"}]}
    with _gateway(tmp_path, monkeypatch) as url, _post(url, "/v1/chat/completions", body) as response:
        data = json.load(response)

    assert data["object"] == "chat.completion"
    message = data["choices"][0]["message"]
    assert message["role"] == "assistant"
    assert message["content"].startswith("Pacer 已接管任务（")
    assert message["content"].endswith("\nprotocol reply")
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"] is None


def test_openai_responses_returns_response_schema(tmp_path, monkeypatch):
    body = {"model": "pacer", "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Fix sample.py"}]}]}
    with _gateway(tmp_path, monkeypatch) as url, _post(url, "/v1/responses", body) as response:
        data = json.load(response)

    assert data["object"] == "response"
    assert data["status"] == "completed"
    assert data["output"][0]["type"] == "message"
    assert data["output"][0]["content"][0]["type"] == "output_text"
    assert data["output_text"].startswith("Pacer 已接管任务（")
    assert data["output_text"].endswith("\nprotocol reply")
    assert data["usage"] is None


def test_openai_chat_stream_uses_chat_chunks_and_done_sentinel(tmp_path, monkeypatch):
    body = {"model": "pacer", "messages": [{"role": "user", "content": "Fix sample.py"}], "stream": True}
    with _gateway(tmp_path, monkeypatch) as url, _post(url, "/v1/chat/completions", body) as response:
        stream = response.read().decode("utf-8")

    chunks = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: {")]
    assert any(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert any(chunk["choices"][0].get("finish_reason") == "stop" for chunk in chunks if chunk["choices"])
    assert stream.rstrip().endswith("data: [DONE]")
    assert "event: response." not in stream


def test_openai_responses_stream_uses_typed_response_events(tmp_path, monkeypatch):
    body = {"model": "pacer", "input": "Fix sample.py", "stream": True}
    with _gateway(tmp_path, monkeypatch) as url, _post(url, "/v1/responses", body) as response:
        stream = response.read().decode("utf-8")

    events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: {")]
    event_types = [event["type"] for event in events]
    assert "response.created" in event_types
    assert "response.output_text.delta" in event_types
    assert "response.completed" in event_types
    completed = next(event["response"] for event in events if event["type"] == "response.completed")
    assert completed["output"][0]["content"][0]["text"].endswith("protocol reply")
    assert all(event.get("object") != "chat.completion.chunk" for event in events)
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
