from __future__ import annotations

import json
from pathlib import Path

from visual_agent.chief_dispatch import _dirty_path_ignored, build_worker_command, dispatch_chief_plan, run_dispatch_verification, workspace_record_dirty_prefixes
from visual_agent.chief_engineer import build_chief_plan, chief_plan_to_dict
from visual_agent.chief_plans_store import load_verification, load_worker_records, save_plan
from visual_agent.codex_check import CodexCheckResult, CodexWorkflowCheck
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


def test_build_worker_command_claude_code_is_headless(tmp_path) -> None:
    plan = {"objective": "fix", "acceptance_criteria": [], "selected_workflows": [], "changed_files": []}
    track = {"id": "track_1_claude_code", "agent": "claude-code", "model": "opus"}

    cmd = build_worker_command(plan=plan, track=track, worktree=Path(tmp_path), verification_command="checkpoint codex-check")
    argv = cmd["argv"]

    assert argv[0] == "claude"
    assert "-p" in argv
    assert "--model" in argv and "opus" in argv
    # Headless-safe permission mode so the worker does not hang on prompts.
    assert "--permission-mode" in argv and "acceptEdits" in argv


def test_chief_dispatch_cheap_tier_routes_to_external_backend(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("claude-code",))
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda name: "claude" if name == "claude" else None)
    # Force a backend so the test does not depend on local credential files.
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.resolve_backend_for_tier",
        lambda tier: {"name": "mimo", "model": "mimo-v2.5-pro", "cost_is_savings": True,
                      "env": {"ANTHROPIC_BASE_URL": "https://mimo.example/anthropic", "ANTHROPIC_API_KEY": "tp-secret"}}
        if tier else None,
    )

    captured = {}

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        captured["argv"] = argv
        captured["env"] = env
        log_path.write_text("done", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root, plan_id=plan_id, execute=True, dry_run=False,
        command_runner=fake_runner, codex_runner=passing_codex_result,
    )

    assert payload["status"] == "verified"
    # The worker ran with the MiMo backend env and the overridden model.
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "https://mimo.example/anthropic"
    assert "mimo-v2.5-pro" in captured["argv"]
    assert payload["worker"]["backend"]["name"] == "mimo"
    # The secret token must not appear in the preview.
    assert "tp-secret" not in str(payload["worker"]["backend"])


def test_summarize_worker_usage_separates_spend_and_savings() -> None:
    from visual_agent.chief_dispatch import summarize_worker_usage

    records = [
        {"usage": {"input_tokens": 100, "output_tokens": 200, "cost_usd": 0.05}},
        None,
        {"usage": {"input_tokens": 10, "output_tokens": 20, "cost_usd": 0.30, "cost_is_savings": True}},
    ]
    summary = summarize_worker_usage(records)
    assert summary["total_tokens"] == 330
    assert summary["input_tokens"] == 110
    assert summary["spent_usd"] == 0.05
    assert summary["saved_usd"] == 0.30
    assert summary["attempts_with_usage"] == 2


def test_looks_like_quota_exhaustion() -> None:
    from visual_agent.agent_backends import looks_like_quota_exhaustion

    assert looks_like_quota_exhaustion("Claude usage limit reached")
    assert looks_like_quota_exhaustion("", "HTTP 429 Too Many Requests")
    assert looks_like_quota_exhaustion("Your credit balance is too low")
    assert not looks_like_quota_exhaustion("build succeeded", "all tests passed")


def test_chief_dispatch_quota_failover_to_mimo(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("claude-code",))
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda name: "claude" if name == "claude" else None)
    # No cheap-tier backend initially (run on subscription), but a failover backend exists.
    monkeypatch.setattr("visual_agent.chief_dispatch.resolve_backend_for_tier", lambda tier: None)
    monkeypatch.setattr(
        "visual_agent.chief_dispatch.resolve_failover_backend",
        lambda: {"name": "mimo", "model": "mimo-v2.5-pro", "cost_is_savings": True,
                 "env": {"ANTHROPIC_BASE_URL": "https://mimo.example/anthropic", "ANTHROPIC_API_KEY": "tp-secret"}},
    )

    calls = []

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        calls.append({"argv": argv, "env": env})
        if len(calls) == 1:
            # Subscription attempt: Claude usage limit reached.
            log_path.write_text("Claude usage limit reached. resets at 3pm", encoding="utf-8")
            return {"exit_code": 1, "stdout_tail": "Claude usage limit reached. resets at 3pm", "stderr_tail": ""}
        log_path.write_text("done on mimo", encoding="utf-8")
        return {"exit_code": 0, "stdout_tail": "done on mimo", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root, plan_id=plan_id, execute=True, dry_run=False,
        command_runner=fake_runner, codex_runner=passing_codex_result,
    )

    assert len(calls) == 2  # subscription attempt, then MiMo failover
    assert payload["failover_worker_record"] is not None
    assert payload["failover_worker_record"]["attempt"] == "quota_failover"
    # The failover ran on the MiMo backend env.
    assert calls[1]["env"]["ANTHROPIC_BASE_URL"] == "https://mimo.example/anthropic"
    assert payload["status"] == "verified"
    assert "tp-secret" not in str(payload["worker"].get("failover_backend"))


