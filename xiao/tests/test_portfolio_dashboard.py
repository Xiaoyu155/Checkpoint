from __future__ import annotations

from datetime import datetime, timezone

from visual_agent.chief_plans_store import append_worker_record, save_verification
from visual_agent.chief_background import save_background_record
from visual_agent.missions import append_round, create_mission, default_budget_policy, save_mission
from visual_agent.portfolio_dashboard import PORTFOLIO_HTML, build_portfolio_data
from visual_agent.workspace import init_workspace


def test_portfolio_data_aggregates_multiple_projects(tmp_path):
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    ws_a = init_workspace(project_a / ".agent-workspace", with_demo=False)
    ws_b = init_workspace(project_b / ".agent-workspace", with_demo=False)
    mission_a = create_mission(
        workspace_root=ws_a.root,
        objective="审查项目 A",
        repo_root=project_a,
        plan_id="p-a",
        budget_policy=default_budget_policy(),
        mission_id="m-a",
        status="verified",
    )
    mission_a["stop_reason"] = "verified"
    save_mission(ws_a.root, mission_a)
    create_mission(
        workspace_root=ws_b.root,
        objective="审查项目 B",
        repo_root=project_b,
        plan_id="p-b",
        budget_policy=default_budget_policy(),
        mission_id="m-b",
        status="running",
    )

    payload = build_portfolio_data([project_a, project_b])

    assert payload["product"] == "xiao"
    assert payload["view"] == "portfolio"
    assert payload["totals"]["projects"] == 2
    assert payload["totals"]["missions"] == 2
    assert payload["totals"]["running"] == 1
    assert [item["name"] for item in payload["projects"]] == ["a", "b"]


def test_portfolio_data_keeps_broken_project_visible(tmp_path):
    missing = tmp_path / "missing"

    payload = build_portfolio_data([missing])

    assert payload["projects"][0]["ok"] is False
    assert "不存在" in payload["projects"][0]["error"]


def test_portfolio_html_contains_multi_project_surface():
    assert "xiao 多项目观察台" in PORTFOLIO_HTML
    assert "/api/portfolio" in PORTFOLIO_HTML
    assert "实时日志" in PORTFOLIO_HTML
    assert "Pacer 测试证据" in PORTFOLIO_HTML
    assert "验收命令" in PORTFOLIO_HTML
    assert "Worker 日志尾部" in PORTFOLIO_HTML
    assert "MiMo 节省" in PORTFOLIO_HTML
    assert "当前进展" in PORTFOLIO_HTML


def test_portfolio_progress_exposes_background_state_and_model(tmp_path, monkeypatch):
    project = tmp_path / "app"
    project.mkdir()
    ws = init_workspace(project / ".agent-workspace", with_demo=False)
    mission = create_mission(
        workspace_root=ws.root,
        objective="用 MiMo 开发一个任务",
        repo_root=project,
        plan_id="p-mimo",
        budget_policy=default_budget_policy(),
        mission_id="m-mimo",
        status="background_running",
    )
    save_mission(ws.root, mission)
    append_round(
        ws.root,
        "m-mimo",
        {
            "round": 0,
            "type": "dispatch_preview",
            "status": "preview",
            "payload": {"worker": {"agent": "mimo", "argv": ["claude", "-p", "--model", "mimo-v2.5-pro"]}},
        },
    )
    logs_dir = ws.root / "missions" / "m-mimo" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "out.log").write_text("", encoding="utf-8")
    (logs_dir / "err.log").write_text("", encoding="utf-8")
    save_background_record(
        ws.root,
        "m-mimo",
        {
            "status": "running",
            "pid": 1234,
            "worker_pid": 1234,
            "stdout_log": str(logs_dir / "out.log"),
            "stderr_log": str(logs_dir / "err.log"),
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    monkeypatch.setattr("visual_agent.chief_background.process_status", lambda _pid: {"alive": True, "exit_code": None})

    payload = build_portfolio_data([project])
    progress = payload["projects"][0]["progress"]

    assert progress["phase"] == "后台执行中"
    assert progress["pid"] == 1234
    assert progress["model"] == "mimo-v2.5-pro"
    assert progress["agent"] == "mimo"
    assert progress["log_note"] == "日志文件已创建，等待 worker 输出。"


def test_portfolio_mission_summary_includes_pacer_evidence(tmp_path):
    project = tmp_path / "app"
    project.mkdir()
    ws = init_workspace(project / ".agent-workspace", with_demo=False)
    mission = create_mission(
        workspace_root=ws.root,
        objective="Pacer smoke test",
        repo_root=project,
        plan_id="p-evidence",
        budget_policy=default_budget_policy(),
        mission_id="m-evidence",
        status="verified",
    )
    mission["stop_reason"] = "verified"
    save_mission(ws.root, mission)
    append_round(ws.root, "m-evidence", {"round": 0, "type": "dispatch_preview", "status": "preview"})
    append_round(ws.root, "m-evidence", {"round": 1, "type": "verification", "status": "pass"})
    log_path = ws.root / "chief_plans" / "p-evidence" / "logs" / "track_1_mimo-initial.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('{"stdout_tail":"mimo unified diff applied."}', encoding="utf-8")
    append_worker_record(
        ws.root,
        "p-evidence",
        {
            "schema_version": 1,
            "plan_id": "p-evidence",
            "attempt": "initial",
            "track_id": "track_1_mimo",
            "agent": "mimo",
            "status": "completed",
            "exit_code": 0,
            "elapsed_seconds": 1.25,
            "cwd": str(tmp_path / "app.checkpoint-worktrees" / "m-evidence" / "track-1-mimo"),
            "command": "low-cost-backend patch-worker",
            "log_path": str(log_path),
            "backend": {"name": "mimo", "model": "mimo-v2.5-pro"},
        },
    )
    save_verification(
        ws.root,
        "p-evidence",
        {
            "verdict": "pass",
            "command_verification": {"command": "cmd /c dir PACER_MIMO_SMOKE.md >nul"},
        },
    )

    payload = build_portfolio_data([project])
    evidence = payload["projects"][0]["missions"][0]["pacer_evidence"]

    assert evidence["worker_status"] == "completed"
    assert evidence["backend"] == "mimo"
    assert evidence["model"] == "mimo-v2.5-pro"
    assert evidence["verification_verdict"] == "pass"
    assert evidence["verification_command"] == "cmd /c dir PACER_MIMO_SMOKE.md >nul"
    assert "mimo unified diff applied" in evidence["log_tail"]
