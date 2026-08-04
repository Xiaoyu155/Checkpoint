from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from time import monotonic

import pytest

from visual_agent.chief_dispatch import (
    _dirty_context_prompt,
    _dirty_context_summary,
    _dirty_path_ignored,
    _dispatch_budget_assessment,
    _managed_budget_policy,
    _managed_retry_decision,
    _run_worker_attempt,
    _refresh_resume_project_memory,
    _verification_is_repairable,
    _write_worktree_gitignore,
    build_verification_command,
    build_worker_command,
    chief_dispatch_to_markdown,
    dispatch_chief_plan,
    git_dirty_files,
    run_dispatch_verification,
    run_process_capture,
    workspace_record_dirty_prefixes,
)
from visual_agent.chief_engineer import build_chief_plan, chief_plan_to_dict
from visual_agent.chief_plans_store import load_plan, load_verification, load_worker_records, save_plan
from visual_agent.codex_check import CodexCheckResult, CodexWorkflowCheck
from visual_agent.managed_state import ManagedBudgetPolicy
from visual_agent.missions import append_round, create_mission, default_budget_policy, save_mission
from visual_agent.workspace import init_workspace


def write_verification_workflow(workspace, name: str, *, affects: str = "src/payment/") -> None:
    workspace.workflows_dir.joinpath(f"{name}.yaml").write_text(
        "schema_version: 1\n"
        f"name: {name}\n"
        "version: 1\n"
        "affects:\n"
        f"  - {affects}\n"
        "tags:\n"
        "  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_ocr\n"
        "    mock_text: ready\n",
        encoding="utf-8",
    )


def saved_ready_plan(tmp_path, monkeypatch, *, agents=("codex",)):
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    plan = build_chief_plan(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        agents=agents,
    )
    saved = save_plan(chief_plan_to_dict(plan), workspace_root=workspace.root, plan_id="20260702-120000-dispatch")
    return workspace, saved["plan_id"]


