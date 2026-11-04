from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

import pytest

from visual_agent import pacer_model as model


def test_serve_lock_rejects_duplicate_and_releases(tmp_path):
    settings = {"workspace_root": str(tmp_path), "auth_token": "test-only"}
    server = model.PacerModelServer(("127.0.0.1", 0), settings)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            model.PacerModelServer(("127.0.0.1", 0), settings)
    finally:
        server.server_close()
    reopened = model.PacerModelServer(("127.0.0.1", 0), settings)
    reopened.server_close()
    assert not (tmp_path / ".pacer/model-gateway/serve.pid").exists()


def test_pacer_model_hot_reloads_providers_json_without_restart(tmp_path, monkeypatch):
    import os
    config_file = Path(os.environ["PACER_PROVIDERS_FILE"])
    config_file.parent.mkdir(parents=True, exist_ok=True)
    data = {"tier1_free": {"antigravity_gemini38": {"enabled": False}, "workbuddy": {"enabled": False}},
            "tier2_domestic": [{"name": "reload-domestic", "enabled": False, "provider": "deepseek", "model": "reload-model", "api_key": "never-print-this-key"}]}
    config_file.write_text(json.dumps(data))
    def forbidden(*_args):
        pytest.fail("Status commands must not invoke chat or execution models")
    with gateway(tmp_path, monkeypatch, engine=forbidden, chat=forbidden) as (url, server):
        with request(url, body=message("/models")) as response:
            first = json.load(response)["content"][0]["text"]
        assert "reload-domestic | reload-model | disabled" in first
        data["tier2_domestic"][0]["enabled"] = True
        config_file.write_text(json.dumps(data))
        with request(url, body=message("查看模型状态")) as response:
            second = json.load(response)["content"][0]["text"]
        assert "reload-domestic | reload-model | configured_unverified" in second
        assert "never-print-this-key" not in first + second
        assert not server.run_lock.locked()


def test_serve_lock_rejects_second_process(tmp_path):
    import subprocess
    import sys
    settings = {"workspace_root": str(tmp_path), "auth_token": "test-only"}
    server = model.PacerModelServer(("127.0.0.1", 0), settings)
    script = """
import json, sys
sys.path.insert(0, sys.argv[1])
from visual_agent.pacer_model import PacerModelServer
try:
    server = PacerModelServer(('127.0.0.1', 0), json.loads(sys.argv[2]))
except RuntimeError:
    sys.exit(0)
else:
    server.server_close()
    sys.exit(5)
"""
    try:
        result = subprocess.run([sys.executable, "-c", script, str(Path(model.__file__).parent.parent),
                                 json.dumps(settings)], capture_output=True, timeout=10)
        assert result.returncode == 0
    finally:
        server.server_close()


