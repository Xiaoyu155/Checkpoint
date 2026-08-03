from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from visual_agent.dashboard import (
    DASHBOARD_HTML,
    _bind_dashboard_server,
    _launch_snapshot,
    build_dashboard_data,
    start_workbench_mission,
)
from visual_agent.chief_queue import submit_mission_queue_item
from visual_agent.missions import create_mission, default_budget_policy, load_mission
from visual_agent.programs import create_program_from_plan
from visual_agent.workbench_board import archive_all_missions_now, archive_mission_now
from visual_agent.workspace import init_workspace


def _wait_launch(launch_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        matches = [item for item in _launch_snapshot() if item.get("launch_id") == launch_id]
        if matches and matches[0].get("state") in {"done", "error"}:
            return matches[0]
        time.sleep(0.2)
    raise AssertionError("workbench launch did not finish in time")


def test_workbench_rejects_empty_goal(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    result = start_workbench_mission(
        workspace_root=ws.root, repo_root=str(tmp_path), goal="   ",
        test_command="", agent="claude-code", execute=False,
    )
    assert result["ok"] is False
    assert "goal" in result["error"] or "目标" in result["error"]


def test_workbench_preview_launch_records_mission(tmp_path):
    # A vague goal should still launch but stop at the clarity/coverage gate,
    # proving the workbench drives the real mission machinery (no execution).
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    result = start_workbench_mission(
        workspace_root=ws.root, repo_root=str(tmp_path),
        goal="确认现有代码是否正确并给出说明",
        test_command="", agent="claude-code", execute=False,
    )
    assert result["ok"] is True
    final = _wait_launch(result["launch_id"])
    assert final["state"] == "done"
    # Preview never executes a worker; it stops with a reason, not "verified".
    assert final["status"] in {"stopped", "preview", "blocked"}


def test_workbench_execute_defaults_to_manual_merge(tmp_path, monkeypatch):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "status": "verified",
            "stop_reason": "verified",
            "mission": {"mission_id": "m1"},
        }

    monkeypatch.setattr("visual_agent.chief_run.run_chief_mission", fake_run)
    result = start_workbench_mission(
        workspace_root=ws.root,
        repo_root=str(tmp_path),
        goal="修复一个明确问题",
        test_command="pytest -q",
        agent="codex",
        execute=True,
    )
    assert result["ok"] is True
    _wait_launch(result["launch_id"])
    assert captured["merge"] is False
    assert captured["execute"] is False
    assert captured["dry_run"] is True


def test_workbench_launch_persists_task_contract(tmp_path, monkeypatch):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)

    def fake_run(**kwargs):
        create_mission(
            workspace_root=ws.root,
            objective=str(kwargs["goal"]),
            repo_root=tmp_path,
            plan_id="contract-plan",
            budget_policy=default_budget_policy(),
            mission_id="contract-mission",
            status="preview",
        )
        return {
            "status": "preview",
            "stop_reason": "preview_only",
            "mission": {"mission_id": "contract-mission"},
        }

    monkeypatch.setattr("visual_agent.chief_run.run_chief_mission", fake_run)

    result = start_workbench_mission(
        workspace_root=ws.root,
        repo_root=str(tmp_path),
        goal="修复一个明确问题",
        test_command="pytest -q",
        agent="codex",
        execute=False,
        merge_policy="manual",
    )

    assert result["ok"] is True
    _wait_launch(result["launch_id"])
    resolved_test_command = result["test_command"]
    assert resolved_test_command.endswith(" -m pytest -q")
    mission = load_mission(ws.root, "contract-mission") or {}
    assert mission["test_command"] == resolved_test_command
    assert mission["agent"] == "codex"
    assert mission["merge_policy"] == "manual"
    data = build_dashboard_data(ws.root)
    summary = next(item for item in data["missions"] if item["mission_id"] == "contract-mission")
    assert summary["test_command"] == resolved_test_command
    assert summary["agent"] == "codex"
    assert summary["repo_root"] == str(tmp_path.resolve())


