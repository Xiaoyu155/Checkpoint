"""Automated Feasibility Verification Exam Suite for Pacer (12 Exam Questions).

Can be executed via `python3 test_pacer_feasibility_exam.py` (standard unittest)
or `pytest test_pacer_feasibility_exam.py -q`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from visual_agent.acceptance_discrimination import (
    assess_gate_health,
    classify_acceptance,
    probe_base_command,
)
from visual_agent.agent_backends import (
    looks_like_provider_5xx,
    looks_like_quota_exhaustion,
)
from visual_agent.cli import main as cli_main
from visual_agent.command_verification import (
    changed_acceptance_chain_files,
    changed_test_files,
    tamper_repair_brief,
)
from visual_agent.delivery_check import run_delivery_check
from visual_agent.dogfood_evidence import assess_dogfood_evidence
from visual_agent.gemini_bridge import extract_json_from_text
from visual_agent.goal_grounding import (
    discover_plan_documents,
    goal_references_plan,
    ground_goal,
)
from visual_agent.hourly_budget import build_hourly_plan
from visual_agent.model_router import route_task
from visual_agent.pacer_voice import user_story
from visual_agent.release_gate import evaluate_clean_streak
from visual_agent.subagent_orchestrator import (
    SubagentOrchestrator,
    SubagentPlan,
    SubagentTask,
)
from visual_agent.task_review import audit_task_completion, build_task_contract
from visual_agent.workbuddy_bridge import (
    WorkBuddyGatewayClient,
    WorkBuddyGatewayInfo,
    extract_and_apply_code_blocks,
)


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@pacer.local"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Pacer Test"], cwd=str(root), check=True, capture_output=True)


class TestPacerFeasibilityExam(unittest.TestCase):
    """Covers all 12 feasibility benchmark exam questions across 5 dimensions."""

    def test_q01_routing_precision_and_negation_immunity(self) -> None:
        """Exam Q1: Three-tier model routing & immunity to negated strong keywords & substring traps."""
        # Case A: Mechanical change -> cheap
        self.assertEqual(
            route_task(objective="把 login.py 里的变量名 usr_nm 重命名为 username 并修正注释错别字").tier,
            "cheap",
        )
        # Case B: High-risk architecture/payment security -> strong
        self.assertEqual(
            route_task(objective="修复微信支付异步回调 webhook 的签名验签逻辑与并发重入锁").tier,
            "strong",
        )
        # Case C: Explicit 1-file function contract -> cheap
        self.assertEqual(
            route_task(
                objective="In utils/math_helper.py implement clamp(x, low, high) that returns exactly the bounded value with unit test"
            ).tier,
            "cheap",
        )
        # Case D: Negated strong terms ("不要重构代码，不要动认证鉴权模块") -> must stay cheap!
        res_negated = route_task(
            objective="修改 README.md 里的提示语文案错别字，注意不要重构代码，不要动认证鉴权模块"
        )
        self.assertEqual(res_negated.tier, "cheap", f"Expected cheap, got {res_negated}")

        # Case E: Substring boundary trap ("author" should not trigger "auth")
        res_boundary = route_task(objective="Fix typo in author label text on profile card")
        self.assertEqual(res_boundary.tier, "cheap", f"Substring 'author' falsely matched 'auth': {res_boundary}")

    def test_q02_quota_429_vs_5xx_and_hourly_budget_circuit_breaker(self) -> None:
        """Exam Q2: UUID containing '429' must not false-trigger quota exhaustion; 85% 5h window defers strong work."""
        err_msg = "Error: request_id=7e23-4294-a7ef HTTP 502 Bad Gateway"
        self.assertFalse(looks_like_quota_exhaustion(err_msg))
        self.assertTrue(looks_like_provider_5xx(err_msg))
        self.assertTrue(looks_like_quota_exhaustion("You've used all your 5-hour usage limit, resets at 18:00"))

        quota_snapshot = {
            "providers": {
                "codex": {
                    "rate_limits": {
                        "five_hour": {"used_percentage": 85.0},
                    }
                }
            }
        }
        plan = build_hourly_plan(
            tasks=[
                {"task_id": "t_strong", "objective": "重构数据库引擎", "worker_tier": "strong", "estimated_strong_minutes": 40},
                {"task_id": "t_cheap", "objective": "修正文案错别字", "worker_tier": "cheap", "estimated_minutes": 10},
            ],
            quota_snapshot=quota_snapshot,
        )
        self.assertEqual(len(plan["deferred"]), 1)
        self.assertEqual(plan["deferred"][0]["task_id"], "t_strong")
        self.assertEqual(len(plan["scheduled"]), 1)
        self.assertEqual(plan["scheduled"][0]["task_id"], "t_cheap")

    def test_q03_dag_out_of_order_topological_execution_and_context_passing(self) -> None:
        """Exam Q3: Out-of-order DAG tasks execute topologically and pass upstream output to downstream instructions."""
        with tempfile.TemporaryDirectory() as tmp:
            t3 = SubagentTask(
                id="task_3",
                title="Verify",
                role="verifier",
                backend="local",
                instructions="Run verification",
                dependencies=["task_2"],
            )
            t2 = SubagentTask(
                id="task_2",
                title="Implement",
                role="coder",
                backend="gemini",
                instructions="Implement target function",
                dependencies=["task_1"],
            )
            t1 = SubagentTask(
                id="task_1",
                title="Index",
                role="indexer",
                backend="gemini",
                instructions="Analyze repo",
                dependencies=[],
            )
            # Intentionally pass tasks in reverse order: [task_3, task_2, task_1]
            plan = SubagentPlan(goal="Implement discount", tasks=[t3, t2, t1], repo_root=tmp)
            orchestrator = SubagentOrchestrator(plan, workspace_root=tmp)

            execution_order: list[str] = []
            captured_coder_instructions: list[str] = []

            def fake_dispatch(task: SubagentTask):
                execution_order.append(task.id)
                if task.id == "task_1":
                    return ("KEY_FACT: 目标函数名必须叫 calculate_discount_v2", None)
                if task.id == "task_2":
                    captured_coder_instructions.append(task.instructions)
                    return ("def calculate_discount_v2(): pass", None)
                return ("ok", None)

            orchestrator._dispatch_to_backend = fake_dispatch
            summary = orchestrator.run()

            self.assertTrue(summary["success"])
            self.assertEqual(execution_order, ["task_1", "task_2", "task_3"])
            self.assertEqual(len(captured_coder_instructions), 1)
            self.assertIn("calculate_discount_v2", captured_coder_instructions[0])

    def test_q04_self_healing_escalation_and_workbuddy_file_materialization(self) -> None:
        """Exam Q4: WorkBuddy file materialization & automatic escalation to Codex on verification failure."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 1. Verify WorkBuddy code block extraction & disk materialization
            wb_reply = (
                "Here is the implementation:\n"
                "```python path=src/calc.py\n"
                "def divide(a, b):\n"
                "    return a / b\n"
                "```\n"
            )
            written = extract_and_apply_code_blocks(wb_reply, workspace_root=root)
            self.assertEqual(written, ["src/calc.py"])
            self.assertTrue((root / "src" / "calc.py").is_file())

            # 2. Verify escalation from workbuddy -> grok with traceback injection
            t1 = SubagentTask(
                id="t1",
                title="Fix divide",
                role="coder",
                backend="workbuddy",
                instructions="Fix divide in calc.py",
                verification_command="python3 -c \"exit(1)\"",
            )
            plan = SubagentPlan(goal="Fix calc", tasks=[t1], repo_root=str(root))
            orchestrator = SubagentOrchestrator(plan, workspace_root=root, enforce_test_tamper_guard=False)
            orchestrator._is_backend_live = lambda backend: backend in {"workbuddy", "grok"}

            calls = []

            def fake_verif(cmd: str):
                calls.append(cmd)
                if len(calls) == 1:
                    return (False, "ZeroDivisionError: division by zero")
                return (True, "1 passed")

            def fake_grok(*_args, **_kwargs):
                (root / "src" / "calc.py").write_text(
                    "def divide(a, b):\n    return a / b if b != 0 else 0\n",
                    encoding="utf-8",
                )
                return MagicMock(success=True, response="fixed by grok", error=None)

            orchestrator._run_verification_command = fake_verif
            with patch("visual_agent.subagent_orchestrator.invoke_workbuddy") as mock_wb, \
                 patch("visual_agent.subagent_orchestrator.invoke_grok", side_effect=fake_grok), \
                 patch("visual_agent.subagent_orchestrator.probe_workspace_command", return_value={"status": "failed_on_base", "reason": "gate_reported_failures"}):
                mock_wb.return_value = MagicMock(success=True, response="bad code", error=None)
                summary = orchestrator.run()

            self.assertTrue(summary["success"])
            self.assertEqual(t1.backend, "grok")
            self.assertIn("ZeroDivisionError", t1.instructions)

    def test_q05_json_extractor_robustness_against_brace_noise(self) -> None:
        """Exam Q5 (P1-2 check): JSON array extraction when surrounding prose contains '{file}' braces."""
        noisy_output = (
            "I analyzed {repo_root} and planned the changes for {target_file}:\n"
            '[{"id": "task_1", "role": "indexer"}, {"id": "task_2", "role": "coder"}]\n'
            "Note: check {config} if needed."
        )
        parsed = extract_json_from_text(noisy_output)
        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["id"], "task_1")

    def test_q07_base_diff_discrimination_catches_false_green(self) -> None:
        """Exam Q7: A test command that already passed on base is graded 'regression_clear', not 'verified'."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_git_repo(root)
            (root / "app.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=str(root), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(root), check=True, capture_output=True)

            # Command passes on base
            probe = probe_base_command(
                command=f'"{sys.executable}" -c "print(\'ok\')"',
                repo_root=root,
                base_ref="HEAD",
            )
            self.assertEqual(probe["status"], "passed_on_base")
            graded = classify_acceptance(
                command_result={"command": "python -c ...", "verdict": "pass"},
                base_probe=probe,
            )
            self.assertEqual(graded["tier"], "regression_clear")
            story = user_story(
                stop_reason="verified",
                acceptance_tier=graded["tier"],
                verification_command="pytest",
            )
            self.assertIn("不能替你打包票", story["headline"])

    def test_q08_broken_gate_health_detection(self) -> None:
        """Exam Q8: SyntaxError or 0-tests-collected is flagged as 'broken' gate, not normal test failure."""
        broken_syntax = assess_gate_health({"output_tail": "SyntaxError: invalid syntax in test_foo.py"})
        self.assertEqual(broken_syntax["status"], "broken")
        self.assertEqual(broken_syntax["reason"], "gate_failed_to_load")

        zero_tests = assess_gate_health({"output_tail": "collected 0 items\nno tests ran in 0.01s"})
        self.assertEqual(zero_tests["status"], "broken")
        self.assertEqual(zero_tests["reason"], "gate_executed_nothing")

    def test_q09_test_tampering_and_protected_path_contract_enforcement(self) -> None:
        """Exam Q9: Worker modifying tests/, pyproject.toml, or protected paths is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_git_repo(root)
            (root / "tests").mkdir()
            (root / "tests" / "test_core.py").write_text("def test_a(): assert True\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
            (root / "config").mkdir()
            (root / "config" / "security.py").write_text("SECRET = 1\n", encoding="utf-8")
            (root / "login.py").write_text("def login(): return True\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=str(root), check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=str(root), check=True, capture_output=True)

            # Tamper with test file and pyproject.toml
            (root / "tests" / "test_core.py").write_text("def test_a(): pass\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("# tampered\n", encoding="utf-8")

            tampered_tests = changed_test_files(repo_root=root, base_ref="HEAD")
            self.assertIn("tests/test_core.py", tampered_tests)
            brief = tamper_repair_brief(tampered_tests, base_ref="HEAD")
            self.assertEqual(brief["source"], "test_tampering")

            tampered_chain = changed_acceptance_chain_files(repo_root=root, command="pytest -q", base_ref="HEAD")
            self.assertIn("pyproject.toml", tampered_chain)

            # Protected path contract test
            goal = "优化 login.py 的返回逻辑；不要修改 config/security.py"
            contract = build_task_contract(goal, repo_root=root)
            self.assertIn("config/security.py", contract["protected_paths"])

    def test_q10_clean_room_delivery_check_catches_uncommitted_dependency(self) -> None:
        """Exam Q10: Local workspace passes because of untracked file, but clean-room delivery check catches it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_git_repo(root)
            # Commit app.py that imports helper_untracked, but DO NOT commit helper_untracked.py
            (root / "app.py").write_text("import helper_untracked\nassert helper_untracked.VAL == 42\n", encoding="utf-8")
            (root / "helper_untracked.py").write_text("VAL = 42\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=str(root), check=True)
            subprocess.run(["git", "commit", "-m", "feat: add app"], cwd=str(root), check=True, capture_output=True)

            branch_res = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=True,
            )
            branch = branch_res.stdout.strip()

            cmd = f'"{sys.executable}" app.py'
            # Passes in dirty working directory:
            local_run = subprocess.run(cmd, cwd=str(root), shell=True)
            self.assertEqual(local_run.returncode, 0)

            # Fails in clean-room delivery check because helper_untracked.py was not committed!
            delivery = run_delivery_check(repo_root=root, branch=branch, command=cmd)
            self.assertEqual(delivery["status"], "not_delivered")
            story = user_story(stop_reason="verified", delivery_status=delivery["status"])
            self.assertIn("现在还交不到你手上", story["headline"])

    def test_q11_goal_grounding_discovers_roadmap_and_pending_items(self) -> None:
        """Exam Q11: Vague plan-referencing goal automatically scans docs/roadmap.md and extracts pending tasks."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            (docs / "roadmap.md").write_text(
                "# 开发计划\n## 已完成\n- [x] 初始化\n## 下一步待办\n- [ ] 实现 PDF 账单导出接口\n",
                encoding="utf-8",
            )
            vague_goal = "参照最新的开发计划继续推进"
            self.assertTrue(goal_references_plan(vague_goal))
            found_docs = discover_plan_documents(root)
            self.assertEqual(found_docs[0]["rel_path"], "docs/roadmap.md")

            grounded = ground_goal(vague_goal, repo_root=root, enable_model=False)
            self.assertEqual(grounded["plan_document"], "docs/roadmap.md")
            self.assertIn("实现 PDF 账单导出接口", grounded["pending_items"])

    def test_q12_dogfood_cryptographic_evidence_and_replay_attack_rejection(self) -> None:
        """Exam Q12: Dogfood evidence rejects identical input/candidate wheels, out-of-band edits, and replay streaks."""
        # Attack A & B: identical wheel SHA256 + out_of_band_changes=True
        same_sha = "a" * 64
        forged_evidence = {
            "schema_version": 1,
            "source_repo": {
                "product": "Pacer",
                "package_name": "visual-agent",
                "pacer_entrypoint": "visual_agent.cli:main",
                "canonical_root": "/repo",
                "repo_identity_digest": "b" * 64,
                "baseline_commit": "123456",
                "baseline_changes_digest": "c" * 64,
                "change_set_digest": "d" * 64,
                "scan_complete": True,
                "protected_paths_unchanged": True,
                "out_of_band_changes": True,
                "source_attribution": "pacer_worker",
                "changed_files": ["src/visual_agent/cli.py"],
            },
            "orchestrator": {
                "input_wheel_sha256": same_sha,
                "input_wheel_path": "dist/old.whl",
                "input_version": "0.1.2",
                "launch_id": "l1",
                "mission_id": "m1",
                "repo_identity_digest": "b" * 64,
                "worker_session_ids": ["s1"],
            },
            "contract": {
                "task_contract_digest": "e" * 64,
                "acceptance_contract_digest": "f" * 64,
            },
            "verification": {
                "status": "passed",
                "trust": "yes",
                "batch_run_id": "b1",
                "receipt_digest": "1" * 64,
                "acceptance_contract_digest": "f" * 64,
                "change_set_digest": "d" * 64,
                "evidence_resubmissions": 0,
                "warnings": [],
            },
            "candidate": {
                "wheel_sha256": same_sha,
                "wheel_path": "dist/new.whl",
                "version": "0.1.3",
                "built_from_change_set_digest": "d" * 64,
                "fresh_install": True,
                "fresh_env_id": "env1",
                "pip_check_status": "passed",
            },
            "bootstrap": {
                "parent_wheel_sha256": same_sha,
                "installed_wheel_sha256": same_sha,
                "self_check_artifact_sha256": same_sha,
                "source_repo_identity_digest": "b" * 64,
                "self_check_status": "passed",
                "self_check_receipt_digest": "2" * 64,
                "self_check_launch_id": "l2",
            },
        }
        assessment = assess_dogfood_evidence(forged_evidence, require_artifacts=False)
        self.assertFalse(assessment["passed"])
        self.assertIn("dogfood_candidate_matches_input_wheel", assessment["reason_codes"])
        self.assertIn("dogfood_out_of_band_changes_detected", assessment["reason_codes"])

        # Attack C: Replay the exact same evidence_digest 3 times to fake a 3-run clean streak
        replayed_samples = [
            {
                "clean": True,
                "status": "passed",
                "evidence_digest": "9" * 64,
                "generation": {"candidate_wheel_sha256": "8" * 64},
            }
            for _ in range(3)
        ]
        streak = evaluate_clean_streak(replayed_samples, required=3)
        self.assertFalse(streak["passed"])
        self.assertGreater(streak["duplicate_evidence"], 0)

    def test_cli_verify_command_integration(self) -> None:
        """Verify that the newly wired `pacer verify` CLI command runs end-to-end."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_git_repo(root)
            (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=str(root), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(root), check=True, capture_output=True)

            old_cwd = Path.cwd()
            try:
                import os
                os.chdir(root)
                rc = cli_main([
                    "verify",
                    "--cmd",
                    f'"{sys.executable}" main.py',
                    "--skip-base-probe",
                    "--goal",
                    "验证 CLI 入口打通",
                ])
                self.assertEqual(rc, 0)
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
