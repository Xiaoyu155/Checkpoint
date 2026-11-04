"""
Pacer Contract-Driven DAG Scheduler & Tiered Routing Engine (v2.1).

Implements First-Priority Architecture:
1. Complexity Tiered Routing (L1 / L2 / L3):
   - L1 (Micro): Fast single-file patch without redundant multi-model pipeline.
   - L2 (Standard): Exam-Driven TDD (Test -> Coder -> Pytest Gate).
   - L3 (Architectural): Role 0 Architect (Stub First) -> Role 1 Exam Setter ->
     TamperGuard Lock -> Topological Waves (Fan-out) -> TamperGuard Verify -> Pytest Gate.

2. Edge Case 2: Kahn's Topological Wave Execution (`topological_waves`):
   Partitions DAG tasks into sequential waves `List[List[SubTask]]`. All tasks within
   the same wave run concurrently, while waves run strictly in dependency order.

3. Edge Case 3: Physical Separation of Contracts (Stub) & Implementations:
   Contracts (`contracts.py`) and tests (`test_*.py`) are locked as read-only by
   TamperGuard. Subagents generate separate implementation files (`impl_*.py` or
   target files) that import and satisfy the contract without touching protected files.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
import enum
from pathlib import Path
import re
import time
from typing import Any, Callable, Coroutine

from .tamper_guard import TamperGuard


class TaskComplexity(str, enum.Enum):
    L1 = "L1"  # Micro: single file, config, doc, simple bugfix (< 3s)
    L2 = "L2"  # Standard: single module feature, test-driven (< 12s)
    L3 = "L3"  # Architecture: multi-file, cross-module, contract-driven (< 30s)


@dataclass
class ContractSpec:
    module_name: str
    stub_file: str            # e.g., "src/core/contracts.py"
    type_definitions: str     # Protocol / TypedDict / ABC definitions
    docstring: str = ""


@dataclass
class DAGSubTask:
    id: str
    title: str
    target_file: str
    role: str                 # architect | tester | coder | verifier
    backend: str              # grok | gemini | workbuddy | codex
    prompt: str
    dependencies: list[str] = field(default_factory=list)
    contract_file: str | None = None
    status: str = "pending"   # pending | running | completed | failed | skipped
    output: str = ""
    error: str | None = None
    elapsed_seconds: float = 0.0


def classify_task_complexity(goal: str, target_files: list[str] | None = None) -> TaskComplexity:
    """
    Classify a goal into L1, L2, or L3 complexity.
    L1: config, typo, single line, css, json, readme, small fix.
    L3: architecture, refactor, framework, multi-module, protocol, dag, ecosystem.
    L2: standard features, API endpoints, bugfixes with tests.
    """
    g = goal.lower()
    files = target_files or []

    # Security-critical keywords strictly prohibit L1 fast-path bypass
    security_keywords = [
        "auth", "guard", "token", "secret", "tamper", "escrow",
        "contract", "gateway", "permission", "security", "credential", "crypto"
    ]
    is_security_sensitive = any(any(sk in f.lower() for sk in security_keywords) for f in files) or any(sk in g for sk in security_keywords)

    # Check for L3 triggers (architectural indicators)
    l3_keywords = [
        "architecture", "架构", "重构", "refactor", "子系统", "subsystem",
        "多模块", "multi-module", "dag", "cross-module", "pipeline", "流水线",
        "contract", "契约", "微服务", "distributed", "protocol", "生态"
    ]
    if any(k in g for k in l3_keywords) or len(files) >= 3:
        return TaskComplexity.L3

    # Check for L1 triggers (micro indicators) - prohibited for security-sensitive tasks
    l1_keywords = [
        "typo", "错别字", "文案", "readme", "comment", "注释", "config",
        "配置", "单行", "微调", "css", "color", "style", "format", "格式化",
        "bump version", "版本号"
    ]
    if not is_security_sensitive and any(k in g for k in l1_keywords) and len(files) <= 1:
        return TaskComplexity.L1

    # Default to standard L2 (includes standard security-sensitive single module tasks with tests)
    return TaskComplexity.L2


def topological_waves(tasks: list[Any]) -> list[list[Any]]:
    """
    Kahn's Topological Wave Partitioning Algorithm (Edge Case 2).
    Partitions tasks into waves where tasks within each wave have no
    unresolved dependencies and can be executed concurrently in parallel.
    Supports both DAGSubTask and SubagentTask instances.
    Raises ValueError if duplicate IDs, missing dependencies, or cyclic dependencies are detected.
    """
    seen_ids: set[str] = set()
    for t in tasks:
        if t.id in seen_ids:
            raise ValueError(f"Duplicate task ID detected in DAG: '{t.id}'")
        seen_ids.add(t.id)

    task_map = {t.id: t for t in tasks}

    for t in tasks:
        missing_deps = [d for d in t.dependencies if d not in task_map]
        if missing_deps:
            raise ValueError(f"Task '{t.id}' has unresolved/missing dependencies in DAG: {missing_deps}")

    in_degree = {t.id: len(t.dependencies) for t in tasks}
    dependents = defaultdict(list)

    for t in tasks:
        for d in t.dependencies:
            dependents[d].append(t.id)

    # Wave 0: tasks with in_degree == 0
    current_wave_ids = [tid for tid, deg in in_degree.items() if deg == 0]
    waves: list[list[DAGSubTask]] = []
    processed_count = 0

    while current_wave_ids:
        # Sort wave deterministically by ID
        current_wave_ids.sort()
        wave_tasks = [task_map[tid] for tid in current_wave_ids]
        waves.append(wave_tasks)
        processed_count += len(wave_tasks)

        next_wave_ids = []
        for tid in current_wave_ids:
            for dep_tid in dependents[tid]:
                in_degree[dep_tid] -= 1
                if in_degree[dep_tid] == 0:
                    next_wave_ids.append(dep_tid)

        current_wave_ids = next_wave_ids

    if processed_count < len(tasks):
        unresolved = [t.id for t in tasks if in_degree[t.id] > 0]
        raise ValueError(f"Cyclic dependency detected among tasks: {unresolved}")

    return waves


class ContractDrivenDAGScheduler:
    """
    Orchestrates Role 0 Architect -> Role 1 Exam Setter -> TamperGuard Lock ->
    Topological Wave Execution -> TamperGuard Enforce -> Verification Gate.
    """

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self.workspace = Path(workspace_root or ".").resolve()
        self.guard = TamperGuard(self.workspace)

    def plan_l3_pipeline(
        self,
        goal: str,
        stub_filename: str = "contracts.py",
        test_filename: str = "test_feature.py",
        impl_files: list[str] | None = None,
    ) -> tuple[ContractSpec, list[DAGSubTask]]:
        """
        Create a Role 0 Stub Specification and fan-out SubTasks with separate
        implementation targets (Edge Case 3).
        """
        if not impl_files:
            raise ValueError("plan_l3_pipeline requires explicit implementation files")
        impl_targets = list(impl_files)

        # Stub Contract Specification
        contract = ContractSpec(
            module_name="contracts",
            stub_file=stub_filename,
            type_definitions=(
                "# --- Pacer Architect Contract (Role 0) ---\n"
                "from typing import Protocol, runtime_checkable, Any, Dict\n\n"
                "@runtime_checkable\n"
                "class FeatureServiceProtocol(Protocol):\n"
                "    def execute(self, payload: Dict[str, Any]) -> Dict[str, Any]: ...\n"
                "    def validate(self, key: str) -> bool: ...\n"
            ),
            docstring="Locked architectural contract interface.",
        )

        # Deconstruct into DAG SubTasks
        tasks: list[DAGSubTask] = []

        # SubTask 1 & 2: Independent module implementations (Fan-out Wave 0)
        for i, target in enumerate(impl_targets, 1):
            tasks.append(
                DAGSubTask(
                    id=f"impl_task_{i}",
                    title=f"Implement module {target}",
                    target_file=target,
                    role="coder",
                    backend="grok",
                    prompt=f"Implement logic in {target} strictly conforming to {stub_filename}.",
                    contract_file=stub_filename,
                    dependencies=[], # No dependencies between implementations -> parallel wave!
                )
            )

        # SubTask 3: Integration / Consumer (Wave 1: depends on all implementations)
        tasks.append(
            DAGSubTask(
                id="integration_task",
                title="Integrate and verify modules",
                target_file="app_main.py",
                role="verifier",
                backend="gemini",
                prompt=f"Integrate {', '.join(impl_targets)} with {stub_filename}.",
                contract_file=stub_filename,
                dependencies=[t.id for t in tasks], # Depends on Wave 0
            )
        )

        return contract, tasks

    async def execute_waves(
        self,
        waves: list[list[DAGSubTask]],
        worker_fn: Callable[[DAGSubTask], Coroutine[Any, Any, bool]],
    ) -> dict[str, Any]:
        """
        Execute tasks wave by wave. Within each wave, all tasks run concurrently.
        Validates with TamperGuard after each wave to ensure read-only files remain untouched.
        """
        t0 = time.time()
        completed_tasks: list[str] = []
        failed_tasks: list[str] = []

        for wave_idx, wave in enumerate(waves):
            # Run all tasks in current wave concurrently
            results = await asyncio.gather(*(worker_fn(task) for task in wave), return_exceptions=True)

            for task, res in zip(wave, results):
                if isinstance(res, Exception) or res is False:
                    task.status = "failed"
                    task.error = str(res) if isinstance(res, Exception) else "Worker returned False"
                    failed_tasks.append(task.id)
                else:
                    task.status = "completed"
                    completed_tasks.append(task.id)

            # Enforce TamperGuard at the end of every wave
            is_intact, alert, tampered = self.guard.verify_and_enforce()
            if not is_intact:
                return {
                    "success": False,
                    "error": f"Wave {wave_idx} Tamper Violation: {alert}",
                    "tampered_files": tampered,
                    "completed_tasks": completed_tasks,
                    "failed_tasks": failed_tasks,
                    "elapsed_seconds": time.time() - t0,
                }

            # Stop subsequent waves if current wave has failures
            if any(t.status == "failed" for t in wave):
                break

        return {
            "success": len(failed_tasks) == 0,
            "waves_count": len(waves),
            "completed_tasks": completed_tasks,
            "failed_tasks": failed_tasks,
            "elapsed_seconds": time.time() - t0,
        }
