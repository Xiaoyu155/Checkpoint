from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from visual_agent import model_workflow, provider_runtime, session_projection
from visual_agent.historical_replay import replay_historical_fix
from visual_agent.session_projection import SessionProjection
from visual_agent.subagent_orchestrator import SubagentOrchestrator, SubagentTask
from visual_agent.task_review import backend_model_family


def test_p9_compaction_preserves_1000_plus_events_across_instances(tmp_path, monkeypatch):
    monkeypatch.setattr(session_projection, "COMPACTION_MAX_EVENTS", 6)
    monkeypatch.setattr(session_projection, "COMPACTION_RETAIN_EVENTS", 2)
    monkeypatch.setattr(session_projection, "COMPACTION_MAX_BYTES", 12000)
    monkeypatch.setattr(session_projection.os, "fsync", lambda fd: None)
    writers = [SessionProjection(tmp_path), SessionProjection(tmp_path)]
    events = [
        {"event_id": "contract", "event_type": "task_contract", "payload": {"acceptance": "pytest tests/test_bug.py"}},
        {"event_id": "invariants", "event_type": "invariants", "payload": {"items": ["no test edits", "deterministic"]}},
        {"event_id": "focus", "event_type": "recent_focus_files", "payload": {"paths": ["src/focus.py"]}},
    ]
    events.extend({"event_id": f"old-{i}", "event_type": "user_input", "payload": {"content": f"request {i}"}} for i in range(1000))
    import json
    writers[0].events_path.parent.mkdir(parents=True)
    writers[0].events_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    for i in range(9):
        events.append(writers[i % 2].append_event("user_input", content=f"request {1000 + i}"))
    expected = SessionProjection.fold_events(events)
    reopened = SessionProjection(tmp_path)
    start = time.perf_counter()
    actual = reopened.snapshot()
    elapsed = time.perf_counter() - start
    assert actual == expected
    assert actual["event_count"] == 1012
    assert actual["latest_task_contract"]["acceptance"] == "pytest tests/test_bug.py"
    assert actual["invariants"] == ["no test edits", "deterministic"]
    assert actual["recent_focus_files"] == ["src/focus.py"]
    assert reopened.snapshot_path.exists()
    assert len(reopened.read_events()) < 6
    assert reopened.events_path.stat().st_size < 12000
    assert elapsed < 0.5


def test_p9_canvas_contract_and_invariants_are_projected(tmp_path):
    from visual_agent.canvas_manager import CanvasManager
    canvas = CanvasManager(tmp_path)
    canvas.update_goal("Repair price", acceptance_cmd="pytest tests/test_price.py")
    canvas.set_invariants(["keep receipt"])
    canvas.add_invariants("preserve totals")
    snapshot = SessionProjection(tmp_path).snapshot()
    assert snapshot["latest_task_contract"]["summary"] == "Repair price"
    assert snapshot["latest_task_contract"]["acceptance_cmd"] == "pytest tests/test_price.py"
    assert snapshot["invariants"] == ["keep receipt", "preserve totals"]


def test_p9_single_large_event_compacts_and_preserves_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(session_projection, "COMPACTION_MAX_BYTES", 1000)
    projection = SessionProjection(tmp_path)
    event = projection.append_event("prompt_projection", prompt="x" * 2000, task_instruction="fix")
    assert projection.events_path.stat().st_size < 1000
    assert projection.snapshot() == SessionProjection.fold_events([event])


def test_p10_explicit_cascade_is_unique_and_not_rotated(monkeypatch):
    monkeypatch.setenv("PACER_CODER_CASCADE", "workbuddy,gemini38,workbuddy,premium_api")
    entries = provider_runtime.resolve_coder_routes(preferred="premium_api", cascade=os.environ["PACER_CODER_CASCADE"])
    assert [row.backend_name for row in entries] == ["workbuddy", "gemini38", "premium_api"]
    assert all(len(row) == 5 and row.priority_reason == "explicit_cascade" for row in entries)
    assert provider_runtime.cost_candidates(2, "L3") == [row.backend_name for row in entries]
    assert entries[0].family == "unknown"


