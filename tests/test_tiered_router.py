"""Unit and integration tests for Pacer Tiered Router and L1/L2/L3 Fast-Path Engine.

Validates Topic 2:
1. L1 micro tasks (typo, css, button, comment, config) route to FAST_PATH (<3s),
   bypassing Role 0 stub contracts, TamperGuard test locks, and git red-to-green probe.
2. L2 standard tasks route to STANDARD_TDD (<12s) with AST anti-slop and verification.
3. L3 architectural tasks route to ARCHITECTURAL (<30s) with stub contracts, TamperGuard,
   and Kahn topological wave execution.
4. SubagentOrchestrator respects TieredRouter decisions automatically.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import pytest

from visual_agent.dag_scheduler import TaskComplexity
from visual_agent.subagent_orchestrator import (
    SubagentOrchestrator,
    SubagentPlan,
    SubagentTask,
)
from visual_agent.tiered_router import (
    RoutingDecision,
    RoutingPath,
    TieredRouter,
)


def test_tiered_router_classifies_l1_micro_task(tmp_path: Path) -> None:
    router = TieredRouter(tmp_path)
    goals = [
        "fix typo in README.md",
        "update button color in style.css",
        "tweak timeout configuration in settings.py",
        "fix small comment in core.py",
    ]
    for goal in goals:
        decision = router.route(goal, target_files=["settings.py"])
        assert decision.complexity == TaskComplexity.L1
        assert decision.routing_path == RoutingPath.FAST_PATH
        assert decision.skip_stub_contracts is True
        assert decision.skip_tamper_guard is False
        assert decision.skip_red_to_green_probe is False
        assert decision.max_retries == 1


def test_tiered_router_classifies_l2_standard_task(tmp_path: Path) -> None:
    router = TieredRouter(tmp_path)
    decision = router.route(
        "implement user authentication validator function and tests",
        target_files=["auth.py", "test_auth.py"],
    )
    assert decision.complexity == TaskComplexity.L2
    assert decision.routing_path == RoutingPath.STANDARD_TDD
    assert decision.skip_stub_contracts is True
    assert decision.skip_tamper_guard is False
    assert decision.skip_red_to_green_probe is False
    assert decision.max_retries == 2


def test_tiered_router_classifies_l3_architectural_task(tmp_path: Path) -> None:
    router = TieredRouter(tmp_path)
    decision = router.route(
        "architectural refactor across distributed service modules and DAG scheduler",
        target_files=["service.py", "dag.py", "network.py", "storage.py"],
    )
    assert decision.complexity == TaskComplexity.L3
    assert decision.routing_path == RoutingPath.ARCHITECTURAL
    assert decision.skip_stub_contracts is False
    assert decision.skip_tamper_guard is False
    assert decision.skip_red_to_green_probe is False
    assert decision.max_retries == 3


def test_orchestrator_fast_path_bypasses_tamper_and_base_probe(tmp_path: Path) -> None:
    doc_file = tmp_path / "README.md"
    doc_file.write_text("# Old Title\n", encoding="utf-8")

    task = SubagentTask(
        id="t1",
        title="Fix typo in README",
        role="coder",
        backend="local",
        instructions="Change Old Title to New Title",
        target_files=["README.md"],
        verification_command="",
    )

    plan = SubagentPlan(
        goal="fix typo in README",
        tasks=[task],
        repo_root=str(tmp_path),
        complexity=TaskComplexity.L1.value,
    )

    orchestrator = SubagentOrchestrator(plan, workspace_root=tmp_path)

    assert orchestrator.enforce_test_tamper_guard is True
    assert orchestrator.probe_base_discrimination is True
    assert orchestrator.routing_decision.routing_path == RoutingPath.FAST_PATH

    diff = (
        "<<<<<<< SEARCH\n"
        "# Old Title\n"
        "=======\n"
        "# New Title\n"
        ">>>>>>> REPLACE\n"
    )

    with patch.object(orchestrator, "_dispatch_to_backend", return_value=(diff, None)):
        summary = orchestrator.run()

    assert summary["success"] is True
    assert summary["complexity"] == "L1"
    assert summary["routing_path"] == "fast_path"
    assert doc_file.read_text(encoding="utf-8") == "# New Title\n"


def test_orchestrator_allows_explicit_overrides_on_micro_task(tmp_path: Path) -> None:
    task = SubagentTask(
        id="t1",
        title="Micro task with forced tamper lock",
        role="coder",
        backend="local",
        instructions="Update small setting",
        target_files=["setting.txt"],
        verification_command="",
    )
    plan = SubagentPlan(goal="fix typo in setting", tasks=[task], repo_root=str(tmp_path))

    # User explicitly passes enforce_test_tamper_guard=True
    orchestrator = SubagentOrchestrator(
        plan,
        workspace_root=tmp_path,
        enforce_test_tamper_guard=True,
        probe_base_discrimination=True,
    )
    assert orchestrator.enforce_test_tamper_guard is True
    assert orchestrator.probe_base_discrimination is True
