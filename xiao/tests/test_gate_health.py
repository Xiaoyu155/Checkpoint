from __future__ import annotations

from visual_agent.acceptance_discrimination import (
    assess_gate_health,
    classify_acceptance,
    probe_base_command,
)

# 2026-08-05 实测里那道坏门的真实输出：报错信息里写了真实换行，
# 塞在 JS 单引号字符串里 = SyntaxError，于是文件根本没被加载。
REAL_BROKEN_GATE = {
    "verdict": "fail",
    "exit_code": 1,
    "output_tail": (
        "# ${offenders.join('\n"
        "#                  ^\n"
        "# SyntaxError: Invalid or unexpected token\n"
        "#     at compileSourceTextModule (node:internal/modules/esm/utils:344:16)\n"
        "not ok 1 - test\\designSystem.test.mjs\n"
        "# tests 1\n# pass 0\n# fail 1\n"
    ),
}

REAL_HONEST_RED = {
    "verdict": "fail",
    "exit_code": 1,
    "output_tail": (
        "not ok 4 - no page or component hardcodes a retired colour\n"
        "  ../src/components/EmptyState.vue 有 2 处 #1A6BFF（应改用 var(--brand-500)）\n"
        "# tests 96\n# pass 95\n# fail 1\n"
    ),
}


def test_a_gate_that_never_loaded_is_called_broken() -> None:
    health = assess_gate_health(REAL_BROKEN_GATE)

    assert health["status"] == "broken"
    assert health["reason"] == "gate_failed_to_load"


def test_a_gate_that_ran_and_found_violations_is_healthy() -> None:
    # 这条红是有意义的红：它执行了，指名了文件和处数。
    assert assess_gate_health(REAL_HONEST_RED)["status"] == "runnable"


def test_a_gate_that_executed_nothing_is_broken() -> None:
    for output in ("# tests 0\n# pass 0\n", "collected 0 items\n", "No tests found\n"):
        assert assess_gate_health({"verdict": "fail", "output_tail": output})["status"] == "broken"


def test_common_load_failures_across_ecosystems_are_recognised() -> None:
    cases = [
        "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'openai'",
        "ModuleNotFoundError: No module named 'app'",
        "ERROR collecting tests/test_x.py",
        "Cannot find module './missing'",
    ]
    for output in cases:
        assert assess_gate_health({"verdict": "fail", "output_tail": output})["status"] == "broken", output


def test_no_output_is_unknown_rather_than_guessed() -> None:
    # 没有输出就没有判断依据；猜"坏了"会挡住正常的契约先行流程。
    assert assess_gate_health({"verdict": "fail"})["status"] == "unknown"
    assert assess_gate_health(None)["status"] == "unknown"


def test_a_broken_gate_is_not_mistaken_for_a_contract_waiting_to_be_met(tmp_path) -> None:
    graded = classify_acceptance(
        command_result={"command": "npm test", "verdict": "fail"},
        base_probe={"status": "gate_broken", "reason": "gate_failed_to_load"},
    )

    # 关键区别：把坏门当成 failed_on_base，会让人以为"契约成立、只是还没做完"。
    assert graded["tier"] == "unverified"
    assert graded["reason_code"] == "acceptance_gate_broken"
    assert "跑不起来" in graded["message"]


def test_an_honest_pre_existing_failure_still_reads_as_such() -> None:
    graded = classify_acceptance(
        command_result={"command": "npm test", "verdict": "fail"},
        base_probe={"status": "failed_on_base", "reason": "gate_red_before_change"},
    )

    assert graded["reason_code"] == "acceptance_pre_existing_failure"


def test_the_probe_reports_gate_broken_on_a_real_repo(tmp_path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
    (repo / "x.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=str(repo), check=True, capture_output=True)

    probe = probe_base_command(
        command="acceptance-gate",
        repo_root=repo,
        base_ref="main",
        runner=lambda **_kwargs: REAL_BROKEN_GATE,
    )

    assert probe["status"] == "gate_broken"


def test_a_probe_of_an_honest_red_gate_still_says_failed_on_base(tmp_path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
    (repo / "x.txt").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=str(repo), check=True, capture_output=True)

    probe = probe_base_command(
        command="acceptance-gate",
        repo_root=repo,
        base_ref="main",
        runner=lambda **_kwargs: REAL_HONEST_RED,
    )

    assert probe["status"] == "failed_on_base"