def test_p10_cooldown_and_infra_failover_are_deterministic(tmp_path, monkeypatch):
    monkeypatch.setenv("PACER_CODER_CASCADE", "gemini38,workbuddy,premium_api")
    orch = SubagentOrchestrator.__new__(SubagentOrchestrator)
    orch._backend_cooldowns = {}
    orch._prompt_dispatches = []
    orch._failover_path = []
    orch.plan = SimpleNamespace(complexity="L2")
    orch.use_stateless_canvas = False
    monkeypatch.setattr(orch, "_is_backend_live", lambda name: True)
    monkeypatch.setattr(orch, "_notify", lambda *args: None)
    calls = []
    def invoke(name, task, prompt):
        calls.append((name, task._route_timeout_seconds))
        return ("", "HTTP 429 quota") if name == "gemini38" else ("patch", None)
    monkeypatch.setattr(orch, "_invoke_single_backend", invoke)
    task = SubagentTask(id="t", title="fix", role="coder", backend="premium_api", instructions="fix")
    assert orch._dispatch_with_failover(task) == ("patch", None)
    assert calls == [("gemini38", 90.0), ("workbuddy", 90.0)]
    entries = provider_runtime.resolve_coder_routes(cascade=os.environ["PACER_CODER_CASCADE"], cooldowns=orch._backend_cooldowns)
    assert entries[0].cooldown_seconds > 0
    calls.clear()
    assert orch._dispatch_with_failover(task) == ("patch", None)
    assert calls == [("workbuddy", 90.0)]


def test_p10_task_error_does_not_fail_over_and_counts_strike(monkeypatch):
    monkeypatch.setenv("PACER_CODER_CASCADE", "gemini38,workbuddy")
    orch = SubagentOrchestrator.__new__(SubagentOrchestrator)
    orch._backend_cooldowns = {}
    orch._prompt_dispatches = []
    orch._failover_path = []
    orch.plan = SimpleNamespace(complexity="L2")
    orch.use_stateless_canvas = False
    monkeypatch.setattr(orch, "_is_backend_live", lambda name: True)
    calls = []
    monkeypatch.setattr(orch, "_invoke_single_backend", lambda name, *args: (calls.append(name) or "", "invalid patch"))
    task = SubagentTask(id="t", title="fix", role="coder", backend="gemini38", instructions="fix")
    assert orch._dispatch_with_failover(task) == ("", "invalid patch")
    assert calls == ["gemini38"]
    orch._record_verification_strike("gemini38")
    assert orch._next_cascade_backend("gemini38") == "gemini38"
    assert orch._verification_strikes["gemini38"] == 1
    assert not orch._backend_cooldowns


def test_p11_discovery_records_exceptions_without_network(monkeypatch, caplog):
    from visual_agent import grok_bridge, subprocess_window, workbuddy_bridge
    monkeypatch.setattr(provider_runtime, "load_providers", lambda: {"tier1_free": {"workbuddy": {"enabled": False}}})
    monkeypatch.setattr(subprocess_window, "resolve_codex_executable", lambda: None)
    def broken(*args, **kwargs):
        raise ValueError("not included in log")
    monkeypatch.setattr(provider_runtime, "antigravity_node", broken)
    monkeypatch.setattr(provider_runtime, "architect_node", broken)
    monkeypatch.setattr(provider_runtime, "api_nodes", broken)
    monkeypatch.setattr(provider_runtime, "tcp_reachable", lambda *args: pytest.fail("network probe"))
    monkeypatch.setattr(workbuddy_bridge, "get_gateway_info", lambda: None)
    monkeypatch.setattr(grok_bridge, "is_grok_available", lambda: False)
    with caplog.at_level(logging.WARNING):
        candidates = model_workflow._adversarial_reviewer_candidates()
    assert candidates == []
    assert {d["backend"] for d in candidates.diagnostics} >= {"gemini38", "domestic_api", "premium_api", "gemini4_vps"}
    assert all(d["skipped_reason"] for d in candidates.diagnostics)
    assert any(getattr(record, "skipped_reason", "") == "discovery_error:ValueError" for record in caplog.records)


