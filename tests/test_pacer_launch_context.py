from __future__ import annotations

import json
from pathlib import Path
import os
import subprocess
import sys
import threading

import pytest

import visual_agent.pacer_launch_context as launch_context_module
from visual_agent.codex_rollout_telemetry import RolloutSnapshot
from visual_agent.pacer_launch_context import (
    LAUNCH_STATE_LOCK_TIMEOUT_SECONDS,
    bind_active_project,
    _commit_orphaned_launch,
    discover_pacer_runtime_roots,
    initialize_active_launch,
    launch_context_path,
    latest_pending_recovery_capsule,
    load_rollout_baseline,
    load_task_source_baseline,
    find_active_launch,
    read_active_launch,
    read_launch_liveness,
    read_reconciled_active_launch,
    record_completion_rejection,
    register_completion_attempt,
    register_trusted_task_source_baseline,
    recover_orphaned_launches,
    resolve_python_runtime,
    resolve_recovery_capsule,
    save_rollout_baseline,
    save_task_source_baseline,
    task_source_baseline_digest,
    trusted_task_source_baseline_errors,
    update_active_launch,
    update_pillar,
    write_active_launch,
    write_launch_liveness,
    write_context_recovery_capsule,
)


def _python_capability(*, pytest_available: bool = True) -> dict[str, object]:
    return {
        "available": True,
        "pytest_available": pytest_available,
        "version": "3.11.9",
        "probe_status": "ok",
        "probe_elapsed_ms": 3,
    }


def test_launch_state_lock_timeout_is_bounded_for_native_startup() -> None:
    assert 0 < LAUNCH_STATE_LOCK_TIMEOUT_SECONDS <= 1.0


def test_completion_attempts_are_persisted_and_bounded(tmp_path: Path) -> None:
    workspace = tmp_path / ".agent-workspace"
    launch_id = "launch-completion-limit"
    manifest = workspace / "pacer_native" / "launches" / f"{launch_id}.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": launch_id, "repo_root": str(tmp_path)},
    )

    first = register_completion_attempt(workspace, launch_id=launch_id, max_attempts=3)
    second = register_completion_attempt(workspace, launch_id=launch_id, max_attempts=3)
    rejected = record_completion_rejection(
        workspace,
        launch_id=launch_id,
        reason_codes=["claim_without_relevant_acceptance"],
        retryable=True,
    )
    third = register_completion_attempt(workspace, launch_id=launch_id, max_attempts=3)
    exhausted = register_completion_attempt(workspace, launch_id=launch_id, max_attempts=3)

    assert first["attempts"] == 1
    assert second["attempts"] == 2
    assert rejected["status"] == "correction_required"
    assert rejected["last_rejection_codes"] == ["claim_without_relevant_acceptance"]
    assert third["attempts"] == 3
    assert third["retryable"] is False
    assert exhausted["attempts"] == 4
    assert exhausted["status"] == "attempts_exhausted"
    assert read_active_launch(workspace, launch_id=launch_id)["completion_control"] == exhausted


