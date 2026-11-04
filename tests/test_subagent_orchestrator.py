from __future__ import annotations

import json
import hashlib
import threading
import time
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from visual_agent.workbuddy_bridge import extract_and_apply_code_blocks
from visual_agent.subagent_orchestrator import (
    SubagentOrchestrator,
    SubagentPlan,
    SubagentTask,
    _conflict_free_wave_batches,
    _decompose_with_rules,
    _goal_allows_test_edits,
    generate_subagent_plan,
    select_best_backend,
)


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("Source: remove a cutoff\n\n测试：删除旧异常断言，改为验证新结果", True),
        ("源码：删除低值规则。 测试：删掉旧异常预期，改为正常计算", True),
        ("修正刚才那组单测，9.99 改为 pytest.raises(ValueError)", True),
        ("Please run tests: run pytest -q", False),
        ("测试：修改预期；不要修改测试文件", False),
    ],
)
def test_test_edit_permission_requires_explicit_edit_intent(goal, expected):
    assert _goal_allows_test_edits(goal) is expected


def test_late_write_after_failure_is_blocked_and_file_hash_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "inventory.py"
    path.write_text("stock = 10\n", encoding="utf-8")
    initial = hashlib.sha256(path.read_bytes()).hexdigest()
    task = SubagentTask(id="coder", title="repair", role="coder", backend="api",
                        instructions="repair", target_files=["inventory.py"])
    orch = SubagentOrchestrator(SubagentPlan(goal="repair", tasks=[task], repo_root=str(tmp_path)),
                               workspace_root=tmp_path, defer_trace_recording=True)
    monkeypatch.setattr(orch, "_dispatch_to_backend", lambda t: ("", "timeout"))
    result = orch.run()
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest()]
    patch = "inventory.py\n<<<<<<< SEARCH\nstock = 10\n=======\nstock = 0\n>>>>>>> REPLACE\n"
    blocked = []
    def late_write():
        time.sleep(0.02)
        try:
            orch._apply_task_patch(task, patch)
        except RuntimeError as exc:
            blocked.append(str(exc))
    thread = threading.Thread(target=late_write)
    thread.start()
    thread.join(timeout=2)
    hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
    orch.guard.unlock_all()
    hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
    assert result["success"] is False
    assert hashes == [initial] * 3
    assert blocked and "Late write blocked" in blocked[0]
    assert orch.deferred_trace_payload["status"] == "failed"
    assert orch.deferred_trace_payload["patched_files"] == []


def test_api_backend_full_completion_applies_patch(tmp_path, monkeypatch):
    monkeypatch.setenv("PACER_API_MODEL", "deepseek:deepseek-chat")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    monkeypatch.setenv("PACER_CODER_CASCADE", "api")
    target = tmp_path / "answer.py"
    target.write_text("answer = 0\n", encoding="utf-8")
    task = SubagentTask(id="writer", title="repair", role="coder", backend="api",
                        instructions="repair answer.py", target_files=["answer.py"])
    orch = SubagentOrchestrator(SubagentPlan(goal="repair", tasks=[task], repo_root=str(tmp_path)), workspace_root=tmp_path)
    patch_text = " " * 600 + "\nanswer.py\n<<<<<<< SEARCH\nanswer = 0\n=======\nanswer = 42\n>>>>>>> REPLACE\n"
    monkeypatch.setattr("visual_agent.subagent_orchestrator.run_llm_completion", lambda **kw: patch_text)
    assert orch.run()["success"]
    assert target.read_text() == "answer = 42\n"


def test_api_backend_429_triggers_pool_failover_and_circuit_breaker(tmp_path, monkeypatch):
    monkeypatch.setenv("PACER_API_POOL", "deepseek:deepseek-chat,qwen:qwen-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    monkeypatch.setenv("QWEN_API_KEY", "test-only")
    task = SubagentTask(id="writer", title="repair", role="coder", backend="api", instructions="repair")
    orch = SubagentOrchestrator(SubagentPlan(goal="repair", tasks=[task], repo_root=str(tmp_path)), workspace_root=tmp_path)
    calls = []
    def complete(**kwargs):
        calls.append(kwargs["backend"].provider)
        if calls[-1] == "deepseek":
            raise RuntimeError("HTTP 429 quota exhausted")
        return "full patch"
    monkeypatch.setattr("visual_agent.subagent_orchestrator.run_llm_completion", complete)
    started = time.monotonic()
    assert orch._invoke_api_completion("repair") == ("full patch", None)
    assert time.monotonic() - started < 1
    assert calls == ["deepseek", "qwen"]
    assert any("deepseek" in key for key in orch._api_node_cooldowns)
    orch._invoke_api_completion("repair again")
    assert calls == ["deepseek", "qwen", "qwen"]


def test_gemini4_vps_coder_uses_effective_prompt(tmp_path, monkeypatch):
    task = SubagentTask(id="writer", title="repair", role="coder", backend="gemini4_vps", instructions="original")
    orch = SubagentOrchestrator(SubagentPlan(goal="repair", tasks=[task], repo_root=str(tmp_path)), workspace_root=tmp_path)
    invoke = MagicMock(return_value=MagicMock(success=True, content="patch"))
    monkeypatch.setattr("visual_agent.gemini4_vps_client.invoke_gemini4_vps", invoke)
    orch._invoke_single_backend("gemini4_vps", task, "canvas SEARCH/REPLACE prompt")
    assert invoke.call_args.args[0] == "canvas SEARCH/REPLACE prompt"
    assert "stateless code writer" in invoke.call_args.kwargs["system_prompt"]


def test_gemini38_writer_requires_a_patch_for_every_requested_file(tmp_path, monkeypatch):
    task = SubagentTask(
        id="writer",
        title="remove the minimum and update tests",
        role="coder",
        backend="gemini38",
        instructions="remove the minimum and update the test file",
        target_files=["feasi_test/fee_calc.py", "tests/test_fee_calc.py"],
    )
    orch = SubagentOrchestrator(
        SubagentPlan(goal="remove minimum and update tests", tasks=[task], repo_root=str(tmp_path)),
        workspace_root=tmp_path,
    )
    invoke = MagicMock(return_value=MagicMock(success=True, response="patch", error=None))
    monkeypatch.setattr("visual_agent.subagent_orchestrator.is_gemini_available", lambda: True)
    monkeypatch.setattr("visual_agent.subagent_orchestrator.invoke_gemini38", invoke)

    output, error = orch._invoke_single_backend("gemini38", task, "change both target files")

    assert output == "patch"
    assert error is None
    system_prompt = invoke.call_args.kwargs["system_prompt"]
    assert "separate patch for every target file" in system_prompt
    assert "test files when test updates are requested" in system_prompt


def test_gemini38_rejects_gateway_self_recursion(tmp_path, monkeypatch):
    from visual_agent.gemini_bridge import invoke_gemini38
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"port": 18199, "upstream_url": "http://127.0.0.1:18199"}))
    monkeypatch.setenv("PACER_GATEWAY_CONFIG", str(config))
    monkeypatch.setenv("PACER_PROVIDERS_FILE", str(tmp_path / "providers.json"))
    monkeypatch.delenv("PACER_ANTIGRAVITY_URL", raising=False)
    monkeypatch.delenv("PACER_FAST_API_BASE", raising=False)
    result = invoke_gemini38("must not call ourselves")
    assert result.success is False
    assert not result.response


