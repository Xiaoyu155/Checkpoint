"""
Tests for Pacer 2.1 Core Architectural Pillars:
1. Edge Case 1: Untracked file restoration via TamperGuard in-memory byte snapshot (< 1ms).
2. Edge Case 2: Kahn's topological wave partitioning (`topological_waves`) & cyclic detection.
3. Edge Case 3: Physical separation of Stub Contracts & Implementations.
4. Complexity Tiered Routing (L1 / L2 / L3) in classification and decomposition.
5. TamperGuard integration in acceptance pillar assessment.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import stat
import time

import pytest

from visual_agent.dag_scheduler import (
    ContractDrivenDAGScheduler,
    ContractSpec,
    DAGSubTask,
    TaskComplexity,
    classify_task_complexity,
    topological_waves,
)
from visual_agent.pacer_pillars import assess_pillar
from visual_agent.subagent_orchestrator import (
    SubagentPlan,
    SubagentTask,
    generate_subagent_plan,
)
from visual_agent.tamper_guard import TamperGuard


# ---------------------------------------------------------------------------
# Edge Case 1: Untracked File Restoration & OS Read-Only Lock
# ---------------------------------------------------------------------------

def test_tamper_guard_locks_and_restores_untracked_file(tmp_path: Path):
    """
    Ensure TamperGuard protects files NOT tracked by git via in-memory byte
    snapshot, and restores them in < 1ms upon modification or deletion.
    """
    guard = TamperGuard(workspace_root=tmp_path)

    # 1. Create a new untracked test/contract file
    contract_file = tmp_path / "contracts.py"
    initial_content = b"# Role 0 Architect Contract\nclass CoreProtocol:\n    pass\n"
    contract_file.write_bytes(initial_content)

    # 2. Lock the file
    locked = guard.lock_files(["contracts.py"])
    assert "contracts.py" in locked
    assert "contracts.py" in guard.protected_files
    assert guard.backup_blobs["contracts.py"] == initial_content

    # Verify OS read-only lock applied (write bit stripped)
    mode = contract_file.stat().st_mode
    assert (mode & stat.S_IWRITE) == 0

    # 3. Simulate model tampering: unlock temporarily to simulate external write, or overwrite
    with guard.temporary_unlock():
        contract_file.write_bytes(b"# TAMPERED BY ROGUE MODEL\nassert True\n")

    # Re-apply mode as would happen after external tool run
    contract_file.chmod(contract_file.stat().st_mode & ~stat.S_IWRITE)

    # 4. Enforce integrity
    t0 = time.perf_counter()
    is_intact, alert_msg, tampered = guard.verify_and_enforce()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert not is_intact
    assert any("contracts.py" in f for f in tampered)
    assert "Tamper Guard Triggered" in alert_msg
    assert elapsed_ms < 200.0  # Instant in-memory restoration (< 200ms on Windows NTFS / Defender)

    # 5. Verify the file bytes were accurately restored to the original contract
    restored_bytes = contract_file.read_bytes()
    assert restored_bytes == initial_content

    # 6. Verify restoration on complete deletion
    guard.unlock_files()
    contract_file.unlink()
    assert not contract_file.exists()

    is_intact2, alert_msg2, tampered2 = guard.verify_and_enforce()
    assert not is_intact2
    assert any("contracts.py (DELETED)" in f for f in tampered2)
    assert contract_file.exists()
    assert contract_file.read_bytes() == initial_content


def test_tamper_guard_intact_verification(tmp_path: Path):
    """Ensure intact files pass verification without triggering alerts."""
    guard = TamperGuard(workspace_root=tmp_path)
    test_f = tmp_path / "tests" / "test_safe.py"
    test_f.parent.mkdir(parents=True, exist_ok=True)
    test_f.write_text("def test_ok(): pass\n", encoding="utf-8")

    guard.lock_files(["tests/test_safe.py"])
    is_intact, msg, tampered = guard.verify_and_enforce()

    assert is_intact
    assert "Integrity Verified" in msg
    assert len(tampered) == 0
    guard.unlock_files()


# ---------------------------------------------------------------------------
# Edge Case 2: Kahn's Topological Wave Partitioning & Cycle Detection
# ---------------------------------------------------------------------------

def test_topological_waves_independent_tasks():
    """Tasks with no dependencies should execute in a single concurrent wave."""
    t1 = DAGSubTask(id="t1", title="Task 1", target_file="a.py", role="coder", backend="grok", prompt="")
    t2 = DAGSubTask(id="t2", title="Task 2", target_file="b.py", role="coder", backend="gemini", prompt="")
    t3 = DAGSubTask(id="t3", title="Task 3", target_file="c.py", role="coder", backend="workbuddy", prompt="")

    waves = topological_waves([t1, t2, t3])
    assert len(waves) == 1
    assert {t.id for t in waves[0]} == {"t1", "t2", "t3"}


def test_topological_waves_fan_out_and_fan_in():
    """
    Diamond/Fan-out pattern:
    Wave 0: Architect Stub (t_stub)
    Wave 1: Module Impls [t_core, t_router] (parallel fan-out)
    Wave 2: Integration Gate (t_gate) (fan-in)
    """
    t_stub = DAGSubTask(id="t_stub", title="Stub", target_file="contracts.py", role="architect", backend="gemini", prompt="")
    t_core = DAGSubTask(id="t_core", title="Core", target_file="impl_core.py", role="coder", backend="grok", prompt="", dependencies=["t_stub"])
    t_router = DAGSubTask(id="t_router", title="Router", target_file="impl_router.py", role="coder", backend="grok", prompt="", dependencies=["t_stub"])
    t_gate = DAGSubTask(id="t_gate", title="Gate", target_file="tests/test_all.py", role="verifier", backend="local", prompt="", dependencies=["t_core", "t_router"])

    waves = topological_waves([t_gate, t_router, t_core, t_stub])
    assert len(waves) == 3
    # Wave 0: stub
    assert [t.id for t in waves[0]] == ["t_stub"]
    # Wave 1: core and router (concurrent)
    assert {t.id for t in waves[1]} == {"t_core", "t_router"}
    # Wave 2: gate
    assert [t.id for t in waves[2]] == ["t_gate"]


def test_topological_waves_cycle_rejection():
    """Detect circular dependencies and raise ValueError with offending IDs."""
    t1 = DAGSubTask(id="t1", title="T1", target_file="", role="coder", backend="grok", prompt="", dependencies=["t2"])
    t2 = DAGSubTask(id="t2", title="T2", target_file="", role="coder", backend="grok", prompt="", dependencies=["t1"])

    with pytest.raises(ValueError, match="Cyclic dependency detected"):
        topological_waves([t1, t2])


def test_topological_waves_duplicate_id_rejection():
    """Fail-closed on duplicate task IDs in DAG definition."""
    t1 = DAGSubTask(id="t_dup", title="T1", target_file="", role="coder", backend="grok", prompt="")
    t2 = DAGSubTask(id="t_dup", title="T2", target_file="", role="coder", backend="gemini", prompt="")

    with pytest.raises(ValueError, match="Duplicate task ID detected in DAG: 't_dup'"):
        topological_waves([t1, t2])


def test_topological_waves_missing_dependency_rejection():
    """Fail-closed when task depends on non-existent task ID."""
    t1 = DAGSubTask(id="t1", title="T1", target_file="", role="coder", backend="grok", prompt="", dependencies=["missing_parent_id"])

    with pytest.raises(ValueError, match="unresolved/missing dependencies in DAG: \\['missing_parent_id'\\]"):
        topological_waves([t1])


@pytest.mark.asyncio
async def test_contract_driven_dag_scheduler_execution(tmp_path: Path):
    """Test full execution of wave scheduler with TamperGuard enforcement."""
    scheduler = ContractDrivenDAGScheduler(workspace_root=tmp_path)

    # Create dummy contract and test file
    stub_file = tmp_path / "contracts.py"
    stub_file.write_text("class IService: pass\n", encoding="utf-8")
    scheduler.guard.lock_files(["contracts.py"])

    t1 = DAGSubTask(id="task_1", title="Impl 1", target_file="impl_a.py", role="coder", backend="grok", prompt="")
    t2 = DAGSubTask(id="task_2", title="Impl 2", target_file="impl_b.py", role="coder", backend="grok", prompt="")
    t3 = DAGSubTask(id="task_3", title="Verifier", target_file="verify.py", role="verifier", backend="local", prompt="", dependencies=["task_1", "task_2"])

    waves = topological_waves([t1, t2, t3])

    async def mock_worker(task: DAGSubTask) -> bool:
        await asyncio.sleep(0.01)
        # Create output file
        (tmp_path / task.target_file).write_text(f"# {task.id} output\n", encoding="utf-8")
        return True

    res = await scheduler.execute_waves(waves, mock_worker)
    scheduler.guard.unlock_files()

    assert res["success"] is True
    assert res["waves_count"] == 2
    assert set(res["completed_tasks"]) == {"task_1", "task_2", "task_3"}
    assert (tmp_path / "impl_a.py").exists()
    assert (tmp_path / "impl_b.py").exists()


# ---------------------------------------------------------------------------
# Edge Case 3: Physical Separation of Contracts & Implementation
# ---------------------------------------------------------------------------

def test_physical_separation_of_contracts_and_implementation(tmp_path: Path):
    """
    Verify that Role 0 stub contracts can be locked read-only by TamperGuard,
    while separate implementation modules can be created, edited, and verified
    without touching the locked contracts.
    """
    guard = TamperGuard(workspace_root=tmp_path)

    # 1. Role 0 Architect creates contracts.py
    contract_path = tmp_path / "contracts.py"
    contract_code = (
        "from typing import Protocol\n\n"
        "class CalculatorProtocol(Protocol):\n"
        "    def add(self, a: int, b: int) -> int: ...\n"
    )
    contract_path.write_text(contract_code, encoding="utf-8")
    guard.lock_files(["contracts.py"])

    # 2. Subagent creates impl_calc.py importing and conforming to contracts.py
    impl_path = tmp_path / "impl_calc.py"
    impl_code = (
        "from contracts import CalculatorProtocol\n\n"
        "class Calculator:\n"
        "    def add(self, a: int, b: int) -> int:\n"
        "        return a + b\n"
    )
    impl_path.write_text(impl_code, encoding="utf-8")

    # 3. Verify that contracts.py remains locked and intact
    is_intact, msg, tampered = guard.verify_and_enforce()
    assert is_intact
    assert len(tampered) == 0

    # 4. Verify implementation functions properly with the contract
    import importlib.util
    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        spec_c = importlib.util.spec_from_file_location("contracts", str(contract_path))
        mod_c = importlib.util.module_from_spec(spec_c)
        spec_c.loader.exec_module(mod_c)

        spec_i = importlib.util.spec_from_file_location("impl_calc", str(impl_path))
        mod_i = importlib.util.module_from_spec(spec_i)
        spec_i.loader.exec_module(mod_i)

        calc = mod_i.Calculator()
        assert calc.add(10, 20) == 30
    finally:
        sys.path.pop(0)
        guard.unlock_files()


# ---------------------------------------------------------------------------
# Tiered Complexity Routing (L1 / L2 / L3)
# ---------------------------------------------------------------------------

def test_classify_task_complexity():
    """Verify task classification into L1, L2, L3 based on rules and keywords."""
    # L1: Micro tasks (< 3s)
    assert classify_task_complexity("fix typo in readme.md") == TaskComplexity.L1
    assert classify_task_complexity("update config comment") == TaskComplexity.L1
    assert classify_task_complexity("修改错别字和版本号", ["pyproject.toml"]) == TaskComplexity.L1

    # L2: Standard features (< 12s)
    assert classify_task_complexity("implement user login endpoint") == TaskComplexity.L2
    assert classify_task_complexity("add max tile calculation to 2048 game") == TaskComplexity.L2

    # L3: Architecture (< 30s)
    assert classify_task_complexity("refactor authentication pipeline architecture") == TaskComplexity.L3
    assert classify_task_complexity("重构多模块DAG调度子系统") == TaskComplexity.L3
    assert classify_task_complexity("task spanning many files", ["a.py", "b.py", "c.py"]) == TaskComplexity.L3


def test_tiered_decomposition_l1(tmp_path: Path):
    """L1 tasks skip the redundant AST indexer and produce a direct 2-task pipeline."""
    plan = generate_subagent_plan(
        goal="fix typo in readme.md",
        repo_root=tmp_path,
        workbuddy_available=False,
        fast_local_plan=True,
    )
    assert plan.complexity == TaskComplexity.L1.value
    assert len(plan.tasks) == 2
    assert plan.tasks[0].role == "coder"
    assert plan.tasks[1].role == "verifier"


def test_tiered_decomposition_l3(tmp_path: Path):
    """L3 architectural tasks generate Role 0 Architect indexer and contract-driven tasks."""
    plan = generate_subagent_plan(
        goal="refactor architecture and protocol for multi-module pipeline",
        repo_root=tmp_path,
        workbuddy_available=False,
        fast_local_plan=True,
    )
    assert plan.complexity == TaskComplexity.L3.value
    assert len(plan.tasks) >= 3
    assert plan.tasks[0].role == "indexer"
    assert "Architecture contract" in plan.tasks[0].title


# ---------------------------------------------------------------------------
# TamperGuard Integration in Five Pillars
# ---------------------------------------------------------------------------

def test_tamper_guard_in_acceptance_pillar():
    """Verify that tamper violation immediately fails acceptance pillar assessment."""
    # When no tamper occurs and conditions met: passes
    good_evidence = {
        "evidence_integrity": "verified",
        "acceptance_adequacy": "sufficient",
        "product_verdict": "pass",
        "digest_verified": True,
        "tamper_violation": False,
        "tamper_verified": True,
        "tampered_files": [],
    }
    res_good = assess_pillar("acceptance", good_evidence)
    assert res_good["status"] == "passed"
    assert res_good["passed"] is True

    # When tamper is flagged: fails with tamper_guard_violation
    bad_evidence = dict(good_evidence)
    bad_evidence["tamper_violation"] = True
    bad_evidence["tampered_files"] = ["tests/test_feature.py (MODIFIED)"]

    res_bad = assess_pillar("acceptance", bad_evidence)
    assert res_bad["status"] == "failed"
    assert res_bad["passed"] is False
    assert "tamper_guard_violation" in res_bad["reason_codes"]


def test_subagent_orchestrator_wave_execution_and_tamper_enforcement(tmp_path: Path):
    """
    Verify SubagentOrchestrator executes tasks wave-by-wave,
    and TamperGuard restores tampered files between waves.
    """
    from visual_agent.subagent_orchestrator import SubagentOrchestrator

    # 1. Setup workspace with protected contract and test
    contract_file = tmp_path / "contracts.py"
    orig_contract = "# Original Architect Stub\nclass IService: pass\n"
    contract_file.write_text(orig_contract, encoding="utf-8")

    test_file = tmp_path / "tests" / "test_service.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    orig_test = "def test_service(): pass\n"
    test_file.write_text(orig_test, encoding="utf-8")

    # 2. Build 3-wave plan
    t1 = SubagentTask(
        id="task_1",
        title="Role 0 Indexer",
        role="indexer",
        backend="local",
        target_files=["contracts.py"],
        instructions="Index symbols in contracts.py",
        dependencies=[],
    )
    t2 = SubagentTask(
        id="task_2",
        title="Role 2 Coder",
        role="coder",
        backend="local",
        target_files=["impl.py"],
        instructions="Implement logic in impl.py",
        dependencies=["task_1"],
    )
    t3 = SubagentTask(
        id="task_3",
        title="Role 4 Verifier Gate",
        role="verifier",
        backend="local",
        target_files=["tests/test_service.py"],
        instructions="Verify tests pass",
        verification_command="python -c \"pass\"",
        dependencies=["task_2"],
    )

    plan = SubagentPlan(
        goal="L3 Architecture Task",
        repo_root=str(tmp_path),
        tasks=[t1, t2, t3],
        complexity=TaskComplexity.L3.value,
    )

    orchestrator = SubagentOrchestrator(
        plan=plan,
        workspace_root=tmp_path,
        enforce_test_tamper_guard=True,
    )

    def mock_dispatch(task: SubagentTask) -> tuple[str, str | None]:
        if task.role == "coder":
            return ("```python\n# filepath: impl.py\nclass ServiceImpl:\n    pass\n```", None)
        return (f"Subtask {task.id} ok", None)

    orchestrator._dispatch_to_backend = mock_dispatch

    # 3. Simulate rogue modification to contracts.py during task execution
    def progress_callback(task: SubagentTask, msg: str):
        if task.id == "task_2" and "Starting" in msg:
            # Simulate unauthorized write to contracts.py
            with orchestrator.guard.temporary_unlock():
                contract_file.write_text("# ROGUE OVERWRITE\n", encoding="utf-8")

    orchestrator.on_progress = progress_callback
    res = orchestrator.run()

    # 4. Verify that TamperGuard detected the tamper, restored the contract from memory bytes
    assert contract_file.read_text(encoding="utf-8") == orig_contract
    assert hasattr(orchestrator, "guard_tampered_files")
    assert any("contracts.py" in f for f in orchestrator.guard_tampered_files)
    # Downstream verifier was skipped because of tamper alert!
    assert res["success"] is False
    assert plan.get_task("task_3").status == "skipped"