def test_resume_refreshes_failure_memory_before_next_worker(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan_id = "20260720-120000-memory-refresh"
    plan = {
        "plan_id": plan_id,
        "objective": "Fix payment checkout amount mismatch",
        "repo_root": str(tmp_path),
        "project_memory": {
            "usage": {"memory_mode": "enabled"},
            "entries": [],
        },
    }
    save_plan(plan, workspace_root=workspace.root, plan_id=plan_id)
    mission = create_mission(
        workspace_root=workspace.root,
        objective=plan["objective"],
        repo_root=tmp_path,
        plan_id=plan_id,
        budget_policy=default_budget_policy(),
        mission_id="mission-memory-refresh",
        status="stopped",
    )
    mission["current_round"] = 1
    mission["stop_reason"] = "worker_error"
    save_mission(workspace.root, mission)
    append_round(
        workspace.root,
        mission["mission_id"],
        {
            "round": 1,
            "type": "verification",
            "status": "fail",
            "failed_signature": "checkout|assert_amount|payment amount mismatch",
        },
    )

    refreshed = _refresh_resume_project_memory(
        workspace_root=workspace.root,
        plan=plan,
        plan_id=plan_id,
        mission_id=mission["mission_id"],
        repo_root=tmp_path,
        memory_mode="enabled",
    )

    assert refreshed is not None
    entry = next(item for item in refreshed["entries"] if item["mission_id"] == mission["mission_id"])
    assert entry["evidence"]["failed_signatures"] == [
        "checkout|assert_amount|payment amount mismatch"
    ]
    assert refreshed["usage"]["refreshed_for_resume"] is True
    persisted = load_plan(workspace.root, plan_id)
    assert persisted["project_memory"]["usage"]["refreshed_for_resume"] is True


def passing_codex_result(*_args, **_kwargs):
    return CodexCheckResult(
        changed_files=["src/payment/checkout.py"],
        selected_workflows=["checkout"],
        skipped_slow_workflows=[],
        coverage={"status": "covered"},
        results=[
            CodexWorkflowCheck(
                name="checkout",
                status="passed",
                step_count=3,
                elapsed_seconds=0.1,
                run_id="run-pass",
                real_interaction_count=1,
                acceptance_level="L4",
                acceptance_name="visual_quality",
                is_product_acceptance=True,
            )
        ],
    )


def failing_codex_result(*_args, **_kwargs):
    return CodexCheckResult(
        changed_files=["src/payment/checkout.py"],
        selected_workflows=["checkout"],
        skipped_slow_workflows=[],
        coverage={"status": "covered"},
        results=[
            CodexWorkflowCheck(
                name="checkout",
                status="failed",
                step_count=2,
                elapsed_seconds=0.1,
                run_id="run-fail",
                failed_step="assert_total",
                message="expected total 128",
            )
        ],
    )


def inspection_only_codex_result(*_args, **_kwargs):
    return CodexCheckResult(
        changed_files=["src/payment/checkout.py"],
        selected_workflows=["checkout"],
        skipped_slow_workflows=[],
        coverage={"status": "covered"},
        results=[
            CodexWorkflowCheck(
                name="checkout",
                status="inspection_only",
                step_count=3,
                elapsed_seconds=0.1,
                run_id="run-inspect",
                skipped_interaction_count=1,
            )
        ],
    )


def fallback_only_codex_result(*_args, **_kwargs):
    return CodexCheckResult(
        changed_files=["src/payment/checkout.py"],
        selected_workflows=["checkout_verification"],
        skipped_slow_workflows=[],
        coverage={
            "status": "fallback_only",
            "fallback_only_files": ["src/payment/checkout.py"],
            "fallback_workflows": ["checkout_verification"],
        },
        results=[
            CodexWorkflowCheck(
                name="checkout_verification",
                status="passed",
                step_count=3,
                elapsed_seconds=0.1,
                run_id="run-fallback",
                real_interaction_count=1,
            )
        ],
    )


def test_chief_dispatch_inspection_only_verdict_is_not_verified(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")

    def fake_runner(argv, cwd, timeout_seconds, log_path):
        log_path.write_text("worker done", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        codex_runner=inspection_only_codex_result,
    )

    # Inspection-only proves rendering, not product behavior; it must never be
    # reported as verified.
    assert payload["status"] == "inspection_only"


def test_chief_dispatch_markdown_includes_preflight() -> None:
    markdown = chief_dispatch_to_markdown(
        {
            "status": "preview",
            "plan_id": "p1",
            "verification": {"command": "npm test"},
            "preflight": {
                "status": "warning",
                "test_command": {"status": "resolved", "requested": "auto", "resolved": "npm test"},
                "verification_env": {"status": "ok", "missing_env_vars": []},
                "dependency": {
                    "package_manager": "npm",
                    "lockfile": "package-lock.json",
                    "deps_installed": False,
                    "cache_available": False,
                    "native_install_risk": False,
                    "estimated_install_minutes": 9,
                    "warnings": ["node_dependencies_not_installed"],
                },
                "verification_timeout": {"base_timeout_seconds": 900.0, "timeout_seconds": 2100.0, "reason": "missing_node_modules"},
            },
        }
    )

    assert "### Preflight" in markdown
    assert "| dependency | `warning` |" in markdown
    assert "reason=missing_node_modules" in markdown


def _codex_track() -> dict:
    return {"id": "track_1_codex", "agent": "codex"}


def test_extract_worker_usage_parses_claude_code_cost(tmp_path) -> None:
    from visual_agent.chief_dispatch import _extract_worker_usage

    log = tmp_path / "worker.log"
    log.write_text(json.dumps({
        "type": "result", "subtype": "success", "result": "DEVPACER_OK",
        "total_cost_usd": 0.041891, "num_turns": 1, "session_id": "abc123",
        "usage": {"input_tokens": 2301, "output_tokens": 11, "cache_read_input_tokens": 11662, "cache_creation_input_tokens": 2428},
    }), encoding="utf-8")

    usage = _extract_worker_usage("claude-code", log, "")

    assert usage["cost_usd"] == 0.041891
    assert usage["input_tokens"] == 2301
    assert usage["session_id"] == "abc123"


def test_extract_worker_usage_tolerates_trailing_stderr(tmp_path) -> None:
    from visual_agent.chief_dispatch import _extract_worker_usage

    log = tmp_path / "worker.log"
    # Real case: the JSON result, then a stderr warning appended after it.
    log.write_text(
        json.dumps({"type": "result", "total_cost_usd": 0.269438, "usage": {"output_tokens": 1576, "input_tokens": 28812}})
        + "\n\n⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY ...",
        encoding="utf-8",
    )

    usage = _extract_worker_usage("claude-code", log, "")

    assert usage["cost_usd"] == 0.269438
    assert usage["output_tokens"] == 1576


def test_extract_worker_usage_none_for_codex(tmp_path) -> None:
    from visual_agent.chief_dispatch import _extract_worker_usage

    log = tmp_path / "worker.log"
    log.write_text("codex output, not json", encoding="utf-8")
    assert _extract_worker_usage("codex", log, "") is None


def test_run_process_capture_writes_log_and_supports_stdin(tmp_path) -> None:
    script = "import sys; data=sys.stdin.read(); print('out:'+data.strip()); print('err-line', file=sys.stderr)"
    log = tmp_path / "worker.log"

    result = run_process_capture(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        timeout_seconds=10,
        log_path=log,
        stdin_text="hello\n",
    )

    assert result["exit_code"] == 0
    assert "out:hello" in result["stdout_tail"]
    assert "err-line" in result["stderr_tail"]
    text = log.read_text(encoding="utf-8")
    assert "out:hello" in text
    assert "err-line" in text
    stdout_log = Path(result["stdout_log_path"])
    stderr_log = Path(result["stderr_log_path"])
    assert stdout_log.name == "worker.stdout.log"
    assert stderr_log.name == "worker.stderr.log"
    assert stdout_log.read_text(encoding="utf-8").strip() == "out:hello"
    assert stderr_log.read_text(encoding="utf-8").strip() == "err-line"


def test_run_process_capture_timeout_writes_stderr_sidecar(tmp_path) -> None:
    log = tmp_path / "timeout.log"

    result = run_process_capture(
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(5)"],
        cwd=tmp_path,
        timeout_seconds=0.1,
        log_path=log,
    )

    assert result["exit_code"] == 124
    assert "Timed out after" in result["stderr_tail"]
    assert "started" in Path(result["stdout_log_path"]).read_text(encoding="utf-8")
    assert "Timed out after" in Path(result["stderr_log_path"]).read_text(encoding="utf-8")
    assert "Timed out after" in log.read_text(encoding="utf-8")


def test_run_process_capture_timeout_terminates_isolated_tree(tmp_path, monkeypatch) -> None:
    captured = {}

    class FakeProcess:
        pid = 9753
        stdout = io.StringIO("child-output\n")
        stderr = io.StringIO("")
        stdin = None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("worker", timeout)

    process = FakeProcess()

    def fake_popen(argv, **kwargs):
        captured["kwargs"] = kwargs
        return process

    def fake_terminate(candidate):
        captured["terminated"] = candidate.pid
        return True

    monkeypatch.setattr("visual_agent.chief_dispatch.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.isolated_process_group_kwargs",
        lambda: {"start_new_session": True},
    )
    monkeypatch.setattr("visual_agent.chief_dispatch.terminate_process_tree", fake_terminate)
    log = tmp_path / "tree-timeout.log"

    result = run_process_capture(
        ["worker"],
        cwd=tmp_path,
        timeout_seconds=0.1,
        log_path=log,
    )

    assert result["exit_code"] == 124
    assert captured["terminated"] == 9753
    assert captured["kwargs"]["start_new_session"] is True
    assert "child-output" in Path(result["stdout_log_path"]).read_text(encoding="utf-8")
    assert "Timed out after" in Path(result["stderr_log_path"]).read_text(encoding="utf-8")
    assert "Timed out after" in log.read_text(encoding="utf-8")


def test_run_process_capture_launch_oserror_writes_stderr_sidecar(tmp_path, monkeypatch) -> None:
    log = tmp_path / "missing.log"

    def fail_to_launch(*_args, **_kwargs):
        raise OSError("worker executable missing")

    monkeypatch.setattr("visual_agent.chief_dispatch.subprocess.Popen", fail_to_launch)

    result = run_process_capture(
        ["missing-worker"],
        cwd=tmp_path,
        timeout_seconds=10,
        log_path=log,
    )

    assert result["exit_code"] == 127
    assert Path(result["stdout_log_path"]).read_text(encoding="utf-8") == ""
    stderr = Path(result["stderr_log_path"]).read_text(encoding="utf-8")
    assert "worker executable missing" in stderr
    assert log.read_text(encoding="utf-8") == stderr


def test_run_process_capture_uses_isolated_launch_kwargs(tmp_path, monkeypatch) -> None:
    captured = {}

    class FakeProcess:
        stdout = io.StringIO("out-line\n")
        stderr = io.StringIO("")
        stdin = None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            raise AssertionError("worker should not be killed")

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("visual_agent.chief_dispatch.isolated_process_group_kwargs", lambda: {"creationflags": 12345})
    monkeypatch.setattr("visual_agent.chief_dispatch.subprocess.Popen", fake_popen)

    result = run_process_capture(
        ["worker"],
        cwd=tmp_path,
        timeout_seconds=10,
        log_path=tmp_path / "worker.log",
    )

    assert result["exit_code"] == 0
    assert captured["kwargs"]["creationflags"] == 12345
    assert "out-line" in result["stdout_tail"]


def test_worker_attempt_persists_progress_heartbeat(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()

    def fake_runner(argv, cwd, timeout_seconds, log_path, **kwargs):
        progress_callback = kwargs.get("progress_callback")
        assert progress_callback is not None
        progress_callback({"stream": "stdout", "chunk": "editing src/app.js\n"})
        log_path.write_text("editing src/app.js\n", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    record = _run_worker_attempt(
        workspace_root=workspace,
        plan_id="p1",
        mission_id="m1",
        attempt="initial",
        track={"id": "track_1_codex", "agent": "codex"},
        argv=["codex", "exec"],
        cwd=tmp_path,
        timeout_seconds=10,
        log_path=workspace / "chief_plans" / "p1" / "logs" / "worker.log",
        runner=fake_runner,
    )

    progress = json.loads((workspace / "missions" / "m1" / "progress.json").read_text(encoding="utf-8"))
    assert record["status"] == "completed"
    assert progress["stage"] == "worker_completed"
    assert progress["worker_status"] == "completed"
    assert "editing src/app.js" in progress["last_output_tail"]


def test_worker_attempt_does_not_retry_internal_type_error(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    calls = []

    def broken_runner(argv, cwd, timeout_seconds, log_path, **kwargs):
        calls.append((argv, kwargs))
        raise TypeError("internal worker bug")

    with pytest.raises(TypeError, match="internal worker bug"):
        _run_worker_attempt(
            workspace_root=workspace,
            plan_id="p-type-error",
            attempt="initial",
            track={"id": "track_1_codex", "agent": "codex"},
            argv=["codex", "exec"],
            cwd=tmp_path,
            timeout_seconds=10,
            log_path=workspace / "worker.log",
            runner=broken_runner,
        )

    assert len(calls) == 1


def test_worker_attempt_missing_exit_code_is_crashed(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()

    def incomplete_runner(argv, cwd, timeout_seconds, log_path):
        return {"stdout_tail": "incomplete", "stderr_tail": ""}

    record = _run_worker_attempt(
        workspace_root=workspace,
        plan_id="p-missing-exit",
        attempt="initial",
        track={"id": "track_1_codex", "agent": "codex"},
        argv=["codex", "exec"],
        cwd=tmp_path,
        timeout_seconds=10,
        log_path=workspace / "worker.log",
        runner=incomplete_runner,
    )

    assert record["status"] == "crashed"
    assert record["exit_code"] == 125


def test_managed_budget_blocks_repair_when_usage_is_unknown_or_repeated() -> None:
    policy = ManagedBudgetPolicy(
        max_wall_seconds=600,
        max_total_tokens=1000,
        max_attempts=3,
        max_repair_rounds=2,
        max_same_failure_count=2,
    )
    unknown = _dispatch_budget_assessment(
        policy,
        dispatch_started=monotonic(),
        records=[{"status": "failed"}],
        repair_rounds=0,
        same_failure_count=1,
        operation="repair",
    )
    repeated = _dispatch_budget_assessment(
        policy,
        dispatch_started=monotonic(),
        records=[{"status": "failed", "usage": {"total_tokens": 100}}],
        repair_rounds=1,
        same_failure_count=2,
        operation="repair",
    )

    assert unknown["allowed"] is False
    assert unknown["status"] == "usage_unknown"
    assert unknown["reason_codes"] == ["token_usage_unknown"]
    assert repeated["allowed"] is False
    assert "same_failure_repeated" in repeated["reason_codes"]


def test_managed_budget_policy_defaults_same_failure_limit_to_two() -> None:
    policy = _managed_budget_policy(
        {
            "max_wall_seconds": 600,
            "max_total_tokens": 1000,
            "max_attempts": 3,
            "max_repair_rounds": 1,
        }
    )

    assert policy.max_same_failure_count == 2


@pytest.mark.parametrize(
    ("worker", "expected_kind"),
    [
        ({"status": "failed", "stderr_tail": "HTTP 503 Service Unavailable"}, "provider_5xx"),
        ({"status": "failed", "stderr_tail": "Timed out after 30s"}, "network_timeout"),
        ({"status": "crashed", "stderr_tail": ""}, "process_crash"),
    ],
)
def test_managed_retry_whitelist_keeps_current_attempt_failed(
    worker: dict,
    expected_kind: str,
) -> None:
    decision = _managed_retry_decision(
        worker,
        verification={"verdict": "fail"},
        idempotency_key="managed:key",
        attempts_completed=1,
        max_attempts=3,
    )

    assert decision["failure_kind"] == expected_kind
    assert decision["retry"] is True
    assert decision["status"] == "scheduled"
    assert decision["delay_seconds"] >= 0
    assert decision["current_attempt_remains_failed"] is True


def test_evidence_rejection_is_never_retried() -> None:
    decision = _managed_retry_decision(
        {"status": "completed"},
        verification={
            "verdict": "fail",
            "repair_brief": {"source": "test_tampering"},
        },
        idempotency_key="managed:key",
        attempts_completed=1,
        max_attempts=3,
    )

    assert decision["failure_kind"] == "evidence_rejected"
    assert decision["retry"] is False
    assert decision["status"] == "not_retryable"


def test_worker_attempt_persists_sidecars_and_prefers_codex_stdout_usage(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()

    def fake_runner(argv, cwd, timeout_seconds, log_path, **kwargs):
        stdout_log = log_path.with_name("worker.stdout.log")
        stderr_log = log_path.with_name("worker.stderr.log")
        log_path.write_text(
            '{"type":"thread.started","thread_id":"combined-session"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":999,"output_tokens":99}}\n',
            encoding="utf-8",
        )
        stdout_log.write_text(
            '{"type":"thread.started","thread_id":"stdout-session"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":3}}\n',
            encoding="utf-8",
        )
        stderr_log.write_text('{"type":"turn.completed","usage":{"input_tokens":777}}\n', encoding="utf-8")
        return {
            "exit_code": 0,
            "stdout_tail": "",
            "stderr_tail": "",
            "stdout_log_path": str(stdout_log),
            "stderr_log_path": str(stderr_log),
        }

    record = _run_worker_attempt(
        workspace_root=workspace,
        plan_id="p-sidecars",
        attempt="initial",
        track={"id": "track_1_codex", "agent": "codex"},
        argv=["codex", "exec", "--json", "-"],
        cwd=tmp_path,
        timeout_seconds=10,
        log_path=workspace / "chief_plans" / "p-sidecars" / "logs" / "worker.log",
        runner=fake_runner,
    )

    assert Path(record["stdout_log_path"]).name == "worker.stdout.log"
    assert Path(record["stderr_log_path"]).name == "worker.stderr.log"
    assert record["usage"]["session_id"] == "stdout-session"
    assert record["usage"]["input_tokens"] == 12
    assert record["usage"]["output_tokens"] == 3
    persisted = load_worker_records(workspace, "p-sidecars")
    assert persisted[0]["stdout_log_path"] == record["stdout_log_path"]
    assert persisted[0]["stderr_log_path"] == record["stderr_log_path"]


def test_build_worker_command_claude_code_is_headless(tmp_path) -> None:
    plan = {"objective": "fix", "acceptance_criteria": [], "selected_workflows": [], "changed_files": []}
    track = {"id": "track_1_claude_code", "agent": "claude-code", "model": "opus"}

    cmd = build_worker_command(
        plan=plan,
        track=track,
        worktree=Path(tmp_path),
        verification_command="python -m pytest -q",
    )
    argv = cmd["argv"]

    assert argv[0] == "claude"
    assert "-p" in argv
    assert "--model" in argv and "opus" in argv
    # Headless-safe permission mode so the worker does not hang on prompts.
    assert "--permission-mode" in argv and "acceptEdits" in argv
    assert "--strict-mcp-config" in argv
    assert "--disable-slash-commands" in argv
    assert "--tools" in argv
    assert "--allowedTools" in argv
    assert "Bash(python -m pytest -q)" in argv
    assert argv.index("--allowedTools") < argv.index("--output-format") < len(argv) - 1
    assert "with no cd prefix or shell wrapper: python -m pytest -q" in argv[-1]
    assert "If a shell or tool action is denied, do not retry" in argv[-1]
    assert cmd["resolved_sandbox"] == "acceptEdits"
    assert cmd["sandbox_source"] == "agent_profile.headless"
    assert cmd["resolved_approval"] == "acceptEdits"
    assert cmd["approval_source"] == "agent_profile.headless"


def test_build_worker_command_claude_code_yolo_uses_bypass_permissions(tmp_path) -> None:
    plan = {"objective": "fix", "acceptance_criteria": [], "selected_workflows": [], "changed_files": []}
    track = {"id": "track_1_claude_code", "agent": "claude-code", "model": "opus"}

    cmd = build_worker_command(
        plan=plan,
        track=track,
        worktree=Path(tmp_path),
        verification_command="python -m pytest -q",
        execution_policy={"permission_mode": "yolo", "tool_permissions": "default"},
    )
    argv = cmd["argv"]

    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "acceptEdits" not in argv
    assert "--allowedTools" not in argv
    assert "--tools" in argv
    assert argv[argv.index("--tools") + 1] == "default"
    assert cmd["resolved_sandbox"] == "bypassPermissions"
    assert cmd["resolved_approval"] == "bypassPermissions"


def test_build_worker_command_non_headless_agent_records_routing_identity(tmp_path) -> None:
    plan = {"objective": "fix", "acceptance_criteria": [], "selected_workflows": [], "changed_files": []}
    track = {"id": "track_1_mimo", "agent": "mimo", "model": "mimo-1"}

    cmd = build_worker_command(
        plan=plan,
        track=track,
        worktree=Path(tmp_path),
        verification_command="python -m pytest -q",
    )

    # Without these the mission journey cannot bind the routing decision to the
    # worker that ran, and reports routing_identity_missing.
    assert cmd["resolved_provider"] == "mimo"
    assert cmd["provider_source"] == "agent_profile"
    assert cmd["routing_evidence"]["request"]["provider"] == "mimo"
    assert cmd["routing_evidence"]["request"]["model"] == cmd["resolved_model"]


def test_build_worker_command_appends_prompt_suffix_to_codex_stdin(tmp_path) -> None:
    plan = {"objective": "fix", "acceptance_criteria": [], "selected_workflows": [], "changed_files": []}
    track = {"id": "track_1_codex", "agent": "codex"}

    cmd = build_worker_command(
        plan=plan,
        track=track,
        worktree=Path(tmp_path),
        verification_command="python -m pytest -q",
        prompt_suffix="Source checkout dirty context: M src/app.py",
    )

    assert "stdin" in cmd
    assert "Source checkout dirty context: M src/app.py" in cmd["stdin"]


def test_chief_dispatch_claude_code_worker_executes(tmp_path, monkeypatch) -> None:
    # Re-enabled 2026-07-05: the free tier runs on the user's own subscriptions,
    # so claude-code must be dispatchable just like codex.
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("claude-code",))
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda name: "claude" if name == "claude" else None)

    calls = []

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        calls.append({"argv": argv, "env": env})
        log_path.write_text("worker done", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "worker done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root, plan_id=plan_id, execute=True, dry_run=False,
        command_runner=fake_runner, codex_runner=passing_codex_result,
    )

    assert len(calls) >= 1
    assert "claude" in str(calls[0]["argv"][0])
    assert payload["status"] != "blocked"
    assert payload["worker_record"]["agent"] == "claude-code"


def test_chief_dispatch_marks_approval_waiting_worker_as_blocked(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("claude-code",))
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda name: "claude" if name == "claude" else None)

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        text = "I need your approval to run `npm test`. Waiting for permission."
        log_path.write_text(text, encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": text, "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        codex_runner=passing_codex_result,
    )

    assert payload["worker_record"]["status"] == "blocked"
    assert payload["worker_record"]["blocked_reason"] == "worker_waiting_for_permission"
    assert payload["status"] == "worker_failed"


def test_chief_dispatch_fallback_only_acceptance_is_coverage_gap(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")

    def fake_runner(argv, cwd, timeout_seconds, log_path):
        log_path.write_text("worker done", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        codex_runner=fallback_only_codex_result,
    )

    assert payload["status"] == "coverage_gap"
    assert payload["latest_verification"]["verdict"] == "coverage_gap"


def test_chief_dispatch_explicit_command_pass_without_changes_is_not_verified(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch._worktree_has_product_changes", lambda _worktree: False)

    def fake_runner(argv, cwd, timeout_seconds, log_path):
        log_path.write_text("worker claims done", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        test_command="python -c pass",
    )

    assert payload["latest_verification"]["verdict"] == "pass"
    assert payload["status"] == "no_product_changes"


def test_chief_dispatch_rejects_worker_sibling_flutter_toolchain(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.run_command_verification",
        lambda **_kwargs: {"verdict": "pass", "exit_code": 0, "stdout_tail": "", "stderr_tail": ""},
    )

    def fake_runner(argv, cwd, timeout_seconds, log_path):
        log_path.write_text(
            r"exec & 'D:\\Projects\\flutter_stable\\bin\\dart.bat' format lib/screens/discovery_page.dart",
            encoding="utf-8",
        )
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        test_command=(
            r"D:\Projects\flutter_stable\bin\cache\dart-sdk\bin\dart.exe "
            r"D:\Projects\flutter_stable\bin\cache\flutter_tools.snapshot analyze lib/screens/discovery_page.dart"
        ),
    )

    assert payload["latest_verification"]["verdict"] == "pass"
    assert payload["status"] == "verified_blocked"
    assert payload["toolchain_violation"]["forbidden_path"].lower().endswith(r"\bin\dart.bat")


def test_chief_dispatch_sanitizes_worker_env_for_exact_dart_toolchain(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch._worktree_has_product_changes", lambda _worktree: True)
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.run_command_verification",
        lambda **_kwargs: {"verdict": "pass", "exit_code": 0, "stdout_tail": "", "stderr_tail": ""},
    )
    monkeypatch.setenv("PATH", r"C:\Windows;D:\Projects\flutter_stable\bin;C:\Tools")
    calls = []

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None, stdin_text=None):
        calls.append(env or {})
        log_path.write_text("worker used exact sdk", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        test_command=(
            r"D:\Projects\flutter_stable\bin\cache\dart-sdk\bin\dart.exe "
            r"D:\Projects\flutter_stable\bin\cache\flutter_tools.snapshot analyze lib/screens/discovery_page.dart"
        ),
    )

    assert payload["status"] == "verified"
    assert payload["toolchain_policy"]["status"] == "active"
    assert payload["worker"]["toolchain_policy_env"] == "enabled"
    assert calls
    env = calls[0]
    assert env["DEVPACER_EXPECTED_DART_EXE"].endswith(r"\bin\cache\dart-sdk\bin\dart.exe")
    assert r"D:\Projects\flutter_stable\bin" not in env["PATH"]


def test_chief_dispatch_toolchain_preflight_blocks_conflicting_verification_command(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("preflight should block before worker launch")

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=should_not_run,
        test_command=(
            r"D:\Projects\flutter_stable\bin\cache\dart-sdk\bin\dart.exe "
            r"D:\Projects\flutter_stable\bin\cache\flutter_tools.snapshot analyze && "
            r"D:\Projects\flutter_stable\bin\flutter.bat doctor"
        ),
    )

    assert payload["status"] == "blocked"
    assert payload["toolchain_preflight"]["status"] == "blocked"
    assert payload["toolchain_preflight"]["forbidden_path"].lower().endswith(r"\bin\flutter.bat")


def test_chief_dispatch_mimo_agent_applies_standalone_patch(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("mimo",))
    target = tmp_path / "src" / "payment" / "checkout.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("total = 0\n", encoding="utf-8")
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "reused", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.default_worktree_path",
        lambda **_kwargs: tmp_path,
    )
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.resolve_backend_by_name",
        lambda _name: {
            "name": "mimo",
            "model": "mimo-v2.5-pro",
            "cost_is_savings": True,
            "env": {"ANTHROPIC_API_KEY": "tp-test", "ANTHROPIC_BASE_URL": "https://mimo.test/anthropic"},
        },
    )
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.run_llm_completion",
        lambda **_kwargs: (
            "diff --git a/src/payment/checkout.py b/src/payment/checkout.py\n"
            "--- a/src/payment/checkout.py\n"
            "+++ b/src/payment/checkout.py\n"
            "@@ -1 +1 @@\n"
            "-total = 0\n"
            "+total = 128\n"
        ),
    )

    payload = dispatch_chief_plan(
        workspace_root=workspace.root, plan_id=plan_id, execute=True, dry_run=False,
        codex_runner=passing_codex_result,
    )

    assert payload["worker_record"]["agent"] == "mimo"
    assert payload["worker_record"]["status"] == "completed"
    assert payload["status"] == "verified"
    assert target.read_text(encoding="utf-8") == "total = 128\n"


def test_chief_dispatch_mimo_agent_blocks_without_backend_token(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("mimo",))
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.resolve_backend_by_name", lambda _name: None)

    payload = dispatch_chief_plan(
        workspace_root=workspace.root, plan_id=plan_id, execute=True, dry_run=False,
        command_runner=lambda *_args, **_kwargs: {"exit_code": 0},
        codex_runner=passing_codex_result,
    )

    assert payload["status"] == "blocked"
    assert "MiMo backend was requested" in payload["reason"]


def test_summarize_worker_usage_separates_spend_and_savings() -> None:
    from visual_agent.chief_dispatch import summarize_worker_usage

    records = [
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 200,
                "reasoning_output_tokens": 5,
                "num_turns": 2,
                "cost_usd": 0.05,
            }
        },
        None,
        {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "reasoning_output_tokens": 7,
                "num_turns": 1,
                "cost_usd": 0.30,
                "cost_is_savings": True,
            }
        },
    ]
    summary = summarize_worker_usage(records)
    assert summary["total_tokens"] == 330
    assert summary["input_tokens"] == 110
    assert summary["spent_usd"] == 0.05
    assert summary["saved_usd"] == 0.30
    assert summary["attempts_with_usage"] == 2
    assert summary["reasoning_output_tokens"] == 12
    assert summary["num_turns"] == 3


