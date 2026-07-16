from __future__ import annotations

import sys

from visual_agent.chief_dispatch import run_dispatch_verification
from visual_agent.codex_check import CodexCheckResult
from visual_agent.command_verification import classify_command_failure, command_repair_brief, run_command_verification
from visual_agent.workspace import init_workspace


def test_command_verification_pass(tmp_path) -> None:
    r = run_command_verification(command=f'"{sys.executable}" -c "exit(0)"', repo_root=tmp_path)
    assert r["verdict"] == "pass"
    assert r["exit_code"] == 0


def test_command_verification_uses_hidden_launch_kwargs(tmp_path, monkeypatch) -> None:
    captured = {}

    class Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr("visual_agent.command_verification.hidden_subprocess_kwargs", lambda: {"creationflags": 12345})
    monkeypatch.setattr("visual_agent.command_verification.subprocess.run", fake_run)

    r = run_command_verification(command="npm test", repo_root=tmp_path)

    assert r["verdict"] == "pass"
    assert captured["kwargs"]["creationflags"] == 12345
    assert captured["kwargs"]["shell"] is True


def test_command_verification_normalizes_windows_cmd_if_exist(tmp_path, monkeypatch) -> None:
    captured = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr("visual_agent.command_verification._is_windows", lambda: True)
    monkeypatch.setattr("visual_agent.command_verification.hidden_subprocess_kwargs", lambda: {})
    monkeypatch.setattr("visual_agent.command_verification.subprocess.run", fake_run)

    r = run_command_verification(
        command=r"cmd /c if exist reports\done.md (exit /b 0) else (exit /b 1)",
        repo_root=tmp_path,
    )

    assert r["verdict"] == "pass"
    assert captured["command"] == ["cmd", "/c", r"if exist reports\done.md (exit /b 0) else (exit /b 1)"]
    assert captured["kwargs"]["shell"] is False


def test_command_verification_fail_keeps_output(tmp_path) -> None:
    r = run_command_verification(command=f'"{sys.executable}" -c "print(chr(66)+chr(65)+chr(68)); exit(3)"', repo_root=tmp_path)
    assert r["verdict"] == "fail"
    assert r["exit_code"] == 3
    assert "BAD" in r["output_tail"]
    brief = command_repair_brief(r)
    assert brief["source"] == "test_command"
    assert "failed" in brief["message"].lower()


def test_enoent_in_test_output_is_repairable_code_failure() -> None:
    kind = classify_command_failure(
        exit_code=1,
        output="Error: ENOENT: no such file or directory, open 'fixtures/a.json'",
    )

    assert kind == "command_failed"


def test_powershell_the_term_in_assertion_text_not_invalid() -> None:
    kind = classify_command_failure(
        exit_code=1,
        output="AssertionError: the term used in the generated explanation is wrong",
    )

    assert kind == "command_failed"


def test_exit_9009_is_invalid() -> None:
    kind = classify_command_failure(exit_code=9009, output="anything")

    assert kind == "test_command_invalid"


def test_shell_error_in_head_is_invalid() -> None:
    kind = classify_command_failure(
        exit_code=1,
        output="\n".join(
            [
                "npm ERR! Missing script: \"acceptance\"",
                "npm ERR!",
                "npm ERR! To see a list of scripts, run:",
            ]
        ),
    )

    assert kind == "test_command_invalid"


def test_command_verification_classifies_invalid_command(tmp_path) -> None:
    r = run_command_verification(command="definitely-not-a-real-devpacer-command-xyz", repo_root=tmp_path)
    assert r["verdict"] == "fail"
    assert r["failure_kind"] == "test_command_invalid"


