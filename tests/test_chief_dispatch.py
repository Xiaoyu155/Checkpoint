from __future__ import annotations

from visual_agent.chief_dispatch import dispatch_chief_plan, run_dispatch_verification, workspace_record_dirty_prefixes
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


def test_workspace_record_dirty_prefixes_ignore_generated_records_only(tmp_path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / ".agent-workspace"
    prefixes = workspace_record_dirty_prefixes(repo_root=repo, workspace_root=workspace)

    assert ".agent-workspace/chief_plans/" in prefixes
    assert ".agent-workspace/missions/" in prefixes
    assert ".agent-workspace/workflows/" not in prefixes