def test_workbench_launch_carries_requirement_contract(tmp_path, monkeypatch):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        create_mission(
            workspace_root=ws.root,
            objective=str(kwargs["goal"]),
            repo_root=tmp_path,
            plan_id="requirement-plan",
            budget_policy=default_budget_policy(),
            mission_id="requirement-mission",
            status="preview",
        )
        return {
            "status": "preview",
            "stop_reason": "preview_only",
            "mission": {"mission_id": "requirement-mission"},
        }

    monkeypatch.setattr("visual_agent.chief_run.run_chief_mission", fake_run)

    result = start_workbench_mission(
        workspace_root=ws.root,
        repo_root=str(tmp_path),
        goal="修复结算页 checkout 金额显示",
        test_command="pytest -q",
        agent="codex",
        execute=False,
        intake={
            "source": "goal_intake",
            "input_goal": "改一下",
            "suggested_goal": "修复结算页 checkout 金额显示",
            "acceptance_hint": "金额必须等于行项目之和",
            "clarifying_questions": ["完成后用户看到什么？"],
            "answers": ["保留现有优惠展示"],
            "model_id": "codex:cli",
            "intake_policy": "selected_agent_cli",
        },
        answers=["金额必须等于行项目之和", "保留现有优惠展示"],
    )

    assert result["ok"] is True
    final = _wait_launch(result["launch_id"])
    assert final["mission_id"] == "requirement-mission"
    contract = result["requirement_contract"]
    assert contract["input_goal"] == "改一下"
    assert contract["final_goal"] == "修复结算页 checkout 金额显示"
    assert contract["model_id"] == "codex:cli"
    assert contract["intake_policy"] == "selected_agent_cli"
    assert contract["answers"] == ["保留现有优惠展示", "金额必须等于行项目之和"]
    assert captured["requirement_contract"] == contract
    assert result["spec"]["requirement_contract"] == contract

    pipeline = json.loads(Path(result["state_path"]).read_text(encoding="utf-8"))
    assert pipeline["context"]["request"]["requirement_contract"] == contract
    mission = load_mission(ws.root, "requirement-mission") or {}
    assert mission["requirement_contract"] == contract
    data = build_dashboard_data(ws.root)
    summary = next(item for item in data["missions"] if item["mission_id"] == "requirement-mission")
    assert summary["requirement_contract"] == contract


def test_workbench_auto_detects_test_command_when_empty(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}', encoding="utf-8")
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"status": "preview", "stop_reason": "preview_only", "mission": {"mission_id": "m1"}}

    monkeypatch.setattr("visual_agent.chief_run.run_chief_mission", fake_run)
    result = start_workbench_mission(
        workspace_root=ws.root,
        repo_root=str(tmp_path),
        goal="修复一个明确问题",
        test_command="",
        agent="codex",
        execute=False,
    )

    assert result["test_command"] == "npm test"
    _wait_launch(result["launch_id"])
    assert captured["test_command"] == "npm test"


def test_workbench_execute_rejects_missing_real_test_command(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)

    result = start_workbench_mission(
        workspace_root=ws.root,
        repo_root=str(tmp_path),
        goal="修复一个明确问题",
        test_command="",
        agent="codex",
        execute=True,
    )

    assert result["ok"] is False
    assert "真实验收命令" in result["error"]
    assert result["verification_profile"]["status"] == "not_found"


def test_workbench_execute_allows_manual_livekit_verification(tmp_path, monkeypatch):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"status": "preview", "stop_reason": "preview_only", "mission": {"mission_id": "m-livekit"}}

    monkeypatch.setattr("visual_agent.chief_run.run_chief_mission", fake_run)
    result = start_workbench_mission(
        workspace_root=ws.root,
        repo_root=str(tmp_path),
        goal="完成LiveKit真机验证：真实手机弱网和户外噪声语音通话验收",
        test_command="",
        agent="codex",
        execute=True,
    )

    assert result["ok"] is True
    assert result["manual_verification"] is True
    _wait_launch(result["launch_id"])
    assert captured["allow_coverage_gap"] is True
    assert captured["execute"] is False
    assert captured["dry_run"] is True


def test_workbench_execute_allows_review_plan_report_without_test_command(tmp_path, monkeypatch):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    captured = {}
    mission_id = "m-review"

    def fake_run(**kwargs):
        captured["run"] = kwargs
        return {"status": "preview", "stop_reason": "preview_only", "mission": {"mission_id": mission_id}}

    def fake_background(**kwargs):
        captured["background"] = kwargs
        return {"status": "background_started", "stop_reason": "", "background": {"pid": 2468}}

    monkeypatch.setattr("visual_agent.chief_run.run_chief_mission", fake_run)
    monkeypatch.setattr("visual_agent.chief_background.start_background_chief_run", fake_background)

    result = start_workbench_mission(
        workspace_root=ws.root,
        repo_root=str(tmp_path),
        goal="对目标产品进行审查并生成下一阶段开发计划",
        test_command="",
        agent="mimo",
        execute=True,
    )

    assert result["ok"] is True
    assert result["review_plan"] is True
    assert result["verification_profile"]["status"] == "report_required"
    final = _wait_launch(result["launch_id"])
    assert final["status"] == "background_started"
    assert captured["run"]["allow_coverage_gap"] is True
    assert captured["run"]["execute"] is False
    assert captured["run"]["dry_run"] is True
    assert captured["background"]["allow_coverage_gap"] is True