def test_summarize_worker_usage_deduplicates_resumed_session_and_adds_fresh_sessions() -> None:
    from visual_agent.chief_dispatch import summarize_worker_usage

    records = [
        {
            "agent": "codex",
            "usage": {
                "session_id": "resumed-thread",
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_input_tokens": 60,
                "cache_creation_input_tokens": 4,
                "reasoning_output_tokens": 2,
                "total_tokens": 110,
                "num_turns": 1,
            },
        },
        {
            "agent": "codex",
            "usage": {
                "session_id": "resumed-thread",
                "input_tokens": 220,
                "output_tokens": 25,
                "cache_read_input_tokens": 170,
                "cache_creation_input_tokens": 8,
                "reasoning_output_tokens": 5,
                "total_tokens": 245,
                "num_turns": 1,
            },
        },
        {
            "agent": "codex",
            "usage": {
                "session_id": "fresh-thread",
                "input_tokens": 40,
                "output_tokens": 8,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 2,
                "reasoning_output_tokens": 1,
                "total_tokens": 48,
                "num_turns": 1,
            },
        },
        {
            "usage": {
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_creation_input_tokens": 1,
                "total_tokens": 7,
                "num_turns": 1,
            },
        },
    ]

    summary = summarize_worker_usage(records)

    assert summary["attempts_with_usage"] == 4
    assert summary["num_turns"] == 4
    assert summary["input_tokens"] == 265
    assert summary["output_tokens"] == 35
    assert summary["cache_read_tokens"] == 180
    assert summary["cache_creation_tokens"] == 11
    assert summary["reasoning_output_tokens"] == 6
    assert summary["total_tokens"] == 300


def test_looks_like_quota_exhaustion() -> None:
    from visual_agent.agent_backends import looks_like_quota_exhaustion

    assert looks_like_quota_exhaustion("Claude usage limit reached")
    assert looks_like_quota_exhaustion("", "HTTP 429 Too Many Requests")
    assert looks_like_quota_exhaustion("Your credit balance is too low")
    assert not looks_like_quota_exhaustion("build succeeded", "all tests passed")


def test_chief_dispatch_claude_quota_exhaustion_is_reported(tmp_path, monkeypatch) -> None:
    # With claude-code re-enabled, a quota wall must surface honestly (and try
    # the MiMo failover hop when a token exists — here it does not).
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("claude-code",))
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda name: "claude" if name == "claude" else None)
    monkeypatch.setattr("visual_agent.chief_dispatch.resolve_backend_by_name", lambda _name: None)

    calls = []

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        calls.append({"argv": argv, "env": env})
        log_path.write_text("Claude usage limit reached. resets at 3pm", encoding="utf-8")
        return {"exit_code": 1, "stdout_tail": "Claude usage limit reached. resets at 3pm", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root, plan_id=plan_id, execute=True, dry_run=False,
        command_runner=fake_runner, codex_runner=passing_codex_result,
    )

    assert len(calls) == 1
    assert "claude" in str(calls[0]["argv"][0])
    assert payload["quota_exhausted"] is True


def test_chief_dispatch_codex_quota_does_not_fall_over_to_claude(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("codex",))
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda name: name if name in {"codex", "claude"} else None)
    monkeypatch.setattr("visual_agent.chief_dispatch.resolve_backend_by_name", lambda _name: None)

    calls = []

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        calls.append({"argv": argv, "env": env})
        if len(calls) == 1:
            # Codex subscription exhausted.
            log_path.write_text("ERROR: You've hit your usage limit.", encoding="utf-8")
            return {"exit_code": 1, "stdout_tail": "", "stderr_tail": "ERROR: You've hit your usage limit."}
        log_path.write_text("done on claude", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done on claude", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root, plan_id=plan_id, execute=True, dry_run=False,
        command_runner=fake_runner, codex_runner=passing_codex_result,
    )

    assert len(calls) == 1
    assert calls[0]["argv"][0] == "codex"
    assert payload["failover_worker_record"] is None
    assert payload["worker"].get("failover_disabled") == (
        "No alternate Codex provider is configured for automatic failover."
    )
    assert payload["quota_exhausted"] is True


def test_chief_dispatch_codex_quota_fails_over_to_alternate_codex_provider(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("codex",))
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.create_worktree",
        lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]},
    )
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda name: name if name == "codex" else None)
    calls = []

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None, stdin_text=None, progress_callback=None):
        calls.append(list(argv))
        if len(calls) == 1:
            log_path.write_text("usage limit reached", encoding="utf-8")
            return {"exit_code": 1, "stdout_tail": "", "stderr_tail": "usage limit reached"}
        log_path.write_text("completed through relay", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "completed through relay", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        codex_runner=passing_codex_result,
        execution_policy={
            "codex_provider": "openai",
            "codex_failover_provider": "custom",
        },
    )

    assert len(calls) == 2
    assert "model_provider='openai'" in calls[0]
    assert "model_provider='custom'" in calls[1]
    assert payload["failover_worker_record"]["status"] == "completed"
    assert payload["failover_worker_record"]["resolved_provider"] == "custom"
    assert payload["quota_exhausted"] is False


def test_chief_dispatch_codex_quota_does_not_auto_fall_over_to_mimo_patch_worker(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("codex",))
    target = tmp_path / "src" / "payment" / "checkout.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("total = 0\n", encoding="utf-8")
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "reused", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.default_worktree_path", lambda **_kwargs: tmp_path)
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda name: name if name in {"codex", "claude"} else None)
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.resolve_backend_by_name",
        lambda _name: {
            "name": "mimo",
            "model": "mimo-v2.5-pro",
            "cost_is_savings": True,
            "env": {"ANTHROPIC_API_KEY": "tp-test", "ANTHROPIC_BASE_URL": "https://mimo.test/anthropic"},
        },
    )
    def unexpected_mimo_call(**_kwargs):
        raise AssertionError("Codex quota exhaustion must not automatically call MiMo")

    monkeypatch.setattr("visual_agent.chief_dispatch.run_llm_completion", unexpected_mimo_call)

    calls = []

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        calls.append({"argv": argv, "env": env})
        log_path.write_text("usage limit reached", encoding="utf-8")
        return {"exit_code": 1, "stdout_tail": "usage limit reached", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root, plan_id=plan_id, execute=True, dry_run=False,
        command_runner=fake_runner, codex_runner=passing_codex_result,
    )

    assert len(calls) == 1
    assert calls[0]["argv"][0] == "codex"
    assert payload["failover_worker_record"] is None
    assert payload["worker"].get("failover_disabled") == (
        "No alternate Codex provider is configured for automatic failover."
    )
    assert payload["quota_exhausted"] is True
    assert payload["status"] == "worker_failed"
    assert target.read_text(encoding="utf-8") == "total = 0\n"


def test_chief_dispatch_quota_exhausted_is_honest_when_no_failover(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("codex",))
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    # Claude is NOT installed: no failover hop is possible.
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda name: name if name == "codex" else None)
    monkeypatch.setattr("visual_agent.chief_dispatch.resolve_backend_by_name", lambda _name: None)

    calls = []

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None, **_kwargs):
        calls.append(list(argv))
        log_path.write_text("ERROR: You've hit your usage limit.", encoding="utf-8")
        return {"exit_code": 1, "stdout_tail": "", "stderr_tail": "ERROR: You've hit your usage limit."}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root, plan_id=plan_id, execute=True, dry_run=False,
        command_runner=fake_runner, codex_runner=failing_codex_result,
    )

    assert payload["quota_exhausted"] is True
    assert len(calls) == 1
    assert payload["repair_worker_records"] == []
    assert any("quota" in w.lower() for w in payload.get("warnings") or [])

    from visual_agent.chief_run import _stop_reason_from_dispatch

    assert _stop_reason_from_dispatch(payload, {"max_rounds": 2}) == "quota_exhausted"