def test_fenced_snippet_appends_instead_of_replacing_module(tmp_path) -> None:
    target = tmp_path / "greetings.py"
    target.write_text("def greeting(name):\n    return name\n", encoding="utf-8")
    reply = "```python\ndef wave(name):\n    return f'Wave, {name}!'\n```"
    written = extract_and_apply_code_blocks(reply, workspace_root=tmp_path, allowed_files=["greetings.py"])
    assert written == ["greetings.py"]
    text = target.read_text(encoding="utf-8")
    assert "def greeting" in text
    assert "def wave" in text


def test_select_best_backend_roles_and_tiers() -> None:
    # Indexer/Planner stay local. Gemini 3.8 does not take the job.
    assert select_best_backend("indexer", "map repository", gemini_available=True) == "local"
    assert select_best_backend("planner", "plan steps", gemini_available=True) == "local"

    # Repeated failure moves to Grok, not Codex.
    assert select_best_backend("coder", "fix typo", repeated_failure=True, grok_available=True, workbuddy_available=True) == "grok"

    # WorkBuddy is the default writer when it is available.
    assert select_best_backend("coder", "fix typo in docstring", grok_available=True, workbuddy_available=True) == "workbuddy"
    assert select_best_backend("coder", "重构核心认证鉴权架构与并发事务", grok_available=True, gemini_available=True, workbuddy_available=True) == "workbuddy"

    # Grok writes when WorkBuddy is absent. Gemini and Codex stay out.
    assert select_best_backend("coder", "fix typo in docstring", grok_available=True, gemini_available=True, workbuddy_available=False) == "grok"
    assert select_best_backend("coder", "fix typo in docstring", grok_available=False, gemini_available=True, workbuddy_available=False) == "local"


def test_generate_subagent_plan_with_gemini(tmp_path) -> None:
    fake_tasks = [
        {
            "id": "t1",
            "title": "Analyze code",
            "role": "indexer",
            "instructions": "read files",
            "target_files": ["foo.py"],
            "verification_command": "",
            "dependencies": [],
        },
        {
            "id": "t2",
            "title": "Implement feature",
            "role": "coder",
            "instructions": "write code",
            "target_files": ["foo.py"],
            "verification_command": "pytest",
            "dependencies": ["t1"],
        },
    ]

    fake_result = MagicMock(success=True, parsed_json=fake_tasks)

    with patch("visual_agent.subagent_orchestrator.invoke_gemini", return_value=fake_result), \
         patch("visual_agent.subagent_orchestrator.build_repo_map", return_value={"files": {}}), \
         patch("visual_agent.subagent_orchestrator.render_repo_map", return_value="repo map excerpt"):
        plan = generate_subagent_plan("Add new feature", repo_root=tmp_path, gemini_available=True)
        assert len(plan.tasks) == 2
        assert plan.tasks[0].id == "t1"
        assert plan.tasks[0].role == "indexer"
        assert plan.tasks[1].dependencies == ["t1"]


def test_generate_subagent_plan_rule_fallback(tmp_path) -> None:
    plan = generate_subagent_plan("Quick bug fix", repo_root=tmp_path, gemini_available=False)
    assert len(plan.tasks) >= 3
    assert plan.tasks[0].role == "indexer"
    assert plan.tasks[1].role == "coder"
    assert plan.tasks[2].role == "verifier"


def _red_before_change(*_args, **_kwargs):
    return {"status": "failed_on_base", "reason": "gate_reported_failures"}