def test_python_runtime_prefers_project_venv_without_recursive_search(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    interpreter = repo / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("fixture", encoding="utf-8")
    probes: list[Path] = []

    runtime = resolve_python_runtime(
        repo,
        environment={},
        path_lookup=lambda _name: None,
        capability_probe=lambda path: probes.append(path) or _python_capability(),
        include_pacer_runtime_roots=False,
    )

    assert runtime["executable"] == str(interpreter)
    assert runtime["source"] == "project_venv"
    assert runtime["pytest_available"] is True
    assert runtime["trusted_venv"] is True
    assert runtime["bound_repo_root"] == str(repo.resolve())
    assert probes == [interpreter]


def test_python_runtime_uses_pacer_launcher_before_untrusted_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    launcher = tmp_path / "pacer-venv" / "Scripts" / "python.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("fixture", encoding="utf-8")
    path_python = tmp_path / "system" / "python.exe"
    path_python.parent.mkdir()
    path_python.write_text("fixture", encoding="utf-8")
    probes: list[Path] = []

    runtime = resolve_python_runtime(
        repo,
        pacer_executable=launcher,
        environment={},
        path_lookup=lambda name: str(path_python) if name == "python" else None,
        capability_probe=lambda path: probes.append(path) or _python_capability(),
        include_pacer_runtime_roots=False,
    )

    assert runtime["executable"] == str(launcher)
    assert runtime["source"] == "pacer_launcher"
    assert runtime["trusted_venv"] is True
    assert runtime["root"] == str(launcher.parent.parent)
    assert runtime["bound_repo_root"] == str(repo.resolve())
    assert probes == [launcher]


def test_python_runtime_does_not_borrow_unrelated_parent_venv(tmp_path: Path) -> None:
    interpreter = tmp_path / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("fixture", encoding="utf-8")
    repo = tmp_path / "unrelated-project"
    repo.mkdir()

    runtime = resolve_python_runtime(
        repo,
        environment={},
        path_lookup=lambda _name: None,
        include_pacer_runtime_roots=False,
    )

    assert runtime["available"] is False
    assert runtime["probe_status"] == "not_found"
    assert runtime["bound_repo_root"] == str(repo.resolve())


def test_python_runtime_accepts_explicit_override_without_trusting_its_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    interpreter = tmp_path / "managed-python.exe"
    interpreter.write_text("fixture", encoding="utf-8")

    runtime = resolve_python_runtime(
        repo,
        environment={"PACER_PYTHON": str(interpreter)},
        path_lookup=lambda _name: None,
        capability_probe=lambda _path: _python_capability(pytest_available=False),
        include_pacer_runtime_roots=False,
    )

    assert runtime["source"] == "environment"
    assert runtime["available"] is True
    assert runtime["pytest_available"] is False
    assert runtime["trusted_venv"] is False
    assert runtime["bound_repo_root"] == str(repo.resolve())


def test_python_runtime_path_probe_exposes_missing_pytest(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    interpreter = tmp_path / "python.exe"
    interpreter.write_text("fixture", encoding="utf-8")

    runtime = resolve_python_runtime(
        repo,
        environment={},
        path_lookup=lambda name: str(interpreter) if name == "python" else None,
        capability_probe=lambda _path: _python_capability(pytest_available=False),
        include_pacer_runtime_roots=False,
    )

    assert runtime["source"] == "path"
    assert runtime["available"] is True
    assert runtime["pytest_available"] is False
    assert runtime["bound_repo_root"] == str(repo.resolve())


def test_pacer_runtime_root_discovery_is_bounded_to_module_ancestors(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    module = root / "src" / "visual_agent" / "pacer_launch_context.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='pacer'\n", encoding="utf-8")
    unrelated = root / "nested" / "other"
    unrelated.mkdir(parents=True)
    (unrelated / ".git").mkdir()

    roots = discover_pacer_runtime_roots(module_path=module)

    assert roots == [root.resolve()]


def _launch(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "source"
    project = root / "app"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='app'\n", encoding="utf-8")
    workspace = root / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-1.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(workspace_root=workspace, manifest_path=manifest, launch={"launch_id": "launch-1", "repo_root": str(root), "auto_compact_token_limit": 96000})
    return workspace, project


def test_binds_only_project_that_existed_at_launch(tmp_path: Path) -> None:
    workspace, project = _launch(tmp_path)
    assert read_active_launch(workspace)["pillars"]["dogfood"]["active"] is False
    assert read_active_launch(workspace)["auto_compact_token_limit"] == 96000
    active = bind_active_project(workspace_root=workspace, repo_root=project, reason="memory")
    assert active["project_root"] == str(project.resolve())
    assert active["pillars"]["dogfood"]["active"] is False
    assert active["pillars"]["dogfood"]["state"] == "source_contract_ready"
    assert active["pillars"]["dogfood"]["project_existed_at_launch"] is True
    assert active["pillars"]["managed"]["active"] is False
    replacement = project.parent / "replacement"
    replacement.mkdir()
    (replacement / "pyproject.toml").write_text("[project]\nname='copy'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not an existing project at launch"):
        bind_active_project(workspace_root=workspace, repo_root=replacement, reason="memory")
    found_workspace, _ = find_active_launch(
        repo_root=replacement,
        suggested_workspace=replacement / ".agent-workspace",
        process_probe=lambda _pid: True,
    )
    assert found_workspace == workspace.resolve()


@pytest.mark.parametrize("name", ["app-copy", "app-backup", "app-worktree", "临时副本"])
def test_refuses_alternate_project_roots(tmp_path: Path, name: str) -> None:
    root = tmp_path / "source"
    alternate = root / name
    alternate.mkdir(parents=True)
    (alternate / "package.json").write_text("{}", encoding="utf-8")
    workspace = root / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-1.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(workspace_root=workspace, manifest_path=manifest, launch={"launch_id": "launch-1", "repo_root": str(root)})
    with pytest.raises(ValueError, match="refuses worktree"):
        bind_active_project(workspace_root=workspace, repo_root=alternate, reason="memory")


def test_project_binding_cannot_drift(tmp_path: Path) -> None:
    workspace, project = _launch(tmp_path)
    other = project.parent / "other"
    other.mkdir()
    (other / "go.mod").write_text("module other\n", encoding="utf-8")
    active = read_active_launch(workspace)
    active["known_project_roots"].append(str(other.resolve()))
    write_active_launch(workspace, active)
    bind_active_project(workspace_root=workspace, repo_root=project, reason="memory")
    with pytest.raises(ValueError, match="already bound"):
        bind_active_project(workspace_root=workspace, repo_root=other, reason="memory")


def test_rebinding_same_project_does_not_downgrade_verified_pillars(tmp_path: Path) -> None:
    workspace, project = _launch(tmp_path)
    active = bind_active_project(workspace_root=workspace, repo_root=project, reason="memory")
    active["pillars"]["managed"] = {"active": True, "state": "completed_in_place", "run_id": "run-1"}
    active["pillars"]["dogfood"] = {"active": True, "state": "verified_source_discipline", "run_id": "run-1"}
    write_active_launch(workspace, active)
    rebound = bind_active_project(workspace_root=workspace, repo_root=project, reason="runtime_telemetry")
    assert rebound["pillars"]["managed"] == {"active": True, "state": "completed_in_place", "run_id": "run-1"}
    assert rebound["pillars"]["dogfood"] == {"active": True, "state": "verified_source_discipline", "run_id": "run-1"}


def test_rollout_baseline_round_trip(tmp_path: Path) -> None:
    workspace, _ = _launch(tmp_path)
    snapshot = RolloutSnapshot(tmp_path / "sessions", "2026-07-13T00:00:00+00:00", {"rollout.jsonl": 42})
    save_rollout_baseline(workspace_root=workspace, launch_id="launch-1", snapshot=snapshot)
    assert load_rollout_baseline(read_active_launch(workspace), workspace_root=workspace) == snapshot


def test_task_source_baseline_round_trip_is_launch_scoped(tmp_path: Path) -> None:
    workspace, project = _launch(tmp_path)
    baseline = {
        "schema_version": 1,
        "kind": "filesystem",
        "repo_root": str(project),
        "complete": True,
        "entries": {"src/app.py": "sha256:abc"},
    }

    path = save_task_source_baseline(
        workspace_root=workspace,
        launch_id="launch-1",
        baseline=baseline,
    )
    active = read_active_launch(workspace, launch_id="launch-1")

    assert path.parent.name == "baselines"
    assert load_task_source_baseline(active, workspace_root=workspace) == baseline
    assert active["source_baseline_kind"] == "filesystem"
    assert active["source_baseline_complete"] is True


def test_task_source_baseline_receipt_binds_payload_workspace_launch_and_repo(tmp_path: Path) -> None:
    workspace, project = _launch(tmp_path)
    baseline = {
        "schema_version": 1,
        "kind": "filesystem",
        "repo_root": str(project),
        "complete": True,
        "entries": {"src/app.py": "sha256:abc"},
    }
    receipt = register_trusted_task_source_baseline(
        baseline,
        workspace_root=workspace,
        launch_id="launch-1",
        repo_root=project,
    )
    digest = task_source_baseline_digest(baseline)

    assert trusted_task_source_baseline_errors(
        baseline,
        workspace_root=workspace,
        launch_id="launch-1",
        repo_root=project,
        trusted_digest=digest,
        trusted_receipt=receipt,
    ) == ()
    tampered = {**baseline, "complete": False}
    errors = trusted_task_source_baseline_errors(
        tampered,
        workspace_root=workspace,
        launch_id="launch-1",
        repo_root=project,
        trusted_digest=digest,
        trusted_receipt=receipt,
    )
    assert "trusted_source_baseline_digest_mismatch" in errors
    assert "trusted_source_baseline_registered_digest_mismatch" in errors


def test_concurrent_launch_updates_merge_without_losing_fields(tmp_path: Path) -> None:
    workspace, _ = _launch(tmp_path)
    barrier = threading.Barrier(3)
    errors: list[BaseException] = []

    def update_cache() -> None:
        try:
            barrier.wait(timeout=2)
            update_active_launch(workspace, memory_cache={"receipt": "receipt-1"})
        except BaseException as exc:  # noqa: BLE001 - surface thread failures in the parent test.
            errors.append(exc)

    def update_acceptance() -> None:
        try:
            barrier.wait(timeout=2)
            update_pillar(workspace, "acceptance", {"active": True, "state": "verified"})
        except BaseException as exc:  # noqa: BLE001 - surface thread failures in the parent test.
            errors.append(exc)

    threads = [threading.Thread(target=update_cache), threading.Thread(target=update_acceptance)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=2)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    active = read_active_launch(workspace)
    assert active["memory_cache"] == {"receipt": "receipt-1"}
    acceptance = active["pillars"]["acceptance"]
    assert acceptance["active"] is True
    assert acceptance["state"] == "verified"
    assert acceptance["assessment"]["status"] == "partial"
    assert acceptance["assessment"]["reason_codes"] == [
        "acceptance_standard_insufficient"
    ]
    assert not list((workspace / "pacer_native").rglob("*.tmp"))


@pytest.mark.parametrize("launch_id", ["../escape", "..\\escape", "bad/id", "bad.id", ""])
def test_launch_id_rejects_path_traversal(tmp_path: Path, launch_id: str) -> None:
    with pytest.raises(ValueError, match="launch_id"):
        launch_context_path(tmp_path / ".agent-workspace", launch_id)


def test_manifest_must_be_canonical_and_tampered_path_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    workspace = root / ".agent-workspace"
    outside = tmp_path / "outside.json"
    outside.write_text('{"sentinel": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="manifest must be"):
        initialize_active_launch(
            workspace_root=workspace,
            manifest_path=outside,
            launch={"launch_id": "launch-1", "repo_root": str(root)},
        )

    canonical = workspace / "pacer_native" / "launches" / "launch-1.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=canonical,
        launch={"launch_id": "launch-1", "repo_root": str(root)},
    )
    active = read_active_launch(workspace)
    active["manifest_path"] = str(outside)
    active["current_goal"] = "safe update"
    write_active_launch(workspace, active)

    assert outside.read_text(encoding="utf-8") == '{"sentinel": true}'
    assert read_active_launch(workspace)["manifest_path"] == str(canonical.resolve())


def test_orphan_commit_closes_liveness_without_process_probe(tmp_path: Path) -> None:
    workspace, _ = _launch(tmp_path)
    active = read_active_launch(workspace)
    active["launcher_pid"] = 424242
    write_active_launch(workspace, active)
    write_launch_liveness(
        workspace,
        "launch-1",
        {"state": "active", "monitoring": True, "lifecycle_status": "running"},
    )
    ended_at = "2026-07-13T10:00:00+00:00"

    launch, capsule = _commit_orphaned_launch(
        workspace,
        active,
        ended_at=ended_at,
        orphaned_pid=424242,
    )

    assert launch["status"] == "orphaned"
    assert capsule["reason"] == "launcher_process_disappeared"
    merged = read_active_launch(workspace)
    assert merged["status"] == "orphaned"
    assert merged["liveness"]["monitoring"] is False
    assert merged["liveness"]["lifecycle_status"] == "orphaned"
    assert merged["liveness"]["stopped_at"] == ended_at


def test_orphan_probe_completion_race_does_not_write_recovery_artifacts(tmp_path: Path) -> None:
    workspace, _ = _launch(tmp_path)
    active = read_active_launch(workspace)
    active["launcher_pid"] = 424242
    write_active_launch(workspace, active)
    probed: list[int] = []

    def complete_during_probe(pid: int) -> bool:
        probed.append(pid)
        update_active_launch(
            workspace,
            expected_launch_id="launch-1",
            status="completed",
            completed_at="2026-07-13T10:00:00+00:00",
        )
        return False

    recovered = recover_orphaned_launches(workspace, process_probe=complete_during_probe)

    assert probed == [424242]
    assert recovered == []
    assert read_active_launch(workspace)["status"] == "completed"
    assert not (workspace / "pacer_native" / "recovery" / "launch-1.json").exists()
    assert not list((workspace / "pacer_native" / "events").rglob("*.json"))


def test_orphan_probe_pid_replacement_invalidates_probe_snapshot(tmp_path: Path) -> None:
    workspace, _ = _launch(tmp_path)
    active = read_active_launch(workspace)
    active["launcher_pid"] = 424242
    write_active_launch(workspace, active)

    def replace_launcher_during_probe(_pid: int) -> bool:
        update_active_launch(
            workspace,
            expected_launch_id="launch-1",
            launcher_pid=434343,
        )
        return False

    recovered = recover_orphaned_launches(workspace, process_probe=replace_launcher_during_probe)

    assert recovered == []
    latest = read_active_launch(workspace)
    assert latest["status"] == "running"
    assert latest["launcher_pid"] == 434343
    assert not (workspace / "pacer_native" / "recovery" / "launch-1.json").exists()
    assert not list((workspace / "pacer_native" / "events").rglob("*.json"))


def test_active_launch_merges_isolated_liveness_without_overwriting_pillars(tmp_path: Path) -> None:
    workspace, _ = _launch(tmp_path)
    active = read_active_launch(workspace)
    active["pillars"]["memory"] = {"active": True, "state": "loaded_with_evidence"}
    write_active_launch(workspace, active)

    write_launch_liveness(
        workspace,
        "launch-1",
        {
            "state": "stalled",
            "monitoring": True,
            "lifecycle_status": "running",
            "destructive_action": False,
        },
    )

    merged = read_active_launch(workspace)
    assert merged["status"] == "running"
    assert merged["liveness"]["state"] == "stalled"
    assert merged["pillars"]["memory"]["active"] is True


def test_terminal_context_overrides_stale_running_liveness_sidecar(tmp_path: Path) -> None:
    workspace, _ = _launch(tmp_path)
    write_launch_liveness(
        workspace,
        "launch-1",
        {
            "state": "active",
            "monitoring": True,
            "lifecycle_status": "running",
        },
    )
    stopped_at = "2026-07-13T10:00:00+00:00"
    update_active_launch(
        workspace,
        expected_launch_id="launch-1",
        status="failed",
        completed_at=stopped_at,
        liveness={
            "state": "active",
            "monitoring": False,
            "lifecycle_status": "failed",
            "stopped_at": stopped_at,
        },
    )

    assert read_launch_liveness(workspace, "launch-1")["monitoring"] is True
    active = read_active_launch(workspace, launch_id="launch-1")
    assert active["status"] == "failed"
    assert active["liveness"]["monitoring"] is False
    assert active["liveness"]["lifecycle_status"] == "failed"
    assert active["liveness"]["stopped_at"] == stopped_at


def test_reconciled_read_rate_limits_non_destructive_process_probe(tmp_path: Path) -> None:
    workspace, _ = _launch(tmp_path)
    active = read_active_launch(workspace)
    active["launcher_pid"] = 424242
    write_active_launch(workspace, active)
    now = [100.0]
    probes: list[int] = []

    def fake_probe(pid: int) -> bool:
        probes.append(pid)
        return True

    for _ in range(2):
        assert read_reconciled_active_launch(
            workspace,
            process_probe=fake_probe,
            reconcile_interval_seconds=5.0,
            monotonic_clock=lambda: now[0],
        )["status"] == "running"
    now[0] += 5.0
    read_reconciled_active_launch(
        workspace,
        process_probe=fake_probe,
        reconcile_interval_seconds=5.0,
        monotonic_clock=lambda: now[0],
    )

    assert probes == [424242, 424242]


def test_reconciled_pointer_read_is_not_redirected_by_inherited_launch_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace, _ = _launch(tmp_path)
    root = tmp_path / "source"
    newer_manifest = workspace / "pacer_native" / "launches" / "launch-2.json"
    newer_manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=newer_manifest,
        launch={
            "launch_id": "launch-2",
            "repo_root": str(root),
            "launcher_pid": 424242,
        },
    )
    monkeypatch.setenv("PACER_LAUNCH_ID", "launch-1")
    probes: list[int] = []

    active = read_reconciled_active_launch(
        workspace,
        process_probe=lambda pid: probes.append(pid) or True,
        reconcile_interval_seconds=0,
    )

    assert active["launch_id"] == "launch-2"
    assert probes == [424242]


def test_reconciled_read_persists_fake_missing_launcher_as_orphaned(tmp_path: Path) -> None:
    workspace, _ = _launch(tmp_path)
    active = read_active_launch(workspace)
    active["launcher_pid"] = 424242
    write_active_launch(workspace, active)

    reconciled = read_reconciled_active_launch(
        workspace,
        process_probe=lambda pid: pid != 424242,
        reconcile_interval_seconds=0,
    )

    assert reconciled["status"] == "orphaned"
    persisted = read_active_launch(workspace, launch_id="launch-1")
    assert persisted["status"] == "orphaned"
    assert persisted["liveness"]["monitoring"] is False
    assert (workspace / "pacer_native" / "recovery" / "launch-1.json").is_file()


def test_older_launch_cannot_overwrite_newer_active_launch(tmp_path: Path) -> None:
    workspace, _ = _launch(tmp_path)
    root = tmp_path / "source"
    newer_manifest = workspace / "pacer_native" / "launches" / "launch-2.json"
    newer_manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=newer_manifest,
        launch={"launch_id": "launch-2", "repo_root": str(root)},
    )
    result = update_active_launch(
        workspace,
        expected_launch_id="launch-1",
        status="completed",
        exit_code=0,
    )
    assert result["launch_id"] == "launch-1"
    assert result["status"] == "completed"
    latest = read_active_launch(workspace)
    assert latest["launch_id"] == "launch-2"
    assert latest["status"] == "running"
    assert "exit_code" not in latest


def test_concurrent_launch_initialization_keeps_newest_started_launch_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    older_root = tmp_path / "older"
    newer_root = tmp_path / "newer"
    older_root.mkdir()
    newer_root.mkdir()
    workspace = tmp_path / ".agent-workspace"
    launches = workspace / "pacer_native" / "launches"
    launches.mkdir(parents=True)
    older_manifest = launches / "launch-older.json"
    newer_manifest = launches / "launch-newer.json"
    older_manifest.write_text("{}", encoding="utf-8")
    newer_manifest.write_text("{}", encoding="utf-8")
    older_ready = threading.Event()
    release_older = threading.Event()
    errors: list[BaseException] = []

    def controlled_discovery(root: str | Path, **_kwargs) -> list[Path]:
        resolved = Path(root).resolve()
        if resolved == older_root.resolve():
            older_ready.set()
            if not release_older.wait(timeout=2):
                raise TimeoutError("older launch was not released")
        return [resolved]

    monkeypatch.setattr(
        launch_context_module,
        "discover_existing_project_roots",
        controlled_discovery,
    )
    runtime = {"python": {"executable": "fixture-python", "available": True}}

    def initialize_older() -> None:
        try:
            initialize_active_launch(
                workspace_root=workspace,
                manifest_path=older_manifest,
                launch={
                    "launch_id": "launch-older",
                    "repo_root": str(older_root),
                    "started_at": "2026-07-13T08:00:00Z",
                    "runtime": runtime,
                },
            )
        except BaseException as exc:  # noqa: BLE001 - surface thread failures in the parent test.
            errors.append(exc)

    older_thread = threading.Thread(target=initialize_older)
    older_thread.start()
    assert older_ready.wait(timeout=2)
    try:
        initialize_active_launch(
            workspace_root=workspace,
            manifest_path=newer_manifest,
            launch={
                "launch_id": "launch-newer",
                "repo_root": str(newer_root),
                "started_at": "2026-07-13T09:00:00+00:00",
                "runtime": runtime,
            },
        )
    finally:
        release_older.set()
    older_thread.join(timeout=2)

    assert errors == []
    assert not older_thread.is_alive()
    active = read_active_launch(workspace)
    assert active["launch_id"] == "launch-newer"
    assert active["started_at"] == "2026-07-13T09:00:00+00:00"
    assert read_active_launch(workspace, launch_id="launch-older")["status"] == "running"
    assert read_active_launch(workspace, launch_id="launch-newer")["status"] == "running"
    assert json.loads(older_manifest.read_text(encoding="utf-8"))["status"] == "running"
    assert json.loads(newer_manifest.read_text(encoding="utf-8"))["status"] == "running"


def test_launch_id_breaks_equal_started_at_ties_deterministically(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    workspace = root / ".agent-workspace"
    launches = workspace / "pacer_native" / "launches"
    launches.mkdir(parents=True)
    runtime = {"python": {"executable": "fixture-python", "available": True}}
    for launch_id in ("launch-z", "launch-a"):
        manifest = launches / f"{launch_id}.json"
        manifest.write_text("{}", encoding="utf-8")
        initialize_active_launch(
            workspace_root=workspace,
            manifest_path=manifest,
            launch={
                "launch_id": launch_id,
                "repo_root": str(root),
                "started_at": "2026-07-13T09:00:00Z",
                "runtime": runtime,
            },
        )

    assert read_active_launch(workspace)["launch_id"] == "launch-z"


def test_find_active_launch_falls_back_from_completed_pointer_to_latest_running_context(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    project = root / "app"
    project.mkdir(parents=True)
    workspace = root / ".agent-workspace"
    launches = workspace / "pacer_native" / "launches"
    launches.mkdir(parents=True)
    runtime = {"python": {"executable": "fixture-python", "available": True}}
    for launch_id, started_at in (
        ("launch-old", "2026-07-13T08:00:00Z"),
        ("launch-middle", "2026-07-13T09:00:00Z"),
        ("launch-completed", "2026-07-13T10:00:00Z"),
    ):
        manifest = launches / f"{launch_id}.json"
        manifest.write_text("{}", encoding="utf-8")
        initialize_active_launch(
            workspace_root=workspace,
            manifest_path=manifest,
            launch={
                "launch_id": launch_id,
                "repo_root": str(root),
                "started_at": started_at,
                "runtime": runtime,
            },
        )
    update_active_launch(
        workspace,
        expected_launch_id="launch-completed",
        status="completed",
    )

    found_workspace, found = find_active_launch(
        repo_root=project,
        suggested_workspace=workspace,
        process_probe=lambda _pid: True,
    )

    assert found_workspace == workspace.resolve()
    assert found["launch_id"] == "launch-middle"


def test_find_active_launch_falls_back_when_pointer_owns_another_repo(tmp_path: Path) -> None:
    owned_root = tmp_path / "owned"
    owned_project = owned_root / "app"
    other_root = tmp_path / "other"
    owned_project.mkdir(parents=True)
    other_root.mkdir()
    workspace = tmp_path / ".agent-workspace"
    launches = workspace / "pacer_native" / "launches"
    launches.mkdir(parents=True)
    runtime = {"python": {"executable": "fixture-python", "available": True}}
    for launch_id, root, started_at in (
        ("launch-owned", owned_root, "2026-07-13T08:00:00Z"),
        ("launch-other", other_root, "2026-07-13T09:00:00Z"),
    ):
        manifest = launches / f"{launch_id}.json"
        manifest.write_text("{}", encoding="utf-8")
        initialize_active_launch(
            workspace_root=workspace,
            manifest_path=manifest,
            launch={
                "launch_id": launch_id,
                "repo_root": str(root),
                "started_at": started_at,
                "runtime": runtime,
            },
        )

    found_workspace, found = find_active_launch(
        repo_root=owned_project,
        suggested_workspace=workspace,
        process_probe=lambda _pid: True,
    )

    assert found_workspace == workspace.resolve()
    assert found["launch_id"] == "launch-owned"


def test_find_active_launch_orphans_dead_fallback_and_selects_next_live_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    project = root / "app"
    project.mkdir(parents=True)
    workspace = root / ".agent-workspace"
    launches = workspace / "pacer_native" / "launches"
    launches.mkdir(parents=True)
    runtime = {"python": {"executable": "fixture-python", "available": True}}
    rows = (
        ("launch-live", "2026-07-13T08:00:00Z", 111111),
        ("launch-dead", "2026-07-13T09:00:00Z", 222222),
        ("launch-completed", "2026-07-13T10:00:00Z", 333333),
    )
    for launch_id, started_at, launcher_pid in rows:
        manifest = launches / f"{launch_id}.json"
        manifest.write_text("{}", encoding="utf-8")
        initialize_active_launch(
            workspace_root=workspace,
            manifest_path=manifest,
            launch={
                "launch_id": launch_id,
                "repo_root": str(root),
                "started_at": started_at,
                "launcher_pid": launcher_pid,
                "runtime": runtime,
            },
        )
    update_active_launch(
        workspace,
        expected_launch_id="launch-completed",
        status="completed",
    )
    probes: list[int] = []

    def fake_probe(pid: int) -> bool:
        probes.append(pid)
        return pid == 111111

    found_workspace, found = find_active_launch(
        repo_root=project,
        suggested_workspace=workspace,
        process_probe=fake_probe,
        reconcile_interval_seconds=0,
    )

    assert found_workspace == workspace.resolve()
    assert found["launch_id"] == "launch-live"
    assert probes == [222222, 111111]
    assert read_active_launch(workspace, launch_id="launch-dead")["status"] == "orphaned"


def test_launch_state_thread_locks_are_isolated_per_workspace(tmp_path: Path, monkeypatch) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    blocked_path = workspace_a.resolve() / "pacer_native" / "liveness" / "launch-a.json"
    entered = threading.Event()
    release = threading.Event()
    workspace_b_finished = threading.Event()
    errors: list[BaseException] = []
    original_write_json = launch_context_module._write_json

    def blocking_write(path: Path, payload: dict[str, object]) -> None:
        if path == blocked_path:
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("workspace A write was not released")
        original_write_json(path, payload)

    monkeypatch.setattr(launch_context_module, "_write_json", blocking_write)

    def write_workspace_a() -> None:
        try:
            write_launch_liveness(workspace_a, "launch-a", {"state": "active"})
        except BaseException as exc:  # noqa: BLE001 - surface thread failures in the parent test.
            errors.append(exc)

    def write_workspace_b() -> None:
        try:
            write_launch_liveness(workspace_b, "launch-b", {"state": "active"})
            workspace_b_finished.set()
        except BaseException as exc:  # noqa: BLE001 - surface thread failures in the parent test.
            errors.append(exc)

    thread_a = threading.Thread(target=write_workspace_a)
    thread_b = threading.Thread(target=write_workspace_b)
    thread_a.start()
    assert entered.wait(timeout=2)
    thread_b.start()
    try:
        assert workspace_b_finished.wait(timeout=1)
    finally:
        release.set()
    thread_a.join(timeout=2)
    thread_b.join(timeout=2)

    assert errors == []
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()


def test_older_launch_baseline_does_not_attach_to_newer_active_launch(tmp_path: Path) -> None:
    workspace, _ = _launch(tmp_path)
    root = tmp_path / "source"
    newer_manifest = workspace / "pacer_native" / "launches" / "launch-2.json"
    newer_manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=newer_manifest,
        launch={"launch_id": "launch-2", "repo_root": str(root)},
    )
    snapshot = RolloutSnapshot(tmp_path / "sessions", "2026-07-13T00:00:00+00:00", {})
    save_rollout_baseline(workspace_root=workspace, launch_id="launch-1", snapshot=snapshot)
    active = read_active_launch(workspace)
    assert active["launch_id"] == "launch-2"
    assert "rollout_baseline_path" not in active


def test_parallel_launch_reads_and_updates_its_own_context(tmp_path: Path, monkeypatch) -> None:
    workspace, project = _launch(tmp_path)
    root = tmp_path / "source"
    newer_manifest = workspace / "pacer_native" / "launches" / "launch-2.json"
    newer_manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=newer_manifest,
        launch={"launch_id": "launch-2", "repo_root": str(root)},
    )
    monkeypatch.setenv("PACER_LAUNCH_ID", "launch-1")
    first = read_active_launch(workspace)
    assert first["launch_id"] == "launch-1"
    bound = bind_active_project(workspace_root=workspace, repo_root=project, reason="memory")
    assert bound["launch_id"] == "launch-1"
    monkeypatch.delenv("PACER_LAUNCH_ID")
    latest = read_active_launch(workspace)
    assert latest["launch_id"] == "launch-2"
    assert latest["project_root"] == str(root.resolve())


def test_find_active_launch_prefers_valid_environment_launch_context(tmp_path: Path, monkeypatch) -> None:
    workspace, project = _launch(tmp_path)
    root = tmp_path / "source"
    newer_manifest = workspace / "pacer_native" / "launches" / "launch-2.json"
    newer_manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=newer_manifest,
        launch={
            "launch_id": "launch-2",
            "repo_root": str(root),
            "started_at": "2026-07-13T10:00:00Z",
        },
    )
    monkeypatch.setenv("PACER_LAUNCH_ID", "launch-1")

    found_workspace, found = find_active_launch(
        repo_root=project,
        suggested_workspace=workspace,
        process_probe=lambda _pid: True,
    )

    assert found_workspace == workspace.resolve()
    assert found["launch_id"] == "launch-1"