def test_workbench_execute_starts_background_worker(tmp_path, monkeypatch):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    mission_id = "m-bg"
    captured = {}

    def fake_run(**kwargs):
        captured["run"] = kwargs
        return {"status": "preview", "stop_reason": "preview_only", "mission": {"mission_id": mission_id}}

    def fake_background(**kwargs):
        captured["background"] = kwargs
        return {
            "status": "background_started",
            "stop_reason": "",
            "background": {"pid": 12345},
        }

    monkeypatch.setattr("visual_agent.chief_run.run_chief_mission", fake_run)
    monkeypatch.setattr("visual_agent.chief_background.start_background_chief_run", fake_background)

    result = start_workbench_mission(
        workspace_root=ws.root,
        repo_root=str(tmp_path),
        goal="修复一个明确问题",
        test_command="pytest -q",
        agent="mimo",
        execute=True,
        merge_policy="auto",
    )

    final = _wait_launch(result["launch_id"])

    assert final["status"] == "background_started"
    assert final["mission_id"] == mission_id
    assert final["background_pid"] == 12345
    assert captured["run"]["agents"] == ("mimo",)
    assert captured["run"]["execute"] is False
    assert captured["run"]["dry_run"] is True
    assert captured["background"]["mission_id"] == mission_id
    assert captured["background"]["agents"] == ("mimo",)
    assert captured["background"]["run_profile"] == "supervised"
    assert captured["background"]["merge"] is True


def test_workbench_execute_passes_allow_dirty_to_background_worker(tmp_path, monkeypatch):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    mission_id = "m-dirty"
    captured = {}

    def fake_run(**kwargs):
        captured["run"] = kwargs
        return {"status": "preview", "stop_reason": "preview_only", "mission": {"mission_id": mission_id}}

    def fake_background(**kwargs):
        captured["background"] = kwargs
        return {"status": "background_started", "stop_reason": "", "background": {"pid": 4321}}

    monkeypatch.setattr("visual_agent.chief_run.run_chief_mission", fake_run)
    monkeypatch.setattr("visual_agent.chief_background.start_background_chief_run", fake_background)

    result = start_workbench_mission(
        workspace_root=ws.root,
        repo_root=str(tmp_path),
        goal="修复一个明确问题",
        test_command="pytest -q",
        agent="mimo",
        execute=True,
        allow_dirty=True,
    )

    final = _wait_launch(result["launch_id"])

    assert final["status"] == "background_started"
    assert captured["run"]["allow_dirty"] is True
    assert captured["background"]["allow_dirty"] is True


def test_workbench_writes_pipeline_state(tmp_path, monkeypatch):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    mission_id = "m-state"

    def fake_run(**kwargs):
        return {"status": "preview", "stop_reason": "preview_only", "mission": {"mission_id": mission_id}}

    monkeypatch.setattr("visual_agent.chief_run.run_chief_mission", fake_run)
    result = start_workbench_mission(
        workspace_root=ws.root,
        repo_root=str(tmp_path),
        goal="修复一个明确问题",
        test_command="pytest -q",
        agent="codex",
        execute=False,
        spec={
            "scope": ["src/visual_agent"],
            "plan": ["修复一个明确问题"],
            "test": ["pytest -q"],
            "risk": ["低风险工作台派发测试"],
            "rollback": ["回退派发包装改动"],
        },
    )

    final = _wait_launch(result["launch_id"])
    payload = json.loads(Path(result["state_path"]).read_text(encoding="utf-8"))
    mission_payload = json.loads((ws.root / "missions" / mission_id / "state.json").read_text(encoding="utf-8"))
    assert final["mission_id"] == mission_id
    assert payload["current_state"] == "REVIEW"
    assert mission_payload["context"]["spec"]["test"] == ["pytest -q"]


