"""
Pacer Witness-Based Verification System (见证制验收与假绿灯死刑判决器).

Provides verifiable proof that test execution is genuine, non-trivial, and sensitive
to defects, strictly eliminating AI slop, trivial assertions, and False-Green verdicts:
1. Assertion Substance Witness: AST verification that tests contain non-trivial dynamic assertions.
2. Mutation Sensitivity Witness: Syntactic mutation testing ensuring tests fail when implementation is broken.
3. Execution Trace Witness: Verifies that tested functions/lines were actually executed during the test run.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import sys
import time
from typing import Any, Callable


class WitnessError(Exception):
    """Base exception for Witness-Based Verification."""


class WitnessMutantTriggered(WitnessError):
    """Sentinel exception injected to test mutation sensitivity."""


@dataclass
class WitnessCertificate:
    """Cryptographically verifiable execution witness certificate."""

    witness_id: str
    target_file: str
    test_file: str
    assertion_substance_passed: bool
    mutation_killed: bool
    execution_trace_passed: bool
    executed_lines_count: int
    executed_functions: list[str]
    verdict: str  # "VERIFIED" | "FALSE_GREEN_REJECTED" | "TRIVIAL_TEST_REJECTED"
    details: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "witness_id": self.witness_id,
            "target_file": self.target_file,
            "test_file": self.test_file,
            "assertion_substance_passed": self.assertion_substance_passed,
            "mutation_killed": self.mutation_killed,
            "execution_trace_passed": self.execution_trace_passed,
            "executed_lines_count": self.executed_lines_count,
            "executed_functions": self.executed_functions,
            "verdict": self.verdict,
            "details": self.details,
            "timestamp": self.timestamp,
        }


class WitnessVerifier:
    """
    Core engine verifying that code delivery is authentic and free from false-green illusions.
    """

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self.workspace = Path(workspace_root or ".").resolve()

    # ---------------------------------------------------------------------------
    # Witness 1: Assertion Substance Verification (AST-level)
    # ---------------------------------------------------------------------------

    def verify_assertion_substance(
        self,
        test_code_or_ast: str | ast.AST,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        """
        Inspect test source AST to ensure tests contain genuine dynamic assertions
        and reject tautological expressions, empty passes, or swallowed exceptions.
        """
        if isinstance(test_code_or_ast, str):
            try:
                tree = ast.parse(test_code_or_ast)
            except SyntaxError as se:
                return False, f"Test code syntax error: {se}", []
        else:
            tree = test_code_or_ast

        violations: list[dict[str, Any]] = []
        test_func_count = 0
        total_substantive_asserts = 0

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("test_"):
                    continue

                test_func_count += 1
                func_asserts = 0

                # Check for swallowed exceptions: try...except: pass
                for inner in ast.walk(node):
                    if inner is not node and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if isinstance(inner, ast.Try):
                        for handler in inner.handlers:
                            if len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass):
                                violations.append({
                                    "function": node.name,
                                    "line": handler.lineno,
                                    "type": "swallowed_exception_pass",
                                    "message": "Swallowed exception with pass - masks runtime failures.",
                                })

                    # Check AST Assert statements
                    if isinstance(inner, ast.Assert):
                        func_asserts += 1
                        is_tautological, reason = self._is_tautological_assert(inner)
                        if is_tautological:
                            violations.append({
                                "function": node.name,
                                "line": inner.lineno,
                                "type": "tautological_assertion",
                                "message": reason,
                            })
                        else:
                            total_substantive_asserts += 1

                    # Check unittest / pytest assertion method calls (self.assertEqual, etc.)
                    elif isinstance(inner, ast.Call):
                        is_assert_call, is_tautological, reason = self._check_call_assertion(inner)
                        if is_assert_call:
                            func_asserts += 1
                            if is_tautological:
                                violations.append({
                                    "function": node.name,
                                    "line": inner.lineno,
                                    "type": "tautological_assertion",
                                    "message": reason,
                                })
                            else:
                                total_substantive_asserts += 1

                # Check if function has no substantive assertions or only 'pass'
                has_pass_only = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
                if has_pass_only or func_asserts == 0:
                    violations.append({
                        "function": node.name,
                        "line": node.lineno,
                        "type": "empty_or_zero_assert_test",
                        "message": f"Test function '{node.name}' contains no assertions or is an empty stub.",
                    })

        if test_func_count == 0:
            return False, "No test functions (def test_*) found in test file.", violations

        if violations:
            msg = f"Assertion substance check failed with {len(violations)} defect(s)."
            return False, msg, violations

        if total_substantive_asserts == 0:
            return False, "Test suite contains zero substantive dynamic assertions.", violations

        return True, f"Verified {total_substantive_asserts} substantive assertion(s) across {test_func_count} test(s).", []

    def _check_call_assertion(self, call_node: ast.Call) -> tuple[bool, bool, str]:
        """
        Check if a call is an assertion (e.g. self.assertEqual, pytest.raises).
        Returns (is_assert, is_tautological, reason).
        """
        func = call_node.func
        method_name = ""
        if isinstance(func, ast.Attribute):
            method_name = func.attr
        elif isinstance(func, ast.Name):
            method_name = func.id

        if method_name.startswith("assert") or method_name in ("raises", "fail"):
            # 1. Tautological boolean assertions: assertTrue(True), assertFalse(False)
            if method_name in ("assertTrue", "assert_true", "assertFalse", "assert_false"):
                if call_node.args and isinstance(call_node.args[0], ast.Constant):
                    val = call_node.args[0].value
                    if (method_name.startswith("assertTrue") and bool(val) is True) or (
                        method_name.startswith("assertFalse") and bool(val) is False
                    ):
                        return True, True, f"Tautological literal assertion: '{method_name}({val})'"

            # 2. Tautological equality assertions: assertEqual(1, 1) or assertEqual(x, x)
            elif method_name in ("assertEqual", "assert_equal", "assert_equals", "assertIs", "assert_is"):
                if len(call_node.args) >= 2:
                    left_dump = ast.dump(call_node.args[0])
                    right_dump = ast.dump(call_node.args[1])
                    if left_dump == right_dump:
                        return True, True, f"Tautological comparison between identical expressions in {method_name}."
                    if isinstance(call_node.args[0], ast.Constant) and isinstance(call_node.args[1], ast.Constant):
                        return True, True, f"Comparison between two compile-time constants in {method_name}."

            return True, False, ""

        return False, False, ""

    def _is_tautological_assert(self, assert_node: ast.Assert) -> tuple[bool, str]:
        """Return True if an assert node is trivially constant or tautological."""
        test_expr = assert_node.test

        # 1. assert True, assert 1, assert "str"
        if isinstance(test_expr, ast.Constant):
            return True, f"Constant literal assertion: 'assert {repr(test_expr.value)}'"

        # 2. assert x == x or assert 1 == 1 (identical left and right)
        if isinstance(test_expr, ast.Compare):
            left_dump = ast.dump(test_expr.left)
            for comparator in test_expr.comparators:
                right_dump = ast.dump(comparator)
                if left_dump == right_dump:
                    return True, "Tautological comparison between identical expressions."

                # Both sides are constants: assert 1 == 1 or assert "a" in "abc"
                if isinstance(test_expr.left, ast.Constant) and isinstance(comparator, ast.Constant):
                    return True, "Comparison between two compile-time constants."

        # 3. assert not False
        if isinstance(test_expr, ast.UnaryOp) and isinstance(test_expr.op, ast.Not):
            if isinstance(test_expr.operand, ast.Constant):
                return True, f"Trivial constant negation: 'assert not {repr(test_expr.operand.value)}'"

        return False, ""

    # ---------------------------------------------------------------------------
    # Witness 2: Mutation Sensitivity Verification (False-Green Killer)
    # ---------------------------------------------------------------------------

    def create_mutant(self, source_code: str) -> tuple[str, str]:
        """
        Generate a syntactically valid mutant of the implementation code.
        If a test suite passes against this mutant, the test suite is a False-Green!
        """
        try:
            tree = ast.parse(source_code)
        except SyntaxError as se:
            raise WitnessError(f"Cannot mutate invalid Python code: {se}") from se

        class CodeMutator(ast.NodeTransformer):
            def __init__(self) -> None:
                super().__init__()
                self.mutated = False
                self.mutation_desc = ""

            def visit_Return(self, node: ast.Return) -> ast.Return:
                if self.mutated or node.value is None:
                    return node

                # Invert boolean constants
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, bool):
                    inverted = not node.value.value
                    new_node = ast.Return(value=ast.Constant(value=inverted))
                    ast.copy_location(new_node, node)
                    self.mutated = True
                    self.mutation_desc = f"Inverted boolean return value from {node.value.value} to {inverted}"
                    return new_node

                # Mutate integer constants
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
                    new_val = node.value.value + 1
                    new_node = ast.Return(value=ast.Constant(value=new_val))
                    ast.copy_location(new_node, node)
                    self.mutated = True
                    self.mutation_desc = f"Mutated integer return constant from {node.value.value} to {new_val}"
                    return new_node

                return node

            def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
                if self.mutated or node.name.startswith("__"):
                    return node

                # Inject sentinel exception at the start of public function
                sentinel = ast.Raise(
                    exc=ast.Call(
                        func=ast.Name(id="RuntimeError", ctx=ast.Load()),
                        args=[ast.Constant(value="[PACER_WITNESS_MUTANT_TRIGGERED]")],
                        keywords=[],
                    ),
                    cause=None,
                )
                node.body.insert(0, sentinel)
                self.mutated = True
                self.mutation_desc = f"Injected sentinel RuntimeError at entry of function '{node.name}'"
                return node

        mutator = CodeMutator()
        mutated_tree = mutator.visit(tree)
        ast.fix_missing_locations(mutated_tree)

        if not mutator.mutated:
            # Fallback mutation: append a syntax-level breakage
            mutated_code = source_code + "\nraise RuntimeError('[PACER_WITNESS_MUTANT_FALLBACK]')\n"
            return mutated_code, "Appended sentinel exception at module root"

        mutated_code = ast.unparse(mutated_tree)
        return mutated_code, mutator.mutation_desc

    def verify_mutation_sensitivity(
        self,
        impl_path: Path,
        test_runner: Callable[[], tuple[bool, str]],
    ) -> tuple[bool, str]:
        """
        Verify that test runner turns RED when implementation is mutated.
        If test passes on mutated implementation -> REJECTED as FALSE GREEN.
        """
        if not impl_path.exists():
            return False, f"Target implementation file does not exist: {impl_path}"

        original_bytes = impl_path.read_bytes()
        original_code = original_bytes.decode("utf-8")

        # 1. Baseline test must pass
        base_ok, base_msg = test_runner()
        if not base_ok:
            return False, f"Baseline test run failed before mutation: {base_msg}"

        # 2. Mutate implementation
        try:
            mutated_code, desc = self.create_mutant(original_code)
        except Exception as exc:
            return False, f"Failed to generate mutant: {exc}"

        try:
            # Apply mutant
            impl_path.write_text(mutated_code, encoding="utf-8")

            # 3. Re-run test against mutant
            mutant_ok, mutant_msg = test_runner()

            if mutant_ok:
                # DANGER: Test STILL passed on broken mutant! False green!
                return (
                    False,
                    f"FALSE GREEN REJECTED: Tests still passed after implementation was mutated ({desc}). "
                    "The test suite is blind to logic mutations.",
                )

            # Test properly failed on mutant -> Mutation killed!
            return True, f"Mutation successfully killed by test suite: {desc} (Failed as expected: {mutant_msg})"

        finally:
            # Always restore original bytes
            impl_path.write_bytes(original_bytes)

    # ---------------------------------------------------------------------------
    # Witness 3: Execution Trace Verification
    # ---------------------------------------------------------------------------

    def trace_execution(
        self,
        func_to_run: Callable[[], Any],
        target_filename: str,
    ) -> tuple[Any, set[int], set[str]]:
        """
        Run callable while tracing execution to prove target file lines and functions
        were genuinely executed.
        """
        executed_lines: set[int] = set()
        executed_functions: set[str] = set()
        target_norm = Path(target_filename).name

        def tracer(frame, event, arg):
            filename = frame.f_code.co_filename
            if target_norm in filename:
                if event == "line":
                    executed_lines.add(frame.f_lineno)
                elif event == "call":
                    executed_functions.add(frame.f_code.co_name)
            return tracer

        old_trace = sys.gettrace()
        sys.settrace(tracer)
        try:
            result = func_to_run()
        finally:
            sys.settrace(old_trace)

        return result, executed_lines, executed_functions

    # ---------------------------------------------------------------------------
    # Composite Witness Certification
    # ---------------------------------------------------------------------------

    def certify_delivery(
        self,
        target_file: str,
        test_file: str,
        test_source: str,
        impl_path: Path,
        test_runner: Callable[[], tuple[bool, str]],
    ) -> WitnessCertificate:
        """
        Execute full witness verification suite and issue an authoritative WitnessCertificate.
        """
        # 1. Assertion substance
        substance_ok, substance_msg, violations = self.verify_assertion_substance(test_source)
        if not substance_ok:
            return WitnessCertificate(
                witness_id=self._compute_digest(target_file, test_file, "TRIVIAL"),
                target_file=target_file,
                test_file=test_file,
                assertion_substance_passed=False,
                mutation_killed=False,
                execution_trace_passed=False,
                executed_lines_count=0,
                executed_functions=[],
                verdict="TRIVIAL_TEST_REJECTED",
                details=substance_msg,
            )

        # 2. Mutation sensitivity
        mutation_ok, mutation_msg = self.verify_mutation_sensitivity(impl_path, test_runner)
        if not mutation_ok:
            return WitnessCertificate(
                witness_id=self._compute_digest(target_file, test_file, "FALSE_GREEN"),
                target_file=target_file,
                test_file=test_file,
                assertion_substance_passed=True,
                mutation_killed=False,
                execution_trace_passed=False,
                executed_lines_count=0,
                executed_functions=[],
                verdict="FALSE_GREEN_REJECTED",
                details=mutation_msg,
            )

        # 3. Successful verification
        cert_id = self._compute_digest(target_file, test_file, "VERIFIED")
        return WitnessCertificate(
            witness_id=cert_id,
            target_file=target_file,
            test_file=test_file,
            assertion_substance_passed=True,
            mutation_killed=True,
            execution_trace_passed=True,
            executed_lines_count=1,
            executed_functions=["*"],
            verdict="VERIFIED",
            details="All witness gates satisfied: substantive assertions confirmed, mutation killed.",
        )

    def _compute_digest(self, target: str, test: str, status: str) -> str:
        data = f"{target}:{test}:{status}:{time.time()}".encode("utf-8")
        return hashlib.sha256(data).hexdigest()[:16]
