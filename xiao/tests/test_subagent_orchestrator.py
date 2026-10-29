from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from visual_agent.subagent_orchestrator import (
    SubagentOrchestrator,
    SubagentPlan,
    SubagentTask,
    generate_subagent_plan,
    select_best_backend,
)


def test_select_best_backend_roles_and_tiers() -> None:
    # Indexer/Planner -> always Gemini Flash (free, large context)
    assert select_best_backend("indexer", "map repository", gemini_available=True) == "gemini"
    assert select_best_backend("planner", "plan steps", gemini_available=True) == "gemini"

    # Repeated failure -> always escalates to Codex
    assert select_best_backend("coder", "fix typo", repeated_failure=True) == "codex"

    # Grok bridge available -> selects grok
    assert select_best_backend("coder", "fix typo in docstring", grok_available=True) == "grok"

    # Strong reasoning / architecture -> Codex when grok is unavailable
    assert select_best_backend("coder", "重构核心认证鉴权架构与并发事务", grok_available=False, gemini_available=True, workbuddy_available=True) == "codex"

    # Coder -> Codex when grok is unavailable (fallback executable coding worker)
    assert select_best_backend("coder", "fix typo in docstring", grok_available=False, gemini_available=True, workbuddy_available=True) == "codex"
    assert select_best_backend("coder", "fix typo in docstring", grok_available=False, gemini_available=False, workbuddy_available=True) == "codex"


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


def test_subagent_orchestrator_execution_success(tmp_path) -> None:
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


def test_subagent_orchestrator_self_correction_and_escalation(tmp_path) -> None:
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
         patch.object(orchestrator, "_run_codex_cli", return_value=("repaired", None)):
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
        # It was escalated from workbuddy to codex!
        assert t1.backend == "codex"
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

    # 2. Abstract goal without keyword match should fall back to source file, NOT empty or .venv
    fallback = discover_target_files_for_goal("Improve calculation logic", tmp_path)
    assert fallback == ["src/engine.py"]


def test_verifier_failure_routes_to_upstream_coder(tmp_path) -> None:
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

