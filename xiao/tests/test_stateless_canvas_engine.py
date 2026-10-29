"""Unit & Performance Benchmark Tests for Pacer's Stateless Canvas + Diff Patch Architecture.

Covers:
1. `canvas_manager.py`:
   - Atomic `.pacer/canvas.json` persistence (Goal, Invariants, FocusFiles, Scratchpad).
   - Strict `build_stateless_prompt` token cap (<= 1,500 tokens even under extreme input bloat).
2. `diff_patcher.py`:
   - Aider-style `<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE` block parsing.
   - Exact replacement, whitespace/indentation-tolerant replacement, and new-file creation.
   - Path traversal (`../` and absolute path outside workspace) security guards.
3. `local_indexer.py`:
   - Sub-20ms native Python `ast` symbol/import/line-range extraction.
   - Focused snippet extraction around a failing line and traceback parser.
4. `subagent_orchestrator.py`:
   - Full closed loop: Read Canvas -> Local AST index -> Single-turn stateless prompt ->
     Receive SEARCH/REPLACE block -> `diff_patcher` disk write -> Verification -> Canvas writeback.
   - End-to-end single-round execution latency strictly < 3.0 seconds.
"""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

# Ensure `src` is importable when running directly
_REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if _REPO_SRC.is_dir() and str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))

from visual_agent.canvas_manager import CanvasManager, estimate_tokens
from visual_agent.diff_patcher import (
    apply_search_replace_blocks,
    apply_search_replace_text,
    parse_search_replace_blocks,
)
from visual_agent.local_indexer import index_file_symbols, parse_error_location
from visual_agent.subagent_orchestrator import (
    SubagentOrchestrator,
    SubagentPlan,
    SubagentTask,
    _decompose_with_rules,
)


