from __future__ import annotations

import json
import subprocess

import pytest

from visual_agent.pacer_support import build_pacer_support_snapshot, inspect_codex_account, support_snapshot_to_markdown
from visual_agent.pacer_management import handle_pacer_management
from visual_agent.user_profile import load_user_profile


def _verification_summary(
    run_id: str,
    *,
    launch_id: str = "launch-test",
    elapsed_seconds: float = 1.0,
    commands: list[list[str]] | None = None,
) -> dict[str, object]:
    verification_commands = commands or [["python", "-m", "pytest", "-q"]]
    records = [
        {"status": "passed", "command": command, "exit_code": 0}
        for command in verification_commands
    ]
    step_classes = ["compile" if "compileall" in command else "test" for command in verification_commands]
    return {
        "schema_version": 1,
        "kind": "pacer_verification_batch",
        "source_tool": "run_pacer_verification",
        "policy_version": 1,
        "run_id": run_id,
        "launch_id": launch_id,
        "status": "passed",
        "elapsed_seconds": elapsed_seconds,
        "requested_steps": len(records),
        "executed_steps": len(records),
        "skipped_steps": [],
        "passed": len(records),
        "failed": 0,
        "timed_out": 0,
        "not_applicable": 0,
        "step_classes": step_classes,
        "records": records,
    }


def test_codex_account_probe_returns_auth_type_without_key() -> None:
    secret_fragment = "sk-secret-value-1234"

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=f"Logged in using an API key - {secret_fragment}", stderr="")

    payload = inspect_codex_account(executable="codex.exe", runner=runner, use_cache=False)

    assert payload["authenticated"] is True
    assert payload["auth_method"] == "api_key"
    assert secret_fragment not in json.dumps(payload)


def test_codex_account_probe_does_not_treat_not_logged_in_as_authenticated() -> None:
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="Not logged in", stderr="")

    payload = inspect_codex_account(executable="codex.exe", runner=runner, use_cache=False)

    assert payload["authenticated"] is False
    assert payload["status"] == "not_authenticated"


