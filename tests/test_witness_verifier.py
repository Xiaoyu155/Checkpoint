"""
Tests for Pacer Witness-Based Verification System (见证制验收与假绿灯死刑判决器).

Validates:
1. Detection and rejection of tautological assertions (assert True, 1==1, constant negations).
2. Detection and rejection of swallowed exceptions (try...except: pass) and zero-assert tests.
3. False-Green Detection via Syntactic Mutation (rejecting tests that pass even when code is broken).
4. Mutation killing on genuine substantive tests.
5. Runtime execution tracing of lines and functions.
6. End-to-end WitnessCertificate issuance.
"""

from __future__ import annotations

from pathlib import Path
import pytest

from visual_agent.witness_verifier import (
    WitnessCertificate,
    WitnessVerifier,
)


@pytest.fixture
def verifier(tmp_path: Path) -> WitnessVerifier:
    return WitnessVerifier(workspace_root=tmp_path)


# ---------------------------------------------------------------------------
# 1. Assertion Substance & Tautology Rejection
# ---------------------------------------------------------------------------

def test_assertion_substance_catches_literal_constant_asserts(verifier: WitnessVerifier):
    """Reject assert True, assert 1, assert "success"."""
    bad_code = """
def test_fake_success():
    x = 10
    assert True
    assert 1
    assert "ok"
"""
    ok, msg, violations = verifier.verify_assertion_substance(bad_code)
    assert not ok
    assert "failed with" in msg
    assert any(v["type"] == "tautological_assertion" for v in violations)


def test_assertion_substance_catches_identical_side_comparisons(verifier: WitnessVerifier):
    """Reject assert x == x or assert 1 == 1."""
    bad_code = """
def test_circular_logic():
    result = 42
    assert result == result
    assert 100 == 100
"""
    ok, msg, violations = verifier.verify_assertion_substance(bad_code)
    assert not ok
    assert any("identical" in v["message"] or "compile-time constants" in v["message"] for v in violations)


def test_assertion_substance_catches_swallowed_exceptions_and_empty_stubs(verifier: WitnessVerifier):
    """Reject try...except: pass without assertions, and empty stub tests."""
    bad_code = """
def test_swallowing_errors():
    try:
        raise ValueError("Boom")
    except Exception:
        pass

def test_empty_stub():
    pass
"""
    ok, msg, violations = verifier.verify_assertion_substance(bad_code)
    assert not ok
    assert any(v["type"] == "swallowed_exception_pass" for v in violations)
    assert any(v["type"] == "empty_or_zero_assert_test" for v in violations)


def test_assertion_substance_passes_genuine_dynamic_assertions(verifier: WitnessVerifier):
    """Valid dynamic assertions must pass cleanly."""
    good_code = """
def test_genuine_calculation():
    val = 10 + 20
    assert val == 30
    assert val > 0
    assert isinstance(val, int)
"""
    ok, msg, violations = verifier.verify_assertion_substance(good_code)
    assert ok
    assert len(violations) == 0
    assert "Verified 3 substantive assertion(s)" in msg


# ---------------------------------------------------------------------------
# 2. Mutation Sensitivity & False-Green Killer
# ---------------------------------------------------------------------------

def test_mutation_sensitivity_detects_false_green(tmp_path: Path, verifier: WitnessVerifier):
    """
    Simulate a lazy model writing a False-Green test:
    The test always passes unconditionally (assert 2 + 2 == 4) and never checks the actual implementation!
    When implementation is mutated/broken, the test STILL passes -> MUST BE REJECTED.
    """
    impl_file = tmp_path / "order_service.py"
    impl_code = (
        "def process_order(price: int) -> int:\n"
        "    return price * 2\n"
    )
    impl_file.write_text(impl_code, encoding="utf-8")

    # Blind/False-Green test runner: passes regardless of order_service state
    def false_green_test_runner() -> tuple[bool, str]:
        # Simulating running pytest that always succeeds because it asserts unrelated constants
        return True, "pytest passed 1 item"

    ok, msg = verifier.verify_mutation_sensitivity(impl_file, false_green_test_runner)

    assert not ok
    assert "FALSE GREEN REJECTED" in msg
    assert "The test suite is blind to logic mutations" in msg
    # Verify file content was safely restored
    assert impl_file.read_text(encoding="utf-8") == impl_code


def test_mutation_sensitivity_passes_genuine_test_killing_mutant(tmp_path: Path, verifier: WitnessVerifier):
    """
    Genuine test runner:
    1. Baseline passes against normal implementation.
    2. When implementation is mutated, test fails -> Mutation killed -> VERIFIED!
    """
    impl_file = tmp_path / "tax_calculator.py"
    impl_code = (
        "def calculate_tax(amount: int) -> int:\n"
        "    return amount + 10\n"
    )
    impl_file.write_text(impl_code, encoding="utf-8")

    # Genuine test runner that checks actual behavior of calculate_tax
    def genuine_test_runner() -> tuple[bool, str]:
        code = impl_file.read_text(encoding="utf-8")
        if "PACER_WITNESS_MUTANT" in code or "raise RuntimeError" in code:
            return False, "RuntimeError: [PACER_WITNESS_MUTANT_TRIGGERED]"
        if "return amount + 11" in code:
            return False, "AssertionError: 110 != 120"
        return True, "All tests passed"

    ok, msg = verifier.verify_mutation_sensitivity(impl_file, genuine_test_runner)

    assert ok
    assert "Mutation successfully killed by test suite" in msg
    assert impl_file.read_text(encoding="utf-8") == impl_code