def test_find_active_launch_does_not_fallback_when_valid_preferred_id_is_missing(tmp_path: Path) -> None:
    workspace, project = _launch(tmp_path)

    found_workspace, found = find_active_launch(
        repo_root=project,
        suggested_workspace=workspace,
        preferred_launch_id="missing-launch",
        process_probe=lambda _pid: True,
    )

    assert found_workspace is None
    assert found == {}


def test_explicit_launch_id_targets_binding_and_pillar_without_moving_pointer(tmp_path: Path) -> None:
    workspace, project = _launch(tmp_path)
    root = tmp_path / "source"
    newer_manifest = workspace / "pacer_native" / "launches" / "launch-2.json"
    newer_manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=newer_manifest,
        launch={"launch_id": "launch-2", "repo_root": str(root)},
    )

    bound = bind_active_project(
        workspace_root=workspace,
        repo_root=project,
        reason="memory",
        launch_id="launch-1",
    )
    updated = update_pillar(
        workspace,
        "memory",
        {"active": True, "state": "loaded_with_evidence"},
        launch_id="launch-1",
    )

    assert bound["launch_id"] == "launch-1"
    assert updated["launch_id"] == "launch-1"
    assert read_active_launch(workspace, launch_id="launch-1")["pillars"]["memory"]["active"] is True
    assert read_active_launch(workspace)["launch_id"] == "launch-2"