def test_p11_explicit_reviewer_without_coder_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(model_workflow, "_adversarial_reviewer_candidates", lambda: [])
    result = model_workflow.review_implementation(SimpleNamespace(goal="review", tasks=[]),
                {"success": True}, {"reviewer": "codex"}, tmp_path, lambda x: None)
    assert result["success"] is False
    assert result["review"]["error"]["type"] == "ReviewerUnavailableError"


def test_p11_p12_no_independent_reviewer_fails_closed(tmp_path, monkeypatch):
    cases = [
        ("gemini38", [], "no_independent_reviewer"),
        ("gemini38", [{"backend": "gemini4_vps", "family": "google", "model": "gemini-4"}], "no_independent_reviewer"),
        ("workbuddy", [{"backend": "codex", "family": "openai", "model": "codex"}], "unknown_coder_family"),
        ("gemini38", [{"backend": "workbuddy", "family": "unknown", "model": "unknown"}], "no_independent_reviewer"),
    ]
    monkeypatch.delenv("PACER_WORKBUDDY_MODEL", raising=False)
    monkeypatch.delenv("PACER_WORKBUDDY_MODEL_FAMILY", raising=False)
    monkeypatch.setattr(model_workflow, "_invoke_adversarial_reviewer", lambda *args: pytest.fail("no independent reviewer"))
    for backend, candidates, reason in cases:
        monkeypatch.setattr(model_workflow, "_adversarial_reviewer_candidates", lambda: candidates)
        task = SimpleNamespace(id="t", role="coder", backend=backend, patched_files=["app.py"])
        result = model_workflow.review_implementation(SimpleNamespace(goal="fix", tasks=[task]),
                    {"success": True, "effective_backend": backend}, {}, tmp_path, lambda x: None)
        assert result["success"] is False
        assert result["review"]["approved"] is False
        assert result["review"]["error"]["type"] == "ReviewerUnavailableError"
        assert result["review"]["error"]["skipped_reason"] == reason


def test_p12_family_uses_anchored_registered_ids():
    cases = [
        ("gemini38", "", "", "google"),
        ("domestic_api", "sub2api", "deepseek-chat", "deepseek"),
        ("workbuddy", "", "", "unknown"),
        ("workbuddy", "", "glm-4", "zhipu"),
        ("api", "somegemini-compatible", "metadata-glm-helper", "unknown"),
        ("api", "https://example/meta/glm", "unrelated-model", "unknown"),
        ("api", "sub2api", "metallic-glm-4", "unknown"),
    ]
    for backend, provider, model, expected in cases:
        assert backend_model_family(backend, provider=provider, model=model) == expected


def test_p12_configured_workbuddy_model_is_explicitly_identified(monkeypatch):
    monkeypatch.setenv("PACER_WORKBUDDY_MODEL_FAMILY", "deepseek")
    plan = SimpleNamespace(tasks=[SimpleNamespace(role="coder", backend="workbuddy")])
    assert model_workflow._effective_coder_family({"effective_backend": "workbuddy"}, plan) == ("deepseek", "workbuddy")


def test_replay_parent_red_fix_green_and_p6_rejects_baits(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True, timeout=4).stdout.strip()
    git("init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    app = tmp_path / "src" / "app.py"
    test = tmp_path / "tests" / "test_bug.py"
    app.write_text("def value():\n    return 0\n", encoding="utf-8")
    test.write_text("from app import value\ndef test_bug():\n    assert value() == 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "parent")
    parent = git("rev-parse", "HEAD")
    app.write_text("def value():\n    return 1\n", encoding="utf-8")
    git("add", "src/app.py")
    git("commit", "-qm", "fix")
    fix = git("rev-parse", "HEAD")
    bait = "tests/test_bug.py\n<<<<<<< SEARCH\n    assert value() == 1\n=======\n    assert True\n>>>>>>> REPLACE\n"
    result = replay_historical_fix(tmp_path, parent, fix, ["tests/test_bug.py"], bait, timeout_seconds=6)
    assert result["passed"] and result["parent_failed"] and result["fix_passed"], result
    assert result["tamper_blocked"] and result["accepted_tamper"] == 0


def test_replay_rejects_unsafe_inputs(tmp_path):
    with pytest.raises(ValueError, match="hexadecimal"):
        replay_historical_fix(tmp_path, "HEAD; bad", "abc1234", ["tests/test.py"], "")