def test_chief_dispatch_claude_normal_failure_reports_worker_failure(tmp_path, monkeypatch) -> None:
    # A non-quota worker failure must execute, then surface as a failed worker
    # record — not silently verify and not block before launch.
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("claude-code",))
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda name: "claude" if name == "claude" else None)
    monkeypatch.setattr("visual_agent.chief_dispatch.resolve_backend_by_name", lambda _name: None)
    calls = []

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        calls.append(argv)
        log_path.write_text("SyntaxError: bad code", encoding="utf-8")
        return {"exit_code": 1, "stdout_tail": "SyntaxError: bad code", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root, plan_id=plan_id, execute=True, dry_run=False,
        command_runner=fake_runner, codex_runner=passing_codex_result,
    )
    assert len(calls) >= 1
    assert payload["status"] != "verified"
    assert payload["worker_record"]["status"] == "failed"


def test_chief_dispatch_claude_code_worker_writes_records(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("claude-code",))
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda name: "claude" if name == "claude" else None)

    executed = []

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        executed.append(argv)
        log_path.write_text("worker done", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        codex_runner=passing_codex_result,
    )

    assert len(executed) >= 1
    assert payload["worker_record"]["agent"] == "claude-code"
    records = load_worker_records(workspace.root, plan_id)
    assert records, "worker record must be persisted for the usage ledger"


def test_coverage_changed_files_excludes_checkpoint_artifacts(tmp_path, monkeypatch) -> None:
    from visual_agent import chief_dispatch

    repo = tmp_path / "xiao"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    workspace.mkdir()
    # Raw diff includes real code plus Checkpoint's own run artifacts (which the
    # verification itself writes into the repo on an earlier round).
    monkeypatch.setattr(
        chief_dispatch,
        "changed_files",
        lambda **_kwargs: [
            "examples/web/checkout_verification_demo.html",
            ".agent-workspace/runs/20260101-abc/report.json",
            ".agent-workspace/run_history.jsonl",
            ".visual-agent-status.md",
            ".pw-browsers/chromium-1223/x",
            ".dart-home/pub-cache/hosted/pub.dev/x",
            ".dart_tool/package_config.json",
            "artifacts/random-local-run/report.json",
            "xiao/artifacts/random-local-run/report.json",
            "xiao/.agent-workspace/runs/20260101-abc/report.json",
        ],
    )

    kept = chief_dispatch._coverage_changed_files(repo_root=repo, workspace_root=workspace)

    assert kept == ["examples/web/checkout_verification_demo.html"]


def test_worktree_product_changes_ignore_devpacer_gitignore_block(tmp_path) -> None:
    from visual_agent import chief_dispatch

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text(".agent-workspace/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / ".gitignore").write_text(
        ".agent-workspace/\n"
        "# Auto-generated by DevPacer for this worktree - safe to commit\n"
        "__pycache__/\n"
        "*.pyc\n"
        ".pytest_cache/\n"
        "node_modules/\n"
        ".npm-cache/\n"
        ".dart_tool/\n"
        ".dart-home/\n"
        "coverage/\n",
        encoding="utf-8",
    )

    assert chief_dispatch._worktree_has_product_changes(tmp_path) is False


def test_worktree_product_changes_incomplete_scan_is_not_change_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    from visual_agent import chief_dispatch

    incomplete = type("IncompleteChangeSet", (), {"complete": False})()
    monkeypatch.setattr(
        chief_dispatch,
        "collect_repository_change_set",
        lambda **_kwargs: incomplete,
    )

    assert chief_dispatch._worktree_has_product_changes(tmp_path) is False


def test_worktree_product_changes_ignore_dart_runtime_caches(tmp_path) -> None:
    from visual_agent import chief_dispatch

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "main.dart").write_text("void main() {}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / ".dart-home" / "pub-cache").mkdir(parents=True)
    (tmp_path / ".dart-home" / "pub-cache" / "README").write_text("cache\n", encoding="utf-8")
    (tmp_path / ".dart_tool").mkdir()
    (tmp_path / ".dart_tool" / "package_config.json").write_text("{}\n", encoding="utf-8")

    assert chief_dispatch._worktree_has_product_changes(tmp_path) is False


def test_worktree_product_changes_ignore_flutter_generated_registrants(tmp_path) -> None:
    from visual_agent import chief_dispatch

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    path = tmp_path / "windows" / "flutter" / "generated_plugin_registrant.cc"
    path.parent.mkdir(parents=True)
    path.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=tmp_path, check=True, capture_output=True)

    path.write_text("new\n", encoding="utf-8")

    assert chief_dispatch._worktree_has_product_changes(tmp_path) is False


def test_worktree_product_changes_keeps_real_source_changes(tmp_path) -> None:
    from visual_agent import chief_dispatch

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    path = tmp_path / "lib" / "screens" / "discovery_page.dart"
    path.parent.mkdir(parents=True)
    path.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=tmp_path, check=True, capture_output=True)

    path.write_text("new\n", encoding="utf-8")

    assert chief_dispatch._worktree_has_product_changes(tmp_path) is True


