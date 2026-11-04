"""Unit tests for Pacer Anti-Slop & AI Laziness Detector."""

from __future__ import annotations

from pathlib import Path
import pytest

from visual_agent.anti_slop import (
    SlopFinding,
    detect_slop_in_code,
    detect_slop_in_files,
    format_slop_critique,
)


def test_detect_tautological_assert_literals():
    code = (
        "def test_foo():\n"
        "    x = 1 + 1\n"
        "    assert True\n"
        "    assert 1\n"
        "    assert not False\n"
    )
    findings = detect_slop_in_code(code, "test_sample.py", is_test_file=True)
    cats = [f.category for f in findings]
    assert cats.count("tautological_assertion") >= 3
    assert all(f.severity == "error" for f in findings)


def test_detect_tautological_comparison_same_expr():
    code = (
        "def test_compare():\n"
        "    val = compute()\n"
        "    assert val == val\n"
        "    assert 1 == 1\n"
        "    assert 'foo' == 'foo'\n"
    )
    findings = detect_slop_in_code(code, "test_compare.py", is_test_file=True)
    assert len(findings) == 3
    assert all(f.category == "tautological_assertion" for f in findings)


def test_detect_tautological_unittest_methods():
    code = (
        "class TestClass:\n"
        "    def test_run(self):\n"
        "        self.assertTrue(True)\n"
        "        self.assertFalse(False)\n"
        "        self.assertEqual(a.b, a.b)\n"
    )
    findings = detect_slop_in_code(code, "test_unit.py", is_test_file=True)
    assert len(findings) == 3
    assert all(f.category == "tautological_assertion" for f in findings)


def test_detect_empty_test_functions():
    code = (
        "def test_empty_pass():\n"
        "    pass\n\n"
        "def test_empty_docstring_only():\n"
        "    '''Just a docstring without any body or asserts.'''\n"
        "    pass\n\n"
        "def test_empty_ellipsis():\n"
        "    ...\n"
    )
    findings = detect_slop_in_code(code, "test_empty.py", is_test_file=True)
    empty_tests = [f for f in findings if f.category == "empty_test"]
    assert len(empty_tests) >= 2


def test_detect_swallowed_exceptions():
    code = (
        "def risky_operation():\n"
        "    try:\n"
        "        do_something()\n"
        "    except Exception:\n"
        "        pass\n\n"
        "    try:\n"
        "        do_something_else()\n"
        "    except:\n"
        "        pass\n"
    )
    findings = detect_slop_in_code(code, "impl.py", is_test_file=False)
    swallowed = [f for f in findings if f.category == "swallowed_exception"]
    assert len(swallowed) == 2
    assert all(f.severity == "error" for f in swallowed)


def test_detect_commented_out_assertions():
    code = (
        "def test_lazy():\n"
        "    res = calc()\n"
        "    # assert res == 42\n"
        "    # self.assertEqual(res, 42)\n"
    )
    findings = detect_slop_in_code(code, "test_comment.py", is_test_file=True)
    commented = [f for f in findings if f.category == "commented_out_assertion"]
    assert len(commented) == 2


def test_clean_code_passes_without_errors():
    code = (
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
        "    assert add(-1, 1) == 0\n"
    )
    findings = detect_slop_in_code(code, "test_clean.py", is_test_file=True)
    errors = [f for f in findings if f.severity == "error"]
    assert len(errors) == 0


def test_format_slop_critique():
    findings = [
        SlopFinding(
            file="test_core.py",
            line=12,
            category="tautological_assertion",
            message="Found 'assert True'",
            code_snippet="assert True",
            severity="error",
        ),
        SlopFinding(
            file="core.py",
            line=40,
            category="swallowed_exception",
            message="Found 'except Exception: pass'",
            code_snippet="except Exception: pass",
            severity="error",
        ),
    ]
    critique = format_slop_critique(findings)
    assert "[CRITICAL PACER REJECTION: AI LAZINESS / SLOP DETECTED]" in critique
    assert "test_core.py:12" in critique
    assert "core.py:40" in critique
    assert "RULES FOR REMEDIATION" in critique


def test_detect_slop_in_files(tmp_path: Path):
    bad_file = tmp_path / "test_bad.py"
    bad_file.write_text("def test_f():\n    assert 1 == 1\n", encoding="utf-8")

    good_file = tmp_path / "test_good.py"
    good_file.write_text("def test_g():\n    assert len([1]) == 1\n", encoding="utf-8")

    findings = detect_slop_in_files([bad_file, good_file], workspace_root=tmp_path)
    assert len(findings) == 1
    assert findings[0].category == "tautological_assertion"