def test_mission_start_api_rejects_missing_spec(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    server = _bind_dashboard_server("127.0.0.1", 0, ws.root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/mission/start",
            data=json.dumps({"goal": "修复一个明确问题"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8-sig"))
            assert exc.code == 400
            assert body["error_code"] == "spec_validation_failed"
        else:
            raise AssertionError("missing spec should return 400")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_data_exposes_workbench_fields(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    data = build_dashboard_data(ws.root)
    assert "installed_agents" in data
    assert "launches" in data
    assert "repo_root" in data


def test_archive_mission_hides_it_from_board(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    mission = create_mission(
        workspace_root=ws.root,
        objective="清理待验收任务",
        repo_root=tmp_path,
        plan_id="plan-archive",
        budget_policy=default_budget_policy(),
        mission_id="mission-archive",
        status="stopped",
    )

    result = archive_mission_now(ws.root, mission["mission_id"])
    assert result["ok"] is True

    data = build_dashboard_data(ws.root)
    assert all(item["mission_id"] != mission["mission_id"] for item in data["missions"])

    state_path = ws.root / "missions" / mission["mission_id"] / "mission.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["status"] == "archived"
    assert payload["hidden"] is True


def test_archive_all_missions_hides_non_running_only(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    create_mission(
        workspace_root=ws.root,
        objective="失败任务",
        repo_root=tmp_path,
        plan_id="plan-stopped",
        budget_policy=default_budget_policy(),
        mission_id="mission-stopped",
        status="stopped",
    )
    create_mission(
        workspace_root=ws.root,
        objective="待办任务",
        repo_root=tmp_path,
        plan_id="plan-created",
        budget_policy=default_budget_policy(),
        mission_id="mission-created",
        status="created",
    )
    create_mission(
        workspace_root=ws.root,
        objective="运行任务",
        repo_root=tmp_path,
        plan_id="plan-running",
        budget_policy=default_budget_policy(),
        mission_id="mission-running",
        status="background_running",
    )
    create_mission(
        workspace_root=ws.root,
        objective="验收通过任务",
        repo_root=tmp_path,
        plan_id="plan-verified",
        budget_policy=default_budget_policy(),
        mission_id="mission-verified",
        status="verified",
    )

    result = archive_all_missions_now(ws.root)
    data = build_dashboard_data(ws.root)

    assert result["ok"] is True
    assert result["archived"] == 2
    assert result["skipped_running"] == 1
    assert [item["mission_id"] for item in data["missions"]] == ["mission-verified", "mission-running"]


def test_dashboard_data_exposes_mission_queue_context(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    mission = create_mission(
        workspace_root=ws.root,
        objective="Fix checkout",
        repo_root=tmp_path,
        plan_id="p1",
        budget_policy=default_budget_policy(),
        mission_id="m1",
        status="preview",
    )
    submit_mission_queue_item(
        workspace_root=ws.root,
        mission_id=mission["mission_id"],
        agent="codex",
        test_command="npm test",
        merge_policy="auto",
    )

    data = build_dashboard_data(ws.root)

    assert data["queue"][0]["agent"] == "codex"
    assert data["queue"][0]["test_command"] == "npm test"
    assert data["queue"][0]["merge_policy"] == "auto"


def test_dashboard_data_exposes_programs(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = tmp_path / "plan.md"
    plan.write_text("- [ ] Implement voice overlay\n", encoding="utf-8")
    program = create_program_from_plan(source_file=plan, workspace_root=ws.root, repo_root=tmp_path)

    data = build_dashboard_data(ws.root)

    assert data["programs"][0]["program_id"] == program["program_id"]


def test_dashboard_html_has_workbench_form():
    assert "托管队列" in DASHBOARD_HTML
    assert "项目托管" in DASHBOARD_HTML
    assert "main-chat" in DASHBOARD_HTML
    assert "确认任务" in DASHBOARD_HTML
    assert "sendMainChat" in DASHBOARD_HTML
    assert "applyMainDraftToMission" in DASHBOARD_HTML
    assert "openTaskIntake" in DASHBOARD_HTML
    assert "applyIntakeToMission" not in DASHBOARD_HTML
    assert "/api/mission/start" in DASHBOARD_HTML
    assert "startMission" in DASHBOARD_HTML


def test_dashboard_html_has_mission_desk_and_relay_rail():
    # The workbench is now a cockpit: a mission desk plus a right-side resource rail,
    # not the old four-column kanban spread.
    for text in ("任务列表", "missionList", "待处理", "停止", "中转站", "订阅额度"):
        assert text in DASHBOARD_HTML
    assert "legacy-board" not in DASHBOARD_HTML
    assert "/api/mission/merge" in DASHBOARD_HTML
    assert "/api/mission/delete-all" in DASHBOARD_HTML
    assert "deleteAllMissions" in DASHBOARD_HTML
    assert "清理非运行任务" in DASHBOARD_HTML
    assert "mergeMission" in DASHBOARD_HTML
    assert "确认把这个已验收任务合并到主分支" in DASHBOARD_HTML
    assert "重试会再次消耗模型额度" in DASHBOARD_HTML
    assert "diffSection" in DASHBOARD_HTML


def test_dashboard_data_missions_carry_board_fields(tmp_path):
    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    create_mission(
        workspace_root=ws.root,
        objective="Fix checkout",
        repo_root=tmp_path,
        plan_id="p1",
        budget_policy=default_budget_policy(),
        mission_id="m1",
        status="verified",
    )

    data = build_dashboard_data(ws.root)

    mission = data["missions"][0]
    assert mission["board_column"] == "in_review"
    assert mission["can_merge"] is True
    assert mission["merge_state"] == ""


def test_dashboard_mimo_efficiency_from_worker_records(tmp_path):
    """Dashboard data must expose mimo_efficiency with core metrics,
    and MiMo backend usage must count as savings, not spend."""
    from visual_agent.chief_plans_store import append_worker_record, save_plan

    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan_id = "20260704-100000-efficiency-test"
    # Save a minimal plan so load_worker_records can find the directory.
    save_plan(
        {"objective": "test efficiency", "worker_tracks": []},
        workspace_root=ws.root,
        plan_id=plan_id,
    )

    # MiMo backend record: cost_is_savings=True -> should be savings
    append_worker_record(ws.root, plan_id, {
        "status": "completed",
        "elapsed_seconds": 120.0,
        "usage": {"cost_usd": 0.05, "cost_is_savings": True, "input_tokens": 1000, "output_tokens": 500},
        "backend": {"name": "mimo", "model": "mimo-v2.5-pro"},
    })

    # Subscription record (no backend) -> should be spend
    append_worker_record(ws.root, plan_id, {
        "status": "completed",
        "elapsed_seconds": 90.0,
        "usage": {"cost_usd": 0.12, "input_tokens": 2000, "output_tokens": 800},
    })

    data = build_dashboard_data(ws.root)
    me = data["value"]["mimo_efficiency"]

    assert me["mimo_runs"] == 1
    assert me["saved_usd"] == 0.05
    assert me["spent_usd"] == 0.12
    assert me["saved_quota_percent"] == 29.4
    assert me["saved_minutes"] > 0
    assert me["efficiency_gain_percent"] > 0
    assert me["capability_score"] > 0
    # Labels should be Chinese
    assert "额度" in me["labels"]["saved_usd"]
    assert "套餐额度" in me["labels"]["saved_quota_percent"]
    assert "时间" in me["labels"]["saved_minutes"]


def test_dashboard_mission_carries_own_efficiency_metrics(tmp_path):
    from visual_agent.chief_plans_store import append_worker_record, save_plan

    ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    save_plan(
        {"objective": "验证工作台效率展示", "worker_tracks": []},
        workspace_root=ws.root,
        plan_id="plan-eff-one",
    )
    mission = create_mission(
        workspace_root=ws.root,
        objective="验证工作台效率展示",
        repo_root=tmp_path,
        plan_id="plan-eff-one",
        budget_policy=default_budget_policy(),
        mission_id="mission-eff-one",
        status="verified",
    )
    append_worker_record(
        ws.root,
        "plan-eff-one",
        {
            "status": "failed",
            "elapsed_seconds": 120,
            "backend": {"name": "mimo"},
            "usage": {"cost_is_savings": True, "backend": "mimo"},
        },
    )

    data = build_dashboard_data(ws.root)
    item = next(m for m in data["missions"] if m["mission_id"] == mission["mission_id"])

    assert item["efficiency"]["mimo_runs"] == 1
    assert item["efficiency"]["saved_minutes"] > 0
    assert item["efficiency"]["saved_usd"] > 0
    assert item["efficiency"]["saved_quota_percent"] == 100.0
    assert item["efficiency"]["actual_worker_seconds"] == 120
    assert item["efficiency"]["actual_cost_available"] is False
    assert item["efficiency"]["actual_cost_label"] == "未回传"
    assert "效率" in item["efficiency"]["labels"]["efficiency_gain_percent"]


def test_dashboard_html_contains_efficiency_chinese_labels():
    """The dashboard HTML must show efficiency metrics in Chinese."""
    assert "低成本后端效率" in DASHBOARD_HTML
    assert "套餐额度" in DASHBOARD_HTML
    assert "时间节省" in DASHBOARD_HTML
    assert "综合效率" in DASHBOARD_HTML
    assert "本任务实际消耗" in DASHBOARD_HTML