def test_worktree_product_changes_ignore_acceptance_and_test_files(tmp_path) -> None:
    from visual_agent import chief_dispatch

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "service-quality-acceptance.mjs").write_text("export const cases = [];\n", encoding="utf-8")
    (tmp_path / "快手").mkdir()
    (tmp_path / "快手" / "test.js").write_text("console.log('test');\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "eval" / "service-quality-acceptance.mjs").write_text("export const cases = ['stricter'];\n", encoding="utf-8")
    (tmp_path / "快手" / "test.js").write_text("console.log('updated test');\n", encoding="utf-8")

    assert chief_dispatch._worktree_has_product_changes(tmp_path) is False


def test_worktree_product_changes_keeps_non_ascii_real_source_changes(tmp_path) -> None:
    from visual_agent import chief_dispatch

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    path = tmp_path / "快手" / "miniapp" / "utils" / "caseStore.js"
    path.parent.mkdir(parents=True)
    path.write_text("export const oldValue = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=tmp_path, check=True, capture_output=True)

    path.write_text("export const newValue = 2;\n", encoding="utf-8")

    assert chief_dispatch._worktree_has_product_changes(tmp_path) is True


def test_mimo_patch_retry_prompt_drops_stale_file_context(tmp_path) -> None:
    from visual_agent.chief_dispatch import _build_mimo_patch_retry_prompt

    target = tmp_path / "src" / "routes" / "diagnosis.js"
    target.parent.mkdir(parents=True)
    target.write_text("export const current = true;\n", encoding="utf-8")

    prompt = _build_mimo_patch_retry_prompt(
        original_prompt=(
            "Objective:\nfix diagnosis\n\n"
            "Rules:\n- Return only a unified git diff.\n\n"
            "File context:\n--- src/routes/diagnosis.js ---\nSTALE_SNIPPET\n"
        ),
        diff_text=(
            "diff --git a/src/routes/diagnosis.js b/src/routes/diagnosis.js\n"
            "--- a/src/routes/diagnosis.js\n"
            "+++ b/src/routes/diagnosis.js\n"
            "@@ -1 +1 @@\n"
            "-STALE_SNIPPET\n"
            "+new\n"
        ),
        stderr_tail="error: patch failed: src/routes/diagnosis.js:1",
        cwd=tmp_path,
    )

    assert "export const current = true;" in prompt
    assert "File context:\n--- src/routes/diagnosis.js ---\nSTALE_SNIPPET" not in prompt
    assert "CURRENT file contents" in prompt


def test_build_worker_command_codex_uses_cli_default_model(tmp_path) -> None:
    plan = {"objective": "fix", "acceptance_criteria": [], "selected_workflows": []}
    policy = {"implementation": "strong", "repair": "fast"}

    impl = build_worker_command(
        plan=plan, track=_codex_track(), worktree=Path(tmp_path), verification_command="checkpoint codex-check",
        phase="implementation", model_policy=policy,
    )
    repair = build_worker_command(
        plan=plan, track=_codex_track(), worktree=Path(tmp_path), verification_command="checkpoint codex-check",
        phase="repair", model_policy=policy, prompt_override="repair it",
    )

    assert "--model" not in impl["argv"]
    assert "--model" not in repair["argv"]
    assert impl["argv"][-1] == "-"
    assert "Objective: fix" in impl["stdin"]
    assert "Objective: fix" not in impl["argv"]


def test_build_worker_command_warns_to_reuse_absolute_verification_toolchain(tmp_path) -> None:
    plan = {
        "objective": "format and analyze Flutter discovery page",
        "acceptance_criteria": [],
        "selected_workflows": [],
    }
    cmd = build_worker_command(
        plan=plan,
        track=_codex_track(),
        worktree=Path(tmp_path),
        verification_command=(
            r"D:\Projects\flutter_stable\bin\cache\dart-sdk\bin\dart.exe "
            r"D:\Projects\flutter_stable\bin\cache\flutter_tools.snapshot analyze lib/screens/discovery_page.dart"
        ),
    )

    assert "prefer that same toolchain" in cmd["stdin"]
    assert "completion evidence matches" in cmd["stdin"]
    assert r"D:\Projects\flutter_stable\bin\cache\dart-sdk\bin\dart.exe" in cmd["stdin"]


def test_build_verification_command_uses_python_module_entrypoint(tmp_path) -> None:
    command = build_verification_command(
        workspace_root=tmp_path / ".agent-workspace",
        repo_root=tmp_path,
        run_profile="supervised",
        include_slow=False,
    )

    assert command.startswith("python -m visual_agent.cli codex-check")
    assert "checkpoint codex-check" not in command


def test_build_worker_command_honors_explicit_codex_model(tmp_path) -> None:
    plan = {"objective": "fix", "acceptance_criteria": [], "selected_workflows": []}
    cmd = build_worker_command(
        plan=plan,
        track={"id": "track_1_codex", "agent": "codex", "model": "custom-codex-model"},
        worktree=Path(tmp_path),
        verification_command="c",
    )
    assert "--model" in cmd["argv"]
    assert "custom-codex-model" in cmd["argv"]


def test_build_worker_command_executes_dynamic_routing_decision(tmp_path) -> None:
    plan = {"objective": "fix", "acceptance_criteria": [], "selected_workflows": []}
    track = {
        "id": "track_1_codex",
        "agent": "codex",
        "model": "gpt-routed",
        "model_selection": {
            "schema_version": 2,
            "policy_version": 2,
            "decision_id": "decision-1",
            "status": "selected",
            "required_tier": "standard",
            "selected": {
                "id": "routed",
                "provider": "custom",
                "model": "gpt-routed",
                "agent_backend": "codex",
            },
        },
    }

    command = build_worker_command(
        plan=plan,
        track=track,
        worktree=tmp_path,
        verification_command="python -m pytest -q",
    )

    assert command["argv"][command["argv"].index("--model") + 1] == "gpt-routed"
    assert "model_provider='custom'" in command["argv"]
    assert command["routing_evidence"]["policy_match"] is True
    assert command["routing_evidence"]["decision_id"] == "decision-1"


def test_worker_reasoning_uses_track_for_implementation_and_profile_for_repair(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.recommend_worker_config",
        lambda _profile, *, task_kind: {
            "model": "",
            "reasoning_effort": "xhigh",
            "sandbox": {},
            "approval": {},
        },
    )
    plan = {"objective": "fix", "acceptance_criteria": [], "selected_workflows": []}
    track = {"id": "track_1_codex", "agent": "codex", "reasoning_effort": "high"}

    implementation = build_worker_command(
        plan=plan,
        track=track,
        worktree=tmp_path,
        verification_command="python -c pass",
        phase="implementation",
    )
    repair = build_worker_command(
        plan=plan,
        track=track,
        worktree=tmp_path,
        verification_command="python -c pass",
        phase="repair",
        prompt_override="repair",
    )

    assert "model_reasoning_effort=high" in implementation["argv"]
    assert implementation["reasoning_effort_source"] == "track"
    assert "model_reasoning_effort=xhigh" in repair["argv"]
    assert repair["reasoning_effort_source"] == "profile"


def test_chief_dispatch_does_not_merge_when_not_verified(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")

    merge_calls = []
    monkeypatch.setattr("visual_agent.chief_dispatch.merge_worktree_branch", lambda **kwargs: merge_calls.append(kwargs) or {"status": "merged"})

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        log_path.write_text("done", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root, plan_id=plan_id, execute=True, dry_run=False,
        command_runner=fake_runner, codex_runner=failing_codex_result, merge=True,
    )

    # Verification failed -> must NOT merge, and say so.
    assert payload["status"] == "verification_failed"
    assert merge_calls == []
    assert payload["merge"]["status"] == "skipped"
    assert "not verified" in payload["merge"]["reason"]


def test_chief_dispatch_merges_when_verified(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch.merge_worktree_branch", lambda **kwargs: {"status": "merged", "target": "main"})

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        log_path.write_text("done", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root, plan_id=plan_id, execute=True, dry_run=False,
        command_runner=fake_runner, codex_runner=passing_codex_result, merge=True,
    )

    assert payload["status"] == "verified"
    assert payload["merge"]["status"] == "merged"


def _merge_repo_with_worktree(tmp_path: Path, branch: str = "worker") -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "devpacer@example.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "DevPacer"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "worktree", "add", "-b", branch, str(worktree), "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    return repo, worktree, branch


def test_merge_keeps_user_gitignore_changes(tmp_path) -> None:
    from visual_agent.chief_dispatch import merge_worktree_branch

    repo, worktree, branch = _merge_repo_with_worktree(tmp_path)
    (worktree / ".gitignore").write_text("*.pyc\nsecrets.env\n", encoding="utf-8")

    result = merge_worktree_branch(repo_root=repo, worktree=worktree, branch=branch, message="keep gitignore")

    assert result["status"] == "merged"
    assert "secrets.env" in (repo / ".gitignore").read_text(encoding="utf-8")


def test_merge_drops_pacer_only_gitignore_block(tmp_path) -> None:
    from visual_agent.chief_dispatch import merge_worktree_branch

    repo, worktree, branch = _merge_repo_with_worktree(tmp_path)
    (worktree / "README.md").write_text("changed\n", encoding="utf-8")
    (worktree / ".gitignore").write_text(
        "*.pyc\n"
        "# Auto-generated by DevPacer for this worktree - safe to commit\n"
        "__pycache__/\n"
        ".pytest_cache/\n"
        "node_modules/\n"
        ".npm-cache/\n",
        encoding="utf-8",
    )

    result = merge_worktree_branch(repo_root=repo, worktree=worktree, branch=branch, message="drop pacer block")

    assert result["status"] == "merged"
    assert (repo / "README.md").read_text(encoding="utf-8") == "changed\n"
    assert (repo / ".gitignore").read_text(encoding="utf-8") == "*.pyc\n"


def test_merge_refused_for_worker_failed_tests_pass(tmp_path, monkeypatch) -> None:
    """Unclean worker exit + green tests + product changes → verified, merge still skipped."""
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch._worktree_has_product_changes", lambda _worktree: True)
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.run_command_verification",
        lambda **_kwargs: {"verdict": "pass", "exit_code": 0, "failure_kind": "", "command": "python -c pass"},
    )
    merge_calls = []
    monkeypatch.setattr("visual_agent.chief_dispatch.merge_worktree_branch", lambda **kwargs: merge_calls.append(kwargs) or {"status": "merged"})

    def fake_runner(argv, cwd, timeout_seconds, log_path):
        log_path.write_text("worker failed after current edits", encoding="utf-8")
        return {"exit_code": 1, "stdout_tail": "", "stderr_tail": "failed"}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        test_command="python -c pass",
        merge=True,
    )

    assert payload["status"] == "verified"
    assert payload["merge"]["status"] == "skipped"
    assert "did not complete normally" in payload["merge"]["reason"]
    assert merge_calls == []
    assert any("did not report a clean completion" in item for item in payload.get("warnings") or [])


def test_post_merge_failure_report_contains_revert_hint() -> None:
    from visual_agent.chief_run import chief_run_to_markdown

    report = chief_run_to_markdown(
        {
            "status": "stopped",
            "stop_reason": "merged_verification_failed",
            "mission": {"mission_id": "m1", "objective": "Fix checkout", "plan_id": "p1"},
            "plan": {"status": "ready"},
            "rounds": [],
            "dispatch": {
                "status": "merged_verification_failed",
                "merge": {
                    "status": "merged",
                    "target": "main",
                    "branch": "checkpoint/p1/track",
                    "commit": "abc123def",
                },
                "latest_verification": {
                    "verdict": "fail",
                    "command_verification": {
                        "command": "npm test",
                        "verdict": "fail",
                        "exit_code": 1,
                        "failure_kind": "command_failed",
                    },
                },
            },
        }
    )

    assert "abc123def" in report
    assert "git revert -m 1 abc123def" in report


def test_chief_dispatch_marks_post_merge_command_failure(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch._worktree_has_product_changes", lambda _worktree: True)
    monkeypatch.setattr("visual_agent.chief_dispatch.merge_worktree_branch", lambda **kwargs: {"status": "merged", "target": "main"})

    calls = []

    def fake_verify(**kwargs):
        calls.append(kwargs["repo_root"])
        if len(calls) == 1:
            return {"verdict": "pass", "command": kwargs["command"], "exit_code": 0, "output_tail": "", "failure_kind": ""}
        return {
            "verdict": "fail",
            "command": kwargs["command"],
            "exit_code": 1,
            "output_tail": "AI知识审查: skipped (QWEN_API_KEY missing)",
            "failure_kind": "verification_environment_missing",
        }

    monkeypatch.setattr("visual_agent.chief_dispatch.run_command_verification", fake_verify)

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        log_path.write_text("done", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        test_command="npm run eval:acceptance",
        merge=True,
    )

    assert payload["status"] == "merged_verification_failed"
    assert payload["latest_verification"]["run_profile"] == "post-merge"
    assert payload["latest_verification"]["command_verification"]["failure_kind"] == "verification_environment_missing"
    assert payload["merge"]["post_merge_verification"]["verdict"] == "fail"
    assert len(calls) == 2
    assert payload["dispatch_record"]["repair_rounds"] == 0
    assert payload["dispatch_record"]["verification_attempts"] == 2


def test_chief_dispatch_dry_run_previews_worktree_and_commands(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)

    payload = dispatch_chief_plan(workspace_root=workspace.root, plan_id=plan_id, dry_run=True, execute=False)

    assert payload["status"] == "preview"
    assert payload["dry_run"] is True
    assert "exec --json" in payload["worker"]["command"]
    assert "python -m visual_agent.cli codex-check" in payload["verification"]["command"]
    assert payload["worktree"]["path"].endswith("track-1-codex")


def test_chief_dispatch_blocks_conditional_npm_ci_short_circuit(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    marker = tmp_path / "node_modules" / "express" / "package.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"name":"express"}\n', encoding="utf-8")

    def should_not_create_worktree(**_kwargs):
        raise AssertionError("preflight should block before creating a worktree")

    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", should_not_create_worktree)

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        test_command=(
            "cmd /d /s /c if not exist node_modules\\express\\package.json "
            "npm ci --cache .npm-cache --prefer-offline ^&^& node --test"
        ),
    )

    assert payload["status"] == "preflight_blocked"
    assert payload["reason"] == "conditional_test_command_short_circuit"
    assert payload["preflight"]["status"] == "blocked"
    assert payload["preflight"]["command_safety"]["marker"] == "node_modules/express/package.json"


def test_chief_dispatch_review_plan_uses_report_as_acceptance(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan_id = "20260706-150000-review-plan"
    save_plan(
        {
            "schema_version": 1,
            "plan_id": plan_id,
            "objective": "对本项目进行审查并给出下一阶段开发计划",
            "status": "needs_workflow_coverage",
            "repo_root": str(tmp_path),
            "selected_workflows": [],
            "acceptance_criteria": ["产出审查与开发计划报告"],
            "worker_tracks": [
                {"id": "track_1_codex", "agent": "codex", "track_kind": "implementation", "tier": "strong"}
            ],
        },
        workspace_root=workspace.root,
        plan_id=plan_id,
    )
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch._git_head", lambda _repo_root: "base")
    monkeypatch.setattr("visual_agent.chief_dispatch._worktree_has_product_changes", lambda _worktree: True)

    def fake_create_worktree(**kwargs):
        kwargs["worktree"].mkdir(parents=True, exist_ok=True)
        return {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]}

    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", fake_create_worktree)

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        Path(cwd, "审查与开发计划.md").write_text(
            "## 产品判断\n\n这是一个可审查产品。\n\n## 当前状态\n\n已有基础代码。\n\n"
            "## 主要风险\n\n需要端到端验收。\n\n## 建议开发计划\n\n1. 收口入口。\n2. 补验证。\n\n## 验收方式\n\n查看报告与测试结果。"
            "每个阶段都要在工作台留下 mission、rounds、日志、最终报告和用量指标，确保不是口头完成。",
            encoding="utf-8",
        )
        log_path.write_text("report written", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "report written", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
    )

    assert payload["status"] == "verified"
    assert payload["latest_verification"]["run_profile"] == "review_plan"
    assert "建议开发计划" in payload["review_plan_report"]


def test_chief_dispatch_review_plan_report_cannot_hide_worker_timeout(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan_id = "review-timeout"
    save_plan(
        {
            "schema_version": 1,
            "plan_id": plan_id,
            "objective": "审查当前产品并输出开发计划",
            "status": "ready",
            "repo_root": str(tmp_path),
            "selected_workflows": [],
            "acceptance_criteria": ["产出审查与开发计划报告"],
            "worker_tracks": [
                {"id": "track_1_codex", "agent": "codex", "track_kind": "implementation", "tier": "strong"}
            ],
        },
        workspace_root=workspace.root,
        plan_id=plan_id,
    )
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch._git_head", lambda _repo_root: "base")
    monkeypatch.setattr("visual_agent.chief_dispatch._worktree_has_product_changes", lambda _worktree: False)

    def fake_create_worktree(**kwargs):
        kwargs["worktree"].mkdir(parents=True, exist_ok=True)
        return {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]}

    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", fake_create_worktree)

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        Path(cwd, "审查与开发计划.md").write_text(
            "## 产品判断\n\n已有可审查产品。\n\n## 当前状态\n\n报告已生成。\n\n"
            "## 主要风险\n\n验收仍需闭环。\n\n## 建议开发计划\n\n1. 收口关键路径。\n2. 补验收。\n\n"
            "## 验收方式\n\n运行固定验收命令并检查报告。报告内容足够长，能作为本轮交付物。"
            "继续保留日志、任务轮次和最终报告路径，便于复盘。",
            encoding="utf-8",
        )
        log_path.write_text("report written before timeout", encoding="utf-8")
        return {"exit_code": 124, "stdout_tail": "", "stderr_tail": "timed out"}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
    )

    assert payload["worker_record"]["status"] == "failed"
    assert payload["latest_verification"]["run_profile"] == "review_plan"
    assert payload["latest_verification"]["verdict"] == "pass"
    assert payload["status"] == "worker_failed"


def test_chief_dispatch_blocks_inspection_lane(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("gemini",))

    payload = dispatch_chief_plan(workspace_root=workspace.root, plan_id=plan_id, dry_run=True, execute=False)

    assert payload["status"] == "blocked"
    assert "inspection lane" in payload["reason"]


def test_chief_dispatch_execute_records_worker_and_verification(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")

    def fake_runner(argv, cwd, timeout_seconds, log_path):
        log_path.write_text("worker done", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        codex_runner=passing_codex_result,
    )

    assert payload["status"] == "verified"
    assert str(payload["latest_verification"]["workspace_root"]).startswith(str(payload["worktree"]["path"]))
    assert payload["verification"]["records_workspace_root"] == str(workspace.root)
    records = load_worker_records(workspace.root, plan_id)
    assert records and records[0]["status"] == "completed"
    verification = load_verification(workspace.root, plan_id)
    assert verification is not None
    assert verification["verdict"] == "pass"


def test_chief_dispatch_nested_tracked_repo_root_runs_inside_worktree_subdir(tmp_path, monkeypatch) -> None:
    parent = tmp_path / "parent"
    project = parent / "pkg"
    project.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=parent, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "devpacer@example.local"], cwd=parent, check=True)
    subprocess.run(["git", "config", "user.name", "DevPacer"], cwd=parent, check=True)
    (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (project / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "pkg"], cwd=parent, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=parent, check=True, capture_output=True, text=True)

    workspace = init_workspace(project / ".agent-workspace", with_demo=False)
    saved = save_plan(
        {
            "schema_version": 1,
            "status": "ready",
            "objective": "Update app.py value from 1 to 2",
            "workspace_root": str(workspace.root),
            "repo_root": str(project),
            "changed_files": ["app.py"],
            "selected_workflows": [],
            "coverage": {"status": "covered"},
            "worker_tracks": [
                {
                    "id": "track_1_codex",
                    "agent": "codex",
                    "track_kind": "implementation",
                    "sandbox": {"flag": "--sandbox workspace-write"},
                }
            ],
        },
        workspace_root=workspace.root,
        plan_id="nested-subdir-plan",
    )
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    captured: dict[str, Path] = {}

    def fake_runner(argv, cwd, timeout_seconds, log_path, **_kwargs):
        captured["worker_cwd"] = Path(cwd).resolve()
        log_path.write_text("worker done", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    def fake_command_verification(**kwargs):
        captured["verification_repo_root"] = Path(kwargs["repo_root"]).resolve()
        return {"verdict": "pass", "command": kwargs["command"], "exit_code": 0, "failure_kind": ""}

    monkeypatch.setattr("visual_agent.chief_dispatch.run_command_verification", fake_command_verification)

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=saved["plan_id"],
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        test_command="python -m pytest tests/test_app.py",
    )

    expected_project_root = (Path(payload["worktree"]["path"]) / "pkg").resolve()
    assert payload["worktree"]["project_root"] == str(expected_project_root)
    assert captured["worker_cwd"] == expected_project_root
    assert captured["verification_repo_root"] == expected_project_root
    assert payload["verification"]["workspace_root"] == str(expected_project_root / ".agent-workspace")


def test_chief_dispatch_reuses_clean_existing_worktree_on_resume(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.create_worktree",
        lambda **kwargs: {"status": "reused", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]},
    )
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")

    def fake_runner(argv, cwd, timeout_seconds, log_path, **_kwargs):
        log_path.write_text("worker done", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        codex_runner=passing_codex_result,
    )

    assert payload["status"] == "verified"
    assert payload["worktree"]["created"] is True
    assert payload["worktree"]["reused"] is True