def test_context_recovery_capsule_requires_abnormal_pressure_threshold(tmp_path: Path) -> None:
    workspace, project = _launch(tmp_path)
    active = bind_active_project(workspace_root=workspace, repo_root=project, reason="memory")
    below = write_context_recovery_capsule(
        workspace,
        launch=active,
        telemetry={"current_context_usage": {"input_tokens": 95999}},
    )
    assert below == {}
    capsule = write_context_recovery_capsule(
        workspace,
        launch={**active, "current_goal": "finish recovery"},
        telemetry={
            "current_context_usage": {"input_tokens": 96000, "total_tokens": 96100},
            "usage": {"input_tokens": 500000, "total_tokens": 501000},
            "compactions": {"count": 0, "timestamps": []},
        },
    )
    assert capsule["status"] == "pending"
    assert capsule["goal"] == "finish recovery"
    assert capsule["reason"] == "abnormal_exit_at_or_above_context_limit"
    assert latest_pending_recovery_capsule(workspace, repo_root=project)["source_launch_id"] == "launch-1"
    resolved = resolve_recovery_capsule(
        workspace,
        source_launch_id="launch-1",
        recovery_launch_id="launch-2",
    )
    assert resolved["status"] == "resolved"
    assert resolved["recovery_launch_id"] == "launch-2"
    assert latest_pending_recovery_capsule(workspace, repo_root=project) == {}