def test_chief_dispatch_no_failover_on_normal_failure(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("claude-code",))
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda name: "claude" if name == "claude" else None)
    monkeypatch.setattr("visual_agent.chief_dispatch.resolve_backend_for_tier", lambda tier: None)
    failover_called = []
    monkeypatch.setattr("visual_agent.chief_dispatch.resolve_failover_backend", lambda: failover_called.append(1) or None)

    def fake_runner(argv, cwd, timeout_seconds, log_path, env=None):
        # A normal compile error, not a quota block -> must NOT trigger failover.
        log_path.write_text("SyntaxError: bad code", encoding="utf-8")
        return {"exit_code": 1, "stdout_tail": "SyntaxError: bad code", "stderr_tail": ""}

    payload = dispatch_chief_plan(
        workspace_root=workspace.root, plan_id=plan_id, execute=True, dry_run=False,
        command_runner=fake_runner, codex_runner=passing_codex_result,
    )
    assert payload["failover_worker_record"] is None
    assert not failover_called  # quota detector never fired, so failover was not even resolved


def test_chief_dispatch_executes_claude_code_worker(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch, agents=("claude-code",))
    monkeypatch.setattr("visual_agent.chief_dispatch._check_repo", lambda _repo_root: {"status": "ok"})
    monkeypatch.setattr("visual_agent.chief_dispatch.git_dirty_files", lambda _repo_root, **_kwargs: [])
    monkeypatch.setattr("visual_agent.chief_dispatch.create_worktree", lambda **kwargs: {"status": "created", "path": str(kwargs["worktree"]), "branch": kwargs["branch"]})
    # Only `claude` is on PATH, not `codex` — proves the single-point dependency is broken.
    monkeypatch.setattr("visual_agent.chief_dispatch.shutil.which", lambda name: "claude" if name == "claude" else None)

    executed = {}

    def fake_runner(argv, cwd, timeout_seconds, log_path):
        executed["argv"] = argv
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
    assert executed["argv"][0] == "claude" and "-p" in executed["argv"]
    records = load_worker_records(workspace.root, plan_id)
    assert records and records[0]["status"] == "completed"


def test_coverage_changed_files_excludes_checkpoint_artifacts(tmp_path, monkeypatch) -> None:
    from visual_agent import chief_dispatch

    repo = tmp_path
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
        ],
    )

    kept = chief_dispatch._coverage_changed_files(repo_root=repo, workspace_root=workspace)

    assert kept == ["examples/web/checkout_verification_demo.html"]


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


def test_chief_dispatch_dry_run_previews_worktree_and_commands(tmp_path, monkeypatch) -> None:
    workspace, plan_id = saved_ready_plan(tmp_path, monkeypatch)

    payload = dispatch_chief_plan(workspace_root=workspace.root, plan_id=plan_id, dry_run=True, execute=False)

    assert payload["status"] == "preview"
    assert payload["dry_run"] is True
    assert "codex exec" in payload["worker"]["command"]
    assert "checkpoint codex-check" in payload["verification"]["command"]
    assert payload["worktree"]["path"].endswith("track-1-codex")


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


def test_workspace_record_dirty_prefixes_ignore_whole_workspace(tmp_path) -> None:
    # The workspace is DevPacer's runtime dir; the dirty gate must ignore all of
    # it, including the fresh-project case where git collapses the entire
    # untracked ".agent-workspace/" into a single porcelain entry.
    repo = tmp_path / "repo"
    workspace = repo / ".agent-workspace"
    prefixes = workspace_record_dirty_prefixes(repo_root=repo, workspace_root=workspace)

    assert prefixes == (".agent-workspace/",)
    assert _dirty_path_ignored("?? .agent-workspace/", prefixes)
    assert _dirty_path_ignored("?? .agent-workspace/missions/m1/plan.json", prefixes)
    assert not _dirty_path_ignored("?? src/calc.py", prefixes)