def test_chief_dispatch_does_not_verify_when_worker_fails_even_if_check_passes(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")

    def fake_runner(argv, cwd, timeout_seconds, log_path):
        log_path.write_text("worker failed", encoding="utf-8")
        return {"exit_code": 1, "stdout_tail": "", "stderr_tail": "failed"}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        codex_runner=passing_codex_result,
    )

    assert payload["status"] == "worker_failed"
    assert payload["latest_verification"]["verdict"] == "pass"


def test_worker_failed_with_passing_tests_is_not_verified(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch._worktree_has_product_changes", lambda _worktree: True)
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.run_command_verification",
        lambda **_kwargs: {"verdict": "pass", "exit_code": 0, "failure_kind": "", "command": "python -c pass"},
    )
    merge_calls = []
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.merge_worktree_branch",
        lambda **kwargs: merge_calls.append(kwargs) or {"status": "merged"},
    )

    def fake_runner(argv, cwd, timeout_seconds, log_path):
        log_path.write_text("worker failed after previous edits", encoding="utf-8")
        return {"exit_code": 1, "stdout_tail": "", "stderr_tail": "patch does not apply"}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        codex_runner=passing_codex_result,
        test_command="python -c pass",
        merge=True,
    )

    assert payload["status"] == "verified"
    assert payload["latest_verification"]["verdict"] == "pass"
    assert payload["merge"]["status"] == "skipped"
    assert merge_calls == []
    assert any("did not report a clean completion" in item for item in payload["warnings"])


