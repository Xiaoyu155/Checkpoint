"""Tests for official pacer host product path."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from visual_agent.pacer_host import (
    build_host_dashboard,
    host_dashboard_to_markdown,
    load_host_policy,
    run_host_session,
    save_host_policy,
    start_hosted_goal,
)
from visual_agent.workspace import init_workspace


def test_host_policy_default_is_economy(tmp_path: Path) -> None:
    from visual_agent.pacer_host import normalize_host_mode

    assert normalize_host_mode(None) == "economy"
    assert normalize_host_mode("unleash") == "unleash"
    assert normalize_host_mode(None, race_flag=True) == "race"
    assert normalize_host_mode("yolo") == "yolo"
    assert normalize_host_mode("yolo", unleash_flag=True) == "yolo"
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    policy = load_host_policy(ws)
    assert policy["mode"] == "economy"
    assert policy["max_active"] == 1
    assert policy["token_cost"] == "low"
    assert policy["reasoning_effort"] == "low"
    assert policy["model_policy"]["implementation"] == "standard"
    assert policy["race"] is False


def test_host_policy_roundtrip(tmp_path: Path) -> None:
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    policy = load_host_policy(ws, mode="standard")
    assert policy["mode"] == "standard"
    assert policy["max_active"] == 2
    policy["max_auto_resumes_per_mission"] = 3
    path = save_host_policy(ws, policy)
    assert path.is_file()
    loaded = load_host_policy(ws, mode="standard")
    assert loaded["max_auto_resumes_per_mission"] == 3


def test_host_dashboard_markdown(tmp_path, monkeypatch) -> None:
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    monkeypatch.setattr(
        "visual_agent.pacer_host.probe_worker_agent_liveness",
        lambda *_a, **_k: {
            "ok": True,
            "agent": "codex",
            "stop_reason": "",
            "message": "",
            "details": {"authenticated": True},
        },
    )
    reconcile_calls = []
    monkeypatch.setattr(
        "visual_agent.chief_background.reconcile_workspace_backgrounds",
        lambda *_a, **kwargs: reconcile_calls.append(kwargs) or [],
    )
    dash = build_host_dashboard(workspace_root=ws, repo_root=tmp_path, agent="codex")
    assert dash["ready_for_host"] is True
    explicit_call = next(call for call in reconcile_calls if call.get("limit") == 40)
    assert explicit_call["auto_resume"] is False
    assert explicit_call["max_auto_resume_attempts"] == 1
    assert dash["auto_resume_enabled"] is False
    text = host_dashboard_to_markdown(dash)
    assert "托管仪表" in text
    assert "可以托管" in text
    assert "economy" in text or "省额度" in text


def test_host_dashboard_auto_resume_requires_live_host_without_stop(tmp_path, monkeypatch) -> None:
    from visual_agent.pacer_host import request_host_stop

    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    monkeypatch.setattr(
        "visual_agent.pacer_host.probe_worker_agent_liveness",
        lambda *_a, **_k: {"ok": True, "agent": "codex"},
    )
    monkeypatch.setattr(
        "visual_agent.pacer_host._mission_counts",
        lambda _ws: {
            "total": 0,
            "verified": 0,
            "stopped": 0,
            "running": 0,
            "preview": 0,
            "other": 0,
            "orphaned_stop": 0,
        },
    )
    monkeypatch.setattr("visual_agent.pacer_host._last_success_meta", lambda _ws: {})
    calls = []
    monkeypatch.setattr(
        "visual_agent.chief_background.reconcile_workspace_backgrounds",
        lambda *_a, **kwargs: calls.append(kwargs) or [],
    )

    live = build_host_dashboard(workspace_root=ws, repo_root=tmp_path, auto_resume=True)
    assert live["auto_resume_enabled"] is True
    assert calls[-1]["auto_resume"] is True

    request_host_stop(ws)
    stopped = build_host_dashboard(workspace_root=ws, repo_root=tmp_path, auto_resume=True)
    assert stopped["auto_resume_enabled"] is False
    assert calls[-1]["auto_resume"] is False
    assert stopped["ready_for_host"] is False
    assert "stop_requested" in stopped["blockers"]


def test_host_dashboard_blocks_when_quota_dead(tmp_path, monkeypatch) -> None:
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    monkeypatch.setattr(
        "visual_agent.pacer_host.probe_worker_agent_liveness",
        lambda *_a, **_k: {
            "ok": False,
            "agent": "codex",
            "stop_reason": "quota_exhausted",
            "message": "no tokens",
            "details": {},
        },
    )
    monkeypatch.setattr(
        "visual_agent.chief_background.reconcile_workspace_backgrounds",
        lambda *_a, **_k: [],
    )
    dash = build_host_dashboard(workspace_root=ws, repo_root=tmp_path)
    assert dash["ready_for_host"] is False
    assert "quota_exhausted" in dash["blockers"]
    text = host_dashboard_to_markdown(dash)
    assert "额度" in text or "quota" in text.lower() or "先别托管" in text


def test_host_dashboard_does_not_advertise_inspection_agent_as_implementation_ready(
    tmp_path,
    monkeypatch,
) -> None:
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    monkeypatch.setattr(
        "visual_agent.pacer_host.probe_worker_agent_liveness",
        lambda *_a, **_k: {"ok": True, "agent": "gemini"},
    )
    monkeypatch.setattr(
        "visual_agent.chief_background.reconcile_workspace_backgrounds",
        lambda *_a, **_k: [],
    )

    dash = build_host_dashboard(
        workspace_root=ws,
        repo_root=tmp_path,
        agent="gemini",
    )
    blocked = start_hosted_goal(
        workspace_root=ws,
        repo_root=tmp_path,
        goal="Implement a feature",
        agent="gemini",
        require_liveness=False,
    )

    assert dash["ready_for_host"] is False
    assert "agent_inspection_only" in dash["blockers"]
    assert dash["agent_capability"]["primary_role"] == "multimodal_inspection"
    assert blocked["status"] == "blocked"
    assert blocked["stop_reason"] == "agent_inspection_only"


def test_start_hosted_goal_blocks_on_liveness(tmp_path, monkeypatch) -> None:
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    monkeypatch.setattr(
        "visual_agent.pacer_host.probe_worker_agent_liveness",
        lambda *_a, **_k: {
            "ok": False,
            "stop_reason": "not_authenticated",
            "message": "login required",
            "agent": "codex",
        },
    )
    result = start_hosted_goal(
        workspace_root=ws,
        repo_root=tmp_path,
        goal="add a docstring",
        agent="codex",
    )
    assert result["status"] == "blocked"
    assert result["stop_reason"] == "not_authenticated"


def test_start_hosted_goal_happy_path(tmp_path, monkeypatch) -> None:
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    captured_preview = {}
    monkeypatch.setattr(
        "visual_agent.pacer_host.probe_worker_agent_liveness",
        lambda *_a, **_k: {"ok": True, "agent": "codex", "stop_reason": "", "message": ""},
    )
    monkeypatch.setattr(
        "visual_agent.chief_run.run_chief_mission",
        lambda **kwargs: captured_preview.update(kwargs) or {
            "status": "preview",
            "mission": {"mission_id": "m-host-1", "objective": "x"},
        },
    )
    monkeypatch.setattr(
        "visual_agent.chief_background.start_background_chief_run",
        lambda **_k: {
            "status": "background_started",
            "stop_reason": "",
            "message": "started",
            "background": {"pid": 1},
        },
    )
    # start_hosted_goal imports inside function from chief_run / chief_background
    result = start_hosted_goal(
        workspace_root=ws,
        repo_root=tmp_path,
        goal="add a Chinese docstring",
        agent="codex",
        reasoning_effort="low",
        model_policy={"implementation": "standard", "repair": "strong"},
        require_liveness=True,
    )
    assert result["status"] == "background_started"
    assert result["mission_id"] == "m-host-1"
    assert captured_preview["reasoning_effort"] == "low"
    assert captured_preview["model_policy"]["implementation"] == "standard"
    assert result["reasoning_effort"] == "low"


def test_maybe_split_goal_wild() -> None:
    from visual_agent.pacer_host import maybe_split_goal

    parts = maybe_split_goal(
        "给风险模块补充中文注释并且给回测模块写模块说明然后更新 README 托管备注",
        enabled=True,
    )
    assert len(parts) >= 2


def test_wait_for_liveness_wakes(monkeypatch) -> None:
    from visual_agent.pacer_host import wait_for_agent_liveness

    calls = {"n": 0}

    def probe(_agent, **_k):
        calls["n"] += 1
        if calls["n"] < 2:
            return {"ok": False, "stop_reason": "quota_exhausted", "message": "wait"}
        return {"ok": True, "stop_reason": "", "message": ""}

    monkeypatch.setattr("visual_agent.pacer_host.probe_worker_agent_liveness", probe)
    monkeypatch.setattr("visual_agent.pacer_host.time.sleep", lambda *_a, **_k: None)
    out = wait_for_agent_liveness("codex", timeout_seconds=30, poll_seconds=1)
    assert out.get("ok") is True
    assert out.get("woke") is True


def test_abort_refuses_verified_or_foreign_process(tmp_path, monkeypatch) -> None:
    from visual_agent.chief_background import save_background_record
    from visual_agent.missions import save_mission
    from visual_agent.pacer_host import abort_hosted_mission

    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    mid = "abort-safety"
    (ws / "missions" / mid).mkdir(parents=True)
    mission = {
        "mission_id": mid,
        "objective": "race",
        "status": "verified",
        "stop_reason": "verified",
    }
    save_mission(ws, mission)
    save_background_record(ws, mid, {"status": "running", "pid": 4242, "worker_pid": 4242})
    terminations = []
    monkeypatch.setattr(
        "visual_agent.chief_background.terminate_process",
        lambda pid: terminations.append(pid) or True,
    )

    verified = abort_hosted_mission(workspace_root=ws, mission_id=mid)
    assert verified["stop_reason"] == "already_verified"
    assert terminations == []

    mission["status"] = "running"
    mission["stop_reason"] = ""
    save_mission(ws, mission)
    monkeypatch.setattr(
        "visual_agent.chief_background.process_status",
        lambda _pid: {"alive": True},
    )
    monkeypatch.setattr(
        "visual_agent.chief_background.process_belongs_to_mission",
        lambda *_a, **_k: False,
    )
    foreign = abort_hosted_mission(workspace_root=ws, mission_id=mid)
    assert foreign["stop_reason"] == "process_ownership_unverified"
    assert terminations == []


def test_abort_failure_preserves_pid_and_state(tmp_path, monkeypatch) -> None:
    from visual_agent.chief_background import load_background_record, save_background_record
    from visual_agent.missions import load_mission, save_mission
    from visual_agent.pacer_host import abort_hosted_mission

    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    mid = "abort-failure"
    (ws / "missions" / mid).mkdir(parents=True)
    save_mission(ws, {"mission_id": mid, "objective": "race", "status": "running", "stop_reason": ""})
    save_background_record(ws, mid, {"status": "running", "pid": 5252, "worker_pid": 5252})
    monkeypatch.setattr("visual_agent.chief_background.process_status", lambda _pid: {"alive": True})
    monkeypatch.setattr(
        "visual_agent.chief_background.process_belongs_to_mission",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr("visual_agent.chief_background.terminate_process", lambda _pid: False)

    result = abort_hosted_mission(workspace_root=ws, mission_id=mid)

    assert result["stop_reason"] == "abort_failed"
    assert load_mission(ws, mid)["status"] == "running"
    background = load_background_record(ws, mid)
    assert background["status"] == "running"
    assert background["worker_pid"] == 5252


def test_abort_success_marks_terminal_only_after_exit(tmp_path, monkeypatch) -> None:
    from visual_agent.chief_background import load_background_record, save_background_record
    from visual_agent.missions import load_mission, save_mission
    from visual_agent.pacer_host import abort_hosted_mission

    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    mid = "abort-success"
    (ws / "missions" / mid).mkdir(parents=True)
    save_mission(ws, {"mission_id": mid, "objective": "race", "status": "running", "stop_reason": ""})
    save_background_record(ws, mid, {"status": "running", "pid": 6262, "worker_pid": 6262})
    probes = iter([{"alive": True}, {"alive": False}])
    monkeypatch.setattr("visual_agent.chief_background.process_status", lambda _pid: next(probes))
    monkeypatch.setattr(
        "visual_agent.chief_background.process_belongs_to_mission",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr("visual_agent.chief_background.terminate_process", lambda _pid: True)

    result = abort_hosted_mission(workspace_root=ws, mission_id=mid, reason="race_lost")

    assert result["status"] == "aborted"
    assert load_mission(ws, mid)["stop_reason"] == "race_lost"
    background = load_background_record(ws, mid)
    assert background["status"] == "aborted"
    assert background["worker_pid"] == 0


def test_host_single_shot_economy_passes_profile_budgets(tmp_path, monkeypatch, capsys) -> None:
    from visual_agent.cli_chief import _handle_host

    captured = {}
    monkeypatch.setattr(
        "visual_agent.pacer_host.start_hosted_goal",
        lambda **kwargs: captured.update(kwargs)
        or {"status": "background_started", "stop_reason": "", "mission_id": "m1", "goal": kwargs["goal"]},
    )
    args = SimpleNamespace(
        host_action="run",
        workspace_root=str(tmp_path / ".agent-workspace"),
        repo_root=str(tmp_path),
        agent="codex",
        format="json",
        mode=None,
        unleash=False,
        race=False,
        wake_on_quota=False,
        self_heal=False,
        execute=True,
        goal="fix one bug",
        goals=[],
        goals_file=None,
        hours=0.0,
        watch=False,
        allow_dirty=True,
        allow_test_edits=False,
        merge=False,
        test_command=None,
        clear_quota_cache=False,
    )

    assert _handle_host(args) == 0
    capsys.readouterr()
    assert captured["max_rounds"] == 2
    assert captured["max_repair_rounds"] == 1
    assert captured["max_wall_minutes"] == 40
    assert captured["max_worker_minutes"] == 30
    assert captured["reasoning_effort"] == "low"
    assert captured["model_policy"]["implementation"] == "standard"


def test_host_yolo_defaults_to_claude_bypass_policy(tmp_path, monkeypatch, capsys) -> None:
    from visual_agent.cli_chief import _handle_host

    captured = {}
    monkeypatch.setattr(
        "visual_agent.pacer_host.run_host_session",
        lambda **kwargs: captured.update(kwargs) or {"status": "completed", "stop_reason": "", "goals": kwargs["goals"]},
    )
    args = SimpleNamespace(
        host_action="yolo",
        workspace_root=str(tmp_path / ".agent-workspace"),
        repo_root=str(tmp_path),
        agent=None,
        format="json",
        mode=None,
        unleash=False,
        yolo=False,
        race=False,
        wake_on_quota=False,
        self_heal=False,
        execute=True,
        goal="fix one bug",
        goals=[],
        goals_file=None,
        hours=0.0,
        watch=False,
        allow_dirty=True,
        allow_test_edits=False,
        merge=False,
        test_command=None,
        clear_quota_cache=False,
        poll_seconds=None,
        max_active=None,
    )

    assert _handle_host(args) == 0
    capsys.readouterr()
    assert captured["agent"] == "claude-code"
    assert captured["mode"] == "yolo"
    assert captured["execution_policy"]["permission_mode"] == "bypassPermissions"
    assert captured["execution_policy"]["tool_permissions"] == "default"


def test_host_doctor_only_runs_pytest_when_requested(tmp_path, monkeypatch, capsys) -> None:
    from visual_agent.cli_chief import _handle_host

    calls = []
    monkeypatch.setattr(
        "visual_agent.pacer_host.build_host_dashboard",
        lambda **kwargs: calls.append(kwargs)
        or {
            "ready_for_host": True,
            "provider_liveness": {"ok": True},
            "missions": {},
            "blockers": [],
        },
    )
    base = {
        "host_action": "doctor",
        "workspace_root": str(tmp_path / ".agent-workspace"),
        "repo_root": str(tmp_path),
        "agent": "codex",
        "format": "json",
        "clear_quota_cache": False,
    }

    assert _handle_host(SimpleNamespace(**base, pytest=False)) == 0
    capsys.readouterr()
    assert calls[-1]["run_pytest"] is False

    assert _handle_host(SimpleNamespace(**base, pytest=True)) == 0
    capsys.readouterr()
    assert calls[-1]["run_pytest"] is True


def test_self_heal_probe_guard_caps_and_debounces() -> None:
    from visual_agent.pacer_host import self_heal_probe_allowed

    assert self_heal_probe_allowed(
        attempts=0, max_attempts=1, last_probe_at=None, interval_seconds=600, now=1000
    )
    assert not self_heal_probe_allowed(
        attempts=1, max_attempts=1, last_probe_at=None, interval_seconds=0, now=1000
    )
    assert not self_heal_probe_allowed(
        attempts=0, max_attempts=1, last_probe_at=900, interval_seconds=600, now=1000
    )
    assert self_heal_probe_allowed(
        attempts=0, max_attempts=1, last_probe_at=300, interval_seconds=600, now=1000
    )


def test_race_hosted_goal(tmp_path, monkeypatch) -> None:
    from visual_agent.pacer_host import race_hosted_goal

    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    monkeypatch.setattr(
        "visual_agent.pacer_host.probe_worker_agent_liveness",
        lambda agent, **_k: {"ok": True, "agent": agent},
    )
    monkeypatch.setattr(
        "visual_agent.pacer_host.start_hosted_goal",
        lambda **kwargs: {
            "status": "background_started",
            "mission_id": f"m-{kwargs.get('agent')}",
            "goal": kwargs.get("goal"),
            "agent": kwargs.get("agent"),
        },
    )
    # No settle wait in unit test
    out = race_hosted_goal(
        workspace_root=ws, repo_root=tmp_path, goal="fix bug", settle=False, abort_losers=False
    )
    assert out["status"] == "race_started"
    assert out["started_count"] == 2


def test_settle_race_aborts_loser(tmp_path, monkeypatch) -> None:
    from visual_agent.missions import save_mission
    from visual_agent.pacer_host import settle_race

    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    # Fabricate two missions: winner verified, loser running
    for mid, status, stop in [
        ("win-1", "verified", "verified"),
        ("lose-1", "running", ""),
    ]:
        mdir = ws / "missions" / mid
        mdir.mkdir(parents=True)
        save_mission(
            ws,
            {
                "mission_id": mid,
                "status": status,
                "stop_reason": stop,
                "objective": "race",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        )
    aborted = []
    monkeypatch.setattr(
        "visual_agent.pacer_host.abort_hosted_mission",
        lambda **kwargs: aborted.append(kwargs) or {
            "status": "aborted",
            "mission_id": kwargs["mission_id"],
            "stop_reason": "race_lost",
        },
    )
    monkeypatch.setattr("visual_agent.pacer_host.time.sleep", lambda *_a, **_k: None)
    out = settle_race(
        workspace_root=ws,
        legs=[
            {"mission_id": "win-1", "status": "background_started"},
            {"mission_id": "lose-1", "status": "background_started"},
        ],
        poll_seconds=1,
        timeout_seconds=5,
        abort_losers=True,
    )
    assert out["status"] == "race_won"
    assert out["winner"]["mission_id"] == "win-1"
    assert any(item.get("mission_id") == "lose-1" for item in aborted)


def test_poll_race_reports_running_without_wait(monkeypatch, tmp_path) -> None:
    from visual_agent.pacer_host import poll_race

    monkeypatch.setattr("visual_agent.pacer_host._mission_is_terminal_win", lambda *_a, **_k: False)
    monkeypatch.setattr("visual_agent.pacer_host._mission_is_dead", lambda *_a, **_k: False)

    result = poll_race(
        workspace_root=tmp_path,
        legs=[{"mission_id": "one", "status": "background_started"}],
    )

    assert result["status"] == "race_running"
    assert result["winner"] is None


def test_self_heal_preempts_low_priority(tmp_path, monkeypatch) -> None:
    from visual_agent.missions import save_mission
    from visual_agent.pacer_host import maybe_self_heal_pytest

    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    low = "low-1"
    (ws / "missions" / low).mkdir(parents=True)
    save_mission(
        ws,
        {
            "mission_id": low,
            "status": "running",
            "stop_reason": "",
            "objective": "low priority work",
            "host_priority": 0,
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        "visual_agent.pacer_host._run_pytest_probe",
        lambda *_a, **_k: {"ok": False, "exit_code": 1, "tail": "1 failed"},
    )
    preempted = []
    monkeypatch.setattr(
        "visual_agent.pacer_host.abort_hosted_mission",
        lambda **kwargs: preempted.append(kwargs) or {"status": "aborted", **kwargs},
    )
    monkeypatch.setattr(
        "visual_agent.pacer_host.start_hosted_goal",
        lambda **kwargs: {
            "status": "background_started",
            "mission_id": "heal-1",
            "goal": kwargs.get("goal"),
        },
    )
    monkeypatch.setattr(
        "visual_agent.pacer_host.probe_worker_agent_liveness",
        lambda *_a, **_k: {"ok": True},
    )
    out = maybe_self_heal_pytest(
        workspace_root=ws,
        repo_root=tmp_path,
        preempt_non_priority=True,
        priority=100,
    )
    assert out is not None
    assert out.get("mission_id") == "heal-1"
    assert any(item.get("mission_id") == "low-1" for item in preempted)


def test_run_host_session_short(tmp_path, monkeypatch) -> None:
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    monkeypatch.setattr(
        "visual_agent.pacer_host.probe_worker_agent_liveness",
        lambda *_a, **_k: {"ok": True, "agent": "codex", "stop_reason": "", "message": ""},
    )
    monkeypatch.setattr(
        "visual_agent.chief_background.reconcile_workspace_backgrounds",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "visual_agent.pacer_host.start_hosted_goal",
        lambda **kwargs: {
            "status": "background_started",
            "stop_reason": "",
            "mission_id": "m1",
            "goal": kwargs.get("goal"),
        },
    )
    monkeypatch.setattr(
        "visual_agent.pacer_host._run_pytest_probe",
        lambda *_a, **_k: {"ok": True, "exit_code": 0, "tail": "1 passed"},
    )
    # Force drain: after launch, mission counts show running 0
    monkeypatch.setattr(
        "visual_agent.pacer_host._mission_counts",
        lambda _ws: {
            "total": 1,
            "verified": 1,
            "stopped": 0,
            "running": 0,
            "preview": 0,
            "other": 0,
            "orphaned_stop": 0,
        },
    )
    result = run_host_session(
        workspace_root=ws,
        repo_root=tmp_path,
        goals=["goal one"],
        hours=0.01,
        agent="codex",
        poll_seconds=1,
        stagger_seconds=0.1,
        max_active=2,
    )
    assert result["status"] in {"completed", "stopped"}
    assert result["session"]["launched_count"] >= 1


def test_run_host_session_launches_race_without_blocking_settle(tmp_path, monkeypatch) -> None:
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False).root
    monkeypatch.setattr(
        "visual_agent.pacer_host.probe_worker_agent_liveness",
        lambda *_a, **_k: {"ok": True, "agent": "codex", "stop_reason": "", "message": ""},
    )
    monkeypatch.setattr(
        "visual_agent.chief_background.reconcile_workspace_backgrounds",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "visual_agent.pacer_host._mission_counts",
        lambda _ws: {
            "total": 0,
            "verified": 0,
            "stopped": 0,
            "running": 0,
            "preview": 0,
            "other": 0,
            "orphaned_stop": 0,
        },
    )
    monkeypatch.setattr("visual_agent.pacer_host._last_success_meta", lambda _ws: {})
    monkeypatch.setattr(
        "visual_agent.pacer_host._run_pytest_probe",
        lambda *_a, **_k: {"ok": True, "exit_code": 0, "tail": "1 passed"},
    )
    captured = {}
    monkeypatch.setattr(
        "visual_agent.pacer_host.race_hosted_goal",
        lambda **kwargs: captured.update(kwargs)
        or {
            "status": "blocked",
            "race_id": "r1",
            "results": [],
            "started_count": 0,
        },
    )

    result = run_host_session(
        workspace_root=ws,
        repo_root=tmp_path,
        goals=["race goal"],
        hours=0.01,
        mode="race",
        race=True,
        wake_on_quota=False,
        self_heal_pytest=False,
        stagger_seconds=0.01,
    )

    assert result["status"] == "completed"
    assert captured["settle"] is False