def test_missing_launcher_process_marks_launch_orphaned_and_creates_capsule(tmp_path: Path) -> None:
    workspace, project = _launch(tmp_path)
    bind_active_project(workspace_root=workspace, repo_root=project, reason="memory")
    active = read_active_launch(workspace)
    active["launcher_pid"] = 424242
    active["current_goal"] = "resume this task"
    write_active_launch(workspace, active)
    recovered = recover_orphaned_launches(workspace, process_probe=lambda _pid: False)
    assert [item["launch_id"] for item in recovered] == ["launch-1"]
    assert read_active_launch(workspace)["status"] == "orphaned"
    capsule = latest_pending_recovery_capsule(workspace, repo_root=project)
    assert capsule["reason"] == "launcher_process_disappeared"
    assert capsule["goal"] == "resume this task"


def test_taskless_interactive_missing_launcher_is_closed_without_task(tmp_path: Path) -> None:
    workspace, project = _launch(tmp_path)
    active = read_active_launch(workspace)
    active["launcher_pid"] = 424242
    active["mode"] = "interactive"
    active["prompt_recorded"] = False
    write_active_launch(workspace, active)

    recovered = recover_orphaned_launches(workspace, process_probe=lambda _pid: False)

    assert [item["launch_id"] for item in recovered] == ["launch-1"]
    closed = read_active_launch(workspace)
    assert closed["status"] == "closed_without_task"
    assert closed["liveness"]["lifecycle_status"] == "closed_without_task"
    assert latest_pending_recovery_capsule(workspace, repo_root=project) == {}