def test_prior_verified_evidence_does_not_verify_new_work(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    mission_dir = workspace.root / "missions" / plan_id
    mission_dir.mkdir(parents=True)
    (mission_dir / "rounds.jsonl").write_text(
        json.dumps({"type": "merge", "status": "merged"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch._worktree_has_product_changes", lambda _worktree: False)
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.run_command_verification",
        lambda **_kwargs: {"verdict": "pass", "exit_code": 0, "failure_kind": "", "command": "python -c pass"},
    )

    def fake_runner(argv, cwd, timeout_seconds, log_path):
        log_path.write_text("worker failed after prior merge", encoding="utf-8")
        return {"exit_code": 1, "stdout_tail": "", "stderr_tail": "patch does not apply"}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        codex_runner=passing_codex_result,
        test_command="python -c pass",
    )

    assert payload["status"] != "verified"
    assert payload["status"] == "worker_failed"
    assert payload["latest_verification"]["verdict"] == "pass"


def test_prior_verified_evidence_is_allowed_for_explicit_resume_verification(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    mission_dir = workspace.root / "missions" / plan_id
    mission_dir.mkdir(parents=True)
    (mission_dir / "rounds.jsonl").write_text(
        json.dumps({"type": "merge", "status": "merged"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch._worktree_has_product_changes", lambda _worktree: False)
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.run_command_verification",
        lambda **_kwargs: {"verdict": "pass", "exit_code": 0, "failure_kind": "", "command": "python -c pass"},
    )

    def fake_runner(argv, cwd, timeout_seconds, log_path):
        log_path.write_text("worker failed after prior merge", encoding="utf-8")
        return {"exit_code": 1, "stdout_tail": "", "stderr_tail": "patch does not apply"}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        codex_runner=passing_codex_result,
        test_command="python -c pass",
        allow_prior_verified_evidence=True,
    )

    assert payload["status"] == "verified"
    assert payload["latest_verification"]["verdict"] == "pass"


def test_worker_completed_with_pass_and_changes_still_verified(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch._worktree_has_product_changes", lambda _worktree: True)
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.run_command_verification",
        lambda **_kwargs: {"verdict": "pass", "exit_code": 0, "failure_kind": "", "command": "python -c pass"},
    )

    def fake_runner(argv, cwd, timeout_seconds, log_path):
        log_path.write_text("worker completed", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        codex_runner=passing_codex_result,
        test_command="python -c pass",
    )

    assert payload["status"] == "verified"
    assert payload["latest_verification"]["verdict"] == "pass"


def test_dispatch_verification_failure_builds_repair_brief(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)

    def fake_evidence(_workspace_root, *, max_chars):
        return {
            "status": "found",
            "run_id": "run-fail",
            "workflow": "checkout",
            "failed_step": {"id": "assert_total"},
            "repair_prompt": "Fix the total assertion failure.",
        }

    payload = run_dispatch_verification(
        workspace_root=workspace.root,
        plan_id=plan_id,
        repo_root=tmp_path,
        run_profile="dry-run",
        include_slow=False,
        max_workflows=10,
        codex_runner=failing_codex_result,
        failure_evidence_builder=fake_evidence,
    )

    assert payload["verdict"] == "fail"
    assert payload["repair_brief"]["repair_prompt"] == "Fix the total assertion failure."
    saved = load_verification(workspace.root, plan_id)
    assert saved is not None
    assert saved["repair_brief"]["workflow"] == "checkout"


def test_untrusted_command_failure_is_not_repairable() -> None:
    verification = {
        "verdict": "fail",
        "repair_brief": {
            "source": "test_command",
            "failure_kind": "test_command_invalid",
        },
    }

    assert _verification_is_repairable(verification) is False


def test_missing_verification_environment_failure_is_not_repairable() -> None:
    verification = {
        "verdict": "fail",
        "repair_brief": {
            "source": "test_command",
            "failure_kind": "verification_environment_missing",
        },
    }

    assert _verification_is_repairable(verification) is False


def test_test_tampering_failure_is_not_repairable() -> None:
    verification = {
        "verdict": "fail",
        "repair_brief": {
            "source": "test_tampering",
        },
    }

    assert _verification_is_repairable(verification) is False


def test_workspace_record_dirty_prefixes_ignore_whole_workspace(tmp_path) -> None:
    # The workspace is DevPacer's runtime dir; the dirty gate must ignore all of
    # it, including the fresh-project case where git collapses the entire
    # untracked ".agent-workspace/" into a single porcelain entry.
    repo = tmp_path / "repo"
    workspace = repo / ".agent-workspace"
    prefixes = workspace_record_dirty_prefixes(repo_root=repo, workspace_root=workspace)

    assert prefixes == (".agent-workspace/", "强制测试记录.md")
    assert _dirty_path_ignored("?? .agent-workspace/", prefixes)
    assert _dirty_path_ignored("?? .agent-workspace/missions/m1/plan.json", prefixes)
    assert not _dirty_path_ignored("?? src/calc.py", prefixes)


def test_dirty_gate_ignores_checkpoint_gitignore_bootstrap(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "devpacer@example.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "DevPacer"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True, text=True)

    (repo / ".gitignore").write_text(
        "*.pyc\n# DevPacer / Checkpoint generated runtime files\n.agent-workspace/\n",
        encoding="utf-8",
    )

    dirty = git_dirty_files(repo, ignored_prefixes=(".agent-workspace/",))
    assert dirty == []


def test_dirty_gate_keeps_user_gitignore_changes(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "devpacer@example.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "DevPacer"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True, text=True)

    (repo / ".gitignore").write_text("*.pyc\nsecrets.env\n", encoding="utf-8")

    dirty = git_dirty_files(repo, ignored_prefixes=(".agent-workspace/",))
    assert dirty == ["M .gitignore"]


def test_dirty_gate_ignores_mandatory_record(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "devpacer@example.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "DevPacer"], cwd=repo, check=True)
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True, text=True)

    (repo / "强制测试记录.md").write_text("# 强制测试记录\n", encoding="utf-8")

    dirty = git_dirty_files(repo, ignored_prefixes=(".agent-workspace/", "强制测试记录.md"))
    assert dirty == []


def test_write_worktree_gitignore_creates_local_exclude_for_python_project(tmp_path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "main.py").write_text("# python\n", encoding="utf-8")

    _write_worktree_gitignore(worktree, repo_root)

    assert not (worktree / ".gitignore").exists()
    exclude = worktree / ".git" / "info" / "exclude"
    assert exclude.exists(), "local git exclude should be written for a Python project"
    content = exclude.read_text(encoding="utf-8")
    assert "__pycache__/" in content
    assert "*.pyc" in content


def test_write_worktree_gitignore_preserves_existing_gitignore_and_appends_runtime_cache_rules_to_exclude(tmp_path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    existing = worktree / ".gitignore"
    existing.write_text("custom\n", encoding="utf-8")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "main.py").write_text("# python\n", encoding="utf-8")
    (repo_root / "package.json").write_text('{"scripts":{"test":"node --test"}}\n', encoding="utf-8")
    (repo_root / "pubspec.yaml").write_text("name: demo\n", encoding="utf-8")

    _write_worktree_gitignore(worktree, repo_root)

    assert existing.read_text(encoding="utf-8") == "custom\n"
    content = (worktree / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "__pycache__/" in content
    assert ".npm-cache/" in content
    assert ".dart-home/" in content
    assert ".dart_tool/" in content


def test_create_worktree_allows_dirty_reuse_only_when_explicit(tmp_path) -> None:
    from visual_agent.chief_dispatch import create_worktree

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
    worktree = tmp_path / "wt"
    created = create_worktree(repo_root=repo, worktree=worktree, branch="devpacer-test")
    assert created["status"] == "created"
    tracked = worktree / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=worktree, check=True, capture_output=True, text=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-m", "tracked"], cwd=worktree, check=True, capture_output=True, text=True)
    tracked.write_text("two\n", encoding="utf-8")

    blocked = create_worktree(repo_root=repo, worktree=worktree, branch="devpacer-test")
    allowed = create_worktree(repo_root=repo, worktree=worktree, branch="devpacer-test", allow_dirty=True)

    assert blocked["status"] == "blocked"
    assert allowed["status"] == "reused"
    assert allowed["dirty"]


def test_create_worktree_overlays_nested_untracked_project_when_dirty_allowed(tmp_path) -> None:
    from visual_agent.chief_dispatch import create_worktree

    parent = tmp_path / "parent"
    parent.mkdir()
    subprocess.run(["git", "init"], cwd=parent, check=True, capture_output=True, text=True)
    (parent / "README.md").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=parent, check=True, capture_output=True, text=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-m", "init"], cwd=parent, check=True, capture_output=True, text=True)

    nested = parent / "xiao"
    nested.mkdir()
    (nested / "pyproject.toml").write_text("[project]\nname='xiao'\n", encoding="utf-8")
    (nested / "tests").mkdir()
    (nested / "tests" / "test_dashboard_browser.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    (nested / "model_api_keys.txt").write_text("mimo=dummy-secret\n", encoding="utf-8")
    (nested / "tests" / ".env.local").write_text("TOKEN=dummy-secret\n", encoding="utf-8")
    (nested / ".agent-workspace").mkdir()
    (nested / ".agent-workspace" / "state.json").write_text("{}", encoding="utf-8")

    worktree = tmp_path / "wt"
    result = create_worktree(repo_root=nested, worktree=worktree, branch="devpacer-nested", allow_dirty=True)

    assert result["status"] == "created"
    assert result["dirty_overlay"] == "copied"
    assert result["dirty_overlay_commit"] == "created"
    assert (worktree / "pyproject.toml").exists()
    assert (worktree / "tests" / "test_dashboard_browser.py").exists()
    assert not (worktree / "model_api_keys.txt").exists()
    assert not (worktree / "tests" / ".env.local").exists()
    assert not (worktree / ".agent-workspace").exists()
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.strip() == ""


def test_create_worktree_overlays_ignored_source_files_when_dirty_allowed(tmp_path) -> None:
    from visual_agent.chief_dispatch import create_worktree

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "devpacer@example.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "DevPacer"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("src/data/\nnode_modules/\n.env\n", encoding="utf-8")
    (repo / "package.json").write_text('{"scripts":{"test":"node --test"}}\n', encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "package.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True, text=True)

    (repo / "src" / "data").mkdir(parents=True)
    (repo / "src" / "data" / "caseAtlas.js").write_text("export const cases = [];\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "leftpad.js").write_text("module.exports = null;\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")

    worktree = tmp_path / "wt"
    result = create_worktree(repo_root=repo, worktree=worktree, branch="devpacer-ignored", allow_dirty=True)

    assert result["status"] == "created"
    assert result["dirty_file_overlay"] == "copied"
    assert result["dirty_file_overlay_ignored_files"] == 1
    assert (worktree / "src" / "data" / "caseAtlas.js").exists()
    assert not (worktree / "node_modules" / "leftpad.js").exists()
    assert not (worktree / ".env").exists()


def test_dirty_context_summary_reports_source_dirty_files_before_overlay(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "devpacer@example.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "DevPacer"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("src/data/\n.env\nnode_modules/\n", encoding="utf-8")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True, text=True)

    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    (repo / "src" / "data").mkdir(parents=True)
    (repo / "src" / "data" / "caseAtlas.js").write_text("export const cases = [];\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")

    summary = _dirty_context_summary(repo_root=repo, allow_dirty=True)
    prompt = _dirty_context_prompt(summary)

    assert "M README.md" in summary["source_dirty_files"]
    assert "src/data/caseAtlas.js" in summary["ignored_overlay_candidate_files"]
    assert ".env" not in prompt
    assert "empty `git status`" in prompt


def test_stop_reason_keeps_worker_failure_when_verification_passes() -> None:
    from visual_agent.chief_run import _stop_reason_from_dispatch

    dispatch = {
        "status": "worker_failed",
        "worker_record": {"status": "failed"},
        "latest_verification": {"verdict": "pass"},
    }

    assert _stop_reason_from_dispatch(dispatch, {"max_rounds": 2}) == "worker_error"


def test_stop_reason_reports_worker_toolchain_violation() -> None:
    from visual_agent.chief_run import _stop_reason_from_dispatch

    dispatch = {
        "status": "worker_toolchain_violation",
        "latest_verification": {"verdict": "pass"},
        "toolchain_violation": {"forbidden_path": r"D:\Projects\flutter_stable\bin\dart.bat"},
    }

    assert _stop_reason_from_dispatch(dispatch, {"max_rounds": 2}) == "worker_toolchain_violation"


def test_codex_worker_command_inherits_user_model_and_reasoning_for_audit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "visual_agent.chief_dispatch._codex_user_defaults",
        lambda: {"model": "gpt-5.6-sol", "reasoning_effort": "ultra"},
    )
    command = build_worker_command(
        plan={"objective": "Fix totals"},
        track={"agent": "codex", "reasoning_effort": "inherit"},
        worktree=tmp_path,
        verification_command="python -m pytest -q",
    )

    assert command["argv"][:3] == ["codex", "exec", "--json"]
    assert "--model" not in command["argv"]
    assert "-c" not in command["argv"]
    assert command["resolved_model"] == "gpt-5.6-sol"
    assert command["resolved_reasoning_effort"] == "ultra"
    assert command["reasoning_effort_source"] == "config.toml"


def test_execute_preflight_blocks_exploration_restricting_prompt_source(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.build_worker_prompt_alignment_check",
        lambda: {
            "status": "blocked",
            "issue_count": 1,
            "issues": [
                {
                    "code": "repository_scan_ban",
                    "message": "Worker prompts must not prohibit repository-wide discovery.",
                }
            ],
        },
    )
    calls = []

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=lambda *_args, **_kwargs: calls.append(True),
        test_command="python -c pass",
    )

    assert payload["status"] == "preflight_blocked"
    assert payload["reason"] == "execution_alignment_prompt_restriction"
    assert payload["preflight"]["execution_alignment"]["issues"][0]["code"] == "repository_scan_ban"
    assert calls == []


def test_codex_worker_command_explicit_effort_and_resume_argv(tmp_path) -> None:
    command = build_worker_command(
        plan={"objective": "Fix totals"},
        track={
            "agent": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "sandbox": {"flag": "--sandbox workspace-write"},
            "approval": {"flag": "--ask-for-approval never"},
        },
        worktree=tmp_path,
        verification_command="python -m pytest -q",
        resume_session_id="019f4a34-2fc9-7622-8e24-21c7200fcf8d",
    )

    assert command["argv"][:7] == [
        "codex",
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "exec",
        "resume",
    ]
    assert command["argv"][7] == "--json"
    assert ("-c", "model_reasoning_effort=high") in zip(command["argv"], command["argv"][1:])
    assert "model_reasoning_effort=inherit" not in command["argv"]
    assert command["argv"][-2:] == ["019f4a34-2fc9-7622-8e24-21c7200fcf8d", "-"]
    assert command["resolved_reasoning_effort"] == "high"
    assert command["reasoning_effort_source"] == "track"
    assert command["resolved_sandbox"] == "workspace-write"
    assert command["sandbox_source"] == "track"
    assert command["resolved_approval"] == "never"
    assert command["approval_source"] == "track"
    assert command["session_mode"] == "resume"


def test_codex_repair_resume_preserves_the_routed_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "visual_agent.chief_dispatch._codex_user_defaults",
        lambda: {"model": "gpt-5.6-sol", "reasoning_effort": "medium"},
    )
    command = build_worker_command(
        plan={"objective": "Fix totals"},
        track={
            "agent": "codex",
            "model_selection": {
                "decision_id": "route-balanced",
                "required_tier": "standard",
                "selected": {"provider": "", "model": "gpt-5.5"},
            },
        },
        worktree=tmp_path,
        verification_command="python -m pytest -q",
        phase="repair",
        model_policy={"repair": "strong"},
        resume_session_id="019f4a34-2fc9-7622-8e24-21c7200fcf8d",
    )

    assert ("--model", "gpt-5.5") in zip(command["argv"], command["argv"][1:])
    assert command["resolved_model"] == "gpt-5.5"
    assert command["model_source"] == "command"
    assert command["routing_evidence"]["policy_match"] is True


def test_worker_prompt_allows_exploration_and_delegated_omits_file_guidance(tmp_path) -> None:
    from visual_agent.chief_dispatch import build_worker_prompt

    plan = {
        "objective": "Find and fix the checkout total bug",
        "changed_files": [f"src/module_{index}.py" for index in range(40)],
        "acceptance_criteria": ["Tests pass"],
    }
    tracked = build_worker_prompt(
        plan=plan,
        track={"id": "track_1_codex", "agent": "codex"},
        worktree=tmp_path,
        verification_command="python -m pytest -q",
        dispatch_mode="tracked",
    )
    delegated = build_worker_prompt(
        plan=plan,
        track={"id": "track_1_codex", "agent": "codex"},
        worktree=tmp_path,
        verification_command="python -m pytest -q",
        dispatch_mode="delegated",
    )

    assert "Explore and implement with full senior-engineer autonomy" in tracked
    assert "likely-relevant files" in tracked
    assert tracked.count("- src/module_") == 30
    assert "likely-relevant files" not in delegated
    assert "Worker track:" not in delegated
    for prompt in (tracked, delegated):
        lowered = prompt.lower()
        assert "do not scan" not in lowered
        assert "conserve model budget" not in lowered


def test_worker_prompt_explains_host_side_docker_acceptance(tmp_path) -> None:
    from visual_agent.chief_dispatch import build_worker_prompt

    prompt = build_worker_prompt(
        plan={"objective": "Fix the containerized API", "acceptance_criteria": []},
        track={"id": "track_1_codex", "agent": "codex"},
        worktree=tmp_path,
        verification_command=(
            "docker compose --project-name acceptance up --abort-on-container-exit "
            "--exit-code-from test --build"
        ),
    )

    assert "Docker acceptance note" in prompt
    assert "host Docker daemon" in prompt
    assert "Pacer runs the exact acceptance command from the host verifier" in prompt


def test_concrete_model_policy_overrides_codex_config_for_implementation(tmp_path, monkeypatch) -> None:
    from visual_agent.chief_dispatch import build_worker_command

    monkeypatch.setattr(
        "visual_agent.chief_dispatch._codex_user_defaults",
        lambda: {"model": "gpt-5.6-sol", "reasoning_effort": "medium"},
    )
    command = build_worker_command(
        plan={"objective": "Update docs", "plan_id": "plan-model"},
        track={"id": "track_1_codex", "agent": "codex", "track_kind": "implementation"},
        worktree=tmp_path,
        verification_command="python -m pytest",
        model_policy={"implementation": "gpt-5.5", "repair": "gpt-5.5"},
    )

    assert command["argv"][-3:] == ["--model", "gpt-5.5", "-"]
    assert command["resolved_model"] == "gpt-5.5"
    assert command["model_source"] == "command"


def test_standard_model_policy_uses_balanced_profile_role(tmp_path, monkeypatch) -> None:
    from visual_agent.chief_dispatch import build_worker_command

    seen = {}

    def fake_recommend(_profile, *, task_kind):
        seen["task_kind"] = task_kind
        return {
            "model": "balanced-model",
            "reasoning_effort": "inherit",
            "sandbox": {},
            "approval": {},
        }

    monkeypatch.setattr("visual_agent.chief_dispatch.recommend_worker_config", fake_recommend)
    command = build_worker_command(
        plan={"objective": "Update docs", "plan_id": "plan-model"},
        track={"id": "track_1_codex", "agent": "codex", "track_kind": "implementation"},
        worktree=tmp_path,
        verification_command="python -m pytest",
        model_policy={"implementation": "standard"},
    )

    assert seen["task_kind"] == "balanced"
    assert command["resolved_model"] == "balanced-model"


def test_codex_provider_override_is_explicit_and_audited(tmp_path, monkeypatch) -> None:
    from visual_agent.chief_dispatch import build_worker_command

    monkeypatch.setattr(
        "visual_agent.chief_dispatch._codex_user_defaults",
        lambda: {"model": "gpt-5.6-sol", "provider": "custom"},
    )
    command = build_worker_command(
        plan={"objective": "Update docs", "plan_id": "plan-provider"},
        track={"id": "track_1_codex", "agent": "codex", "track_kind": "implementation"},
        worktree=tmp_path,
        verification_command="python -m pytest",
        codex_provider="openai",
    )

    assert "model_provider='openai'" in command["argv"]
    assert command["resolved_provider"] == "openai"
    assert command["provider_source"] == "command"


def test_strict_acceptance_blocks_marker_only_command(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        test_command="rg ONLY_A_MARKER docs/report.md",
        execution_policy={"acceptance_policy": "strict"},
    )

    assert payload["status"] == "preflight_blocked"
    assert payload["reason"] == "weak_command_gate"


def test_memory_disabled_is_visible_in_dispatch_preview(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execution_policy={"memory_mode": "disabled"},
    )

    assert payload["worker"]["memory_mode"] == "disabled"
    assert payload["project_memory_usage"]["dispatch_injected"] is False
    assert payload["project_memory_usage"]["dispatch_memory_ids"] == []


def test_delegated_dispatch_uses_one_session_long_timeout_and_shared_gate(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch._git_head", lambda _repo_root: "base")
    monkeypatch.setattr("visual_agent.chief_dispatch._worktree_has_product_changes", lambda _worktree: True)

    def fake_create_worktree(**kwargs):
        kwargs["worktree"].mkdir(parents=True, exist_ok=True)
        return {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]}

    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", fake_create_worktree)
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.run_command_verification",
        lambda **_kwargs: {
            "verdict": "pass",
            "exit_code": 0,
            "failure_kind": "",
            "command": "python -c pass",
        },
    )
    calls = []

    def fake_runner(argv, cwd, timeout_seconds, log_path, **kwargs):
        calls.append(
            {
                "argv": list(argv),
                "stdin": kwargs.get("stdin_text", ""),
                "timeout_seconds": timeout_seconds,
            }
        )
        log_path.write_text(
            '{"type":"thread.started","thread_id":"delegated-thread"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":20,"output_tokens":5}}\n',
            encoding="utf-8",
        )
        return {"exit_code": 0, "stdout_tail": "", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        test_command="python -c pass",
        timeout_seconds=11,
        delegated_timeout_seconds=37,
        dispatch_mode="delegated",
        max_repair_rounds=0,
    )

    assert payload["status"] == "verified"
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] == 37
    assert "Worker track:" not in calls[0]["stdin"]
    assert "Likely-relevant files" not in calls[0]["stdin"]
    assert payload["worker_record"]["dispatch_mode"] == "delegated"
    assert payload["dispatch_record"]["dispatch_mode"] == "delegated"
    assert payload["dispatch_record"]["verification_attempts"] == 1


def test_scope_gate_rejects_restricted_worker_path(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "pacer@example.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Pacer"], cwd=repo, check=True)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    (repo / "archive").mkdir()
    (repo / "archive" / "old.py").write_text("changed\n", encoding="utf-8")
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)

    payload = run_dispatch_verification(
        workspace_root=workspace.root,
        plan_id="scope-plan",
        repo_root=repo,
        run_profile="dry-run",
        include_slow=False,
        max_workflows=1,
        test_command="python -c pass",
        worktree_base=base,
    )

    assert payload["verdict"] == "fail"
    assert payload["failure_kind"] == "scope_violation"
    assert payload["restricted_worker_files"] == ["archive/old.py"]


def test_scope_gate_rejects_tracked_agent_workspace_records(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "pacer@example.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Pacer"], cwd=repo, check=True)
    record = repo / ".agent-workspace" / "chief_plans" / "plan.json"
    record.parent.mkdir(parents=True)
    record.write_text('{"status":"ready"}\n', encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".agent-workspace/chief_plans/plan.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    record.write_text('{"status":"verified"}\n', encoding="utf-8")
    workspace = init_workspace(tmp_path / "records", with_demo=False)

    payload = run_dispatch_verification(
        workspace_root=workspace.root,
        plan_id="workspace-record-plan",
        repo_root=repo,
        run_profile="dry-run",
        include_slow=False,
        max_workflows=1,
        test_command="python -c pass",
        worktree_base=base,
    )

    assert payload["verdict"] == "fail"
    assert payload["failure_kind"] == "scope_violation"
    assert payload["restricted_worker_files"] == [".agent-workspace/chief_plans/plan.json"]


def test_scope_gate_ignores_pacer_workspace_runtime_config(tmp_path) -> None:
    from visual_agent.chief_dispatch import _restricted_worker_changes

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "pacer@example.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Pacer"], cwd=repo, check=True)
    record = repo / ".agent-workspace" / "workspace.json"
    record.parent.mkdir(parents=True)
    record.write_text('{"status":"initial"}\n', encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".agent-workspace/workspace.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    record.write_text('{"status":"prepared-by-pacer"}\n', encoding="utf-8")

    restricted, error = _restricted_worker_changes(repo_root=repo, base_ref=base)

    assert error == ""
    assert restricted == []


def test_verification_workspace_snapshot_rejects_worker_tampering(tmp_path) -> None:
    from visual_agent.chief_dispatch import prepare_worktree_workspace

    source = tmp_path / "source-workspace"
    target = tmp_path / "target-workspace"
    (source / "workflows").mkdir(parents=True)
    (source / "workflows" / "gate.yaml").write_text("name: gate\n", encoding="utf-8")
    setup = prepare_worktree_workspace(source_workspace=source, target_workspace=target)
    (target / "workflows" / "gate.yaml").write_text("name: weakened\n", encoding="utf-8")

    payload = run_dispatch_verification(
        workspace_root=source,
        verification_workspace_root=target,
        plan_id="workspace-tamper",
        repo_root=tmp_path,
        run_profile="dry-run",
        include_slow=False,
        max_workflows=1,
        test_command="python -c pass",
        trusted_workspace_snapshot=setup["trusted_snapshot"],
    )

    assert payload["verdict"] == "fail"
    assert payload["failure_kind"] == "verification_workspace_tampering"
    assert payload["tampered_workspace_files"] == ["workflows/gate.yaml"]


def test_dispatch_repair_resumes_codex_session_and_writes_ledger(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch._git_head", lambda _repo_root: "base")
    monkeypatch.setattr("visual_agent.chief_dispatch._worktree_has_product_changes", lambda _worktree: True)

    def fake_create_worktree(**kwargs):
        kwargs["worktree"].mkdir(parents=True, exist_ok=True)
        return {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]}

    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", fake_create_worktree)
    verification_results = [
        {"verdict": "fail", "exit_code": 1, "failure_kind": "command_failed", "command": "python -c pass", "raw_output_tail": "assertion failed"},
        {"verdict": "fail", "exit_code": 1, "failure_kind": "changed_failure", "command": "python -c pass", "raw_output_tail": "a different assertion is now failing"},
        {"verdict": "pass", "exit_code": 0, "failure_kind": "", "command": "python -c pass"},
    ]
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.run_command_verification",
        lambda **_kwargs: verification_results.pop(0),
    )
    calls = []
    cumulative_usage = [
        {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10, "reasoning_output_tokens": 2},
        {"input_tokens": 220, "cached_input_tokens": 170, "output_tokens": 25, "reasoning_output_tokens": 5},
        {"input_tokens": 350, "cached_input_tokens": 300, "output_tokens": 40, "reasoning_output_tokens": 9},
    ]

    def fake_runner(argv, cwd, timeout_seconds, log_path, **kwargs):
        calls.append({"argv": list(argv), "stdin": kwargs.get("stdin_text", "")})
        usage = cumulative_usage[len(calls) - 1]
        log_path.write_text(
            '{"type":"thread.started","thread_id":"thread-123"}\n'
            + json.dumps({"type": "turn.completed", "usage": usage})
            + "\n",
            encoding="utf-8",
        )
        return {"exit_code": 0, "stdout_tail": "", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        test_command="python -c pass",
        repair_strategy="resume",
        execution_policy={
            "idempotency_key": "managed:repair-test",
            "managed_budget": {
                "max_wall_seconds": 600,
                "max_total_tokens": 1000,
                "max_attempts": 3,
                "max_repair_rounds": 2,
                "max_same_failure_count": 2,
            },
        },
    )

    assert payload["status"] == "verified"
    assert len(payload["verification_attempts"]) == 3
    assert [record["attempt"] for record in payload["repair_worker_records"]] == [
        "repair_1_resume",
        "repair_2_resume",
    ]
    for call in calls[1:]:
        exec_index = call["argv"].index("exec")
        assert call["argv"][exec_index : exec_index + 3] == ["exec", "resume", "--json"]
        assert call["argv"][-2:] == ["thread-123", "-"]
    assert "assertion failed" in calls[1]["stdin"]
    assert "a different assertion is now failing" in calls[2]["stdin"]
    assert all("Objective:" not in call["stdin"] for call in calls[1:])
    assert payload["usage_summary"]["input_tokens"] == 350
    assert payload["usage_summary"]["cache_read_tokens"] == 300
    assert payload["usage_summary"]["output_tokens"] == 40
    assert payload["usage_summary"]["reasoning_output_tokens"] == 9
    assert payload["usage_summary"]["total_tokens"] == 390
    assert payload["usage_summary"]["num_turns"] == 3
    assert Path(payload["dispatch_record_path"]).exists()
    assert payload["dispatch_record"]["session_ids"] == ["thread-123"]
    assert payload["dispatch_record"]["resolved_sandbox"] == "workspace-write"
    assert payload["dispatch_record"]["repair_rounds"] == 2
    assert payload["dispatch_record"]["verification_attempts"] == 3
    assert payload["managed_runtime"]["idempotency_key"] == "managed:repair-test"
    assert payload["managed_runtime"]["budget_status"] == "within_budget"
    assert payload["managed_runtime"]["budget"]["usage"]["total_tokens"] == 390


def test_dispatch_resume_unavailable_falls_back_to_fresh_with_full_evidence(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("visual_agent.chief_dispatch._git_head", lambda _repo_root: "base")
    monkeypatch.setattr("visual_agent.chief_dispatch._worktree_has_product_changes", lambda _worktree: True)

    def fake_create_worktree(**kwargs):
        kwargs["worktree"].mkdir(parents=True, exist_ok=True)
        return {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]}

    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", fake_create_worktree)
    verification_results = [
        {
            "verdict": "fail",
            "exit_code": 1,
            "failure_kind": "command_failed",
            "command": "python -c pass",
            "raw_output_tail": "ORIGINAL FAILURE OUTPUT",
        },
        {"verdict": "pass", "exit_code": 0, "failure_kind": "", "command": "python -c pass"},
    ]
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.run_command_verification",
        lambda **_kwargs: verification_results.pop(0),
    )
    calls = []

    def fake_runner(argv, cwd, timeout_seconds, log_path, **kwargs):
        calls.append({"argv": list(argv), "stdin": kwargs.get("stdin_text", "")})
        if "resume" in argv:
            log_path.write_text("error: unrecognized subcommand 'resume'\n", encoding="utf-8")
            return {
                "exit_code": 2,
                "stdout_tail": "",
                "stderr_tail": "error: unrecognized subcommand 'resume'",
            }
        thread_id = "thread-initial" if len(calls) == 1 else "thread-fallback"
        log_path.write_text(
            '{"type":"thread.started","thread_id":"' + thread_id + '"}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n',
            encoding="utf-8",
        )
        return {"exit_code": 0, "stdout_tail": "", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=fake_runner,
        test_command="python -c pass",
        max_repair_rounds=1,
        repair_strategy="resume",
    )

    assert payload["status"] == "verified"
    assert [record["attempt"] for record in payload["repair_worker_records"]] == [
        "repair_1_resume",
        "repair_1_fresh_fallback",
    ]
    assert "resume" in calls[1]["argv"]
    assert "resume" not in calls[2]["argv"]
    assert "Objective: Fix checkout total display" in calls[2]["stdin"]
    assert "ORIGINAL FAILURE OUTPUT" in calls[2]["stdin"]


def test_repair_evidence_is_redacted_and_capped_at_32kb_utf8() -> None:
    from visual_agent.chief_dispatch import _repair_evidence_text

    secret = "sk-" + ("s" * 48)
    raw = secret + ("错" * 20000)
    evidence = _repair_evidence_text({"command_verification": {"raw_output_tail": raw}})

    assert secret not in evidence
    assert len(evidence.encode("utf-8")) <= 32768
    assert evidence.endswith("错")


def test_create_worktree_applies_uncommitted_deletions_not_just_edits(tmp_path) -> None:
    from visual_agent.chief_dispatch import create_worktree

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "devpacer@example.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "DevPacer"], cwd=repo, check=True)
    (repo / "keep.js").write_text("export const keep = 1;\n", encoding="utf-8")
    (repo / "removed.js").write_text("export const gone = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True, text=True)

    # The user deleted a file but has not committed it yet.
    (repo / "removed.js").unlink()
    (repo / "keep.js").write_text("export const keep = 2;\n", encoding="utf-8")

    worktree = tmp_path / "wt"
    result = create_worktree(repo_root=repo, worktree=worktree, branch="devpacer-deletions", allow_dirty=True)

    assert result["status"] == "created"
    # Copying only edits hands the worker a repo that never existed, with
    # already-deleted files still present and their stale tests still running.
    assert result["dirty_file_overlay_deleted_files"] == 1
    assert not (worktree / "removed.js").exists()
    assert (worktree / "keep.js").read_text(encoding="utf-8") == "export const keep = 2;\n"


def test_post_merge_verification_keeps_the_acceptance_grade(tmp_path) -> None:
    from visual_agent.chief_dispatch import _run_post_merge_command_verification
    from visual_agent.chief_plans_store import load_verification

    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    grade = {
        "tier": "verified",
        "reason_code": "acceptance_gate_discriminating",
        "discriminating": True,
    }

    payload = _run_post_merge_command_verification(
        workspace_root=workspace,
        plan_id="p-merge",
        mission_id="m-merge",
        repo_root=repo,
        command="python -c \"pass\"",
        timeout_seconds=120.0,
        prior_acceptance=grade,
    )

    # This record overwrites the pre-merge one on disk, so a merged mission —
    # the only path that actually delivers — would otherwise lose the answer to
    # "did this prove the objective?".
    assert payload["acceptance"]["tier"] == "verified"
    saved = load_verification(workspace, "p-merge")
    assert saved["acceptance"]["reason_code"] == "acceptance_gate_discriminating"


def test_upstream_outage_triggers_failover_like_an_exhausted_quota() -> None:
    from visual_agent.agent_backends import looks_like_provider_5xx, looks_like_quota_exhaustion

    outage = (
        '{"type":"error","message":"Reconnecting... 1/5 (unexpected status 503 Service '
        'Unavailable: Service temporarily unavailable, url: https://relay.example/responses, '
        'request id: c92f7c6e-7e23-4294-a7ef)"}'
    )

    # Every failover path used to be gated on quota exhaustion alone, so a relay
    # outage — the most common cause of lost runs in dogfooding — failed the
    # mission instead of trying another backend. The two causes differ but the
    # remedy is the same.
    assert looks_like_quota_exhaustion(outage) is False
    assert looks_like_provider_5xx(outage) is True


def test_a_transient_outage_does_not_poison_the_quota_cache() -> None:
    from visual_agent.agent_backends import looks_like_quota_exhaustion, looks_like_provider_5xx

    outage = "unexpected status 502 Bad Gateway from upstream"
    exhausted = "You have hit your usage limit for this week"

    # record_quota_failure makes later dispatches skip the agent entirely, so it
    # must fire for a real quota block and never for a passing outage.
    assert looks_like_quota_exhaustion(outage) is False
    assert looks_like_provider_5xx(outage) is True
    assert looks_like_quota_exhaustion(exhausted) is True
    assert looks_like_provider_5xx(exhausted) is False


def test_cache_reads_are_not_charged_against_the_token_budget() -> None:
    from visual_agent.chief_dispatch import summarize_worker_usage

    # Real numbers from a claude-code mission that finished first try in 94s
    # for $0.53. Charging cache reads at full weight made it report
    # token_budget_exhausted against the default 120k budget, and an exhausted
    # budget refuses repair rounds — turning a fixable failure into a hard stop.
    records = [
        {
            "status": "completed",
            "usage": {
                "input_tokens": 18,
                "output_tokens": 4142,
                "cache_read_input_tokens": 210675,
                "cache_creation_input_tokens": 31676,
                "num_turns": 11,
                "cost_usd": 0.5311815,
            },
        }
    ]

    summary = summarize_worker_usage(records)

    assert summary["total_tokens"] == 246511, "reporting keeps the honest total"
    assert summary["budget_tokens"] == 35836, "the budget excludes replayed context"
    assert summary["budget_tokens"] < 120_000, "a cheap first-try run must not read as exhausted"


def test_budget_tokens_never_go_negative() -> None:
    from visual_agent.chief_dispatch import summarize_worker_usage

    records = [{"status": "completed", "usage": {"total_tokens": 100, "cache_read_input_tokens": 999}}]

    assert summarize_worker_usage(records)["budget_tokens"] == 0


def test_a_diff_that_trails_a_markdown_fence_still_applies() -> None:
    from visual_agent.chief_dispatch import _extract_unified_diff

    # A model that writes a sentence before its ```diff block sends the whole
    # reply down the "find diff --git" path, which used to read to the end of
    # the message — so the closing fence became a diff line and git apply
    # refused the patch with "corrupt patch at line N".
    reply = (
        "好的，这是补丁：\n"
        "```diff\n"
        "diff --git a/x.js b/x.js\n"
        "--- a/x.js\n"
        "+++ b/x.js\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "```\n"
        "希望有帮助！"
    )

    diff = _extract_unified_diff(reply)

    assert "```" not in diff
    assert diff.strip().endswith("+new")


def test_a_clean_diff_is_left_intact() -> None:
    from visual_agent.chief_dispatch import _extract_unified_diff

    reply = "diff --git a/x.js b/x.js\n--- a/x.js\n+++ b/x.js\n@@ -1 +1 @@\n-old\n+new\n"

    assert _extract_unified_diff(reply).strip().endswith("+new")
