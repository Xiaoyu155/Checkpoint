"""
Pacer Tiered Complexity Router & Fast-Path Engine (v2.2).

Addresses Topic 2:
Five-stage pipeline cost and latency explosion on micro-tasks.

Architecture:
1. TaskComplexity:
   - L1 (Micro, < 3s): Single-file, typo, comment, button color, CSS, config, small bugfix.
     -> Activates Fast-Path:
        * Bypasses Role 0 Stub generation (`contracts.py`)
        * Bypasses TamperGuard contract lock
        * Bypasses Git Red-to-Green base discrimination probe (saves 5-10s git operations)
        * Dispatches single micro-coder task directly with fast verification.
   - L2 (Standard, < 12s): Single module feature, bugfix with tests.
     -> Test-Driven TDD (Tester -> Coder -> AST Anti-Slop -> Pytest Gate).
   - L3 (Architecture, < 30s): Cross-module, architectural refactor, DAG, multi-file.
     -> Full Protocol: Role 0 Architect Stub -> TamperGuard Physical Lock ->
        Kahn Topological Waves -> AST Anti-Slop -> Red-to-Green Base Discrimination Gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import enum
from pathlib import Path
import re
from typing import Any

from .dag_scheduler import TaskComplexity, classify_task_complexity


class RoutingPath(str, enum.Enum):
    FAST_PATH = "fast_path"        # L1: Direct single patch, no stub contracts, no red-to-green probe
    STANDARD_TDD = "standard_tdd"  # L2: Test-first, anti-slop, pytest gate
    ARCHITECTURAL = "architectural"# L3: Stub first, tamper guard lock, wave fan-out, base probe


@dataclass(frozen=True)
class RoutingDecision:
    """Routing decision produced by TieredRouter."""
    complexity: TaskComplexity
    routing_path: RoutingPath
    target_files: list[str]
    skip_stub_contracts: bool
    skip_tamper_guard: bool
    skip_red_to_green_probe: bool
    max_retries: int
    rationale: str

    @property
    def path(self) -> RoutingPath:
        return self.routing_path

    @property
    def enforce_tamper_guard(self) -> bool:
        return not self.skip_tamper_guard

    @property
    def probe_base_first(self) -> bool:
        return not self.skip_red_to_green_probe


class TieredRouter:
    """Classifies task complexity and routes execution to optimal pipeline."""

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self.workspace = Path(workspace_root or ".").resolve()

    def route(
        self,
        goal: str,
        target_files: list[str] | None = None,
    ) -> RoutingDecision:
        """Analyze goal and target files, returning an optimized RoutingDecision."""
        files = target_files or []
        complexity = classify_task_complexity(goal, files)

        if complexity == TaskComplexity.L1:
            return RoutingDecision(
                complexity=TaskComplexity.L1,
                routing_path=RoutingPath.FAST_PATH,
                target_files=files,
                skip_stub_contracts=True,
                skip_tamper_guard=False,
                skip_red_to_green_probe=False,
                max_retries=1,
                rationale="L1 Micro-task: single-file patch. Tamper lock and red-to-green probe stay on.",
            )

        if complexity == TaskComplexity.L2:
            return RoutingDecision(
                complexity=TaskComplexity.L2,
                routing_path=RoutingPath.STANDARD_TDD,
                target_files=files,
                skip_stub_contracts=True,
                skip_tamper_guard=False,
                skip_red_to_green_probe=False,
                max_retries=2,
                rationale="L2 Standard task: Test-driven TDD with anti-slop inspection.",
            )

        return RoutingDecision(
            complexity=TaskComplexity.L3,
            routing_path=RoutingPath.ARCHITECTURAL,
            target_files=files,
            skip_stub_contracts=False,
            skip_tamper_guard=False,
            skip_red_to_green_probe=False,
            max_retries=3,
            rationale="L3 Architectural task: Full protocol with Role 0 stub contracts, TamperGuard, and wave DAG.",
        )
