"""Behavioral regressions for gateway, acceptance integrity and prompt budgets."""
from concurrent.futures import Future
import io
import json
from pathlib import Path
import queue
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from visual_agent import pacer_model as model
from visual_agent.canvas_manager import CanvasManager, estimate_tokens
from visual_agent.tamper_guard import TamperGuard, TamperFatalError, _escape_calls


def handler(tmp_path, monkeypatch, executor, *, timeout=0.03, stream=False):
    monkeypatch.setattr(model, "legacy_execution_running", lambda root: False)
    body = json.dumps({"model": "pacer", "stream": stream,
                       "messages": [{"role": "user", "content": "Fix app.py"}]}).encode()
    h = model.PacerModelHandler.__new__(model.PacerModelHandler)
    h.path = "/v1/messages"
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body)
    h.server = SimpleNamespace(
        config={"workspace_root": str(tmp_path), "runs_dir": str(tmp_path / "runs")},
        executor=executor, run_lock=threading.Lock(), amendment_lock=threading.Lock(),
        amendment_queue=[], amendment_count=0, amendment_limit=8,
        active_workspace_root=None, run_timeout_seconds=timeout,
    )
    h.responses = []
    h._authorized = lambda: True
    h._error = lambda status, message: h.responses.append((status, message))
    h._json = lambda status, message: h.responses.append((status, message))
    return h


def test_p1_full_queue_publication_has_independent_deadline(monkeypatch):
    monkeypatch.setattr(model, "RESULT_PUBLISH_TIMEOUT_SECONDS", 0.02)
    events = queue.Queue(maxsize=256)
    for _ in range(256):
        events.put_nowait(("text", "progress"))
    start = time.monotonic()
    assert not model.PacerModelHandler._publish_result(events, "done", threading.Event(), start + 3600)
    assert time.monotonic() - start < 0.3
    disconnected = threading.Event()
    disconnected.set()
    assert not model.PacerModelHandler._publish_result(queue.Queue(), "done", disconnected, start + 3600)


@pytest.mark.parametrize("protocol", ["anthropic", "openai-chat", "openai-responses"])
def test_p1_p2_idle_heartbeat_detects_windows_abort(monkeypatch, protocol):
    monkeypatch.setattr(model, "SSE_POLL_INTERVAL_SECONDS", 0.005)
    disconnected = threading.Event()
    class Writer(io.BytesIO):
        def write(self, value):
            if value.startswith(b": keep-alive"):
                raise ConnectionAbortedError(10053, "Windows connection aborted")
            return super().write(value)
    h = model.PacerModelHandler.__new__(model.PacerModelHandler)
    h.wfile = Writer()
    h.send_response = lambda *args: None
    h.send_header = lambda *args: None
    h.end_headers = lambda: None
    start = time.monotonic()
    h._stream(queue.Queue(), "run", protocol=protocol, disconnected=disconnected, deadline=start + 0.5)
    assert disconnected.is_set()
    assert time.monotonic() - start < 0.3


def test_p2_nonstream_total_deadline(tmp_path, monkeypatch):
    h = handler(tmp_path, monkeypatch, SimpleNamespace(submit=lambda fn: Future()))
    start = time.monotonic()
    try:
        h.do_POST()
        assert h.responses[-1][0] == 504
        assert time.monotonic() - start < 0.5
    finally:
        h.server.run_lock.release()


def test_p2_stream_total_deadline():
    h = model.PacerModelHandler.__new__(model.PacerModelHandler)
    h.wfile = io.BytesIO()
    h.send_response = lambda *args: None
    h.send_header = lambda *args: None
    h.end_headers = lambda: None
    disconnected = threading.Event()
    h._stream(queue.Queue(), "run", disconnected=disconnected, deadline=time.monotonic() + 0.01)
    assert disconnected.is_set()
    assert b"timeout_error" in h.wfile.getvalue()


def test_p2_slow_stream_writes_use_remaining_deadline(monkeypatch):
    ticks = iter(i * 0.001 for i in range(100))
    monkeypatch.setattr(model.time, "monotonic", lambda: next(ticks))
    h = model.PacerModelHandler.__new__(model.PacerModelHandler)
    h.wfile = io.BytesIO()
    timeouts = []
    h.connection = SimpleNamespace(settimeout=timeouts.append)
    h.send_response = lambda *args: None
    h.send_header = lambda *args: None
    h.end_headers = lambda: None
    class Events:
        def get(self, **kwargs):
            return "done", "complete"
    h._stream(Events(), "run", deadline=0.1)
    assert len(timeouts) >= 4
    assert all(0 < timeout <= 0.1 for timeout in timeouts)
    assert timeouts[-1] < timeouts[0]