def test_subagent_orchestrator_execution_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.subagent_orchestrator.probe_workspace_command", _red_before_change)
    (tmp_path / "test_dummy.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
        encoding="utf-8",
    )
    t1 = SubagentTask(
        id="t1",
        title="Analyze",
        role="indexer",
        backend="local",
        instructions="do analysis",
    )
    t2 = SubagentTask(
        id="t2",
        title="Verify",
        role="verifier",
        backend="local",
        instructions="do verification",
        verification_command="python -m unittest test_dummy.py",
        dependencies=["t1"],
    )

    plan = SubagentPlan(goal="Test goal", tasks=[t1, t2], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(plan, workspace_root=tmp_path)
    summary = orchestrator.run()

    assert summary["success"] is True
    assert summary["completed_tasks"] == 2
    assert summary["failed_tasks"] == 0
    assert t1.status == "completed"
    assert t2.status == "completed"


def test_subagent_orchestrator_self_correction_and_escalation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PACER_CODER_CASCADE", "workbuddy,grok")
    monkeypatch.setattr("visual_agent.subagent_orchestrator.probe_workspace_command", _red_before_change)
    t1 = SubagentTask(
        id="t1",
        title="Code with initial error",
        role="coder",
        backend="workbuddy",
        instructions="write bad code",
        verification_command="python -c \"exit(1)\"",
    )

    plan = SubagentPlan(goal="Test repair", tasks=[t1], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(plan, workspace_root=tmp_path)

    # Mock workbuddy run
    with patch("visual_agent.subagent_orchestrator.invoke_workbuddy") as mock_wb, \
         patch("visual_agent.subagent_orchestrator.invoke_grok", return_value=MagicMock(success=True, response="repaired", error=None)):
        mock_wb.return_value = MagicMock(success=True, response="initial code", error=None)

        # Verification command will fail twice then succeed on third (after escalation)
        call_count = 0
        def fake_verification(cmd):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return (False, "SyntaxError: invalid syntax")
            return (True, "All 5 tests passed")

        orchestrator._run_verification_command = fake_verification

        summary = orchestrator.run()
        assert summary["success"] is True
        assert t1.status == "completed"
        assert t1.backend == "grok"
        assert t1.retries == 1
        assert len(t1.history) == 1
        assert t1.history[0]["backend"] == "workbuddy"


def test_discover_target_files_pruning_and_fallback(tmp_path) -> None:
    from visual_agent.subagent_orchestrator import discover_target_files_for_goal

    # Create dummy .venv file (should be pruned)
    venv_dir = tmp_path / ".venv" / "lib"
    venv_dir.mkdir(parents=True, exist_ok=True)
    (venv_dir / "ignored.py").write_text("# ignored\n", encoding="utf-8")

    # Create source file
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    engine_file = src_dir / "engine.py"
    engine_file.write_text("class Engine:\n    pass\n", encoding="utf-8")

    # 1. Matching keyword
    matched = discover_target_files_for_goal("Refactor the Engine class", tmp_path)
    assert matched == ["src/engine.py"]

    # 2. A goal with no file name and no path segment match selects nothing.
    fallback = discover_target_files_for_goal("Improve calculation logic", tmp_path)
    assert fallback == []


def test_verifier_failure_routes_to_upstream_coder(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PACER_CODER_CASCADE", "local")
    monkeypatch.setattr("visual_agent.subagent_orchestrator.probe_workspace_command", _red_before_change)
    coder_task = SubagentTask(
        id="coder_1",
        title="Implement logic",
        role="coder",
        backend="local",
        instructions="initial code",
    )
    verifier_task = SubagentTask(
        id="verifier_1",
        title="Run acceptance test",
        role="verifier",
        backend="local",
        instructions="verify code",
        verification_command="python -c \"test()\"",
        dependencies=["coder_1"],
    )

    plan = SubagentPlan(goal="Test verifier escalation", tasks=[coder_task, verifier_task], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(plan, workspace_root=tmp_path)

    verif_calls = 0
    def fake_verify(cmd):
        nonlocal verif_calls
        verif_calls += 1
        if verif_calls == 1:
            return (False, "AssertionError: expected 42 got 0")
        return (True, "All tests passed")

    orchestrator._run_verification_command = fake_verify

    # When upstream coder is executed, update output
    coder_runs = 0
    def fake_dispatch(task):
        nonlocal coder_runs
        if task.id == "coder_1":
            coder_runs += 1
            return ("def test(): pass", None)
        return ("", None)

    orchestrator._dispatch_to_backend = fake_dispatch

    summary = orchestrator.run()
    assert summary["success"] is True
    assert coder_runs >= 2  # upstream coder was re-executed via _execute_task_with_retry
    assert verifier_task.status == "completed"


def test_subagent_orchestrator_resume_skips_completed_tasks(tmp_path: Path) -> None:
    from visual_agent.managed_checkpoint import ManagedCheckpoint, save_managed_checkpoint, load_managed_checkpoint

    task1 = SubagentTask(
        id="task_1",
        title="First step",
        role="coder",
        backend="local",
        instructions="Step 1",
    )
    task2 = SubagentTask(
        id="task_2",
        title="Second step",
        role="coder",
        backend="local",
        instructions="Step 2",
        dependencies=["task_1"],
    )
    plan = SubagentPlan(goal="Test Resume Execution", tasks=[task1, task2], repo_root=str(tmp_path))

    task1.target_files = ["resume.py"]
    (tmp_path / "resume.py").write_text("value = 1\n", encoding="utf-8")
    from visual_agent.subagent_orchestrator import _capture_file_fingerprints

    # Pre-populate checkpoint indicating task_1 was already completed
    chk = ManagedCheckpoint(
        checkpoint_id="chk_resume_1",
        goal="Test Resume Execution",
        workspace_root=str(tmp_path),
        status="in_progress",
        current_wave_index=1,
        total_waves=2,
        completed_task_ids=["task_1"],
        pending_task_ids=["task_2"],
        patched_files=[],
        task_states={"task_1": {"status": "passed", "backend": "codex", "elapsed_seconds": 1.0,
                               "file_fingerprints": _capture_file_fingerprints(tmp_path, task1.target_files)}},
    )
    save_managed_checkpoint(tmp_path, chk)

    orchestrator = SubagentOrchestrator(plan, workspace_root=tmp_path, resume=True)

    executed_tasks = []
    def fake_dispatch(task):
        executed_tasks.append(task.id)
        return ("", None)

    orchestrator._dispatch_to_backend = fake_dispatch
    summary = orchestrator.run()

    assert summary["success"] is True
    assert "task_1" not in executed_tasks  # task_1 was skipped because it was already completed
    assert "task_2" in executed_tasks
    assert load_managed_checkpoint(tmp_path) is None  # Checkpoint cleared on complete success


def test_subagent_orchestrator_dogfood_evidence(tmp_path: Path) -> None:
    from visual_agent.managed_checkpoint import get_checkpoint_path
    import json

    target_file = tmp_path / "visual_agent" / "model_health.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("# initial\n")

    task1 = SubagentTask(
        id="task_dogfood",
        title="Dogfood self modification",
        role="coder",
        backend="local",
        instructions="Update visual_agent",
        target_files=["visual_agent/model_health.py"],
    )
    plan = SubagentPlan(goal="Dogfood self update", tasks=[task1], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(plan, workspace_root=tmp_path)

    def fake_dispatch(task):
        target_file.write_text("# dogfood modified\n")
        return ("", None)

    orchestrator._dispatch_to_backend = fake_dispatch
    summary = orchestrator.run()

    assert summary["success"] is True
    assert not (tmp_path / ".pacer" / "dogfood_evidence.json").exists()


def test_degraded_routing_skips_unavailable_and_uses_next_live_backend(tmp_path, monkeypatch) -> None:
    """When the primary backend is unavailable, Pacer cascades to the next
    live backend instead of failing. Partial model availability is the norm."""
    monkeypatch.setattr("visual_agent.subagent_orchestrator.probe_workspace_command", _red_before_change)
    (tmp_path / "test_dummy.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
        encoding="utf-8",
    )
    task = SubagentTask(
        id="t1",
        title="Code fix",
        role="coder",
        backend="workbuddy",
        instructions="fix the bug",
        verification_command="python -m unittest test_dummy.py",
    )
    plan = SubagentPlan(goal="Test degraded routing", tasks=[task], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(plan, workspace_root=tmp_path)

    # Pin the cascade so this test is deterministic and never touches the real
    # network (gemini4_vps would otherwise be tried first and make a live call).
    monkeypatch.setenv("PACER_CODER_CASCADE", "workbuddy,grok")

    # workbuddy is down (returns empty + infra error), grok returns valid output
    from visual_agent.workbuddy_bridge import WorkBuddyRunResult
    with patch("visual_agent.subagent_orchestrator.invoke_workbuddy",
               return_value=WorkBuddyRunResult(success=False, error="All WorkBuddy gateway accounts exhausted or unavailable.")), \
         patch("visual_agent.subagent_orchestrator.invoke_grok",
               return_value=MagicMock(success=True, response="```python\npass\n```", error=None)):
        # Mark workbuddy as not live, grok as live
        monkeypatch.setattr(orchestrator, "_is_backend_live", lambda backend: backend != "workbuddy")

        summary = orchestrator.run()
        # The task should complete via grok after workbuddy failed
        assert task.status == "completed"
        assert summary["success"] is True
        # The effective backend should be grok (not workbuddy)
        assert summary["effective_backend"] == "grok"


def test_is_backend_live_returns_false_for_unconfigured_backends(tmp_path) -> None:
    """_is_backend_live correctly reports backends as unavailable when they
    are not configured, without making real network calls."""
    task = SubagentTask(
        id="t1",
        title="Test",
        role="coder",
        backend="local",
        instructions="test",
    )
    plan = SubagentPlan(goal="Test probe", tasks=[task], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(plan, workspace_root=tmp_path)

    # local is always live
    assert orchestrator._is_backend_live("local") is True
    # Unknown backend
    assert orchestrator._is_backend_live("nonexistent_backend") is False


def test_gemini4_empty_pool_is_not_live(tmp_path, monkeypatch) -> None:
    from visual_agent import gemini4_vps_client, provider_runtime

    task = SubagentTask(id="t1", title="Test", role="coder", backend="local", instructions="test")
    plan = SubagentPlan(goal="Gemini 4 pool health", tasks=[task], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(plan, workspace_root=tmp_path)
    monkeypatch.setattr(gemini4_vps_client, "is_gemini4_vps_available", lambda: True)
    monkeypatch.setattr(gemini4_vps_client, "gemini4_settings", lambda: {"base_url": "https://direct.aiboxz.com/gemini4"})
    monkeypatch.setattr(provider_runtime, "_is_vps_gateway", lambda _url: True)
    monkeypatch.setattr(gemini4_vps_client, "is_gemini4_vps_pool_healthy", lambda: False)

    assert orchestrator._is_backend_live("gemini4_vps") is False


def test_gemini4_non_pool_api_uses_configured_availability(tmp_path, monkeypatch) -> None:
    from visual_agent import gemini4_vps_client, provider_runtime

    task = SubagentTask(id="t1", title="Test", role="coder", backend="local", instructions="test")
    plan = SubagentPlan(goal="Gemini 4 direct API", tasks=[task], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(plan, workspace_root=tmp_path)
    monkeypatch.setattr(gemini4_vps_client, "is_gemini4_vps_available", lambda: True)
    monkeypatch.setattr(gemini4_vps_client, "gemini4_settings", lambda: {"base_url": "https://api.example.test/gemini4"})
    monkeypatch.setattr(provider_runtime, "_is_vps_gateway", lambda _url: False)
    monkeypatch.setattr(gemini4_vps_client, "is_gemini4_vps_pool_healthy", lambda: pytest.fail("Non-pool APIs must not probe the VPS account pool"))

    assert orchestrator._is_backend_live("gemini4_vps") is True

def test_coder_cascade_default_uses_cost_tiers(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PACER_CODER_CASCADE", raising=False)
    task = SubagentTask(id="t", title="t", role="coder", backend="local", instructions="x")
    plan = SubagentPlan(goal="cascade order", tasks=[task], repo_root=str(tmp_path))
    orch = SubagentOrchestrator(plan, workspace_root=tmp_path)
    order = orch._coder_cascade()
    assert order == ["gemini38", "workbuddy", "domestic_api", "premium_api"]


def test_coder_cascade_is_env_overridable(tmp_path, monkeypatch) -> None:
    """A dead provider must be droppable without a code change."""
    monkeypatch.setenv("PACER_CODER_CASCADE", "gemini4_vps,codex")
    task = SubagentTask(id="t", title="t", role="coder", backend="local", instructions="x")
    plan = SubagentPlan(goal="cascade env", tasks=[task], repo_root=str(tmp_path))
    orch = SubagentOrchestrator(plan, workspace_root=tmp_path)
    assert orch._coder_cascade() == ["gemini4_vps", "codex"]


def test_circuit_breaker_demotes_backend_after_infra_failure(tmp_path) -> None:
    """After an infra failure a backend is skipped, so the next task does not
    pay the same failure latency again."""
    task = SubagentTask(id="t", title="t", role="coder", backend="local", instructions="x")
    plan = SubagentPlan(goal="breaker", tasks=[task], repo_root=str(tmp_path))
    orch = SubagentOrchestrator(plan, workspace_root=tmp_path)

    assert orch._backend_in_cooldown("grok") is False
    orch._note_backend_failure("grok", "HTTP 502: upstream_error")
    assert orch._backend_in_cooldown("grok") is True
    # _is_backend_live must honour the cooldown
    assert orch._is_backend_live("grok") is False


def test_circuit_breaker_auth_failure_cools_longer_than_transient(tmp_path) -> None:
    task = SubagentTask(id="t", title="t", role="coder", backend="local", instructions="x")
    plan = SubagentPlan(goal="breaker2", tasks=[task], repo_root=str(tmp_path))
    orch = SubagentOrchestrator(plan, workspace_root=tmp_path)

    orch._note_backend_failure("workbuddy", "AuthenticationRequiredError: login required")
    auth_until = orch._backend_cooldowns["workbuddy"]
    orch2 = SubagentOrchestrator(plan, workspace_root=tmp_path)
    orch2._note_backend_failure("codex", "timed out")
    transient_until = orch2._backend_cooldowns["codex"]

    import time as _t
    now = _t.monotonic()
    assert (auth_until - now) > (transient_until - now)


def test_circuit_breaker_clears_on_success(tmp_path, monkeypatch) -> None:
    """A later success must clear the cooldown so the backend is usable again."""
    monkeypatch.setattr("visual_agent.subagent_orchestrator.probe_workspace_command", _red_before_change)
    monkeypatch.setenv("PACER_CODER_CASCADE", "workbuddy")
    (tmp_path / "test_dummy.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
        encoding="utf-8",
    )
    task = SubagentTask(
        id="t1", title="Code fix", role="coder", backend="workbuddy",
        instructions="fix", verification_command="python -m unittest test_dummy.py",
    )
    plan = SubagentPlan(goal="clear cooldown", tasks=[task], repo_root=str(tmp_path))
    orch = SubagentOrchestrator(plan, workspace_root=tmp_path)

    # Pre-open the circuit, then bypass the cooldown filter so the call is made.
    orch._note_backend_failure("workbuddy", "timed out")
    assert orch._backend_in_cooldown("workbuddy") is True
    monkeypatch.setattr(orch, "_is_backend_live", lambda backend: True)

    from visual_agent.workbuddy_bridge import WorkBuddyRunResult
    with patch("visual_agent.subagent_orchestrator.invoke_workbuddy",
               return_value=WorkBuddyRunResult(success=True, response="```python\npass\n```")):
        orch.run()
    assert orch._backend_in_cooldown("workbuddy") is False


def test_codex_not_in_default_cascade(tmp_path) -> None:
    """Codex can hang for its full CLI timeout without a verified login, so it
    must not sit in the default failover hot path."""
    task = SubagentTask(id="t", title="t", role="coder", backend="local", instructions="x")
    plan = SubagentPlan(goal="no codex", tasks=[task], repo_root=str(tmp_path))
    orch = SubagentOrchestrator(plan, workspace_root=tmp_path)
    assert "codex" not in orch._coder_cascade()


def test_codex_timeout_is_bounded_and_overridable(tmp_path, monkeypatch) -> None:
    task = SubagentTask(id="t", title="t", role="coder", backend="local", instructions="x")
    plan = SubagentPlan(goal="codex timeout", tasks=[task], repo_root=str(tmp_path))
    orch = SubagentOrchestrator(plan, workspace_root=tmp_path)
    monkeypatch.delenv("PACER_CODEX_TIMEOUT_SECONDS", raising=False)
    assert orch._codex_timeout() == 60.0
    monkeypatch.setenv("PACER_CODEX_TIMEOUT_SECONDS", "15")
    assert orch._codex_timeout() == 15.0


def test_gemini4_retries_once_on_transient_transport_error(tmp_path) -> None:
    """An SSL handshake timeout consumes no tokens; one retry is warranted."""
    from visual_agent.gemini4_vps_client import Gemini4Response
    task = SubagentTask(id="t", title="t", role="coder", backend="gemini4_vps",
                        instructions="write code", target_files=["a.py"])
    plan = SubagentPlan(goal="retry transient", tasks=[task], repo_root=str(tmp_path))
    orch = SubagentOrchestrator(plan, workspace_root=tmp_path)

    fail = Gemini4Response(False, "", error="Gemini 4 gateway URLError: <urlopen error _ssl.c:1015: The handshake operation timed out>")
    ok = Gemini4Response(True, "```python\npass\n```")
    with patch("visual_agent.gemini4_vps_client.invoke_gemini4_vps", side_effect=[fail, ok]) as m:
        out, err = orch._invoke_single_backend("gemini4_vps", task, "prompt")
    assert out == "```python\npass\n```"
    assert err is None
    assert m.call_count == 2


def test_gemini4_does_not_retry_auth_errors(tmp_path) -> None:
    """Auth/quota errors are not transient; retrying would waste quota."""
    from visual_agent.gemini4_vps_client import Gemini4Response
    task = SubagentTask(id="t", title="t", role="coder", backend="gemini4_vps",
                        instructions="write code", target_files=["a.py"])
    plan = SubagentPlan(goal="no retry auth", tasks=[task], repo_root=str(tmp_path))
    orch = SubagentOrchestrator(plan, workspace_root=tmp_path)

    fail = Gemini4Response(False, "", error="Gemini 4 gateway HTTP 401: Unauthorized")
    with patch("visual_agent.gemini4_vps_client.invoke_gemini4_vps", side_effect=[fail]) as m:
        out, err = orch._invoke_single_backend("gemini4_vps", task, "prompt")
    assert out == ""
    assert "401" in (err or "")
    assert m.call_count == 1


def test_transient_transport_classifier() -> None:
    from visual_agent.subagent_orchestrator import _is_transient_transport_error
    assert _is_transient_transport_error("_ssl.c:1015: The handshake operation timed out")
    assert _is_transient_transport_error("read timeout")
    assert not _is_transient_transport_error("HTTP 401: Unauthorized")
    assert not _is_transient_transport_error("429 quota exceeded")
    assert not _is_transient_transport_error(None)


def test_502_is_not_treated_as_transient_retryable() -> None:
    """A 502 wrapped in URLError means upstream is down; retrying wastes time."""
    from visual_agent.subagent_orchestrator import _is_transient_transport_error
    assert not _is_transient_transport_error(
        "Gemini 4 gateway URLError: <urlopen error Tunnel connection failed: 502 Bad Gateway>"
    )
    assert not _is_transient_transport_error("HTTP 503 Service Unavailable")
    # A genuine SSL handshake timeout is still retryable
    assert _is_transient_transport_error("_ssl.c:1015: The handshake operation timed out")


def test_all_backends_cooling_down_fails_fast(tmp_path) -> None:
    """When every candidate is in cooldown, do not hammer them again."""
    task = SubagentTask(id="t", title="t", role="coder", backend="workbuddy",
                        instructions="x", target_files=["a.py"])
    plan = SubagentPlan(goal="fail fast", tasks=[task], repo_root=str(tmp_path))
    orch = SubagentOrchestrator(plan, workspace_root=tmp_path)
    for b in [*orch._coder_cascade(), "codex"]:
        orch._note_backend_failure(b, "HTTP 502 upstream_error")

    out, err = orch._dispatch_with_failover(task)
    assert out == ""
    assert "cooling down" in (err or "").lower()
    # No failover hops recorded because no call was attempted
    assert orch._failover_path == []


def test_workbuddy_and_empty_gemini_fail_over_to_local_codex(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PACER_CODER_CASCADE", raising=False)
    task = SubagentTask(
        id="coder", title="repair", role="coder", backend="workbuddy",
        instructions="repair fee validation", target_files=["fee_calc.py"],
    )
    orch = SubagentOrchestrator(
        SubagentPlan(goal="repair fee validation", tasks=[task], repo_root=str(tmp_path)),
        workspace_root=tmp_path,
    )
    attempts: list[str] = []
    monkeypatch.setattr(orch, "_is_backend_live", lambda name: name in {"workbuddy", "gemini38", "codex"})
    monkeypatch.setattr(orch, "_note_backend_failure", lambda _backend, _error: None)

    def invoke(backend, _task, _prompt):
        attempts.append(backend)
        if backend == "workbuddy":
            return "", "AuthenticationRequiredError: not logged in"
        if backend == "gemini38":
            return "", "Gemini 3.8 returned empty content"
        return "codex patch", None

    monkeypatch.setattr(orch, "_invoke_single_backend", invoke)
    output, error = orch._dispatch_with_failover(task)

    assert output == "codex patch"
    assert error is None
    assert attempts == ["workbuddy", "gemini38", "codex"]


def test_verification_only_goal_builds_local_verifier_with_existing_pytest_command(tmp_path) -> None:
    from visual_agent.subagent_orchestrator import _decompose_with_rules

    (tmp_path / "feasi_test").mkdir()
    (tmp_path / "feasi_test" / "fee_calc.py").write_text(
        "def calc_maker_taker_fee(notional, is_maker):\n    return notional\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fee_calc.py").write_text(
        "def test_existing_contract():\n    assert True\n",
        encoding="utf-8",
    )
    goal = (
        '只验证、不写码；仅执行 `python -m pytest "tests/test_fee_calc.py" -q`，'
        "不要修改文件或创建新考题。"
    )

    plan = _decompose_with_rules(goal, str(tmp_path), False, False)
    verifier = next(task for task in plan.tasks if task.role == "verifier")

    assert verifier.backend == "local"
    # The command is a display serialization; execution uses the original argv.
    import subprocess
    import sys
    assert verifier.verification_argv == [sys.executable, "-m", "pytest", "tests/test_fee_calc.py", "-q"]
    assert verifier.verification_command == subprocess.list2cmdline(verifier.verification_argv)


def test_target_discovery_ranks_specific_names_over_generic_pacer_keyword(tmp_path) -> None:
    from visual_agent.subagent_orchestrator import discover_target_files_for_goal

    expected = {
        "src/visual_agent/subprocess_window.py",
        "src/visual_agent/subagent_orchestrator.py",
        "src/visual_agent/delivery_check.py",
        "src/visual_agent/context_profile.py",
    }
    decoys = {
        "dist/Pacer/_internal/visual_agent/dashboard/static/app.js",
        "examples/pacer_token_exchange/ban_arbitration.py",
    }
    for relative in expected | decoys:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def placeholder():\n    return 1\n", encoding="utf-8")

    goal = (
        "审计 Pacer 自动任务路径，聚焦 subprocess_window、subagent_orchestrator、"
        "delivery_check、context_profile；只分析，不修改文件。"
    )
    discovered = discover_target_files_for_goal(goal, tmp_path)
    assert set(discovered) == expected


def test_read_only_plan_uses_local_tasks_and_skips_coder(tmp_path) -> None:
    target = tmp_path / "src/visual_agent/subagent_orchestrator.py"
    target.parent.mkdir(parents=True)
    target.write_text("def placeholder():\n    return 1\n", encoding="utf-8")

    plan = generate_subagent_plan(
        "只分析，不修改文件：src/visual_agent/subagent_orchestrator.py",
        repo_root=tmp_path,
        gemini_available=False,
    )

    assert [task.role for task in plan.tasks] == ["indexer", "verifier"]
    assert all(task.backend == "local" for task in plan.tasks)
    assert all(
        "src/visual_agent/subagent_orchestrator.py" in task.target_files
        for task in plan.tasks
    )


@pytest.mark.parametrize("mutation", ["missing", "same_size_tamper", "legacy_no_hash"])
def test_resume_reexecutes_coder_when_target_file_missing_or_tampered(tmp_path, monkeypatch, mutation):
    from visual_agent.managed_checkpoint import ManagedCheckpoint, save_managed_checkpoint
    from visual_agent.subagent_orchestrator import _capture_file_fingerprints
    target = tmp_path / "missing.py"
    target.write_text("value = 1\n", encoding="utf-8")
    fingerprints = _capture_file_fingerprints(tmp_path, ["missing.py"])
    coder = SubagentTask(id="coder", title="repair", role="coder", backend="api",
                         instructions="repair", target_files=["missing.py"])
    verifier = SubagentTask(id="verify", title="inspect", role="verifier", backend="local", instructions="inspect",
                            dependencies=["coder"])
    plan = SubagentPlan(goal="resume audit", tasks=[coder, verifier], repo_root=str(tmp_path))
    save_managed_checkpoint(tmp_path, ManagedCheckpoint(
        checkpoint_id="strict", goal=plan.goal, workspace_root=str(tmp_path),
        completed_task_ids=["coder"], pending_task_ids=["verify"],
        task_states={"coder": {"patched_files": [], "file_fingerprints": fingerprints}}))
    if mutation == "missing":
        target.unlink()
    elif mutation == "same_size_tamper":
        target.write_text("value = 9\n", encoding="utf-8")
    else:
        fingerprints["missing.py"].pop("sha256")
        save_managed_checkpoint(tmp_path, ManagedCheckpoint(
            checkpoint_id="legacy", goal=plan.goal, workspace_root=str(tmp_path),
            completed_task_ids=["coder"], pending_task_ids=["verify"],
            task_states={"coder": {"patched_files": [], "file_fingerprints": fingerprints}}))
    orch = SubagentOrchestrator(plan, workspace_root=tmp_path, resume=True)
    executed = []
    def dispatch(task):
        executed.append(task.id)
        if task.id == "coder":
            before = target.read_text() if target.exists() else ""
            return f"missing.py\n<<<<<<< SEARCH\n{before}=======\nvalue = 2\n>>>>>>> REPLACE\n", None
        return "inspected", None
    monkeypatch.setattr(orch, "_dispatch_to_backend", dispatch)
    assert orch.run()["success"]
    assert "coder" in executed
    assert target.read_text() == "value = 2\n"


def test_read_only_scan_reports_real_ast_and_window_calls(tmp_path):
    target = tmp_path / "worker.py"
    target.write_text("import subprocess as sp\nclass Worker: pass\ndef run():\n    sp.run(['echo'])\n    sp.Popen(['echo'], **hidden_subprocess_kwargs())\n", encoding="utf-8")
    task = SubagentTask(id="inspect", title="inspect", role="inspector", backend="local",
                        instructions="inspect only", target_files=["worker.py"])
    orch = SubagentOrchestrator(SubagentPlan(goal="inspect", tasks=[task], repo_root=str(tmp_path)), workspace_root=tmp_path)
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    report, error = orch._dispatch_to_backend(task)
    assert error is None
    assert "functions=1, classes=1" in report
    assert "line 4: sp.run; hidden_subprocess_kwargs=False" in report
    assert "line 5: sp.Popen; hidden_subprocess_kwargs=True" in report
    assert task.acceptance_tier == "local_static_only"
    assert "index_only" in report
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before


def test_numbered_independent_modules_become_one_parallel_wave_with_shared_tests(tmp_path):
    goal = (
        "Implement three independent modules:\n"
        "1. In sandbox_exam/funding_rate.py implement parse_funding_rate(payload).\n"
        "2. In sandbox_exam/drawdown.py implement max_drawdown(equity_curve).\n"
        "3. In sandbox_exam/cooldown_timer.py implement is_symbol_cooled_down(last_trade_ts, now_ts, window_sec).\n"
        "Write complete tests in tests/test_sandbox_parallel.py and run them."
    )

    plan = _decompose_with_rules(
        goal,
        str(tmp_path),
        workbuddy_available=True,
        gemini_available=True,
    )

    module_tasks = [task for task in plan.tasks if task.id.startswith("coder_") and task.id != "coder_tests"]
    test_task = next(task for task in plan.tasks if task.id == "coder_tests")
    assert len(module_tasks) == 3
    assert all(task.dependencies == [] for task in module_tasks)
    assert len({task.target_files[0] for task in module_tasks}) == 3
    assert [task.backend for task in module_tasks[:2]] == ["gemini38", "workbuddy"]
    assert set(test_task.dependencies) == {task.id for task in module_tasks}
    assert test_task.target_files == ["tests/test_sandbox_parallel.py"]


def test_stage_markers_become_dependency_waves(tmp_path):
    goal = (
        "阶段 A：实现 sandbox_exam/kline_loader.py，归一化 14 天日线数据。"
        "阶段 B：依赖阶段 A，实现 sandbox_exam/signal_ranker.py。"
        "阶段 C：依赖阶段 B，实现 sandbox_exam/backtest_runner.py 与 tests/test_top_gainer_short.py。"
    )

    plan = _decompose_with_rules(
        goal,
        str(tmp_path),
        workbuddy_available=True,
        gemini_available=True,
    )
    tasks = {task.id: task for task in plan.tasks}

    assert tasks["phase_a_1"].dependencies == []
    assert tasks["phase_b_2"].dependencies == ["phase_a_1"]
    assert tasks["phase_c_3"].dependencies == ["phase_b_2"]
    assert tasks["verify_phases"].dependencies == ["phase_c_3"]


def test_conflict_batches_keep_overlapping_targets_serial():
    tasks = [
        SubagentTask(id="a", title="a", role="coder", backend="local", instructions="", target_files=["src/a.py"]),
        SubagentTask(id="b", title="b", role="coder", backend="local", instructions="", target_files=["src/b.py"]),
        SubagentTask(id="c", title="c", role="coder", backend="local", instructions="", target_files=["src/a.py"]),
    ]

    batches = _conflict_free_wave_batches(tasks)

    assert [task.id for task in batches[0]] == ["a", "b"]
    assert [task.id for task in batches[1]] == ["c"]


def test_three_independent_same_wave_tasks_run_in_parallel_under_1_8_seconds(tmp_path, monkeypatch):
    tasks = [
        SubagentTask(
            id=f"worker_{index}",
            title=f"worker {index}",
            role="verifier",
            backend="local",
            instructions="independent simulated work",
            target_files=[f"src/worker_{index}.py"],
        )
        for index in range(3)
    ]
    orch = SubagentOrchestrator(
        SubagentPlan(goal="parallel wave", tasks=tasks, repo_root=str(tmp_path)),
        workspace_root=tmp_path,
        enforce_test_tamper_guard=False,
        probe_base_discrimination=False,
        heartbeat_interval=0,
        defer_trace_recording=True,
    )
    started = []

    def one_second_task(task):
        started.append((task.id, time.monotonic()))
        time.sleep(1.0)
        return True

    monkeypatch.setattr(orch, "_run_task_with_heartbeat", one_second_task)
    before = time.monotonic()

    result = orch.run()

    elapsed = time.monotonic() - before
    assert result["success"]
    assert len(started) == 3
    assert elapsed < 1.8, f"three independent one-second tasks took {elapsed:.2f}s"


def test_successful_parallel_task_is_checkpointed_before_sibling_finishes(tmp_path, monkeypatch):
    from threading import Event
    from visual_agent import subagent_orchestrator as orchestrator_module

    fast_saved = Event()
    recorded = []
    original_save = orchestrator_module.save_managed_checkpoint
    tasks = [
        SubagentTask(id="fast", title="fast", role="verifier", backend="local", instructions="", target_files=["fast.py"]),
        SubagentTask(id="slow", title="slow", role="verifier", backend="local", instructions="", target_files=["slow.py"]),
    ]
    orch = SubagentOrchestrator(
        SubagentPlan(goal="checkpoint each task", tasks=tasks, repo_root=str(tmp_path)),
        workspace_root=tmp_path,
        enforce_test_tamper_guard=False,
        probe_base_discrimination=False,
        heartbeat_interval=0,
        defer_trace_recording=True,
    )

    def save_and_signal(root, payload):
        recorded.append(list(payload.get("completed_task_ids", [])))
        path = original_save(root, payload)
        if "fast" in payload.get("completed_task_ids", []):
            fast_saved.set()
        return path

    def execute(task):
        if task.id == "slow":
            assert fast_saved.wait(2.0), "fast task completion was not persisted while slow task was running"
        return True

    monkeypatch.setattr(orchestrator_module, "save_managed_checkpoint", save_and_signal)
    monkeypatch.setattr(orch, "_run_task_with_heartbeat", execute)

    result = orch.run()

    assert result["success"]
    assert any(ids == ["fast"] for ids in recorded)


def test_resume_injects_completed_hashes_and_interruption_reason(tmp_path, monkeypatch):
    from visual_agent.managed_checkpoint import ManagedCheckpoint, save_managed_checkpoint

    completed_file = tmp_path / "first.py"
    pending_file = tmp_path / "second.py"
    completed_file.write_text("value = 1\n", encoding="utf-8")
    pending_file.write_text("value = 0\n", encoding="utf-8")
    digest = hashlib.sha256(completed_file.read_bytes()).hexdigest()
    goal = "resume stored work"
    plan = SubagentPlan(
        goal=goal,
        repo_root=str(tmp_path),
        tasks=[
            SubagentTask(id="coder_1", title="first", role="coder", backend="gemini38", instructions="keep first", target_files=["first.py"]),
            SubagentTask(id="coder_2", title="second", role="coder", backend="gemini38", instructions="finish second", target_files=["second.py"]),
        ],
    )
    checkpoint = ManagedCheckpoint(
        checkpoint_id="resume-test",
        goal=goal,
        workspace_root=str(tmp_path.resolve()),
        status="in_progress",
        interruption_reason="simulated interruption after first module",
        current_wave_index=0,
        total_waves=1,
        completed_task_ids=["coder_1"],
        pending_task_ids=["coder_2"],
        patched_files=["first.py"],
        task_states={
            "coder_1": {
                "status": "completed",
                "patched_files": ["first.py"],
                "output": "first module verified",
                "file_fingerprints": {"first.py": {"sha256": digest}},
            },
            "coder_2": {"status": "pending", "patched_files": [], "file_fingerprints": {}},
        },
        plan_goal_hash=hashlib.sha256(goal.encode("utf-8")).hexdigest(),
        plan_task_count=2,
    )
    save_managed_checkpoint(tmp_path, checkpoint)
    orch = SubagentOrchestrator(
        plan,
        workspace_root=tmp_path,
        resume=True,
        enforce_test_tamper_guard=False,
        probe_base_discrimination=False,
        heartbeat_interval=0,
        defer_trace_recording=True,
    )
    seen_instructions = []

    def execute(task):
        seen_instructions.append(task.instructions)
        return True

    monkeypatch.setattr(orch, "_run_task_with_heartbeat", execute)

    result = orch.run()

    assert result["success"]
    assert len(seen_instructions) == 1
    assert "[Recovery Note]" in seen_instructions[0]
    assert digest in seen_instructions[0]
    assert "simulated interruption after first module" in seen_instructions[0]
    assert "git log" in seen_instructions[0]


def test_parallel_pytest_verification_uses_unique_cache_and_temp_environment(tmp_path, monkeypatch):
    from visual_agent import subagent_orchestrator as orchestrator_module

    task = SubagentTask(id="coder_1", title="test", role="verifier", backend="local", instructions="")
    orch = SubagentOrchestrator(
        SubagentPlan(goal="verify", tasks=[task], repo_root=str(tmp_path)),
        workspace_root=tmp_path,
        enforce_test_tamper_guard=False,
        probe_base_discrimination=False,
    )
    captured = {}

    def run_command(**kwargs):
        captured.update(kwargs)
        assert Path(kwargs["env"]["TMP"]).is_dir()
        return {"verdict": "pass", "command": kwargs["command"], "exit_code": 0, "output_tail": "ok"}

    monkeypatch.setattr(orchestrator_module, "run_command_verification", run_command)

    passed, _output = orch._run_verification_command(
        "python -m pytest tests/test_sample.py -q",
        task_id="coder_1",
    )

    assert passed
    assert isinstance(captured["command"], list)
    assert captured["command"][-2:] == ["-o", "cache_dir=.pytest_cache_coder_1"]
    assert captured["env"]["TMP"] == captured["env"]["TEMP"] == captured["env"]["TMPDIR"]
    assert not Path(captured["env"]["TMP"]).exists()


def test_recent_focus_file_becomes_target_for_pathless_follow_up(tmp_path):
    from visual_agent.canvas_manager import CanvasManager

    target = tmp_path / "sandbox_exam" / "pricing.py"
    target.parent.mkdir(parents=True)
    target.write_text("def position_size(balance):\n    return balance\n", encoding="utf-8")
    CanvasManager(tmp_path).set_recent_focus_files(["sandbox_exam/pricing.py"])
    task = SubagentTask(
        id="follow_up",
        title="adjust threshold",
        role="coder",
        backend="gemini38",
        instructions="Change the threshold we just edited.",
    )
    orch = SubagentOrchestrator(
        SubagentPlan(goal="Change the threshold we just edited", tasks=[task], repo_root=str(tmp_path)),
        workspace_root=tmp_path,
        enforce_test_tamper_guard=False,
        probe_base_discrimination=False,
    )

    orch._populate_canvas_ast_focus(task)

    assert task.target_files == ["sandbox_exam/pricing.py"]
    assert "position_size" in orch.canvas.build_stateless_prompt(
        task.instructions,
        target_files=task.target_files,
    )


def test_green_incremental_delta_requires_source_and_test_evidence(tmp_path):
    import hashlib

    source = tmp_path / "feasi_test" / "fee_calc.py"
    test = tmp_path / "tests" / "test_fee_calc.py"
    source.parent.mkdir()
    test.parent.mkdir()
    source.write_text("def fee(value):\n    return value\n", encoding="utf-8")
    test.write_text("def test_fee():\n    assert fee(10) == 10\n", encoding="utf-8")
    task = SubagentTask(
        id="coder", title="increment fee", role="coder", backend="gemini38",
        instructions="add the minimum notional rule", target_files=[
            "feasi_test/fee_calc.py", "tests/test_fee_calc.py",
        ],
    )
    orch = SubagentOrchestrator(
        SubagentPlan(goal="increment fee", tasks=[task], repo_root=str(tmp_path)),
        workspace_root=tmp_path,
        enforce_test_tamper_guard=False,
        probe_base_discrimination=False,
        require_red_baseline=False,
        green_incremental=True,
    )
    task._green_baseline_fingerprints = {
        "feasi_test/fee_calc.py": hashlib.sha256(source.read_bytes()).hexdigest(),
        "tests/test_fee_calc.py": hashlib.sha256(test.read_bytes()).hexdigest(),
    }
    task._green_incremental_test_paths = ["tests/test_fee_calc.py"]
    task._green_baseline_passed_count = 3

    source.write_text("def fee(value):\n    if value < 10: raise ValueError\n    return value\n", encoding="utf-8")
    assert orch._green_incremental_delta(task, "3 passed") == (
        False,
        "no test-file SHA-256 change or pass-count increase (baseline=3, current=3)",
    )

    test.write_text("def test_fee():\n    assert fee(10) == 10\n    assert fee(9)\n", encoding="utf-8")
    assert orch._green_incremental_delta(task, "3 passed") == (True, "source and delta-test evidence verified")

    test.write_text("def test_fee():\n    assert fee(10) == 10\n", encoding="utf-8")
    assert orch._green_incremental_delta(task, "4 passed") == (True, "source and delta-test evidence verified")