@contextmanager
def gateway(tmp_path, monkeypatch, engine=None, chat=None, **config):
    monkeypatch.setattr(model, "legacy_execution_running", lambda root: False)
    settings = {"workspace_root": str(tmp_path), "allowed_roots": [str(tmp_path)],
                "auth_token": "test-key", "runs_dir": str(tmp_path / "runs"), **config}
    server = model.PacerModelServer(("127.0.0.1", 0), settings,
                                   engine=engine or (lambda p, r, cb: ("implemented", {"success": True})),
                                   chat_backend=chat or (lambda p, b: "conversation"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request(url, path="/v1/messages", body=None, key="test-key"):
    headers = {"x-api-key": key, "Content-Type": "application/json"}
    raw = json.dumps(body).encode() if body is not None else None
    return urllib.request.urlopen(urllib.request.Request(url + path, data=raw, headers=headers), timeout=5)


def message(text, **extra):
    return {"model": "pacer", "max_tokens": 1024,
            "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}], **extra}


def test_actual_http_task_owned_by_engine_not_frontend(tmp_path, monkeypatch):
    calls = []

    def engine(prompt, root, progress):
        calls.append((prompt, root))
        progress("[coder/grok] dispatched\n")
        return "verification failed", {"success": False}

    def forbidden_chat(*args):
        pytest.fail("A coding task must not fall back to a conversation model")

    with gateway(tmp_path, monkeypatch, engine=engine, chat=forbidden_chat) as (url, _):
        with request(url, body=message("Fix example.py", tools=[{"name": "Write", "input_schema": {"type": "object"}}])) as response:
            data = json.load(response)
    assert calls == [("Fix example.py", tmp_path)]
    assert data["model"] == "pacer"
    assert data["stop_reason"] == "end_turn"
    assert all(item["type"] == "text" for item in data["content"])
    records = [json.loads(p.read_text()) for p in (tmp_path / "runs").glob("*.json")]
    assert records[0]["status"] == "failed"


def test_stream_progress_arrives_before_engine_finishes(tmp_path, monkeypatch):
    release = threading.Event()

    def engine(prompt, root, progress):
        progress("[coder/grok] executing\n")
        assert release.wait(3)
        return "DONE", {"success": True}

    with gateway(tmp_path, monkeypatch, engine=engine) as (url, _):
        with request(url, body=message("Implement feature", stream=True)) as response:
            first = []
            while True:
                line = response.readline().decode()
                first.append(line)
                if "executing" in line:
                    break
            assert not release.is_set()
            release.set()
            rest = response.read().decode()
    text = "".join(first) + rest
    for name in ("message_start", "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"):
        assert "event: " + name in text
    assert "DONE" in rest
    assert "tool_use" not in text


def test_stream_disconnect_signals_worker_without_waiting_for_done():
    disconnected = threading.Event()
    reads = []

    class Events:
        def get(self, *args, **kwargs):
            reads.append(True)
            return "done", "finished"

    class BrokenWriter:
        def write(self, _payload):
            raise BrokenPipeError("client disconnected")

        def flush(self):
            return None

    handler = object.__new__(model.PacerModelHandler)
    handler.wfile = BrokenWriter()
    handler.send_response = lambda *_args, **_kwargs: None
    handler.send_header = lambda *_args, **_kwargs: None
    handler.end_headers = lambda *_args, **_kwargs: None

    handler._stream(Events(), "pm-test", disconnected=disconnected)

    assert disconnected.is_set()
    assert reads == []


def test_concurrent_mutating_requests_rejected(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()

    calls = []

    def engine(prompt, root, progress):
        calls.append(prompt)
        if len(calls) == 1:
            started.set()
            assert release.wait(3)
        return "done", {"success": True}

    with gateway(tmp_path, monkeypatch, engine=engine) as (url, server):
        with request(url, body=message("Fix a.py", stream=True)) as first:
            assert started.wait(2)
            try:
                with request(url, body=message("改成蓝色的")) as second:
                    data = json.load(second)
                assert data["content"][0]["text"].count("改成蓝色的") == 1
                assert "改成蓝色的" in server.amendment_queue
            finally:
                release.set()
            first.read()
            deadline = time.time() + 3
            while "改成蓝色的" not in calls and time.time() < deadline:
                time.sleep(0.05)
            assert calls[-1] == "改成蓝色的"


def test_concurrent_mutating_request_from_other_workspace_is_rejected(tmp_path, monkeypatch):
    other_root = tmp_path.parent / "other-workspace"
    other_root.mkdir()
    started, release = threading.Event(), threading.Event()
    calls = []

    def engine(prompt, root, progress):
        calls.append((prompt, root))
        started.set()
        assert release.wait(3)
        return "done", {"success": True}

    with gateway(tmp_path, monkeypatch, engine=engine) as (url, server):
        first = request(url, body=message("Fix workspace one", stream=True))
        assert started.wait(2)
        try:
            raw = json.dumps(message("Fix workspace two")).encode()
            second = urllib.request.Request(
                url + "/v1/messages",
                data=raw,
                headers={"x-api-key": "test-key", "x-pacer-workspace": str(other_root)},
            )
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(second, timeout=5)
            assert error.value.code == 409
            assert server.amendment_queue == []
        finally:
            release.set()
            first.read()

    assert len(calls) == 1
    assert calls[0][0] == "Fix workspace one"
    assert calls[0][1] == tmp_path.resolve()
    assert server.active_workspace_root is None
    assert server.amendment_queue == []


def test_auth_and_models_and_token_count_do_not_execute(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("Metadata routes must not execute a task")

    with gateway(tmp_path, monkeypatch, engine=forbidden, chat=forbidden) as (url, _):
        with pytest.raises(urllib.error.HTTPError) as err:
            request(url, body=message("Fix a.py"), key="wrong")
        assert err.value.code == 401
        with request(url, "/v1/models") as response:
            assert json.load(response)["data"][0]["id"] == "pacer"
        with request(url, "/v1/messages/count_tokens", message("Fix a.py")) as response:
            assert json.load(response)["input_tokens"] > 0


def test_plain_wish_uses_latest_sentence_and_not_chat(tmp_path, monkeypatch):
    seen = []

    def engine(prompt, root, progress):
        seen.append(prompt)
        assert "第一轮修登录" not in prompt
        return "要改哪个文件？", {"success": True, "awaiting_user": True}

    def forbidden_chat(*args):
        pytest.fail("A wish must enter the Pacer workflow, not chat history")

    body = message("按钮点不动")
    body["messages"] = [
        {"role": "user", "content": "第一轮修登录"},
        {"role": "assistant", "content": "已经看过登录模块"},
        {"role": "user", "content": "第二轮换颜色"},
        {"role": "assistant", "content": "颜色可以再定"},
        {"role": "user", "content": "按钮点不动"},
    ]
    with gateway(tmp_path, monkeypatch, engine=engine, chat=forbidden_chat) as (url, _):
        with request(url, body=body) as response:
            data = json.load(response)
    assert seen == ["按钮点不动"]
    assert "要改哪个文件" in data["content"][0]["text"]
    record = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
    assert record["status"] == "awaiting_input"


def test_gemini38_antigravity_enters_pacer_workflow(tmp_path, monkeypatch):
    seen = []

    def engine(prompt, root, progress):
        seen.append(prompt)
        return "已进入工作流", {"success": True}

    with gateway(tmp_path, monkeypatch, engine=engine) as (url, _):
        body = message("按钮点不动", model="gemini-3.8-flash-low")
        with request(url, body=body) as response:
            data = json.load(response)
    assert seen == ["按钮点不动"]
    assert data["model"] == "pacer"


def test_run_engine_reports_locked_acceptance_test_files(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from visual_agent import model_workflow, subagent_orchestrator, wish_intake
    from visual_agent.canvas_manager import CanvasManager

    (tmp_path / "calc.py").write_text("def calculate(value):\n    return value * 2\n", encoding="utf-8")

    plan = SimpleNamespace(
        goal="Fix calculation",
        tasks=[SimpleNamespace(patched_files=["calc.py"], id="coder", error=None)],
    )

    class FinishedOrchestrator:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self):
            return {
                "success": True,
                "completed_tasks": 1,
                "total_tasks": 1,
                "acceptance_test_files": ["tests/test_calc.py"],
            }

    monkeypatch.setattr(wish_intake, "resolve_wish", lambda *_args: SimpleNamespace(
        proceed=True, goal="Fix calculation", files=[], resume=False,
    ))
    monkeypatch.setattr(model_workflow, "plan_with_gemini4", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(model_workflow, "configure_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(model_workflow, "review_implementation", lambda _plan, result, *_args: result)
    monkeypatch.setattr(subagent_orchestrator, "SubagentOrchestrator", FinishedOrchestrator)

    text, result = model.run_engine("Fix calculation", tmp_path, lambda _message: None)

    assert result["acceptance_test_files"] == ["tests/test_calc.py"]
    assert "calc.py" in text
    assert "tests/test_calc.py" in text
    assert CanvasManager(tmp_path).load().recent_focus_files == ["calc.py"]


def test_run_engine_does_not_require_model_review_for_local_verification_only(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from visual_agent import model_workflow, subagent_orchestrator, wish_intake

    tasks = [
        SimpleNamespace(id="index", role="indexer", backend="local", patched_files=[], error=None),
        SimpleNamespace(id="verify", role="verifier", backend="local", patched_files=[], error=None),
    ]
    plan = SimpleNamespace(goal="Verify only", tasks=tasks, planner_backend="local", eta_seconds=1)

    class FinishedOrchestrator:
        def __init__(self, *_args, **_kwargs):
            self.deferred_trace_payload = None
            self._prompt_dispatches = []

        def run(self):
            return {
                "success": True,
                "completed_tasks": 2,
                "total_tasks": 2,
                "verification_receipts": [{
                    "command": 'python -m pytest "tests/test_fee_calc.py" -q',
                    "verdict": "pass",
                    "exit_code": 0,
                }],
                "acceptance_test_files": [],
            }

    monkeypatch.setattr(wish_intake, "resolve_wish", lambda *_args: SimpleNamespace(
        proceed=True,
        goal="只验证、不写码",
        files=["feasi_test/fee_calc.py", "tests/test_fee_calc.py"],
        resume=False,
    ))
    monkeypatch.setattr(model_workflow, "plan_with_gemini4", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(model_workflow, "configure_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        model_workflow,
        "review_implementation",
        lambda *_args: (_ for _ in ()).throw(AssertionError("read-only verification must not invoke a model reviewer")),
    )
    monkeypatch.setattr(subagent_orchestrator, "SubagentOrchestrator", FinishedOrchestrator)

    text, result = model.run_engine("只验证、不写码", tmp_path, lambda _message: None)

    assert result["success"] is True
    assert result["review"]["status"] == "not_required_verification_only"
    assert "Pacer 已完成执行" in text


def test_failed_adversarial_counterexample_retries_coder_once(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from visual_agent import model_workflow, subagent_orchestrator, wish_intake

    coder = SimpleNamespace(
        id="coder",
        role="coder",
        backend="gemini38",
        instructions="Implement the requested function",
        patched_files=[],
        error=None,
        retries=0,
    )
    plan = SimpleNamespace(goal="Implement a safe parser", tasks=[coder], eta_seconds=1)
    instructions_seen = []

    class FinishedOrchestrator:
        def __init__(self, current_plan, **_kwargs):
            instructions_seen.append(current_plan.tasks[0].instructions)
            self.deferred_trace_payload = None
            self._prompt_dispatches = []

        def run(self):
            return {
                "success": True,
                "completed_tasks": 1,
                "total_tasks": 1,
                "verification_receipts": [{"verdict": "pass", "exit_code": 0}],
                "effective_backend": "gemini38",
            }

    review_calls = []

    def review(_plan, result, *_args):
        review_calls.append(1)
        if len(review_calls) == 1:
            result["success"] = False
            result["review"] = {
                "approved": False,
                "repair_required": True,
                "counter_examples": ["assert target.parse(None) == {}"],
                "counterexample_result": {"passed": False, "output": "AssertionError: None is not normalized"},
            }
            result["collaboration_error"] = "counter-example failed"
            return result
        result["review"] = {"approved": True, "status": "reviewed"}
        return result

    monkeypatch.setattr(wish_intake, "resolve_wish", lambda *_args: SimpleNamespace(
        proceed=True, goal=plan.goal, files=[], resume=False,
    ))
    monkeypatch.setattr(model_workflow, "plan_with_gemini4", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(model_workflow, "configure_plan", lambda *_args, **_kwargs: {"reviewer": "heterogeneous"})
    monkeypatch.setattr(model_workflow, "review_implementation", review)
    monkeypatch.setattr(subagent_orchestrator, "SubagentOrchestrator", FinishedOrchestrator)

    _text, result = model.run_engine("Implement a safe parser", tmp_path, lambda _message: None)

    assert result["success"] is True
    assert len(review_calls) == 2
    assert len(instructions_seen) == 2
    assert "[Adversarial Counter-Example Repair]" in instructions_seen[1]
    assert "AssertionError: None is not normalized" in instructions_seen[1]


def test_gemini38_picker_is_executed_by_pacer_not_relayed(tmp_path, monkeypatch):
    calls = []

    def engine(prompt, root, progress):
        calls.append(prompt)
        return "owned", {"success": True}

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            pytest.fail("Gemini 3.8 must not receive a Code task")

        def log_message(self, *args):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        with gateway(tmp_path, monkeypatch, engine=engine, upstream_url=f"http://127.0.0.1:{upstream.server_port}") as (url, _):
            body = message("修复 a.py", model="gemini-3.8-flash-tiered")
            with request(url, body=body) as response:
                data = json.load(response)
        assert calls == ["修复 a.py"]
        assert data["model"] == "pacer"
        assert "owned" in data["content"][0]["text"]
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=3)


def test_non_task_message_uses_chat_backend_without_starting_engine(tmp_path, monkeypatch):
    chat_calls = []

    def forbidden_engine(*_args):
        pytest.fail("A conversation must not start the coding workflow")

    def chat(prompt, body):
        chat_calls.append((prompt, body["model"]))
        return "ordinary conversation"

    with gateway(tmp_path, monkeypatch, engine=forbidden_engine, chat=chat) as (url, _):
        with request(url, body=message("你是谁")) as response:
            data = json.load(response)

    assert chat_calls == [("你是谁", "pacer")]
    assert "ordinary conversation" in data["content"][0]["text"]
    assert not list((tmp_path / "runs").glob("*.json"))


def test_non_task_message_is_not_queued_as_task_amendment(tmp_path, monkeypatch):
    chat_calls = []

    def forbidden_engine(*_args):
        pytest.fail("A conversation must not enter the task amendment queue")

    with gateway(tmp_path, monkeypatch, engine=forbidden_engine,
                 chat=lambda prompt, _body: chat_calls.append(prompt) or "Pacer is ready") as (url, server):
        assert server.run_lock.acquire(blocking=False)
        try:
            with request(url, body=message("你好")) as response:
                data = json.load(response)
        finally:
            server.run_lock.release()

    assert chat_calls == ["你好"]
    assert server.amendment_queue == []
    assert "Pacer is ready" in data["content"][0]["text"]


def test_non_task_stream_returns_chat_response_without_engine(tmp_path, monkeypatch):
    def forbidden_engine(*_args):
        pytest.fail("A streamed conversation must not start the coding workflow")

    with gateway(tmp_path, monkeypatch, engine=forbidden_engine,
                 chat=lambda _prompt, _body: "Pacer is ready") as (url, _):
        with request(url, body=message("你好", stream=True)) as response:
            assert response.headers["Content-Type"].startswith("text/event-stream")
            stream = response.read()

    assert b"message_start" in stream
    assert b"Pacer is ready" in stream
    assert b"message_stop" in stream


def test_simple_greeting_responds_locally_without_model_call(monkeypatch):
    from visual_agent import grok_bridge

    monkeypatch.setattr(grok_bridge, "invoke_grok", lambda *_args, **_kwargs: pytest.fail("Greeting should be local"))
    assert "已就绪" in model.chat("你好", {})
    assert "已就绪" in model.chat("你现在是否可以给我干活呢", {})


def test_other_models_are_relayed_with_stream_and_auth_intact(tmp_path, monkeypatch):
    received = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers.get("x-api-key"), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n")

        def log_message(self, *args):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    t = threading.Thread(target=upstream.serve_forever, daemon=True)
    t.start()
    try:
        with gateway(tmp_path, monkeypatch, upstream_url=f"http://127.0.0.1:{upstream.server_port}") as (url, _):
            body = message("hello", model="existing-model", stream=True)
            with request(url, body=body) as response:
                assert response.headers["Content-Type"] == "text/event-stream"
                assert b"message_stop" in response.read()
        assert received[0][:2] == ("/v1/messages", "test-key")
        assert json.loads(received[0][2]) == body
    finally:
        upstream.shutdown()
        upstream.server_close()
        t.join(timeout=3)


def test_workspace_must_come_from_system_and_stay_in_scope(tmp_path):
    config = {
        "workspace_root": str(tmp_path),
        "allowed_roots": [str(tmp_path)],
        "allow_all_workspaces": False,
    }
    child = tmp_path / "child"
    child.mkdir()
    body = message("Fix files in /elsewhere", system=[{"type": "text", "text": f"Primary working directory: {child}\n"}])
    assert model.resolve_workspace(body, config) == child
    body["system"] = f"Working directory: {tmp_path.parent}"
    with pytest.raises(ValueError, match="outside"):
        model.resolve_workspace(body, config)


def test_tool_results_and_image_only_cannot_trigger_implementation():
    body = message("hello")
    body["messages"][-1]["content"] = [{"type": "tool_result", "tool_use_id": "old", "content": "Fix a.py"}]
    with pytest.raises(ValueError, match="tool call"):
        model.latest_prompt(body)
    body["messages"][-1]["content"] = [{"type": "image", "source": {}}]
    with pytest.raises(ValueError, match="image-only"):
        model.latest_prompt(body)


def test_current_code_trailing_system_message_is_not_the_user_task(tmp_path):
    body = message("<system-reminder>Old context: Fix unrelated.py</system-reminder>\nWho are you?")
    body["messages"].append({"role": "system", "content": [{"type": "text", "text": f"Working directory: {tmp_path}"}]})
    assert model.latest_prompt(body) == "Who are you?"
    assert model.resolve_workspace(body, {"workspace_root": str(tmp_path), "allowed_roots": [str(tmp_path)]}) == tmp_path


@pytest.mark.parametrize("text,task", [("修复 demo.py 的错误", True), ("Implement a parser", True),
                                     ("继续", True), ("接着把上次的做完", True),
                                     ("按钮点不动", True), ("分析欧意交易机器人为什么亏损", True),
                                     ("改成蓝色的", True),
                                     ("只回复 PACER_READY，不要修改文件", False), ("你是谁", False),
                                     ("你好", False), ("什么是重构", False)])
def test_task_routing_does_not_treat_greetings_as_mutations(text, task):
    assert model.is_task(text) is task


def test_install_preserves_providers_and_is_idempotent(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8090", "ANTHROPIC_AUTH_TOKEN": "original-key", "KEEP": "yes"},
                                    "model": "gemini-original", "modelPicker": {"options": [{"model": "gemini-original", "label": "Original"}]},
                                    "hooks": {"Existing": []}}))
    config = tmp_path / "gateway/config.json"
    first = model.install(tmp_path, settings, config, 8091, autostart=False)
    second = model.install(tmp_path, settings, config, 8091, autostart=False)
    data = json.loads(settings.read_text())
    assert data["model"] == "pacer"
    assert data["env"]["KEEP"] == "yes"
    assert data["env"]["ANTHROPIC_AUTH_TOKEN"] == "original-key"
    bypass = {item.casefold() for item in data["env"]["NO_PROXY"].split(",")}
    assert {"localhost", "127.0.0.1", "::1"} <= bypass
    assert "no_proxy" not in data["env"]
    assert data["hooks"] == {"Existing": []}
    assert [row["model"] for row in data["modelPicker"]["options"]] == ["pacer", "gemini-original"]
    assert first["upstream_url"] == second["upstream_url"] == "http://127.0.0.1:8090"
    assert Path(first["settings_backup"]).exists()


def test_install_preserves_existing_no_proxy_entries(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"NO_PROXY": "service.internal", "no_proxy": "*.local"}}))
    config = tmp_path / "gateway/config.json"

    model.install(tmp_path, settings, config, 8091, autostart=False)

    env = json.loads(settings.read_text(encoding="utf-8"))["env"]
    entries = {item.casefold() for item in env["NO_PROXY"].split(",")}
    assert {"service.internal", "*.local", "localhost", "127.0.0.1", "::1"} <= entries
    assert "no_proxy" not in env


def test_pacer_model_cli_is_not_forwarded_to_codex(monkeypatch):
    from visual_agent.cli import main
    seen = []
    monkeypatch.setattr(model, "main", lambda argv: seen.append(argv) or 19)
    assert main(["model", "status"]) == 19
    assert seen == [["status"]]
