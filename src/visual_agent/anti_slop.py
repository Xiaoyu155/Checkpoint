"""AST-based Anti-Slop & AI Laziness Detector for Pacer.

"pacer的第一目标不是省了多少token，不是省了多少时间，不是干了什么活。
 而是纠错了多少ai的工作，发现了多少问题，发现了多少ai的糊弄。"

Cheap, fast, or weak models (Doubao, GLM, DeepSeek, Sub2API tokens, small models)
often attempt shortcuts to 'pass' verification gates:
1. Tautological assertions:
   - `assert True`
   - `assert 1 == 1`
   - `assert "a" == "a"`
   - `assert not False`
   - `self.assertTrue(True)`
   - `self.assertEqual(x, x)`
2. Empty or hollow test functions:
   - `def test_something(): pass`
   - `def test_something(): ...`
   - Test functions with zero assertions or verification calls.
3. Blind exception swallowing:
   - `try: ... except Exception: pass`
   - `try: ... except: pass`
   - `try: ... except BaseException: pass`
4. Hollow / dummy implementation stubs:
   - Replacing non-trivial functions with `pass` or `return True` or `return {}`.
5. Commented-out test assertions:
   - Commenting out failing assertions while claiming test passed.

This module statically audits AST trees, raises typed `SlopFinding`s, and produces
authoritative critique prompts to feed back into the model's self-healing loop.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Sequence


@dataclass
class SlopFinding:
    """A detected instance of AI laziness or fake-work."""

    file: str
    line: int
    category: str
    message: str
    code_snippet: str = ""
    severity: str = "error"  # "error" (hard reject) or "warning" (flagged)

    def to_dict(self) -> dict[str, str | int]:
        return {
            "file": self.file,
            "line": self.line,
            "category": self.category,
            "message": self.message,
            "code_snippet": self.code_snippet,
            "severity": self.severity,
        }


class _AntiSlopVisitor(ast.NodeVisitor):
    """AST Visitor scanning for known AI slop and laziness patterns."""

    def __init__(self, filename: str, is_test_file: bool = False, source_lines: list[str] | None = None) -> None:
        self.filename = filename
        self.is_test_file = is_test_file
        self.source_lines = source_lines or []
        self.findings: list[SlopFinding] = []

    def _get_snippet(self, lineno: int) -> str:
        if 1 <= lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].strip()
        return ""

    # ---------------------------------------------------------------------------
    # 1. Tautological assertions
    # ---------------------------------------------------------------------------
    def visit_Assert(self, node: ast.Assert) -> None:
        test_expr = node.test
        snippet = self._get_snippet(node.lineno)

        # 1.1 Direct constant boolean/literal: assert True, assert 1, assert "foo"
        if isinstance(test_expr, ast.Constant):
            if test_expr.value is True or (isinstance(test_expr.value, (int, str)) and bool(test_expr.value)):
                self.findings.append(
                    SlopFinding(
                        file=self.filename,
                        line=node.lineno,
                        category="tautological_assertion",
                        message=f"Tautological assert with constant literal '{test_expr.value}' detected. Assert must test actual runtime behavior.",
                        code_snippet=snippet,
                        severity="error",
                    )
                )

        # 1.2 UnaryOp: assert not False
        elif isinstance(test_expr, ast.UnaryOp) and isinstance(test_expr.op, ast.Not):
            if isinstance(test_expr.operand, ast.Constant) and test_expr.operand.value is False:
                self.findings.append(
                    SlopFinding(
                        file=self.filename,
                        line=node.lineno,
                        category="tautological_assertion",
                        message="Tautological 'assert not False' detected.",
                        code_snippet=snippet,
                        severity="error",
                    )
                )

        # 1.3 Compare: assert 1 == 1, assert "a" == "a", assert x == x
        elif isinstance(test_expr, ast.Compare):
            left_dump = ast.dump(test_expr.left)
            for comparator in test_expr.comparators:
                right_dump = ast.dump(comparator)
                if left_dump == right_dump:
                    self.findings.append(
                        SlopFinding(
                            file=self.filename,
                            line=node.lineno,
                            category="tautological_assertion",
                            message="Tautological comparison comparing expression against itself.",
                            code_snippet=snippet,
                            severity="error",
                        )
                    )

        self.generic_visit(node)

    # ---------------------------------------------------------------------------
    # 2. Method call assertions (e.g. self.assertTrue(True), self.assertEqual(1, 1))
    # ---------------------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        snippet = self._get_snippet(node.lineno)
        method_name = ""

        if isinstance(func, ast.Attribute):
            method_name = func.attr
        elif isinstance(func, ast.Name):
            method_name = func.id

        if method_name in ("assertTrue", "assert_true", "assertFalse", "assert_false"):
            if node.args and isinstance(node.args[0], ast.Constant):
                val = node.args[0].value
                if (method_name.startswith("assertTrue") and val is True) or (
                    method_name.startswith("assertFalse") and val is False
                ):
                    self.findings.append(
                        SlopFinding(
                            file=self.filename,
                            line=node.lineno,
                            category="tautological_assertion",
                            message=f"Tautological call '{method_name}({val})' detected.",
                            code_snippet=snippet,
                            severity="error",
                        )
                    )

        elif method_name in ("assertEqual", "assert_equal", "assert_equals"):
            if len(node.args) >= 2:
                arg0_dump = ast.dump(node.args[0])
                arg1_dump = ast.dump(node.args[1])
                if arg0_dump == arg1_dump:
                    self.findings.append(
                        SlopFinding(
                            file=self.filename,
                            line=node.lineno,
                            category="tautological_assertion",
                            message=f"Tautological call '{method_name}' comparing identical expressions.",
                            code_snippet=snippet,
                            severity="error",
                        )
                    )

        self.generic_visit(node)

    # ---------------------------------------------------------------------------
    # 3. Blind exception swallowing: try: ... except Exception: pass
    # ---------------------------------------------------------------------------
    def visit_Try(self, node: ast.Try) -> None:
        snippet = self._get_snippet(node.lineno)
        for handler in node.handlers:
            # Check if handler body contains only pass, return None, or empty statements
            is_empty_or_pass = False
            if len(handler.body) == 1:
                stmt = handler.body[0]
                if isinstance(stmt, ast.Pass):
                    is_empty_or_pass = True
                elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is ...:
                    is_empty_or_pass = True
                elif isinstance(stmt, ast.Return) and (
                    stmt.value is None or (isinstance(stmt.value, ast.Constant) and stmt.value.value is None)
                ):
                    is_empty_or_pass = True

            if is_empty_or_pass:
                # Check exception type
                exc_type_name = "bare except"
                if handler.type:
                    if isinstance(handler.type, ast.Name):
                        exc_type_name = handler.type.id
                    elif isinstance(handler.type, ast.Attribute):
                        exc_type_name = handler.type.attr
                    elif isinstance(handler.type, ast.Tuple):
                        tuple_names = []
                        for elt in handler.type.elts:
                            if isinstance(elt, ast.Name):
                                tuple_names.append(elt.id)
                            elif isinstance(elt, ast.Attribute):
                                tuple_names.append(elt.attr)
                        if "Exception" in tuple_names or "BaseException" in tuple_names:
                            exc_type_name = "Exception"
                        else:
                            exc_type_name = ",".join(tuple_names) or "typed_tuple"
                    else:
                        exc_type_name = "typed_except"

                # Blindly catching Exception, BaseException, or bare except
                if exc_type_name in ("Exception", "BaseException", "bare except"):
                    self.findings.append(
                        SlopFinding(
                            file=self.filename,
                            line=handler.lineno,
                            category="swallowed_exception",
                            message=f"Blindly catching '{exc_type_name}' with empty handler / pass statement masks defects.",
                            code_snippet=snippet,
                            severity="error",
                        )
                    )

        self.generic_visit(node)

    # ---------------------------------------------------------------------------
    # 4. Empty test functions and tests with zero assertions
    # ---------------------------------------------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_function(node)
        self.generic_visit(node)

    def _check_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        name = node.name
        is_test_func = name.startswith("test_") or name.endswith("_test") or (self.is_test_file and name.startswith("test"))

        # Filter out docstrings from body
        effective_stmts = [
            s for s in node.body
            if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
        ]

        snippet = self._get_snippet(node.lineno)

        # 4.1 Empty test function with only pass or ...
        if is_test_func:
            if not effective_stmts or all(isinstance(s, (ast.Pass, ast.Return)) for s in effective_stmts):
                self.findings.append(
                    SlopFinding(
                        file=self.filename,
                        line=node.lineno,
                        category="empty_test",
                        message=f"Test function '{name}' is completely empty (only pass/return). Fake tests are rejected.",
                        code_snippet=snippet,
                        severity="error",
                    )
                )
                return

            # 4.2 Check for tests with zero asserts or verification calls
            has_assert = False
            for child in ast.walk(node):
                if isinstance(child, ast.Assert):
                    has_assert = True
                    break
                if isinstance(child, ast.Call):
                    # Check method calls like self.assert*, pytest.raises, etc.
                    callee = ""
                    if isinstance(child.func, ast.Attribute):
                        callee = child.func.attr
                    elif isinstance(child.func, ast.Name):
                        callee = child.func.id
                    if "assert" in callee.lower() or callee in ("raises", "fail"):
                        has_assert = True
                        break

            if not has_assert:
                self.findings.append(
                    SlopFinding(
                        file=self.filename,
                        line=node.lineno,
                        category="missing_assertions",
                        message=f"Test function '{name}' contains no assertions or check statements.",
                        code_snippet=snippet,
                        severity="error",
                    )
                )

        # 4.3 Hollow dummy implementations (non-test functions)
        elif not self.is_test_file:
            # If function has a non-trivial docstring and parameters, but body is just `pass` or `return True` or `return {}`
            if len(effective_stmts) == 1 and not name.startswith("__"):
                first_stmt = effective_stmts[0]
                if isinstance(first_stmt, ast.Pass):
                    # Only flag if not an abstract method or protocol method
                    is_abstract = any(
                        (isinstance(d, ast.Name) and d.id in ("abstractmethod", "override"))
                        or (isinstance(d, ast.Attribute) and d.attr == "abstractmethod")
                        for d in node.decorator_list
                    )
                    if not is_abstract:
                        self.findings.append(
                            SlopFinding(
                                file=self.filename,
                                line=node.lineno,
                                category="dummy_implementation",
                                message=f"Function '{name}' is an empty pass-stub without abstract decorator.",
                                code_snippet=snippet,
                                severity="error",
                            )
                        )
                if isinstance(first_stmt, ast.Return) and not name.startswith("__"):
                    value = first_stmt.value
                    fake_constant = isinstance(value, ast.Constant) and value.value is True
                    fake_dict = isinstance(value, ast.Dict) and not value.keys
                    fake_list = isinstance(value, ast.List) and not value.elts
                    if fake_constant or fake_dict or fake_list:
                        self.findings.append(
                            SlopFinding(
                                file=self.filename,
                                line=node.lineno,
                                category="fake_return",
                                message=f"Function '{name}' returns a hardcoded success value.",
                                code_snippet=snippet,
                                severity="error",
                            )
                        )


def detect_slop_in_code(
    code_str: str,
    filename: str = "<memory>",
    is_test_file: bool | None = None,
) -> list[SlopFinding]:
    """Inspect Python source code string for AI slop patterns."""
    # Python accepts a UTF-8 BOM in source files; ast.parse(str) does not.
    # Match the interpreter by removing only a leading BOM before inspection.
    if code_str.startswith("\ufeff"):
        code_str = code_str[1:]
    if is_test_file is None:
        p = filename.lower()
        is_test_file = "test" in p or p.startswith("test_") or p.endswith("_test.py")

    findings: list[SlopFinding] = []

    # Check for commented-out assertions before AST parse
    lines = code_str.splitlines()
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if re.match(r"^#\s*(assert\s+|self\.assert|pytest\.raises)", stripped):
            findings.append(
                SlopFinding(
                    file=filename,
                    line=idx,
                    category="commented_out_assertion",
                    message="Commented-out assertion detected in test file. Commenting out assertions to pass gates is prohibited.",
                    code_snippet=stripped,
                    severity="error" if is_test_file else "warning",
                )
            )

    try:
        tree = ast.parse(code_str, filename=filename)
    except SyntaxError as syn_err:
        findings.append(
            SlopFinding(
                file=filename,
                line=syn_err.lineno or 1,
                category="syntax_error",
                message=f"Syntax error prevents AST validation: {syn_err.msg}",
                code_snippet=syn_err.text.strip() if syn_err.text else "",
                severity="error",
            )
        )
        return findings

    visitor = _AntiSlopVisitor(filename=filename, is_test_file=is_test_file, source_lines=lines)
    visitor.visit(tree)
    findings.extend(visitor.findings)

    return findings


def detect_slop_in_files(
    files: Sequence[str | Path],
    workspace_root: str | Path | None = None,
) -> list[SlopFinding]:
    """Inspect a list of file paths for AI slop patterns."""
    root = Path(workspace_root or Path.cwd()).resolve()
    all_findings: list[SlopFinding] = []

    for f in files:
        fp = Path(f)
        if not fp.is_absolute():
            fp = root / fp

        if not fp.exists() or not fp.is_file():
            continue

        if fp.suffix != ".py":
            continue

        rel_name = str(fp.relative_to(root)) if fp.is_relative_to(root) else str(fp)
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
            findings = detect_slop_in_code(content, filename=rel_name)
            all_findings.extend(findings)
        except OSError as err:
            all_findings.append(
                SlopFinding(
                    file=rel_name,
                    line=1,
                    category="io_error",
                    message=f"Failed to read file for anti-slop inspection: {err}",
                    severity="warning",
                )
            )

    return all_findings


def format_slop_critique(findings: Sequence[SlopFinding]) -> str:
    """Format slop findings into an authoritative critique prompt for LLM self-repair."""
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]

    lines = [
        "═══════════════════════════════════════════════════════════════════",
        " [CRITICAL PACER REJECTION: AI LAZINESS / SLOP DETECTED]",
        " Pacer's anti-slop AST inspector detected fake work or shortcuts.",
        " Weak models often use tautological assertions or empty handlers to fake passes.",
        " You MUST eliminate all slop and test real functionality.",
        "═══════════════════════════════════════════════════════════════════",
    ]

    if errors:
        lines.append("\n[BLOCKING REJECTIONS]:")
        for f in errors:
            snippet_str = f" -> `{f.code_snippet}`" if f.code_snippet else ""
            lines.append(f"  ✗ {f.file}:{f.line} [{f.category}] {f.message}{snippet_str}")

    if warnings:
        lines.append("\n[QUALITY WARNINGS]:")
        for f in warnings:
            snippet_str = f" -> `{f.code_snippet}`" if f.code_snippet else ""
            lines.append(f"  ⚠ {f.file}:{f.line} [{f.category}] {f.message}{snippet_str}")

    lines.extend([
        "",
        "RULES FOR REMEDIATION:",
        "1. Never use 'assert True', 'assert 1 == 1', or compare variables to themselves.",
        "2. Never leave test functions empty with only 'pass' or '...'.",
        "3. Never catch Exception with 'except Exception: pass'. Log, handle, or raise.",
        "4. Never comment out assertions to fake a passing test suite.",
        "Please rewrite the code and tests to implement genuine behavior and verification.",
    ])

    return "\n".join(lines)