def test_support_snapshot_includes_session_handoffs(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    handoff_root = workspace / "pacer_native" / "session_handoffs"
    handoff_root.mkdir(parents=True)
    handoff_path = handoff_root / "launch-handoff.json"
    handoff_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pacer_interactive_session_handoff",
                "launch_id": "launch-handoff",
                "status": "running",
                "reason": "launch_started",
                "recorded_at": "2026-07-28T13:20:00+00:00",
                "repo_root": str(tmp_path.resolve()),
                "rollout": {
                    "status": "attributed",
                    "source_files": 1,
                    "sessions": [{"session_id": "session-123"}],
                },
                "resume_hints": ["pacer code resume session-123"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace)

    handoffs = payload["session_handoffs"]
    assert handoffs["total"] == 1
    assert handoffs["latest"]["launch_id"] == "launch-handoff"
    assert handoffs["latest"]["session_count"] == 1
    assert handoffs["latest"]["resume_hints"] == ["pacer code resume session-123"]
    assert handoffs["latest"]["path"] == str(handoff_path)


def test_support_snapshot_includes_managed_task_checkpoints(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    task_root = workspace / "pacer_native" / "managed_tasks"
    task_root.mkdir(parents=True)
    checkpoint_path = task_root / "attempt-1.json"
    checkpoint_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "pacer_managed_task_checkpoint",
                "attempt_id": "attempt-1",
                "status": "running",
                "reason": "",
                "objective": "修复 demo 项目并跑测试",
                "repo_root": str(tmp_path.resolve()),
                "program_id": "program-1",
                "plan_path": str(workspace / "intake" / "task.md"),
                "test_command": "python -m pytest -q",
                "created_at": "2026-07-28T13:30:00+00:00",
                "updated_at": "2026-07-28T13:31:00+00:00",
                "tasks": [{"task_id": "task-001", "mission_id": "mission-1", "status": "running"}],
                "worker_runs": [{"ran": True}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace)
    markdown = support_snapshot_to_markdown(payload)

    managed = payload["managed_tasks"]
    assert managed["total"] == 1
    assert managed["latest"]["attempt_id"] == "attempt-1"
    assert managed["latest"]["task_count"] == 1
    assert managed["latest"]["worker_run_count"] == 1
    assert managed["latest"]["path"] == str(checkpoint_path)
    assert "Latest managed task: running - 修复 demo 项目并跑测试" in markdown


def test_support_snapshot_aggregates_native_outcomes_and_commands(tmp_path, monkeypatch) -> None:
    from visual_agent.pacer_launch_context import initialize_active_launch, write_launch_liveness

    workspace = tmp_path / ".agent-workspace"
    native = workspace / "pacer_native"
    native.mkdir(parents=True)
    repo = workspace.parent.resolve()
    run_id = "20260713-130000-native123"
    (native / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(repo),
                "recorded_at": "2026-07-13T13:00:00+00:00",
                "goal": "native task",
                "summary": "done",
                "verification": f"run_id={run_id}",
                "status": "completed",
                "task_review": {
                    "schema_version": 1,
                    "kind": "pacer_task_review",
                    "valid": True,
                    "verdict": "approved",
                    "trust": "with_limits",
                    "evidence_integrity": "verified",
                    "acceptance_adequacy": "insufficient",
                    "product_verdict": "indeterminate",
                    "acceptance_assessment": {
                        "schema_version": 1,
                        "standard_source": "template",
                        "standard_digest": "digest-1",
                        "digest_verified": True,
                        "adequacy": "insufficient",
                        "final_phase": True,
                        "required_step_classes": ["test"],
                        "observed_step_classes": ["test", "compile"],
                        "missing_step_classes": [],
                        "missing_commands": [],
                        "reason_codes": ["acceptance_standard_template_only"],
                    },
                    "errors": [],
                    "warnings": [],
                    "user_report": {
                        "headline": "审查通过，可以交付。",
                        "goal": "native task",
                        "completed": ["完成 native task"],
                        "not_completed": [],
                        "evidence": ["验收 tests：通过"],
                        "blocking_issues": [],
                        "risks": [],
                        "can_trust": "with_limits",
                        "evidence_integrity": "verified",
                        "acceptance_adequacy": "insufficient",
                        "product_verdict": "indeterminate",
                        "next_action": "补充验收标准。",
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = native / "commands" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                **_verification_summary(
                    run_id,
                    launch_id="launch-status",
                    elapsed_seconds=12.5,
                    commands=[
                        ["python", "-m", "pytest", "-q"],
                        ["python", "-m", "compileall", "-q", "src"],
                    ],
                ),
                "run_dir": str(run_dir),
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {"provider": "custom", "model": "gpt-test"})
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=native / "launches" / "launch-status.json",
        launch={"launch_id": "launch-status", "repo_root": str(repo)},
    )
    write_launch_liveness(
        workspace,
        "launch-status",
        {"state": "stalled", "monitoring": True, "lifecycle_status": "running"},
    )

    payload = build_pacer_support_snapshot(workspace)
    markdown = support_snapshot_to_markdown(payload)

    assert payload["memory"]["total_outcomes"] == 1
    assert payload["memory"]["latest"]["evidence_level"] == "verified_batch"
    latest_review = payload["memory"]["latest"]["task_review"]
    assert latest_review["trust"] == "with_limits"
    assert latest_review["evidence_integrity"] == "verified"
    assert latest_review["acceptance_adequacy"] == "insufficient"
    assert latest_review["product_verdict"] == "indeterminate"
    assert latest_review["acceptance_assessment"]["standard_source"] == "template"
    assert latest_review["acceptance_assessment"]["digest_verified"] is True
    assert latest_review["user_report"]["product_verdict"] == "indeterminate"
    assert payload["commands"]["passed_runs"] == 1
    assert payload["commands"]["passed_steps"] == 2
    assert payload["launches"]["active"]["liveness"]["state"] == "stalled"
    active_assessment = payload["launches"]["active"]["assessment"]
    assert active_assessment["passed"] is False
    assert set(active_assessment["pillars"]) == {
        "routing",
        "memory",
        "managed",
        "acceptance",
        "dogfood",
    }
    assert "lifecycle=running, liveness=stalled" in markdown
    assert "Pacer 任务审查：审查通过，可以交付。" in markdown
    assert "产品结论：无法判定" in markdown


def test_support_keeps_trusted_documentation_compile_batch_verified(tmp_path, monkeypatch) -> None:
    from visual_agent.task_review import build_task_contract

    workspace = tmp_path / ".agent-workspace"
    native = workspace / "pacer_native"
    native.mkdir(parents=True)
    run_id = "20260715-120000-doccompile"
    launch_id = "launch-documentation"
    goal = (
        "更新 README.md，增加 Usage 小节，写明 python app.py 启动命令，只修改该文档"
        "并使用 python -m compileall -q app.py 验证。"
    )
    (native / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(tmp_path.resolve()),
                "recorded_at": "2026-07-15T12:00:00+00:00",
                "goal": goal,
                "summary": "README usage updated",
                "verification": f"run_id={run_id}; status=passed",
                "status": "completed",
                "evidence_level": "verified_batch",
                "batch_run_id": run_id,
                "launch_id": launch_id,
                "task_review": {
                    "schema_version": 1,
                    "kind": "pacer_task_review",
                    "valid": True,
                    "verdict": "approved",
                    "trust": "yes",
                    "task_contract": build_task_contract(goal),
                    "errors": [],
                    "warnings": [],
                    "user_report": {"can_trust": "yes"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = native / "commands" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            _verification_summary(
                run_id,
                launch_id=launch_id,
                commands=[["python", "-m", "compileall", "-q", "app.py"]],
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace)

    assert payload["commands"]["verified_runs"] == 1
    assert payload["commands"]["invalid_verification_runs"] == 0
    assert payload["memory"]["latest"]["evidence_level"] == "verified_batch"
    assert payload["memory"]["latest"]["verification_batch_valid"] is True
    assert payload["memory"]["latest"]["verification_errors"] == []


@pytest.mark.parametrize("summary_kind", ["legacy", "ordinary_command"])
def test_support_does_not_upgrade_non_verification_passed_summary(
    tmp_path,
    monkeypatch,
    summary_kind,
) -> None:
    workspace = tmp_path / ".agent-workspace"
    native = workspace / "pacer_native"
    native.mkdir(parents=True)
    run_id = "20260713-130100-legacy"
    (native / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(tmp_path.resolve()),
                "recorded_at": "2026-07-13T13:01:00+00:00",
                "goal": "legacy command batch",
                "summary": "must remain unverified",
                "verification": f"run_id={run_id}",
                "status": "completed",
                "evidence_level": "verified_batch",
                "batch_run_id": run_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = native / "commands" / run_id
    run_dir.mkdir(parents=True)
    summary = {
        "run_id": run_id,
        "status": "passed",
        "requested_steps": 1,
        "executed_steps": 1,
        "passed": 1,
        "failed": 0,
        "timed_out": 0,
        "not_applicable": 0,
    }
    if summary_kind == "ordinary_command":
        summary.update(
            {
                "kind": "pacer_command_batch",
                "source_tool": "run_pacer_commands",
                "policy_version": 1,
                "launch_id": "launch-test",
                "step_classes": ["test"],
                "records": [
                    {
                        "status": "passed",
                        "command": ["python", "-m", "pytest", "-q"],
                        "exit_code": 0,
                    }
                ],
            }
        )
    (run_dir / "summary.json").write_text(
        json.dumps(summary),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace)

    assert payload["commands"]["passed_runs"] == 1
    assert payload["commands"]["verified_runs"] == 0
    assert payload["commands"]["verified_run_ids"] == []
    assert payload["commands"]["invalid_verification_runs"] == 1
    assert payload["memory"]["latest"]["evidence_level"] == "self_reported"
    assert payload["memory"]["latest"]["verification_batch_valid"] is False
    assert payload["memory"]["latest"]["verification_errors"]


def test_support_snapshot_reconciles_fake_missing_launcher_before_counting(tmp_path, monkeypatch) -> None:
    from visual_agent.pacer_launch_context import (
        initialize_active_launch,
        read_reconciled_active_launch,
    )

    workspace = tmp_path / ".agent-workspace"
    repo = tmp_path.resolve()
    manifest = workspace / "pacer_native" / "launches" / "launch-orphan.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": "launch-orphan", "repo_root": str(repo), "launcher_pid": 424242},
    )
    probes: list[int] = []

    def reconciled(root):
        return read_reconciled_active_launch(
            root,
            process_probe=lambda pid: probes.append(pid) or False,
            reconcile_interval_seconds=0,
        )

    monkeypatch.setattr("visual_agent.pacer_support.read_reconciled_active_launch", reconciled)
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "subscription"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace)

    assert probes == [424242]
    assert payload["launches"]["running"] == 0
    assert payload["launches"]["active"]["lifecycle_status"] == "orphaned"
    assert payload["launches"]["active"]["liveness"]["monitoring"] is False
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "orphaned"


def test_support_snapshot_merges_split_storage_without_double_counting(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    canonical = workspace / "pacer_native"
    misplaced = tmp_path / "pacer_native"
    canonical.mkdir(parents=True)
    misplaced.mkdir(parents=True)
    repo = tmp_path.resolve()
    completed = {
        "repo_root": str(repo),
        "recorded_at": "2026-07-13T13:00:00+00:00",
        "goal": "completed task",
        "summary": "done",
        "verification": "run_id=20260713-130000-shared",
        "status": "completed",
    }
    failed = {
        "repo_root": str(repo),
        "recorded_at": "2026-07-13T14:00:00+00:00",
        "goal": "failed task",
        "summary": "failed safely",
        "verification": "run_id=20260713-140000-failed",
        "status": "failed",
    }
    blocked = {
        "repo_root": str(repo),
        "recorded_at": "2026-07-13T15:00:00+00:00",
        "goal": "blocked task",
        "summary": "waiting for external input",
        "verification": "not run",
        "status": "blocked",
    }
    (canonical / "history.jsonl").write_text(json.dumps(completed) + "\n", encoding="utf-8")
    (misplaced / "history.jsonl").write_text(
        "\n".join(json.dumps(item) for item in (completed, failed, blocked)) + "\n",
        encoding="utf-8",
    )

    shared_summary = _verification_summary(
        "20260713-130000-shared",
        elapsed_seconds=4,
    )
    for native in (canonical, misplaced):
        run_dir = native / "commands" / shared_summary["run_id"]
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(json.dumps(shared_summary), encoding="utf-8")
    failed_run_dir = misplaced / "commands" / "20260713-140000-failed"
    failed_run_dir.mkdir(parents=True)
    (failed_run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": failed_run_dir.name,
                "status": "failed",
                "elapsed_seconds": 2,
                "executed_steps": 1,
                "passed": 0,
                "failed": 1,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace)
    markdown = support_snapshot_to_markdown(payload)

    assert payload["storage"]["status"] == "split"
    assert payload["storage"]["canonical_has_data"] is True
    assert payload["storage"]["misplaced_has_data"] is True
    assert payload["memory"]["total_outcomes"] == 3
    assert payload["memory"]["completed"] == 1
    assert payload["memory"]["failed"] == 1
    assert payload["memory"]["blocked"] == 1
    assert payload["memory"]["latest"]["status"] == "blocked"
    assert payload["commands"]["total_runs"] == 2
    assert payload["commands"]["passed_runs"] == 1
    assert payload["commands"]["failed_runs"] == 1
    assert payload["commands"]["latest"]["run_id"] == "20260713-140000-failed"
    assert "WARNING: Pacer evidence is split" in markdown
    assert "1 failed, 1 blocked" in markdown
    assert "Latest command run: failed - 20260713-140000-failed" in markdown


def test_support_snapshot_recovers_misplaced_only_storage(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    misplaced = tmp_path / "pacer_native"
    misplaced.mkdir(parents=True)
    (misplaced / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(tmp_path.resolve()),
                "recorded_at": "2026-07-13T16:00:00+00:00",
                "goal": "misplaced outcome",
                "summary": "recovered",
                "verification": "not run",
                "status": "failed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace)

    assert payload["storage"]["status"] == "misplaced"
    assert payload["memory"]["total_outcomes"] == 1
    assert payload["memory"]["latest"]["goal"] == "misplaced outcome"
    assert str(misplaced / "history.jsonl") in payload["memory"]["source_paths"]


def test_support_snapshot_prefers_canonical_conflict_and_excludes_it_from_passed_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / ".agent-workspace"
    canonical = workspace / "pacer_native"
    legacy = tmp_path / "pacer_native"
    canonical.mkdir(parents=True)
    legacy.mkdir(parents=True)
    run_id = "20260713-170000-conflict"
    (canonical / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(tmp_path.resolve()),
                "recorded_at": "2026-07-13T17:00:00+00:00",
                "goal": "conflicted verification",
                "summary": "must not become verified",
                "verification": f"run_id={run_id}",
                "status": "completed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    canonical_run_dir = canonical / "commands" / run_id
    canonical_run_dir.mkdir(parents=True)
    (canonical_run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "passed",
                "executed_steps": 1,
                "passed": 1,
                "run_dir": str(canonical_run_dir),
            }
        ),
        encoding="utf-8",
    )
    legacy_run_dir = legacy / "commands" / run_id
    legacy_run_dir.mkdir(parents=True)
    (legacy_run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "failed",
                "executed_steps": 1,
                "passed": 0,
                "failed": 1,
                "run_dir": str(legacy_run_dir),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace, repo_root=tmp_path)

    assert payload["storage"]["status"] == "inconsistent"
    assert payload["storage"]["conflicted_run_ids"] == [run_id]
    assert payload["commands"]["conflicted_run_ids"] == [run_id]
    assert payload["commands"]["latest"]["status"] == "passed"
    assert payload["commands"]["latest"]["run_dir"] == str(canonical_run_dir)
    assert payload["commands"]["passed_run_ids"] == []
    assert payload["memory"]["latest"]["evidence_level"] == "self_reported"


def test_support_snapshot_does_not_merge_legacy_data_for_custom_workspace(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    workspace = repo / ".custom-agent-workspace"
    canonical = workspace / "pacer_native"
    legacy = repo / "pacer_native"
    canonical.mkdir(parents=True)
    legacy.mkdir(parents=True)
    canonical_outcome = {
        "repo_root": str(repo.resolve()),
        "recorded_at": "2026-07-13T17:10:00+00:00",
        "goal": "custom workspace outcome",
        "summary": "canonical only",
        "verification": "not run",
        "status": "completed",
    }
    legacy_outcome = {
        "repo_root": str(repo.resolve()),
        "recorded_at": "2026-07-13T17:20:00+00:00",
        "goal": "unrelated legacy outcome",
        "summary": "must remain isolated",
        "verification": "not run",
        "status": "failed",
    }
    (canonical / "history.jsonl").write_text(json.dumps(canonical_outcome) + "\n", encoding="utf-8")
    (legacy / "history.jsonl").write_text(json.dumps(legacy_outcome) + "\n", encoding="utf-8")
    for native, run_id in ((canonical, "20260713-171000-custom"), (legacy, "20260713-172000-legacy")):
        run_dir = native / "commands" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps({"run_id": run_id, "status": "passed", "executed_steps": 1, "passed": 1}),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace, repo_root=repo)

    assert payload["storage"]["status"] == "healthy"
    assert payload["storage"]["legacy_eligible"] is False
    assert payload["storage"]["misplaced_has_data"] is False
    assert payload["memory"]["total_outcomes"] == 1
    assert payload["memory"]["latest"]["goal"] == "custom workspace outcome"
    assert payload["commands"]["total_runs"] == 1
    assert payload["commands"]["latest"]["run_id"] == "20260713-171000-custom"


def test_support_snapshot_exposes_prompt_free_rollout_telemetry(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    launch_dir = workspace / "pacer_native" / "launches"
    launch_dir.mkdir(parents=True)
    (launch_dir / "launch-1.json").write_text(
        json.dumps(
            {
                "launch_id": "launch-1",
                "started_at": "2026-07-13T16:00:00+00:00",
                "status": "completed",
                "rollout_telemetry": {
                    "status": "captured",
                    "attribution_confidence": "high",
                    "source_files": 3,
                    "usage": {
                        "input_tokens": 1200,
                        "cached_input_tokens": 900,
                        "output_tokens": 80,
                        "reasoning_output_tokens": 20,
                        "total_tokens": 1280,
                    },
                    "compactions": {"count": 2, "timestamps": ["private"]},
                    "agents": {
                        "total": 2,
                        "completed": 1,
                        "interrupted": 1,
                        "active": 0,
                        "timeline": [
                            {
                                "depth": 1,
                                "started_at": "2026-07-13T16:01:00+00:00",
                                "completed_at": "2026-07-13T16:02:00+00:00",
                                "elapsed_seconds": 60,
                                "status": "completed",
                                "prompt": "must not escape",
                                "thread_id": "private-thread",
                            }
                        ],
                    },
                    "prompt": "secret task",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace)
    markdown = support_snapshot_to_markdown(payload)
    serialized = json.dumps(payload["telemetry"])

    assert payload["telemetry"]["usage"]["cached_input_tokens"] == 900
    assert payload["telemetry"]["compactions"]["count"] == 2
    assert payload["telemetry"]["agents"]["completed"] == 1
    assert "input 1,200, cached 900, output 80" in markdown
    assert "secret task" not in serialized
    assert "private-thread" not in serialized


def test_support_snapshot_rejects_conflicting_legacy_outcome_for_same_batch(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    canonical = workspace / "pacer_native"
    legacy = tmp_path / "pacer_native"
    run_id = "20260713-170000-outcome-conflict"
    for native in (canonical, legacy):
        run_dir = native / "commands" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps({"run_id": run_id, "status": "passed", "executed_steps": 1, "passed": 1}),
            encoding="utf-8",
        )
    canonical_outcome = {
        "repo_root": str(tmp_path.resolve()),
        "recorded_at": "2026-07-13T17:00:00+00:00",
        "goal": "canonical failure",
        "summary": "failed honestly",
        "verification": f"run_id={run_id}",
        "status": "failed",
        "evidence_level": "verified_failed_batch",
        "batch_run_id": run_id,
    }
    legacy_outcome = {
        **canonical_outcome,
        "recorded_at": "2026-07-13T17:01:00+00:00",
        "goal": "legacy false success",
        "summary": "must not override canonical",
        "status": "completed",
        "evidence_level": "verified_batch",
    }
    (canonical / "history.jsonl").write_text(json.dumps(canonical_outcome) + "\n", encoding="utf-8")
    (legacy / "history.jsonl").write_text(json.dumps(legacy_outcome) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace)

    assert payload["storage"]["status"] == "inconsistent"
    assert payload["storage"]["outcome_conflicted_run_ids"] == [run_id]
    assert payload["commands"]["passed_run_ids"] == []
    assert payload["memory"]["latest"]["status"] == "failed"
    assert payload["memory"]["latest"]["goal"] == "canonical failure"


def test_support_snapshot_rejects_old_format_outcome_conflict_inferred_from_verification(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    canonical = workspace / "pacer_native"
    legacy = tmp_path / "pacer_native"
    run_id = "20260713-171000-old-outcome"
    for native in (canonical, legacy):
        run_dir = native / "commands" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps({"run_id": run_id, "status": "passed", "executed_steps": 1, "passed": 1}),
            encoding="utf-8",
        )
    canonical_outcome = {
        "repo_root": str(tmp_path.resolve()),
        "recorded_at": "2026-07-13T17:10:00+00:00",
        "goal": "old canonical failure",
        "summary": "failed honestly",
        "verification": f"Pacer run_id={run_id}",
        "status": "failed",
    }
    legacy_outcome = {
        **canonical_outcome,
        "recorded_at": "2026-07-13T17:11:00+00:00",
        "goal": "old legacy false success",
        "summary": "must not override",
        "status": "completed",
    }
    (canonical / "history.jsonl").write_text(json.dumps(canonical_outcome) + "\n", encoding="utf-8")
    (legacy / "history.jsonl").write_text(json.dumps(legacy_outcome) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace)

    assert payload["storage"]["status"] == "inconsistent"
    assert payload["storage"]["outcome_conflicted_run_ids"] == [run_id]
    assert payload["commands"]["passed_run_ids"] == []
    assert payload["memory"]["latest"]["goal"] == "old canonical failure"
    assert payload["memory"]["latest"]["status"] == "failed"


def test_support_snapshot_infers_repo_for_external_custom_workspace(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "external" / "pacer-data"
    native = workspace / "pacer_native"
    native.mkdir(parents=True)
    repo.mkdir()
    (native / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(repo.resolve()),
                "recorded_at": "2026-07-13T18:00:00+00:00",
                "goal": "external custom workspace",
                "summary": "repo inferred from canonical ledger",
                "verification": "review",
                "status": "failed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace)

    assert payload["repo_root"] == str(repo.resolve())
    assert payload["memory"]["total_outcomes"] == 1
    assert payload["storage"]["legacy_eligible"] is False


def test_support_snapshot_infers_repo_for_external_workspace_named_agent_workspace(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "external" / ".agent-workspace"
    native = workspace / "pacer_native"
    native.mkdir(parents=True)
    repo.mkdir()
    (native / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(repo.resolve()),
                "recorded_at": "2026-07-13T18:30:00+00:00",
                "goal": "external same-name workspace",
                "summary": "ledger wins over directory name",
                "verification": "review",
                "status": "failed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    payload = build_pacer_support_snapshot(workspace)

    assert payload["repo_root"] == str(repo.resolve())
    assert payload["memory"]["latest"]["goal"] == "external same-name workspace"


def test_pacer_status_accepts_explicit_external_workspace_and_repo(tmp_path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    workspace = tmp_path / "external-workspace"
    repo.mkdir()
    (workspace / "pacer_native").mkdir(parents=True)
    (workspace / "pacer_native" / "history.jsonl").write_text(
        json.dumps(
            {
                "repo_root": str(repo.resolve()),
                "recorded_at": "2026-07-13T19:00:00+00:00",
                "goal": "explicit status workspace",
                "summary": "visible",
                "verification": "review",
                "status": "failed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "visual_agent.pacer_support.inspect_codex_account",
        lambda: {"installed": True, "authenticated": True, "auth_method": "api_key", "status": "authenticated"},
    )
    monkeypatch.setattr("visual_agent.pacer_support.load_codex_user_defaults", lambda: {})

    result = handle_pacer_management(
        ["status", "--workspace-root", str(workspace), "--repo-root", str(repo), "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["repo_root"] == str(repo.resolve())
    assert payload["memory"]["latest"]["goal"] == "explicit status workspace"


def test_pacer_management_routes_quality_commands_to_pacer_cli(monkeypatch) -> None:
    from visual_agent import cli

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "main", lambda argv: calls.append(argv) or 23)

    result = handle_pacer_management(
        ["pacer-release-manifest-check", "--manifest", ".pacer/release.json"]
    )

    assert result == 23
    assert calls == [
        ["pacer-release-manifest-check", "--manifest", ".pacer/release.json"]
    ]


def test_pacer_account_bind_saves_only_local_profile(tmp_path, monkeypatch, capsys) -> None:
    profile_path = tmp_path / "profile.json"
    monkeypatch.setenv("PACER_PROFILE_PATH", str(profile_path))

    result = handle_pacer_management(
        ["account", "bind", "--email", "user@example.com", "--name", "User", "--organization", "Example"]
    )

    assert result == 0
    assert load_user_profile().email == "user@example.com"
    assert "Codex authentication remains separate" in capsys.readouterr().out