# ---------------------------------------------------------------------------
# 3. Execution Trace Verification
# ---------------------------------------------------------------------------

def test_execution_tracing_captures_executed_lines_and_calls(verifier: WitnessVerifier):
    """Ensure tracer identifies lines and functions executed inside target module."""
    def target_logic():
        a = 1
        b = 2
        return a + b

    res, lines, funcs = verifier.trace_execution(target_logic, "test_witness_verifier.py")

    assert res == 3
    assert len(lines) >= 3
    assert "target_logic" in funcs


# ---------------------------------------------------------------------------
# 4. Composite Witness Certification Lifecycle
# ---------------------------------------------------------------------------

def test_certify_delivery_lifecycle(tmp_path: Path, verifier: WitnessVerifier):
    """Verify full end-to-end certification."""
    impl_file = tmp_path / "impl_auth.py"
    impl_code = "def authenticate() -> bool:\n    return True\n"
    impl_file.write_text(impl_code, encoding="utf-8")

    valid_test_source = (
        "def test_auth():\n"
        "    from impl_auth import authenticate\n"
        "    assert authenticate() is True\n"
    )

    def test_runner() -> tuple[bool, str]:
        code = impl_file.read_text(encoding="utf-8")
        if "raise RuntimeError" in code or "return False" in code:
            return False, "Auth failed during mutation"
        return True, "Auth verified"

    cert = verifier.certify_delivery(
        target_file="impl_auth.py",
        test_file="test_auth.py",
        test_source=valid_test_source,
        impl_path=impl_file,
        test_runner=test_runner,
    )

    assert isinstance(cert, WitnessCertificate)
    assert cert.verdict == "VERIFIED"
    assert cert.assertion_substance_passed is True
    assert cert.mutation_killed is True
    assert cert.witness_id != ""

    cert_dict = cert.to_dict()
    assert cert_dict["verdict"] == "VERIFIED"
    assert cert_dict["target_file"] == "impl_auth.py"


# ---------------------------------------------------------------------------
# 5. Orchestrator Witness Gate Integration Tests
# ---------------------------------------------------------------------------

def test_orchestrator_witness_gate_rejects_trivial_assert_in_test_patch(tmp_path: Path):
    """
    When subagent writes a test with 'assert True', the verification command passes,
    but the Witness Gate intercepts it as a fake test and rejects the green verdict.
    """
    from unittest.mock import patch
    from visual_agent.subagent_orchestrator import (
        SubagentOrchestrator,
        SubagentPlan,
        SubagentTask,
    )

    test_file = tmp_path / "test_fake.py"
    test_file.write_text("def test_dummy():\n    pass\n", encoding="utf-8")

    task = SubagentTask(
        id="task_fake",
        title="Write test",
        role="coder",
        backend="local",
        instructions="Write test",
        target_files=["test_fake.py"],
        verification_command="python -m pytest test_fake.py",
        max_retries=0,
    )
    plan = SubagentPlan(goal="Write fake test", tasks=[task], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(
        plan,
        workspace_root=tmp_path,
        enforce_witness_verifier=True,
        enforce_anti_slop=False,  # Isolate witness gate test
    )

    diff = (
        "<<<<<<< SEARCH\n"
        "def test_dummy():\n"
        "    pass\n"
        "=======\n"
        "def test_dummy():\n"
        "    assert True\n"
        ">>>>>>> REPLACE\n"
    )

    with patch.object(orchestrator, "_dispatch_to_backend", return_value=(diff, None)), \
         patch.object(orchestrator, "_run_verification_command", return_value=(True, "pytest passed")):
        summary = orchestrator.run()

    assert summary["success"] is False
    assert summary["false_greens_caught"] >= 1
    assert "TRIVIAL / FAKE TEST DETECTED" in task.error


def test_orchestrator_keeps_real_diff_when_existing_suite_is_blind(tmp_path: Path):
    """A real diff stays when the only signal is an existing suite that does not cover it."""
    from unittest.mock import patch
    from visual_agent.subagent_orchestrator import (
        SubagentOrchestrator,
        SubagentPlan,
        SubagentTask,
    )

    impl_file = tmp_path / "service.py"
    impl_file.write_text("def run():\n    return 42\n", encoding="utf-8")

    task = SubagentTask(
        id="task_impl",
        title="Implement service",
        role="coder",
        backend="local",
        instructions="Implement service",
        target_files=["service.py"],
        verification_command="python -m pytest tests/test_service.py",
        max_retries=0,
    )
    plan = SubagentPlan(goal="Implement service", tasks=[task], repo_root=str(tmp_path))
    orchestrator = SubagentOrchestrator(
        plan,
        workspace_root=tmp_path,
        enforce_witness_verifier=True,
        enforce_anti_slop=False,
    )

    diff = (
        "<<<<<<< SEARCH\n"
        "def run():\n"
        "    return 42\n"
        "=======\n"
        "def run():\n"
        "    return 100\n"
        ">>>>>>> REPLACE\n"
    )

    # Verification runner always returns True (simulating a blind test that doesn't check run())
    with patch.object(orchestrator, "_dispatch_to_backend", return_value=(diff, None)), \
         patch.object(orchestrator, "_run_verification_command", return_value=(True, "pytest passed 1")):
        summary = orchestrator.run()

    assert summary["success"] is True
    assert summary["false_greens_caught"] == 0
    assert "return 100" in (tmp_path / "service.py").read_text(encoding="utf-8")

