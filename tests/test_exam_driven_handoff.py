"""Tests for Exam-Driven Task Handoff & Stateless Context Profile in Pacer.

Validates:
1. Out-of-band context profile extraction (< 1,500 tokens).
2. Default acceptance exam generation with AST and contract checks.
3. Physical verification of acceptance exams (syntax AST, contract presence, exit codes).
4. Subagent task execution gating against acceptance exams.
"""

from __future__ import annotations

import ast
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from visual_agent.context_profile import ContextProfile, ContextProfileManager
from visual_agent.subagent_orchestrator import (
    SubagentPlan,
    SubagentTask,
    SubagentOrchestrator,
    build_default_acceptance_exam,
    verify_acceptance_exam,
)


def test_context_profile_extraction_and_compactness(tmp_path: Path) -> None:
    # Setup mock project structure
    src_dir = tmp_path / "src" / "sample"
    src_dir.mkdir(parents=True, exist_ok=True)
    sample_py = src_dir / "worker.py"
    sample_py.write_text(
        "class HeavyWorker:\n    def process(self):\n        pass\n\ndef run():\n    pass\n",
        encoding="utf-8",
    )

    cpm = ContextProfileManager(tmp_path)
    profile = cpm.build_profile()

    assert profile.repo_name == tmp_path.name
    assert "src/sample/worker.py" in profile.exported_symbols
    symbols = profile.exported_symbols["src/sample/worker.py"]
    assert "class HeavyWorker" in symbols
    assert "def run()" in symbols

    compact_md = profile.to_compact_markdown(max_tokens=1200)
    assert "HeavyWorker" in compact_md
    assert profile.estimated_tokens <= 1200

    # Save and reload
    saved_path = cpm.save_profile(profile)
    assert saved_path.is_file()
    loaded = cpm.load_profile()
    assert loaded is not None
    assert loaded.repo_name == tmp_path.name


def test_build_default_acceptance_exam() -> None:
    targets = ["src/module_a.py", "src/module_b.py"]
    exam = build_default_acceptance_exam(
        task_title="Implement Core Service",
        instructions="Implement Module A and B",
        target_files=targets,
        repo_root="/fake/root",
        verification_cmd="pytest tests/test_core.py",
    )

    check_types = [item["check_type"] for item in exam]
    assert "syntax_ast" in check_types
    assert "contract" in check_types
    assert "execution" in check_types
    assert any("module_a.py" in item["question"] for item in exam)


def test_verify_acceptance_exam_detects_syntax_errors(tmp_path: Path) -> None:
    bad_py = tmp_path / "broken.py"
    bad_py.write_text("def broken_syntax(:\n    pass\n", encoding="utf-8")

    task = SubagentTask(
        id="t1",
        title="Fix broken",
        role="coder",
        backend="codex",
        instructions="Fix syntax",
        target_files=["broken.py"],
        acceptance_exam=[
            {
                "id": "exam_syntax_ast",
                "question": "broken.py must parse cleanly via AST",
                "check_type": "syntax_ast",
                "target_files": ["broken.py"],
            }
        ],
    )

    passed, results, critique = verify_acceptance_exam(task, tmp_path)
    assert passed is False
    assert len(results) == 1
    assert results[0]["passed"] is False
    assert "SyntaxError" in results[0]["details"]
    assert "Syntax/AST verification failed" in critique


def test_verify_acceptance_exam_detects_contract_and_passes_clean_code(tmp_path: Path) -> None:
    good_py = tmp_path / "valid.py"
    good_py.write_text("def valid_function():\n    return 42\n", encoding="utf-8")

    task = SubagentTask(
        id="t2",
        title="Implement valid",
        role="coder",
        backend="codex",
        instructions="Implement valid function",
        target_files=["valid.py"],
        acceptance_exam=[
            {
                "id": "exam_syntax_ast",
                "question": "valid.py must parse cleanly",
                "check_type": "syntax_ast",
                "target_files": ["valid.py"],
            },
            {
                "id": "exam_contract",
                "question": "valid.py must exist and export definitions",
                "check_type": "contract",
                "target_files": ["valid.py"],
            },
        ],
    )

    passed, results, critique = verify_acceptance_exam(task, tmp_path)
    assert passed is True
    assert all(r["passed"] for r in results)
    assert critique == ""