def test_live_launcher_process_is_not_marked_orphaned(tmp_path: Path) -> None:
    workspace, _ = _launch(tmp_path)
    assert recover_orphaned_launches(workspace, process_probe=lambda _pid: True) == []
    assert read_active_launch(workspace)["status"] == "running"


def test_real_exited_helper_process_is_recovered_without_signals(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='orphan-fixture'\n", encoding="utf-8")
    workspace = root / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "helper-launch.json"
    code = (
        "import os,sys; "
        "from pathlib import Path; "
        "from visual_agent.pacer_launch_context import initialize_active_launch; "
        "w=Path(sys.argv[1]); r=Path(sys.argv[2]); m=w/'pacer_native'/'launches'/'helper-launch.json'; "
        "m.parent.mkdir(parents=True,exist_ok=True); m.write_text('{}',encoding='utf-8'); "
        "initialize_active_launch(workspace_root=w,manifest_path=m,launch={'launch_id':'helper-launch','repo_root':str(r),'launcher_pid':os.getpid()}); "
        "os._exit(7)"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    completed = subprocess.run(
        [sys.executable, "-c", code, str(workspace), str(root)],
        env=env,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 7
    assert manifest.exists()
    recovered = recover_orphaned_launches(workspace)
    assert [item["launch_id"] for item in recovered] == ["helper-launch"]
    assert read_active_launch(workspace)["status"] == "orphaned"
    assert latest_pending_recovery_capsule(workspace, repo_root=root)["reason"] == "launcher_process_disappeared"
