from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import urllib.request

import pytest

from visual_agent import pacer_model
from visual_agent.subagent_orchestrator import SubagentOrchestrator, SubagentPlan, SubagentTask


@contextmanager
def model_endpoint(replies, captured):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append((self.path, body, self.headers.get("User-Agent")))
            reply = replies[min(len(captured) - 1, len(replies) - 1)]
            status = reply if isinstance(reply, int) else 200
            data = {"error": {"message": "quota exhausted"}} if status != 200 else {"content": [{"type": "text", "text": reply}]}
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def runtime_config(tmp_path, monkeypatch):
    directory = tmp_path / "settings"
    directory.mkdir()
    # This module routes all model traffic to an in-process loopback server.
    monkeypatch.setenv("PACER_ALLOW_LIVE_IN_PYTEST", "1")
    monkeypatch.delenv("PACER_CODER_CASCADE", raising=False)
    monkeypatch.setenv("PACER_GATEWAY_CONFIG", str(directory / "config.json"))
    monkeypatch.setenv("PACER_PROVIDERS_FILE", str(directory / "providers.json"))
    for name in ("PACER_CODER_CASCADE", "PACER_API_POOL", "PACER_API_MODEL", "PACER_PREMIUM_API_POOL", "PACER_DOMESTIC_API_POOL", "PACER_ANTIGRAVITY_URL", "PACER_FAST_API_BASE", "PACER_GEMINI4_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    from visual_agent.provider_runtime import _COOLDOWNS
    _COOLDOWNS.clear()
    return directory


def broken_project(root):
    root.mkdir(exist_ok=True)
    (root / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "test_calculator.py").write_text("from calculator import add\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    return "calculator.py\n<<<<<<< SEARCH\n    return a - b\n=======\n    return a + b\n>>>>>>> REPLACE\n"


def test_gemini38_http_applies_patch_without_cli(tmp_path, runtime_config, monkeypatch):
    project = tmp_path / "project"
    patch = broken_project(project)
    monkeypatch.setattr("visual_agent.gemini_bridge.find_gemini_executable", lambda: None)
    captured = []
    with model_endpoint([patch], captured) as upstream:
        (runtime_config / "config.json").write_text(json.dumps({"port": 8091, "upstream_url": upstream}))
        (runtime_config / "providers.json").write_text(json.dumps({"tier1_free": {"antigravity_gemini38": {"enabled": True}, "workbuddy": {"enabled": False}}}))
        monkeypatch.setenv("PACER_CODER_CASCADE", "gemini38")
        command = f'"{sys.executable}" -m pytest test_calculator.py -q'
        task = SubagentTask(id="writer", title="repair", role="coder", backend="gemini38", instructions="Fix calculator.py add", target_files=["calculator.py"], verification_command=command)
        before = subprocess.run([sys.executable, "-m", "pytest", "test_calculator.py", "-q"], cwd=project, capture_output=True)
        assert before.returncode == 1
        orch = SubagentOrchestrator(SubagentPlan(goal="Fix calculator.py add", tasks=[task], repo_root=str(project)), workspace_root=project)
        result = orch.run()
    assert result["success"], result
    assert "return a + b" in (project / "calculator.py").read_text()
    assert captured[0][0] == "/v1/messages"
    assert "SEARCH" in captured[0][1]["messages"][-1]["content"]
    assert captured[0][2] == "Pacer-Client/1.0"


def test_cost_tiered_routing_uses_free_gemini38_on_first_attempt(tmp_path, runtime_config, monkeypatch):
    task = SubagentTask(id="coder", title="repair", role="coder", backend="premium_api", instructions="repair")
    orch = SubagentOrchestrator(SubagentPlan(goal="repair", tasks=[task], repo_root=str(tmp_path)), workspace_root=tmp_path)
    monkeypatch.setattr(orch, "_is_backend_live", lambda backend: backend in {"gemini38", "premium_api"})
    seen = []
    def invoke(backend, *_args):
        seen.append(backend)
        return "patch", None
    monkeypatch.setattr(orch, "_invoke_single_backend", invoke)
    assert orch._dispatch_to_backend(task) == ("patch", None)
    assert seen == ["gemini38"]


def test_cost_tiered_routing_escalates_to_premium_on_verification_failure(tmp_path, runtime_config, monkeypatch):
    project = tmp_path / "project"
    correct = broken_project(project)
    wrong = correct.replace("return a + b", "return a * b")
    free_calls, premium_calls = [], []
    monkeypatch.setattr("visual_agent.gemini_bridge.find_gemini_executable", lambda: None)
    with model_endpoint([wrong], free_calls) as free_url, model_endpoint([correct], premium_calls) as paid_url:
        (runtime_config / "config.json").write_text(json.dumps({"port": 8091, "upstream_url": free_url}))
        (runtime_config / "providers.json").write_text(json.dumps({
            "tier1_free": {"antigravity_gemini38": {"enabled": True}, "workbuddy": {"enabled": False}},
            "tier3_premium_escalation": [{"enabled": True, "provider": "anthropic", "model": "test-premium", "api_key": "test-only", "base_url": paid_url + "/v1"}],
        }))
        task = SubagentTask(id="writer", title="repair", role="coder", backend="gemini38", instructions="Fix calculator.py add", target_files=["calculator.py"], verification_command=f'"{sys.executable}" -m pytest test_calculator.py -q')
        orch = SubagentOrchestrator(SubagentPlan(goal="Fix calculator.py add", tasks=[task], repo_root=str(project)), workspace_root=project)
        result = orch.run()
    assert result["success"], result
    assert len(free_calls) == 1
    assert len(premium_calls) == 1
    assert "AssertionError" in premium_calls[0][1]["messages"][-1]["content"]
    assert task.backend == "premium_api"
    assert task.retries == 1


def test_premium_primary_429_uses_distinct_backup_and_keeps_cooldown_across_runs(tmp_path, runtime_config):
    from visual_agent.provider_runtime import api_nodes, cooldown_remaining
    captured = []
    with model_endpoint([429, "complete patch"], captured) as base:
        (runtime_config / "providers.json").write_text(json.dumps({
            "tier3_premium_escalation": [
                {"enabled": True, "name": "primary", "provider": "anthropic", "model": "same-model", "base_url": base + "/v1", "api_key": "test-primary"},
                {"enabled": True, "name": "backup", "provider": "anthropic", "model": "same-model", "base_url": base + "/v1", "api_key": "test-backup"},
            ]}))
        nodes = api_nodes("premium_api")
        assert nodes[0].node_id != nodes[1].node_id
        task = SubagentTask(id="coder", title="repair", role="coder", backend="premium_api", instructions="repair")
        plan = SubagentPlan(goal="repair", tasks=[task], repo_root=str(tmp_path))
        first = SubagentOrchestrator(plan, workspace_root=tmp_path)
        assert first._invoke_api_completion("repair", slot="premium_api") == ("complete patch", None)
        assert len(captured) == 2
        assert cooldown_remaining(nodes[0].node_id) > 0
        second = SubagentOrchestrator(plan, workspace_root=tmp_path)
        assert second._invoke_api_completion("repair", slot="premium_api") == ("complete patch", None)
        assert len(captured) == 3


@pytest.mark.parametrize("reviewer_unavailable", [False, True])
def test_run_engine_blocks_when_pytest_passes_but_no_reviewer_can_run_sandbox(tmp_path, runtime_config, monkeypatch, reviewer_unavailable):
    project = tmp_path / "project"
    patch = broken_project(project)
    before_hash = hashlib.sha256((project / "test_calculator.py").read_bytes()).hexdigest()
    before = subprocess.run([sys.executable, "-m", "pytest", "test_calculator.py", "-q"], cwd=project, capture_output=True)
    assert before.returncode == 1
    captured = []
    monkeypatch.setattr("visual_agent.gemini_bridge.find_gemini_executable", lambda: None)
    # Exercise the gateway and physical pytest against local servers only.
    # Host-installed model CLIs must not become accidental test dependencies.
    candidate = {"backend": "codex", "family": "openai", "model": "test-reviewer"}
    monkeypatch.setattr("visual_agent.model_workflow._adversarial_reviewer_candidates", lambda: [candidate])
    monkeypatch.setattr("visual_agent.model_workflow._invoke_adversarial_reviewer", lambda *_args: ("", "Quota exceeded"))
    with model_endpoint([patch, 429] if reviewer_unavailable else [patch], captured) as upstream:
        (runtime_config / "config.json").write_text(json.dumps({"port": 8091, "upstream_url": upstream}))
        data = {"tier1_free": {"antigravity_gemini38": {"enabled": True}, "workbuddy": {"enabled": False}}}
        if reviewer_unavailable:
            data["tier4_architect_reviewer"] = {"gemini4": {"enabled": True, "provider": "anthropic", "model": "test-reviewer", "base_url": upstream + "/v1", "api_key": "test-only"}}
        (runtime_config / "providers.json").write_text(json.dumps(data))
        settings = {"workspace_root": str(project), "auth_token": "frontend-test", "runs_dir": str(tmp_path / "runs")}
        server = pacer_model.PacerModelServer(("127.0.0.1", 0), settings)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = {"model": "pacer", "stream": True, "messages": [{"role": "user", "content": "Fix calculator.py add to return the sum instead of subtraction"}]}
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/messages", data=json.dumps(body).encode(), headers={"x-api-key": "frontend-test", "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as response:
                streamed = response.read().decode()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
    records = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "runs").glob("*.json")]
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "failed", record
    assert record["result"]["success"] is False
    assert record["result"]["review"]["status"] == "skipped_unconfigured_or_quota"
    assert record["result"]["review"]["approved"] is False
    assert record["result"]["review"]["sandbox_executed"] is False
    assert record["result"]["api_request_count"] == 0
    assert any(r["exit_code"] == 0 for r in record["result"]["verification_receipts"])
    assert "message_stop" in streamed
    assert "gemini38" in streamed
    assert hashlib.sha256((project / "test_calculator.py").read_bytes()).hexdigest() == before_hash
    after = subprocess.run([sys.executable, "-m", "pytest", "test_calculator.py", "-q"], cwd=project, capture_output=True)
    assert after.returncode == 0


def test_valid_negative_review_is_not_silently_discarded(tmp_path, runtime_config):
    from visual_agent.model_workflow import review_implementation
    captured = []
    with model_endpoint([json.dumps({"approved": False, "issues": []})], captured) as endpoint:
        (runtime_config / "providers.json").write_text(json.dumps({"tier4_architect_reviewer": {"gemini4": {"enabled": True, "provider": "anthropic", "model": "test-reviewer", "base_url": endpoint + "/v1", "api_key": "test-only"}}}))
        plan = SubagentPlan(goal="review", tasks=[], repo_root=str(tmp_path))
        result = review_implementation(plan, {"success": True}, {"reviewer": "gemini4_vps"}, tmp_path, lambda _: None)
    assert result["success"] is False
    assert result["review"]["approved"] is False
    assert len(captured) == 1