def test_orchestrator_acceptance_exam_gate_rejection_and_escalation(tmp_path: Path) -> None:
    # Task with an acceptance exam that fails AST check on first attempt
    target = tmp_path / "target.py"
    target.write_text("def broken(:\n    pass\n", encoding="utf-8")

    task = SubagentTask(
        id="task_fail",
        title="Implement service",
        role="coder",
        backend="gemini",
        instructions="Implement valid python function",
        target_files=["target.py"],
        acceptance_exam=[
            {
                "id": "exam_syntax_ast",
                "question": "target.py must pass AST check",
                "check_type": "syntax_ast",
                "target_files": ["target.py"],
            }
        ],
        max_retries=2,
    )
    plan = SubagentPlan(goal="Test exam gate", repo_root=str(tmp_path), tasks=[task])

    orchestrator = SubagentOrchestrator(plan, workspace_root=tmp_path)
    orchestrator.enforce_anti_slop = False  # Isolate acceptance exam gate testing

    # Mock _dispatch_to_backend to simulate a patch that leaves the file broken on attempt 1, then fixes it on attempt 2
    call_count = 0
    def mock_dispatch(t: SubagentTask) -> tuple[str, str | None]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            t.patched_files = ["target.py"]
            return "diff applied (broken)", None
        else:
            # Fix syntax error on retry
            target.write_text("def fixed():\n    return True\n", encoding="utf-8")
            t.patched_files = ["target.py"]
            return "diff applied (fixed)", None

    orchestrator._dispatch_to_backend = mock_dispatch  # type: ignore

    summary = orchestrator.run()
    assert summary["success"] is True
    assert task.status == "completed"
    assert task.backend == "workbuddy"
    assert orchestrator.mistakes_caught >= 1


def test_unknown_check_type_is_rejected_not_rubber_stamped(tmp_path: Path) -> None:
    """An unknown check_type must fail, not silently pass."""
    (tmp_path / "x.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    task = SubagentTask(
        id="t_unknown",
        title="unknown check",
        role="coder",
        backend="codex",
        instructions="x",
        target_files=["x.py"],
        acceptance_exam=[{"id": "q1", "check_type": "totally_made_up", "target_files": ["x.py"]}],
    )
    passed, results, critique = verify_acceptance_exam(task, tmp_path)
    assert passed is False
    assert results[0]["passed"] is False
    assert "Unknown check_type" in results[0]["details"]


def test_execution_check_without_command_is_rejected(tmp_path: Path) -> None:
    """An execution check with no command cannot be verified and must fail."""
    (tmp_path / "x.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    task = SubagentTask(
        id="t_nocmd",
        title="no cmd",
        role="coder",
        backend="codex",
        instructions="x",
        target_files=["x.py"],
        verification_command="",
        acceptance_exam=[{"id": "q1", "check_type": "execution", "target_files": ["x.py"]}],
    )
    passed, results, critique = verify_acceptance_exam(task, tmp_path)
    assert passed is False
    assert "cannot verify" in results[0]["details"].lower()


def test_assertion_cmd_takes_precedence_over_verification_command(tmp_path: Path) -> None:
    """Each exam's own assertion_cmd must run, not be replaced by the
    task-wide verification_command."""
    (tmp_path / "x.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    task = SubagentTask(
        id="t_prec",
        title="precedence",
        role="coder",
        backend="codex",
        instructions="x",
        target_files=["x.py"],
        verification_command="python -m pytest --version",
        acceptance_exam=[
            {
                "id": "q1",
                "check_type": "execution",
                "assertion_cmd": "python -c \"import sys; sys.exit(3)\"",
                "expected_exit_code": 0,
                "target_files": ["x.py"],
            }
        ],
    )
    passed, results, critique = verify_acceptance_exam(task, tmp_path)
    # assertion_cmd exits 3, so the check must fail (not be delegated away)
    assert passed is False
    assert "Exit code 3" in results[0]["details"]


def test_contract_rejects_comment_only_python_file(tmp_path: Path) -> None:
    """A non-empty but comment-only .py file has no executable code and
    must not satisfy a contract check."""
    (tmp_path / "hollow.py").write_text("# no implementation\n# just comments\n", encoding="utf-8")
    task = SubagentTask(
        id="t_hollow",
        title="hollow",
        role="coder",
        backend="codex",
        instructions="x",
        target_files=["hollow.py"],
        acceptance_exam=[{"id": "q1", "check_type": "contract", "target_files": ["hollow.py"]}],
    )
    passed, results, critique = verify_acceptance_exam(task, tmp_path)
    assert passed is False
    assert "no executable statements" in results[0]["details"]