def test_p3_executor_rejection_releases_lock_even_if_record_write_fails(tmp_path, monkeypatch):
    def reject(fn):
        raise RuntimeError("executor shutdown")
    h = handler(tmp_path, monkeypatch, SimpleNamespace(submit=reject))
    def fail_record(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(model, "write_json", fail_record)
    h.do_POST()
    assert h.responses[-1][0] == 503
    assert not h.server.run_lock.locked()
    assert h.server.active_workspace_root is None
    assert not h.server.amendment_queue


def test_p4_authorization_precedes_admin_dispatch(tmp_path, monkeypatch):
    import visual_agent.model_admin as admin
    h = handler(tmp_path, monkeypatch, None)
    h.path = "/api/model-config"
    h._authorized = lambda: False
    called = []
    monkeypatch.setattr(admin, "handle", lambda *args: called.append(args))
    h.do_POST()
    assert h.responses[-1][0] == 401
    assert called == []


def test_p5_queued_followup_gets_error_and_record(tmp_path, monkeypatch):
    h = handler(tmp_path, monkeypatch, SimpleNamespace(submit=lambda fn: fn()), timeout=2)
    def engine(*args):
        h.server.amendment_queue.append("followup marker")
        raise RuntimeError("primary failed")
    h.server.engine = engine
    h.do_POST()
    assert "followup marker" in str(h.responses[-1])
    record = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
    assert record["failed_amendments"][0]["status"] == "failed"
    assert record["failed_amendments"][0]["prompt"] == "followup marker"
    assert not h.server.run_lock.locked()


@pytest.mark.parametrize("source,expected", [
    ("import pytest as p\np.skip('x')", "pytest.skip"),
    ("from pytest import xfail as fail\nfail('x')", "pytest.xfail"),
    ("import os as o\nstop=o._exit\nstop(0)", "os._exit"),
    ("import sys\ngetattr(sys, 'exit')(0)", "sys.exit"),
    ("import pytest as p\n@p.mark.skip\ndef test_x(): pass", "pytest.mark.skip"),
    ("from pytest import mark as m\n@m.xfail(reason='x')\ndef test_x(): pass", "pytest.mark.xfail"),
])
def test_p6_static_alias_and_decorator_escapes(source, expected):
    assert _escape_calls(source)[expected] == 1


def test_p6_scan_precedes_collection_execution(tmp_path, monkeypatch):
    source = tmp_path / "app.py"
    source.write_text("value=1\n", encoding="utf-8")
    guard = TamperGuard(tmp_path)
    guard.freeze_acceptance_suite()
    source.write_text("import os\nos._exit(0)\n", encoding="utf-8")
    def forbidden(**kwargs):
        raise AssertionError("Collection must not execute hostile source")
    monkeypatch.setattr(guard, "_collect_test_ids", forbidden)
    intact, _, changed = guard.verify_and_enforce()
    assert not intact and any("NEW ESCAPE os._exit" in item for item in changed)


def test_p6_cannot_rebaseline_modified_protected_input(tmp_path):
    path = tmp_path / "pytest.ini"
    path.write_text("[pytest]\n", encoding="utf-8")
    guard = TamperGuard(tmp_path)
    guard.lock_files(["pytest.ini"])
    try:
        with guard.temporary_unlock():
            path.write_text("[pytest]\naddopts=--ignore=tests\n", encoding="utf-8")
        with pytest.raises(TamperFatalError, match="immutable baseline"):
            guard.lock_files(["pytest.ini"])
        assert not guard.verify_and_enforce()[0]
        assert path.read_text(encoding="utf-8") == "[pytest]\n"
    finally:
        guard.unlock_files()


def test_p6_worker_patch_rejected_before_write(tmp_path):
    from visual_agent.subagent_orchestrator import SubagentOrchestrator, SubagentTask
    target = tmp_path / "app.py"
    target.write_text("value=1\n", encoding="utf-8")
    orch = SubagentOrchestrator.__new__(SubagentOrchestrator)
    orch.workspace_root = tmp_path
    orch._write_lock = threading.RLock()
    orch._run_finalized = False
    orch.enforce_test_tamper_guard = True
    orch.guard = TamperGuard(tmp_path)
    task = SubagentTask(id="worker", title="fix", role="coder", backend="api", instructions="fix", target_files=["app.py"])
    patch = "app.py\n<<<<<<< SEARCH\nvalue=1\n=======\nimport sys\nsys.exit(0)\n>>>>>>> REPLACE\n"
    with pytest.raises(TamperFatalError, match="acceptance escape"):
        orch._apply_task_patch(task, patch)
    assert target.read_text(encoding="utf-8") == "value=1\n"
    with pytest.raises(TamperFatalError, match="immutable acceptance"):
        orch.guard.validate_candidate("tests/test_new.py", "assert True")


def test_p7_verification_preserves_metacharacters_as_one_argument(tmp_path, monkeypatch):
    import visual_agent.subagent_orchestrator as module
    orch = module.SubagentOrchestrator.__new__(module.SubagentOrchestrator)
    orch.workspace_root = tmp_path
    orch.enforce_test_tamper_guard = False
    orch._verification_context = threading.local()
    orch._verification_receipts = []
    seen = []
    def gate(**kwargs):
        seen.append(kwargs["command"])
        return {"verdict": "pass", "exit_code": 0, "output_tail": "verified"}
    monkeypatch.setattr(module, "run_command_verification", gate)
    path = 'tests/test space $literal`quote".py'
    argv = [sys.executable, "-m", "pytest", path, "-q"]
    assert orch._run_verification_command(argv)[0]
    assert seen == [argv]


def test_p7_physical_pytest_path_with_shell_metacharacters(tmp_path):
    from visual_agent.command_verification import run_command_verification
    path = tmp_path / "test space $literal`name.py"
    path.write_text("def test_literal_path():\n    assert 2 + 2 == 4\n", encoding="utf-8")
    result = run_command_verification(
        command=[sys.executable, "-m", "pytest", str(path), "-q"],
        repo_root=tmp_path, timeout_seconds=3,
    )
    assert result["exit_code"] == 0
    assert result["verdict"] == "pass"
    assert "1 passed" in result["output_tail"]


def test_p6_freezes_fixture_bytes_and_nested_configuration(tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    fixture = tests / "data.json"
    fixture.write_text('{"value": 1}', encoding="utf-8")
    config = tests / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    guard = TamperGuard(tmp_path)
    # Collection behavior is covered with real pytest by test_tamper_guard.py.
    monkeypatch.setattr(guard, "_collect_test_ids", lambda **kwargs: frozenset())
    guard.freeze_acceptance_suite()
    try:
        assert {"tests/data.json", "tests/pytest.ini"} <= guard.protected_files.keys()
        with guard.temporary_unlock():
            fixture.write_text('{"value": 0}', encoding="utf-8")
        assert not guard.verify_and_enforce()[0]
        assert fixture.read_text(encoding="utf-8") == '{"value": 1}'
    finally:
        guard.unlock_files()


@pytest.mark.parametrize("budget", [128, 256, 512, 1500])
def test_p8_budget_preserves_focus_lines_and_patch_markers(tmp_path, budget):
    canvas = CanvasManager(tmp_path)
    code_lines = ["def critical(value):", *[f"    value_{i} = value + {i}" for i in range(100)]]
    canvas.upsert_focus_file("src/focus.py", symbols=[f"def op_{i}(value)" for i in range(12)], target_snippet="\n".join(code_lines))
    prompt = canvas.build_stateless_prompt("repair " * 300, max_tokens=budget, context_profile="context " * 300)
    assert estimate_tokens(prompt) <= budget
    assert "src/focus.py" in prompt
    assert all(marker in prompt for marker in ("<<<<<<< SEARCH", "=======", ">>>>>>> REPLACE"))
    focus = prompt.split("[3. TARGET FILE AST SIGNATURES]", 1)[1].split("[4. DYNAMIC TASK AND EXECUTION ERRORS]", 1)[0]
    for line in focus.splitlines():
        if line.startswith("    value_"):
            assert line in code_lines
    if budget >= 256:
        assert estimate_tokens(focus) >= int(budget * 0.25) - 20


def test_p8_impossible_budget_fails_explicitly(tmp_path):
    with pytest.raises(ValueError, match="at least 128"):
        CanvasManager(tmp_path).build_stateless_prompt("fix", max_tokens=32)
