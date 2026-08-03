"""Tests for provider liveness probes and orphan auto-resume."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from visual_agent.chief_background import (
    maybe_auto_resume_orphaned_mission,
    reconcile_workspace_backgrounds,
    save_background_record,
)
from visual_agent.chief_run import run_chief_mission
from visual_agent.missions import load_mission, save_mission
from visual_agent.provider_liveness import (
    clear_worker_agent_quota_cache,
    liveness_block_payload,
    normalize_agent_name,
    probe_worker_agent_liveness,
)
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


def preview_payload(**_kwargs):
    return {
        "status": "preview",
        "plan_id": "plan-x",
        "worker": {"agent": "codex", "argv": ["codex"]},
        "verification": {},
    }


def test_normalize_agent_name() -> None:
    assert normalize_agent_name("Claude") == "claude-code"
    assert normalize_agent_name("openai") == "codex"
    assert normalize_agent_name(None) == "codex"


def test_clear_worker_agent_quota_cache_clears_aliases(tmp_path: Path) -> None:
    path = tmp_path / "quota_failures.json"
    path.write_text('{"codex": 1, "openai": 2, "other": 3}\n', encoding="utf-8")

    result = clear_worker_agent_quota_cache("codex", store_path=path)

    assert result["cleared_keys"] == ["codex", "openai"]
    assert path.read_text(encoding="utf-8") == '{\n  "other": 3\n}\n'


def test_probe_codex_not_installed(monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.provider_liveness.has_recent_quota_failure", lambda *_a, **_k: False)
    probe = probe_worker_agent_liveness(
        "codex",
        account_inspector=lambda **_k: {"installed": False, "authenticated": False, "status": "not_installed"},
    )
    assert probe["ok"] is False
    assert probe["stop_reason"] == "agent_unavailable"


def test_probe_codex_not_authenticated(monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.provider_liveness.has_recent_quota_failure", lambda *_a, **_k: False)
    probe = probe_worker_agent_liveness(
        "codex",
        account_inspector=lambda **_k: {
            "installed": True,
            "authenticated": False,
            "status": "not_authenticated",
        },
    )
    assert probe["ok"] is False
    assert probe["stop_reason"] == "not_authenticated"


def test_probe_codex_ok(monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.provider_liveness.has_recent_quota_failure", lambda *_a, **_k: False)
    probe = probe_worker_agent_liveness(
        "codex",
        account_inspector=lambda **_k: {
            "installed": True,
            "authenticated": True,
            "status": "authenticated",
            "auth_method": "chatgpt_subscription",
        },
    )
    assert probe["ok"] is True


def test_probe_does_not_treat_login_as_quota_recovery(monkeypatch) -> None:
    cleared: list[str] = []
    monkeypatch.setattr(
        "visual_agent.provider_liveness.has_recent_quota_failure",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        "visual_agent.provider_liveness.clear_worker_agent_quota_cache",
        lambda agent, **_k: cleared.append(agent) or {"agent": agent},
    )
    probe = probe_worker_agent_liveness(
        "codex",
        account_inspector=lambda **_k: {
            "installed": True,
            "authenticated": True,
            "status": "authenticated",
        },
    )
    assert probe["ok"] is False
    assert probe["stop_reason"] == "quota_exhausted"
    assert probe["details"]["source"] == "recent_quota_failure"
    assert cleared == []


def test_probe_keeps_quota_block_when_codex_account_is_not_recovered(monkeypatch) -> None:
    monkeypatch.setattr(
        "visual_agent.provider_liveness.has_recent_quota_failure",
        lambda *_a, **_k: True,
    )
    probe = probe_worker_agent_liveness(
        "codex",
        account_inspector=lambda **_k: {
            "installed": True,
            "authenticated": False,
            "status": "not_authenticated",
        },
    )
    assert probe["ok"] is False
    assert probe["stop_reason"] == "quota_exhausted"


def test_probe_claude_missing(monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.provider_liveness.has_recent_quota_failure", lambda *_a, **_k: False)
    probe = probe_worker_agent_liveness("claude-code", which=lambda _n: None)
    assert probe["ok"] is False
    assert probe["stop_reason"] == "agent_unavailable"


def test_liveness_block_payload_shape() -> None:
    probe = {"ok": False, "stop_reason": "quota_exhausted", "message": "no tokens"}
    payload = liveness_block_payload(probe=probe, mission={"mission_id": "m1"})
    assert payload["status"] == "blocked"
    assert payload["stop_reason"] == "quota_exhausted"


def test_start_background_blocks_when_not_authenticated(tmp_path, monkeypatch) -> None:
    from visual_agent.chief_background import start_background_chief_run

    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]
    monkeypatch.setattr(
        "visual_agent.provider_liveness.probe_worker_agent_liveness",
        lambda *_a, **_k: {
            "ok": False,
            "stop_reason": "not_authenticated",
            "message": "not logged in",
            "agent": "codex",
        },
    )
    payload = start_background_chief_run(
        workspace_root=workspace.root,
        mission_id=mission_id,
        agents=("codex",),
        allow_dirty=True,
    )
    assert payload["status"] == "blocked"
    assert payload["stop_reason"] == "not_authenticated"


def test_auto_resume_orphaned_once(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]
    mission = load_mission(workspace.root, mission_id)
    assert mission is not None
    mission["status"] = "stopped"
    mission["stop_reason"] = "worker_orphaned"
    mission["agent"] = "codex"
    mission["allow_dirty"] = True
    save_mission(workspace.root, mission)
    save_background_record(
        workspace.root,
        mission_id,
        {
            "status": "orphaned",
            "orphan_reason": "worker_orphaned",
            "agents": ["codex"],
            "test_command": "python -m pytest -q",
            "auto_resume_count": 0,
        },
    )

    monkeypatch.setattr(
        "visual_agent.provider_liveness.probe_worker_agent_liveness",
        lambda *_a, **_k: {"ok": True, "stop_reason": "", "message": "", "agent": "codex"},
    )
    launches: list[str] = []

    def fake_start(**kwargs):
        launches.append(str(kwargs.get("mission_id")))
        return {
            "status": "background_started",
            "stop_reason": "",
            "mission": kwargs,
        }

    monkeypatch.setattr("visual_agent.chief_background.start_background_chief_run", fake_start)

    first = maybe_auto_resume_orphaned_mission(
        workspace_root=workspace.root, mission_id=mission_id, max_attempts=1
    )
    assert first is not None
    assert first["status"] == "background_started"
    assert first["auto_resume_count"] == 1
    assert launches == [mission_id]

    second = maybe_auto_resume_orphaned_mission(
        workspace_root=workspace.root, mission_id=mission_id, max_attempts=1
    )
    assert second is not None
    assert second["status"] == "skipped"
    assert second["stop_reason"] == "auto_resume_exhausted"
    assert len(launches) == 1


def test_auto_resume_preserves_explicit_allow_dirty_false(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        allow_dirty=False,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]
    mission = load_mission(workspace.root, mission_id)
    assert mission is not None
    assert mission["allow_dirty"] is False
    mission["status"] = "stopped"
    mission["stop_reason"] = "worker_orphaned"
    save_mission(workspace.root, mission)
    save_background_record(
        workspace.root,
        mission_id,
        {"status": "orphaned", "orphan_reason": "worker_orphaned", "agents": ["codex"]},
    )
    monkeypatch.setattr(
        "visual_agent.provider_liveness.probe_worker_agent_liveness",
        lambda *_a, **_k: {"ok": True, "agent": "codex"},
    )
    captured = {}
    monkeypatch.setattr(
        "visual_agent.chief_background.start_background_chief_run",
        lambda **kwargs: captured.update(kwargs) or {"status": "background_started", "stop_reason": ""},
    )

    result = maybe_auto_resume_orphaned_mission(
        workspace_root=workspace.root,
        mission_id=mission_id,
        max_attempts=1,
    )

    assert result is not None
    assert result["status"] == "background_started"
    assert captured["allow_dirty"] is False


def test_auto_resume_skips_quota_orphan(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]
    mission = load_mission(workspace.root, mission_id)
    assert mission is not None
    mission["status"] = "stopped"
    mission["stop_reason"] = "quota_exhausted"
    save_mission(workspace.root, mission)
    save_background_record(
        workspace.root,
        mission_id,
        {"status": "orphaned", "orphan_reason": "quota_exhausted", "agents": ["codex"]},
    )
    called = []
    monkeypatch.setattr(
        "visual_agent.chief_background.start_background_chief_run",
        lambda **k: called.append(k) or {"status": "background_started"},
    )
    result = maybe_auto_resume_orphaned_mission(workspace_root=workspace.root, mission_id=mission_id)
    assert result is None
    assert called == []


def test_reconcile_triggers_auto_resume(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    preview = run_chief_mission(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        dispatch_runner=preview_payload,
    )
    mission_id = preview["mission"]["mission_id"]
    mission = load_mission(workspace.root, mission_id)
    assert mission is not None
    mission["status"] = "stopped"
    mission["stop_reason"] = "worker_orphaned"
    mission["agent"] = "codex"
    save_mission(workspace.root, mission)
    save_background_record(
        workspace.root,
        mission_id,
        {
            "status": "orphaned",
            "orphan_reason": "worker_orphaned",
            "agents": ["codex"],
            "auto_resume_count": 0,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    monkeypatch.setattr(
        "visual_agent.provider_liveness.probe_worker_agent_liveness",
        lambda *_a, **_k: {"ok": True, "agent": "codex"},
    )
    monkeypatch.setattr(
        "visual_agent.chief_background.start_background_chief_run",
        lambda **_k: {"status": "background_started", "stop_reason": ""},
    )
    results = reconcile_workspace_backgrounds(workspace.root, auto_resume=True, max_auto_resumes=2)
    assert any(item.get("mission_id") == mission_id for item in results)
    assert any(
        isinstance(item.get("auto_resume"), dict)
        and item["auto_resume"].get("status") == "background_started"
        for item in results
    )