def test_declared_env_var_missing_blocks_before_run(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("QWEN_API_KEY", raising=False)

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("declared env var check should block before subprocess.run")

    monkeypatch.setattr("visual_agent.command_verification.subprocess.run", should_not_run)

    r = run_command_verification(
        command="npm run eval:acceptance",
        repo_root=tmp_path,
        verification_env=[{"kind": "env_var", "name": "QWEN_API_KEY"}],
    )

    assert r["verdict"] == "fail"
    assert r["failure_kind"] == "verification_environment_missing"
    assert r["classification_confidence"] == "definitive"
    assert r["exit_code"] is None
    assert r["missing_env_vars"] == ["QWEN_API_KEY"]


def test_conditional_npm_ci_short_circuit_blocks_before_run(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "node_modules" / "express" / "package.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"name":"express"}\n', encoding="utf-8")

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("short-circuiting command should block before subprocess.run")

    monkeypatch.setattr("visual_agent.command_verification.subprocess.run", should_not_run)

    r = run_command_verification(
        command=(
            "cmd /d /s /c if not exist node_modules\\express\\package.json "
            "npm ci --cache .npm-cache --prefer-offline ^&^& node --test"
        ),
        repo_root=tmp_path,
    )

    assert r["verdict"] == "fail"
    assert r["failure_kind"] == "conditional_test_command_short_circuit"
    assert r["classification_confidence"] == "definitive"
    assert r["exit_code"] is None
    assert r["short_circuit_marker"] == "node_modules/express/package.json"


def test_declared_marker_match(tmp_path, monkeypatch) -> None:
    class Completed:
        returncode = 1
        stdout = "external ai judge missing"
        stderr = ""

    monkeypatch.setattr("visual_agent.command_verification.hidden_subprocess_kwargs", lambda: {})
    monkeypatch.setattr("visual_agent.command_verification.subprocess.run", lambda *_args, **_kwargs: Completed())

    r = run_command_verification(
        command="npm run eval:acceptance",
        repo_root=tmp_path,
        verification_env=[{"kind": "marker", "pattern": "external ai judge missing"}],
    )

    assert r["failure_kind"] == "verification_environment_missing"
    assert r["classification_confidence"] == "definitive"
    assert r["matched_marker"] == "external ai judge missing"


def test_legacy_marker_is_heuristic_confidence(tmp_path, monkeypatch) -> None:
    class Completed:
        returncode = 1
        stdout = "QWEN_API_KEY missing"
        stderr = ""

    monkeypatch.setattr("visual_agent.command_verification.hidden_subprocess_kwargs", lambda: {})
    monkeypatch.setattr("visual_agent.command_verification.subprocess.run", lambda *_args, **_kwargs: Completed())

    r = run_command_verification(command="npm run eval:acceptance", repo_root=tmp_path)

    assert r["failure_kind"] == "verification_environment_missing"
    assert r["classification_confidence"] == "heuristic"


def test_command_verification_classifies_missing_external_ai_environment() -> None:
    kind = classify_command_failure(
        exit_code=1,
        output="AI知识验收与防作弊校准 FAIL ai_knowledge_acceptance\nQWEN_API_KEY missing",
    )
    assert kind == "verification_environment_missing"

    brief = command_repair_brief(
        {
            "command": "npm run eval:acceptance",
            "exit_code": 1,
            "failure_kind": kind,
            "output_tail": "QWEN_API_KEY missing",
        }
    )

    assert brief["failure_kind"] == "verification_environment_missing"
    assert "Do not change product code" in brief["repair_prompt"]
    assert "verification environment" in brief["repair_prompt"]


def test_command_verification_reads_referenced_log_for_external_ai_failure(tmp_path) -> None:
    log = tmp_path / "reports" / "continuous-acceptance" / "0001" / "ai_knowledge_acceptance.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "AI知识审查(qwen-plus): 6/6 passed\n"
        "AI知识审查: skipped (Expected ',' or ']' after array element in JSON at position 601)\n",
        encoding="utf-8",
    )
    script = tmp_path / "fail_command.py"
    script.write_text(
        "print('FAIL ai_knowledge_acceptance (reports/continuous-acceptance/0001/ai_knowledge_acceptance.log)')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )

    r = run_command_verification(command=f'"{sys.executable}" "{script}"', repo_root=tmp_path)

    assert r["verdict"] == "fail"
    assert r["failure_kind"] == "verification_environment_missing"
    assert "referenced log" in r["output_tail"]
    assert "AI知识审查: skipped" in r["output_tail"]


def _no_workflow_runner(*_args, **_kwargs):
    return CodexCheckResult(changed_files=[], selected_workflows=[], skipped_slow_workflows=[], coverage={"status": "no_changed_files"}, results=[])


def test_dispatch_verification_command_pass_is_acceptance_without_workflows(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    payload = run_dispatch_verification(
        workspace_root=workspace.root,
        plan_id="p1",
        repo_root=tmp_path,
        run_profile="dry-run",
        include_slow=False,
        max_workflows=5,
        codex_runner=_no_workflow_runner,
        test_command=f'"{sys.executable}" -c "exit(0)"',
    )
    # Command passed and there are no product workflows -> the command is the gate.
    assert payload["verdict"] == "pass"
    assert payload["command_verification"]["verdict"] == "pass"


def test_dispatch_verification_command_fail_short_circuits(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)

    def _should_not_run(*_a, **_k):
        raise AssertionError("workflow verification should be skipped when the test command fails")

    payload = run_dispatch_verification(
        workspace_root=workspace.root,
        plan_id="p1",
        repo_root=tmp_path,
        run_profile="dry-run",
        include_slow=False,
        max_workflows=5,
        codex_runner=_should_not_run,
        test_command=f'"{sys.executable}" -c "exit(1)"',
    )
    assert payload["verdict"] == "fail"
    assert payload["repair_brief"]["source"] == "test_command"