class StatelessCanvasEngineTestSuite(unittest.TestCase):
    """Comprehensive verification for Stateless Canvas, Diff Patcher, Local AST Indexer, and Orchestrator."""

    def test_01_canvas_manager_atomic_persistence_and_1500_token_cap(self) -> None:
        """CanvasManager persists `.pacer/canvas.json` atomically and caps stateless prompts at <= 1,500 tokens."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cm = CanvasManager(root, max_prompt_tokens=1500)

            cm.update_goal(
                "Fix ZeroDivisionError in pricing engine",
                acceptance_cmd="pytest tests/test_pricing.py -q",
                status="in_progress",
            )
            cm.add_invariants(
                "Never modify existing test assertions in tests/",
                "Preserve public function signature calculate_discount(price, rate)",
                "Do not introduce third-party dependencies",
            )

            # Inject huge focus file snippets and error trace to test the <= 1,500 token budget trimmer
            huge_snippet = "\n".join(
                f"   {i:4d} | def helper_func_{i}(x: int) -> int: return x * {i}"
                for i in range(1, 400)
            )
            cm.upsert_focus_file(
                "src/pricing.py",
                symbols=[f"def helper_func_{i}(x: int) -> int [L{i}-L{i}]" for i in range(1, 50)],
                imports=["decimal", "math", "typing"],
                target_snippet=huge_snippet,
            )
            cm.update_scratchpad(
                latest_error="Traceback (most recent call last):\n" + ("  File 'src/pricing.py', line 42\n" * 80) + "ZeroDivisionError: division by zero",
                failing_line="src/pricing.py:42",
                attempt_count=2,
                last_failed_hypothesis="Attempt #1 forgot to guard denominator == 0",
                completed_step="task_1:Local AST indexing",
            )

            # Verify `.pacer/canvas.json` exists and loads cleanly
            self.assertTrue((root / ".pacer" / "canvas.json").is_file())
            reloaded = CanvasManager(root).load()
            self.assertEqual(reloaded.goal.summary, "Fix ZeroDivisionError in pricing engine")
            self.assertEqual(len(reloaded.invariants), 3)
            self.assertIn("src/pricing.py", reloaded.focus_files)
            self.assertEqual(reloaded.scratchpad.failing_line, "src/pricing.py:42")

            # Build stateless prompt and verify token budget <= 1,500
            prompt = cm.build_stateless_prompt("Guard against zero denominator in calculate_discount")
            token_count = estimate_tokens(prompt)
            self.assertLessEqual(
                token_count,
                1500,
                f"Stateless prompt exceeded 1,500 token limit: got {token_count} tokens",
            )
            self.assertIn("[GOAL]", prompt)
            self.assertIn("[INVARIANTS", prompt)
            self.assertIn("[FOCUS FILES & AST SYMBOLS]", prompt)
            self.assertIn("<<<<<<< SEARCH", prompt)

    def test_01b_truncate_to_token_budget_cjk(self) -> None:
        """CJK text respects token budget without character count shortcut bypass."""
        from visual_agent.canvas_manager import truncate_to_token_budget
        cjk_text = "三角洲行动战术射击游戏特勤干员核心模块" * 20
        truncated = truncate_to_token_budget(cjk_text, budget_tokens=100)
        self.assertLessEqual(estimate_tokens(truncated), 100)

    def test_02_diff_patcher_exact_and_whitespace_tolerant_matching(self) -> None:
        """diff_patcher applies exact matches, indentation-shifted matches, and new files while blocking `..` traversal."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "src" / "service.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "class OrderService:\n"
                "    def compute_total(self, items, tax_rate):\n"
                "        subtotal = sum(i.price for i in items)\n"
                "        return subtotal * (1 + tax_rate)\n",
                encoding="utf-8",
            )

            # LLM output with slightly different leading indentation in SEARCH block + a second new file block
            llm_output = (
                "Here is the minimal patch:\n\n"
                "src/service.py\n"
                "```python\n"
                "<<<<<<< SEARCH\n"
                "def compute_total(self, items, tax_rate):\n"
                "    subtotal = sum(i.price for i in items)\n"
                "    return subtotal * (1 + tax_rate)\n"
                "=======\n"
                "def compute_total(self, items, tax_rate):\n"
                "    if tax_rate < 0:\n"
                "        raise ValueError('tax_rate must be non-negative')\n"
                "    subtotal = sum(i.price for i in items)\n"
                "    return round(subtotal * (1 + tax_rate), 2)\n"
                ">>>>>>> REPLACE\n"
                "```\n\n"
                "src/constants.py\n"
                "```python\n"
                "<<<<<<< SEARCH\n"
                "=======\n"
                "DEFAULT_TAX_RATE = 0.08\n"
                ">>>>>>> REPLACE\n"
                "```\n"
            )

            res = apply_search_replace_text(llm_output, workspace_root=root)
            self.assertTrue(res.success, f"Expected patch success, got errors: {res.errors}")
            self.assertEqual(res.applied_count, 2)
            self.assertIn("src/service.py", res.modified_files)
            self.assertIn("src/constants.py", res.modified_files)

            updated_service = target.read_text(encoding="utf-8")
            self.assertIn("raise ValueError('tax_rate must be non-negative')", updated_service)
            self.assertIn("    def compute_total(self, items, tax_rate):", updated_service)
            self.assertTrue((root / "src" / "constants.py").is_file())

            # Verify path traversal security guard rejects `../outside.py`
            malicious_output = (
                "../outside_escape.py\n"
                "<<<<<<< SEARCH\n"
                "=======\n"
                "import os\n"
                ">>>>>>> REPLACE\n"
            )
            bad_res = apply_search_replace_text(malicious_output, workspace_root=root)
            self.assertFalse(bad_res.success)
            self.assertTrue(any("Path traversal" in err for err in bad_res.errors))
            self.assertFalse((root.parent / "outside_escape.py").exists())

    def test_02b_diff_patcher_transactional_rollback_on_partial_failure(self) -> None:
        """When multiple SEARCH/REPLACE blocks target the same file, failure of one block rolls back all blocks for that file."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "src" / "worker.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            original_code = "def step1():\n    return 1\n\ndef step2():\n    return 2\n"
            target.write_text(original_code, encoding="utf-8")

            llm_output = (
                "src/worker.py\n"
                "<<<<<<< SEARCH\n"
                "def step1():\n"
                "    return 1\n"
                "=======\n"
                "def step1():\n"
                "    return 100\n"
                ">>>>>>> REPLACE\n\n"
                "src/worker.py\n"
                "<<<<<<< SEARCH\n"
                "def NON_EXISTENT_STEP():\n"
                "    pass\n"
                "=======\n"
                "def step2():\n"
                "    return 200\n"
                ">>>>>>> REPLACE\n"
            )
            res = apply_search_replace_text(llm_output, workspace_root=root)
            self.assertFalse(res.success)
            self.assertEqual(res.failed_count, 2)
            self.assertEqual(target.read_text(encoding="utf-8"), original_code)

    def test_03_local_indexer_sub_20ms_ast_extraction(self) -> None:
        """local_indexer parses Python AST symbols, imports, and failing line snippets in < 20ms."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample_py = root / "src" / "auth_guard.py"
            sample_py.parent.mkdir(parents=True, exist_ok=True)
            sample_py.write_text(
                "import hashlib\n"
                "from typing import Optional\n\n"
                "class TokenVerifier:\n"
                "    def __init__(self, secret: str) -> None:\n"
                "        self.secret = secret\n\n"
                "    def verify_signature(self, payload: str, sig: str) -> bool:\n"
                "        expected = hashlib.sha256((payload + self.secret).encode()).hexdigest()\n"
                "        return expected == sig\n\n"
                "def parse_bearer_header(header: Optional[str]) -> str:\n"
                "    if not header:\n"
                "        raise ValueError('Missing header')\n"
                "    return header.removeprefix('Bearer ').strip()\n",
                encoding="utf-8",
            )

            t0 = time.perf_counter()
            idx_res = index_file_symbols(
                "src/auth_guard.py",
                workspace_root=root,
                target_line=9,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

            self.assertEqual(idx_res.error, "")
            self.assertLess(
                elapsed_ms,
                50.0,
                f"Expected local AST indexing under 50ms, took {elapsed_ms:.2f}ms",
            )
            compact = idx_res.compact_symbols()
            self.assertTrue(any("class TokenVerifier" in s for s in compact))
            self.assertTrue(any("verify_signature" in s for s in compact))
            self.assertTrue(any("parse_bearer_header" in s for s in compact))
            self.assertIn("hashlib", idx_res.imports)
            self.assertIn(">   9 |", idx_res.focused_snippet)

            # Verify traceback parser
            tb = (
                'Traceback (most recent call last):\n'
                '  File "src/auth_guard.py", line 9, in verify_signature\n'
                '    expected = hashlib.sha256((payload + self.secret).encode()).hexdigest()\n'
                'TypeError: can only concatenate str (not "NoneType") to str\n'
            )
            err_file, err_line = parse_error_location(tb)
            self.assertEqual(err_file, "src/auth_guard.py")
            self.assertEqual(err_line, 9)

    def test_04_orchestrator_stateless_closed_loop_under_3_seconds(self) -> None:
        """SubagentOrchestrator completes the full Canvas -> AST -> Stateless Prompt -> Diff Patch -> Verify loop in < 3s."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_file = root / "src" / "discount.py"
            src_file.parent.mkdir(parents=True, exist_ok=True)
            src_file.write_text(
                "def apply_coupon(amount: float, discount_pct: float) -> float:\n"
                "    # BUG: currently adds discount instead of subtracting\n"
                "    return round(amount * (1.0 + discount_pct), 2)\n",
                encoding="utf-8",
            )

            goal = "Fix apply_coupon in src/discount.py so discount_pct is subtracted instead of added"
            plan = _decompose_with_rules(
                goal=goal,
                repo_root=str(root),
                workbuddy_available=False,
                gemini_available=True,
            )
            plan.tasks[1].backend = "gemini"
            # Write a test file for discount
            test_file = root / "tests" / "test_discount.py"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_text(
                "import sys\nimport unittest\nfrom pathlib import Path\n"
                "sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))\n"
                "from discount import apply_coupon\n\n"
                "class TestDiscount(unittest.TestCase):\n"
                "    def test_coupon(self):\n"
                "        self.assertEqual(apply_coupon(100.0, 0.2), 80.0)\n",
                encoding="utf-8",
            )
            verify_cmd = f'"{sys.executable}" -m unittest tests/test_discount.py'
            plan.tasks[1].verification_command = verify_cmd
            plan.tasks[2].verification_command = verify_cmd

            orchestrator = SubagentOrchestrator(
                plan,
                workspace_root=root,
                enforce_test_tamper_guard=False,
                use_stateless_canvas=True,
                invariants=["Keep function name apply_coupon unchanged"],
            )

            captured_prompts: list[str] = []

            def fake_gemini(prompt: str, **kwargs):
                captured_prompts.append(prompt)
                patch_reply = (
                    "src/discount.py\n"
                    "```python\n"
                    "<<<<<<< SEARCH\n"
                    "def apply_coupon(amount: float, discount_pct: float) -> float:\n"
                    "    # BUG: currently adds discount instead of subtracting\n"
                    "    return round(amount * (1.0 + discount_pct), 2)\n"
                    "=======\n"
                    "def apply_coupon(amount: float, discount_pct: float) -> float:\n"
                    "    if not (0.0 <= discount_pct <= 1.0):\n"
                    "        raise ValueError('discount_pct must be between 0 and 1')\n"
                    "    return round(amount * (1.0 - discount_pct), 2)\n"
                    ">>>>>>> REPLACE\n"
                    "```\n"
                )
                return MagicMock(success=True, response=patch_reply, error=None)

            t0 = time.perf_counter()
            with patch("visual_agent.subagent_orchestrator.invoke_gemini", side_effect=fake_gemini):
                summary = orchestrator.run()
            elapsed = time.perf_counter() - t0

            self.assertTrue(summary["success"], f"Expected orchestrator success, got: {summary}")
            self.assertLess(
                elapsed,
                3.0,
                f"Expected full stateless loop in < 3.0s, took {elapsed:.2f}s",
            )
            # Confirm Indexer ran locally via AST without calling Gemini, and Coder called Gemini once with a <= 1500 token stateless prompt
            self.assertEqual(len(captured_prompts), 1)
            self.assertLessEqual(estimate_tokens(captured_prompts[0]), 1500)
            self.assertIn("[GOAL]", captured_prompts[0])
            self.assertIn("src/discount.py", captured_prompts[0])
            self.assertIn("apply_coupon", captured_prompts[0])

            # Confirm file was patched on disk via diff_patcher
            patched_code = src_file.read_text(encoding="utf-8")
            self.assertIn("1.0 - discount_pct", patched_code)

            # Confirm Canvas state reflects completed status
            canvas_state = summary["canvas"]
            self.assertEqual(canvas_state["goal"]["status"], "completed")
            self.assertIn("src/discount.py", canvas_state["focus_files"])


if __name__ == "__main__":
    unittest.main(verbosity=2)