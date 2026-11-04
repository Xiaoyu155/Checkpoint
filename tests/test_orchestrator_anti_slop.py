"""Unit and integration tests for Pacer Orchestrator Anti-Slop & Self-Repair Loop.

Validates that:
1. Shortcuts (tautological assertions, empty tests, swallowed exceptions) are caught
   by the Anti-Slop AST inspector BEFORE running verification commands.
2. Violations are recorded in Pacer's true core scorecard (mistakes_caught, slop_detected).
3. Authoritative critique prompts are fed back into the model's scratchpad/context.
4. Self-repair loop successfully resolves the slop, incrementing self_heals_succeeded.
5. Repeated laziness exhausts retries, causing clear and safe task failure.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import pytest

from visual_agent.subagent_orchestrator import (
    SubagentOrchestrator,
    SubagentPlan,
    SubagentTask,
)


def test_anti_slop_rejects_tautological_assert_and_self_heals(tmp_path: Path) -> None:
    test_file = tmp_path / "test_calc.py"
    test_file.write_text(
        "import unittest\n\n"
        "class TestCalc(unittest.TestCase):\n"
        "    def test_run(self):\n"
        "        val = 10\n"
        "        self.assertTrue(val > 0)\n",
        encoding="utf-8",
    )

    task = SubagentTask(
        id="t1",
        title="Add calculation test",
        role="coder",
        backend="local",
        instructions="Implement robust calculation test",
        target_files=["test_calc.py"],
        verification_command="python -m unittest test_calc.py",
        max_retries=2,
    )

    plan = SubagentPlan(goal="Test calculations", tasks=[task], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(
        plan, workspace_root=tmp_path, enforce_anti_slop=True, enforce_test_tamper_guard=False,
        probe_base_discrimination=False
    )

    # Attempt 1: Returns tautological assert 1 == 1
    # Attempt 2: Self-repairs with genuine assertion
    call_count = 0

    def mock_dispatch(t: SubagentTask) -> tuple[str, str | None]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            diff = (
                "<<<<<<< SEARCH\n"
                "        self.assertTrue(val > 0)\n"
                "=======\n"
                "        self.assertTrue(True)\n"
                ">>>>>>> REPLACE\n"
            )
            return diff, None
        else:
            diff = (
                "<<<<<<< SEARCH\n"
                "        self.assertTrue(val > 0)\n"
                "=======\n"
                "        res = 10 + 20\n"
                "        self.assertEqual(res, 30)\n"
                ">>>>>>> REPLACE\n"
            )
            return diff, None

    with patch.object(orchestrator, "_dispatch_to_backend", side_effect=mock_dispatch):
        summary = orchestrator.run()

    # Orchestrator caught the mistake on attempt 1, self-healed on attempt 2
    assert summary["success"] is True
    assert summary["mistakes_caught"] == 1
    assert summary["self_heals_succeeded"] == 1
    assert len(summary["slop_detected"]) == 1
    assert summary["slop_detected"][0]["category"] == "tautological_assertion"
    assert task.status == "completed"
    assert task.retries == 1

    # Verify critique was injected into task history
    slop_history = [h for h in task.history if h.get("category") == "anti_slop_rejection"]
    assert len(slop_history) == 1
    assert "[CRITICAL PACER REJECTION: AI LAZINESS / SLOP DETECTED]" in slop_history[0]["error"]

    # Verify final disk content has real assertion
    final_content = test_file.read_text(encoding="utf-8")
    assert "self.assertEqual(res, 30)" in final_content
    assert "self.assertTrue(True)" not in final_content


def test_anti_slop_rejects_empty_test_function(tmp_path: Path) -> None:
    test_file = tmp_path / "test_auth.py"
    test_file.write_text(
        "import unittest\n\n"
        "class TestAuth(unittest.TestCase):\n"
        "    def test_initial(self):\n"
        "        self.assertTrue(1 > 0)\n",
        encoding="utf-8",
    )

    task = SubagentTask(
        id="t2",
        title="Add auth verification",
        role="coder",
        backend="local",
        instructions="Create auth test",
        target_files=["test_auth.py"],
        verification_command="python -m unittest test_auth.py",
        max_retries=2,
    )

    plan = SubagentPlan(goal="Test auth", tasks=[task], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(
        plan, workspace_root=tmp_path, enforce_anti_slop=True, enforce_test_tamper_guard=False,
        probe_base_discrimination=False
    )

    call_count = 0

    def mock_dispatch(t: SubagentTask) -> tuple[str, str | None]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            diff = (
                "<<<<<<< SEARCH\n"
                "    def test_initial(self):\n"
                "        self.assertTrue(1 > 0)\n"
                "=======\n"
                "    def test_authenticate_user(self):\n"
                "        pass\n"
                ">>>>>>> REPLACE\n"
            )
            return diff, None
        else:
            diff = (
                "<<<<<<< SEARCH\n"
                "    def test_initial(self):\n"
                "        self.assertTrue(1 > 0)\n"
                "=======\n"
                "    def test_authenticate_user(self):\n"
                "        token = 'secret'\n"
                "        self.assertTrue(len(token) > 0)\n"
                ">>>>>>> REPLACE\n"
            )
            return diff, None

    with patch.object(orchestrator, "_dispatch_to_backend", side_effect=mock_dispatch):
        summary = orchestrator.run()

    assert summary["success"] is True
    assert summary["mistakes_caught"] == 1
    assert summary["self_heals_succeeded"] == 1
    assert summary["slop_detected"][0]["category"] == "empty_test"
    assert "test_authenticate_user" in summary["slop_detected"][0]["message"]


def test_anti_slop_rejects_swallowed_exception_in_implementation(tmp_path: Path) -> None:
    impl_file = tmp_path / "payment.py"
    impl_file.write_text("def process():\n    return 0\n", encoding="utf-8")

    test_file = tmp_path / "test_payment.py"
    test_file.write_text(
        "import unittest\nimport payment\n\n"
        "class TestPay(unittest.TestCase):\n"
        "    def test_pay(self):\n"
        "        self.assertEqual(payment.process(), 100)\n",
        encoding="utf-8",
    )

    task = SubagentTask(
        id="t3",
        title="Implement payment handler",
        role="coder",
        backend="local",
        instructions="Implement safe payment processing",
        target_files=["payment.py"],
        verification_command="python -m unittest test_payment.py",
        max_retries=2,
    )

    plan = SubagentPlan(goal="Payment handler", tasks=[task], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(
        plan, workspace_root=tmp_path, enforce_anti_slop=True, probe_base_discrimination=False
    )

    call_count = 0

    def mock_dispatch(t: SubagentTask) -> tuple[str, str | None]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            diff = (
                "<<<<<<< SEARCH\n"
                "def process():\n"
                "    return 0\n"
                "=======\n"
                "def process():\n"
                "    try:\n"
                "        charge()\n"
                "    except Exception:\n"
                "        pass\n"
                "    return 100\n"
                ">>>>>>> REPLACE\n"
            )
            return diff, None
        else:
            diff = (
                "<<<<<<< SEARCH\n"
                "def process():\n"
                "    return 0\n"
                "=======\n"
                "def process():\n"
                "    try:\n"
                "        return 100\n"
                "    except ValueError as err:\n"
                "        raise RuntimeError('Charge failed') from err\n"
                ">>>>>>> REPLACE\n"
            )
            return diff, None

    with patch.object(orchestrator, "_dispatch_to_backend", side_effect=mock_dispatch):
        summary = orchestrator.run()

    assert summary["success"] is True
    assert summary["mistakes_caught"] == 1
    assert summary["self_heals_succeeded"] == 1
    assert summary["slop_detected"][0]["category"] == "swallowed_exception"


def test_anti_slop_exhausts_retries_on_persistent_slop(tmp_path: Path) -> None:
    test_file = tmp_path / "test_stubborn.py"
    test_file.write_text(
        "import unittest\n\n"
        "class TestStubborn(unittest.TestCase):\n"
        "    def test_run(self):\n"
        "        self.assertTrue(1 > 0)\n",
        encoding="utf-8",
    )

    task = SubagentTask(
        id="t4",
        title="Stubborn slop test",
        role="coder",
        backend="local",
        instructions="Fix test",
        target_files=["test_stubborn.py"],
        verification_command="python -m unittest test_stubborn.py",
        max_retries=2,
    )

    plan = SubagentPlan(goal="Stubborn test", tasks=[task], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(
        plan, workspace_root=tmp_path, enforce_anti_slop=True, probe_base_discrimination=False
    )

    attempt_count = 0

    # Each attempt continues to submit tautological assertions
    def stubborn_dispatch(t: SubagentTask) -> tuple[str, str | None]:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            diff = (
                "<<<<<<< SEARCH\n"
                "        self.assertTrue(1 > 0)\n"
                "=======\n"
                "        self.assertTrue(True)\n"
                ">>>>>>> REPLACE\n"
            )
        elif attempt_count == 2:
            diff = (
                "<<<<<<< SEARCH\n"
                "        self.assertTrue(1 > 0)\n"
                "=======\n"
                "        assert 1 == 1\n"
                ">>>>>>> REPLACE\n"
            )
        else:
            diff = (
                "<<<<<<< SEARCH\n"
                "        self.assertTrue(1 > 0)\n"
                "=======\n"
                "        assert 'bad' == 'bad'\n"
                ">>>>>>> REPLACE\n"
            )
        return diff, None

    with patch.object(orchestrator, "_dispatch_to_backend", side_effect=stubborn_dispatch):
        summary = orchestrator.run()

    # Must fail after exceeding max_retries
    assert summary["success"] is False
    assert task.status == "failed"
    assert summary["mistakes_caught"] == 3
    assert len(summary["slop_detected"]) == 3
    assert summary["self_heals_succeeded"] == 0
    assert "[CRITICAL PACER REJECTION: AI LAZINESS / SLOP DETECTED]" in task.error


def test_tamper_guard_violation_counts_as_mistake_caught(tmp_path: Path) -> None:
    test_file = tmp_path / "test_core.py"
    test_file.write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n", encoding="utf-8")

    task = SubagentTask(
        id="t5",
        title="Malicious or rogue task",
        role="indexer",
        backend="local",
        instructions="Analyze system",
        target_files=["test_core.py"],
        verification_command="",
    )

    plan = SubagentPlan(goal="Test tamper", tasks=[task], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(plan, workspace_root=tmp_path, enforce_test_tamper_guard=True)

    try:
        # Pre-lock test file
        orchestrator.guard.lock_files(["test_core.py"])

        # Tamper with the locked file by chmodding and modifying
        test_file.chmod(0o666)
        test_file.write_text("# HACKED FILE\n", encoding="utf-8")

        with patch.object(orchestrator, "_dispatch_to_backend", return_value=("analysis complete", None)):
            summary = orchestrator.run()

        # Orchestrator should detect tamper violation and record as mistake_caught
        assert summary["tamper_violations_blocked"] >= 1
        assert summary["mistakes_caught"] >= 1
        assert summary["success"] is False
    finally:
        # Clean up read-only permission for tmp_path deletion
        orchestrator.guard.unlock_files()
        try:
            test_file.chmod(0o666)
        except OSError:
            pass

