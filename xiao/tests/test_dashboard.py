from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from visual_agent.chief_plans_store import append_worker_record, save_plan, save_verification
from visual_agent.commercial_config import CommercialConfig
from visual_agent.dashboard.api import refine_goal_intake, run_chat
from visual_agent.dashboard import DASHBOARD_HTML, _bind_dashboard_server, build_dashboard_data, build_mission_detail, start_worker, stop_worker
from visual_agent.missions import missions_dir, save_mission
from visual_agent.user_profile import LocalUserProfile
from visual_agent.workbench_model_config import WorkbenchModelConfig


def test_dashboard_data_on_empty_workspace(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    data = build_dashboard_data(workspace)

    assert data["product"] == "Pacer"
    assert data["orchestrator"] == "Pacer"
    assert data["engine"] == "Checkpoint"
    assert data["missions"] == []
    assert data["plans"] == []
    # Value panel is always present so the user can see benefit accrue.
    assert data["value"]["verified"] == 0
    assert data["value"]["tier_counts"] == {"cheap": 0, "standard": 0, "strong": 0}
    assert data["value"]["spent_usd"] == 0.0
    # MiMo efficiency metrics are present even when empty.
    me = data["value"]["mimo_efficiency"]
    assert me["mimo_runs"] == 0
    assert me["saved_usd"] == 0.0
    assert me["saved_quota_percent"] == 0.0
    assert me["saved_minutes"] == 0.0
    assert me["efficiency_gain_percent"] == 0
    assert me["capability_score"] == 0.0
    assert "summary" in data["subscription_quota"]
    assert "level" in data["subscription_quota"]["summary"]
    assert "promotion_readiness" in data
    assert "checks" in data["promotion_readiness"]
    assert "core_readiness" in data
    assert "checks" in data["core_readiness"]
    assert any(check["id"] == "intake" for check in data["core_readiness"]["checks"])
    assert "labels" in me
    # agents doctor always reports the known profiles (installed or not).
    assert {a["agent"] for a in data["agents"]} >= {"codex", "claude-code"}


def test_dashboard_api_rejects_cross_site_post(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/chat"
    try:
        request = urllib.request.Request(
            url,
            data=b'{"message":"test"}',
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:  # pragma: no cover - defensive failure path
            raise AssertionError("cross-site POST was not rejected")
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_api_requires_json_content_type(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/api/chat"
    try:
        request = urllib.request.Request(
            url,
            data=b'{"message":"test"}',
            method="POST",
            headers={
                "Content-Type": "text/plain",
                "Origin": f"http://127.0.0.1:{port}",
            },
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 415
        else:  # pragma: no cover - defensive failure path
            raise AssertionError("text/plain POST was not rejected")
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_profile_endpoint_round_trips(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PACER_PROFILE_PATH", str(tmp_path / "profile.json"))
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/profile",
            data=json.dumps({"email": "user@example.com", "display_name": "小鱼", "organization": "Pacer"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        saved = json.loads(urllib.request.urlopen(request, timeout=5).read().decode("utf-8-sig"))
        loaded = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/profile", timeout=5).read().decode("utf-8-sig"))
        assert saved["ok"] is True
        assert loaded["configured"] is True
        assert loaded["email"] == "user@example.com"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_commercial_config_endpoint_round_trips(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "commercial.json"
    monkeypatch.setenv("PACER_COMMERCIAL_CONFIG", str(config_path))
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        payload = {
            "supabase_url": "https://pacer.supabase.co",
            "supabase_anon_key": "anon-key",
            "supabase_service_role_key": "service-role",
            "google_oauth_configured": True,
            "google_client_id": "google-client",
            "google_client_secret": "google-secret",
            "stripe_publishable_key": "pk_test_123",
            "stripe_secret_key": "sk_test_123",
            "stripe_webhook_secret": "whsec_123",
            "stripe_price_id": "price_123",
            "stripe_customer_portal_url": "https://billing.stripe.com/p/session",
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/commercial-config",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        saved = json.loads(urllib.request.urlopen(request, timeout=5).read().decode("utf-8-sig"))
        loaded = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/commercial-config", timeout=5).read().decode("utf-8-sig"))

        assert saved["ok"] is True
        assert loaded["auth_configured"] is True
        assert loaded["billing_configured"] is True
        assert loaded["portal_configured"] is True
        assert loaded["stripe_secret_key"] == "****"
        assert "sk_test_123" in config_path.read_text(encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_events_stream_sends_snapshot_event(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/events", method="GET")
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.headers.get_content_type() == "text/event-stream"
            lines: list[str] = []
            for _ in range(6):
                line = response.readline().decode("utf-8").strip()
                if line:
                    lines.append(line)
                if any(item.startswith("data:") for item in lines):
                    break
            joined = "\n".join(lines)
            assert "event: snapshot" in joined
            assert "snapshot_changed" in joined
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_promotion_readiness_lists_user_required_items(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    (workspace / "missions").mkdir(parents=True)
    monkeypatch.setattr("visual_agent.dashboard.data.load_user_profile", lambda: LocalUserProfile())
    monkeypatch.setattr("visual_agent.dashboard.data.load_notification_config", lambda: None)
    monkeypatch.setattr("visual_agent.dashboard.data.load_commercial_config", lambda: CommercialConfig())
    monkeypatch.setattr("visual_agent.dashboard.data.load_workbench_model_config", lambda: WorkbenchModelConfig(api_key=""))

    data = build_dashboard_data(workspace)
    readiness = data["promotion_readiness"]

    assert readiness["score"] < 100
    assert readiness["checks"]
    assert any("邮箱" in item for item in readiness["user_required"])
    assert any(check["id"] == "relay" for check in readiness["checks"])
    assert any(check["id"] == "auth_supabase" for check in readiness["checks"])
    assert any("Stripe" in item for item in readiness["user_required"])
    usage_meter = next(check for check in readiness["checks"] if check["id"] == "stripe_usage_meter")
    assert usage_meter["status"] == "success"


def test_dashboard_promotion_readiness_marks_commercial_stack_configured(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    (workspace / "missions").mkdir(parents=True)
    monkeypatch.setattr(
        "visual_agent.dashboard.data.load_commercial_config",
        lambda: CommercialConfig(
            supabase_url="https://pacer.supabase.co",
            supabase_anon_key="anon-key",
            google_oauth_configured=True,
            google_client_id="google-client",
            stripe_publishable_key="pk_test_123",
            stripe_secret_key="sk_test_123",
            stripe_webhook_secret="whsec_123",
            stripe_price_id="price_123",
            stripe_customer_portal_url="https://billing.stripe.com/p/session",
        ),
    )

    data = build_dashboard_data(workspace)
    checks = {item["id"]: item for item in data["promotion_readiness"]["checks"]}

    assert checks["auth_supabase"]["status"] == "success"
    assert checks["google_oauth"]["status"] == "success"
    assert checks["stripe_billing"]["status"] == "success"
    assert checks["stripe_portal"]["status"] == "success"


def test_dashboard_core_readiness_focuses_on_product_loop(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / ".agent-workspace"
    (workspace / "missions").mkdir(parents=True)
    monkeypatch.setattr(
        "visual_agent.dashboard.data.get_agents_cached",
        lambda: [{"agent": "codex", "installed": True}],
    )
    monkeypatch.setattr(
        "visual_agent.dashboard.data.worker_status",
        lambda: {"running": True, "pid": 1234},
    )

    data = build_dashboard_data(workspace)
    readiness = data["core_readiness"]
    checks = {item["id"]: item for item in readiness["checks"]}

    assert checks["agents"]["status"] == "success"
    assert checks["intake"]["status"] == "success"
    assert checks["worker"]["status"] == "success"
    assert "auth_supabase" not in checks
    assert "stripe_billing" not in checks
    assert readiness["score"] >= 50



def test_dashboard_chat_batch_shim_keeps_prompt_off_command_line(monkeypatch) -> None:
    captured = {}

    class Completed:
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr("shutil.which", lambda exe: r"C:\Tools\claude.cmd")

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")
        return Completed()

    monkeypatch.setattr("subprocess.run", fake_run)

    result = run_chat({"agent": "claude-code", "message": 'test " & calc.exe & "'})

    assert result["ok"] is True
    assert captured["argv"] == [r"C:\Tools\claude.cmd", "-p"]
    assert "calc.exe" not in " ".join(captured["argv"])
    assert 'test " & calc.exe & "' in captured["input"]


def test_dashboard_data_reads_status_file(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    (tmp_path / ".visual-agent-status.md").write_text("## Status: PASSING\n", encoding="utf-8")

    data = build_dashboard_data(workspace)

    assert data["status"]["state"] == "PASSING"


def test_dashboard_work_traces_include_plan_saved_at_timestamp(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    saved_at = "2026-07-07T08:09:10+00:00"
    save_plan(
        {
            "objective": "留下带时间戳的计划痕迹",
            "status": "planned",
            "saved_at": saved_at,
        },
        workspace_root=workspace,
        plan_id="20260707-080910-plan",
    )

    data = build_dashboard_data(workspace)

    trace = next(item for item in data["work_traces"] if item["kind"] == "plan")
    assert trace["timestamp"] == saved_at
    assert "留下带时间戳的计划痕迹" in trace["title"]


def test_dashboard_mission_detail_includes_rounds_and_report(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    mission = {"mission_id": "m1", "objective": "fix checkout", "status": "verified", "stop_reason": "verified"}
    save_mission(workspace, mission)
    mdir = missions_dir(workspace) / "m1"
    (mdir / "rounds.jsonl").write_text(json.dumps({"round": 1, "type": "verification", "status": "pass"}) + "\n", encoding="utf-8")
    (mdir / "final_report.md").write_text("# Mission verified\n", encoding="utf-8")

    detail = build_mission_detail(workspace, "m1")

    assert detail["mission"]["objective"] == "fix checkout"
    assert len(detail["rounds"]) == 1
    assert "verified" in detail["final_report"]
    assert detail["live_logs"]["count"] == 0


def test_dashboard_mission_detail_includes_live_worker_log_tail(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix checkout",
        "status": "running",
        "stop_reason": "",
    }
    save_mission(workspace, mission)
    logs = workspace / "chief_plans" / "p1" / "logs"
    logs.mkdir(parents=True)
    (logs / "track-initial.log").write_text("first\nworker is editing\nlatest line\n", encoding="utf-8")

    detail = build_mission_detail(workspace, "m1")

    assert detail["live_logs"]["count"] == 1
    assert "latest line" in detail["live_logs"]["latest_tail"]
    assert detail["live_logs"]["latest_path"].endswith("track-initial.log")


def test_dashboard_mission_detail_includes_pacer_evidence(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix checkout",
        "status": "verified",
        "stop_reason": "verified",
    }
    save_mission(workspace, mission)
    log_dir = workspace / "chief_plans" / "p1" / "logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "track_1_mimo-initial.log"
    log_path.write_text("{\"stdout_tail\":\"mimo unified diff applied.\"}", encoding="utf-8")
    save_plan(
        {
            "plan_id": "p1",
            "objective": "fix checkout",
            "status": "needs_workflow_coverage",
            "verification_mode": "command",
        },
        workspace_root=workspace,
        plan_id="p1",
    )
    append_worker_record(
        workspace,
        "p1",
        {
            "schema_version": 1,
            "plan_id": "p1",
            "attempt": "initial",
            "track_id": "track_1_mimo",
            "agent": "mimo",
            "status": "completed",
            "exit_code": 0,
            "elapsed_seconds": 1.5,
            "cwd": str(tmp_path / "worktree"),
            "command": "low-cost-backend patch-worker",
            "log_path": str(log_path),
            "backend": {"name": "mimo", "model": "mimo-v2.5-pro"},
        },
    )
    save_verification(
        workspace,
        "p1",
        {
            "verdict": "pass",
            "command_verification": {"command": "cmd /c dir PACER_MIMO_SMOKE.md >nul"},
        },
    )

    detail = build_mission_detail(workspace, "m1")

    assert detail["worker_records"][0]["agent"] == "mimo"
    assert detail["verification"]["verdict"] == "pass"
    assert detail["pacer_evidence"]["worker_status"] == "completed"
    assert detail["pacer_evidence"]["backend"] == "mimo"
    assert detail["pacer_evidence"]["verification_command"] == "cmd /c dir PACER_MIMO_SMOKE.md >nul"
    assert detail["pacer_evidence"]["verification_mode"] == "command"
    assert "mimo unified diff applied" in detail["pacer_evidence"]["log_tail"]
    assert detail["progress"]["stage"] == "verified"
    assert detail["pacer_evidence"]["stage"] == "verified"


def test_dashboard_data_propagates_command_verification_mode(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    mission = {
        "mission_id": "m1",
        "plan_id": "p1",
        "objective": "fix checkout",
        "status": "stopped",
        "stop_reason": "coverage_gap",
    }
    save_mission(workspace, mission)
    save_plan(
        {
            "plan_id": "p1",
            "objective": "fix checkout",
            "status": "needs_workflow_coverage",
            "verification_mode": "command",
        },
        workspace_root=workspace,
        plan_id="p1",
    )
    save_verification(
        workspace,
        "p1",
        {
            "verdict": "pass",
            "command_verification": {"command": "python -m pytest -q"},
        },
    )

    data = build_dashboard_data(workspace)

    mission_payload = data["missions"][0]
    assert mission_payload["verification_mode"] == "command"
    assert mission_payload["pacer_evidence"]["verification_mode"] == "command"


def test_start_worker_uses_cli_module_entrypoint(tmp_path, monkeypatch) -> None:
    captured = {}

    class FakeProc:
        pid = 1234

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr("visual_agent.dashboard.api.hidden_subprocess_kwargs", lambda *, detached=False: {"creationflags": 12345})
    monkeypatch.setattr("subprocess.Popen", fake_popen)

    result = start_worker(tmp_path / ".agent-workspace")
    stop_worker()

    assert result["ok"] is True
    assert captured["cmd"][1:4] == ["-m", "visual_agent.cli", "mission"]
    assert captured["kwargs"]["creationflags"] == 12345


def test_dashboard_html_is_self_contained() -> None:
    # No external script/style URLs — the page must work offline/local-only.
    assert "<title>Pacer 工作台</title>" in DASHBOARD_HTML
    assert "http://" not in DASHBOARD_HTML.split("<script>")[0]


def test_dashboard_inline_js_has_valid_syntax(tmp_path) -> None:
    # The page JS lives in a Python string, where one swallowed backslash (e.g.
    # a \\ regex becoming \) produces a SyntaxError that kills every button on
    # the page while all API tests stay green. Parse it for real.
    import re
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    match = re.search(r"<script>(.*)</script>", DASHBOARD_HTML, re.S)
    assert match, "inline <script> block missing"
    js_file = tmp_path / "inline.js"
    js_file.write_text(match.group(1), encoding="utf-8")
    completed = subprocess.run([node, "--check", str(js_file)], capture_output=True, text=True)
    assert completed.returncode == 0, f"页面内联 JS 语法错误（会导致工作台所有按钮失灵）:\n{completed.stderr}"


def test_dashboard_html_has_mimo_efficiency_display() -> None:
    # The HTML must display low-cost backend efficiency metrics with Chinese labels.
    assert "低成本后端效率" in DASHBOARD_HTML
    assert "套餐额度" in DASHBOARD_HTML
    assert "订阅额度" in DASHBOARD_HTML
    assert "中转站" in DASHBOARD_HTML
    assert "流式工作任务" in DASHBOARD_HTML
    assert "自己的付费模型网关" in DASHBOARD_HTML
    assert "时间节省" in DASHBOARD_HTML
    assert "综合效率" in DASHBOARD_HTML
    assert "rSavedUsd" in DASHBOARD_HTML
    assert "rQuotaSave" in DASHBOARD_HTML
    assert "rSavedMin" in DASHBOARD_HTML
    assert "rEfficiency" in DASHBOARD_HTML
    assert "推广就绪" in DASHBOARD_HTML
    assert "邮箱身份" in DASHBOARD_HTML
    assert "月预算 USD" in DASHBOARD_HTML


def test_dashboard_html_has_professional_overview_strip() -> None:
    assert "工作台总览" in DASHBOARD_HTML
    assert "托管控制台" in DASHBOARD_HTML
    assert "功能导航" in DASHBOARD_HTML
    assert "开发入口" in DASHBOARD_HTML
    assert "后端节省" in DASHBOARD_HTML
    assert "pageMeta" in DASHBOARD_HTML
    assert "cockpit-layout" in DASHBOARD_HTML
    assert "ops-rail" in DASHBOARD_HTML
    assert 'data-workbench-view="relay-panel"' in DASHBOARD_HTML
    assert 'data-nav-view="mission-desk"' in DASHBOARD_HTML


def test_dashboard_app_js_exposes_execution_evidence_surface() -> None:
    app_js = Path(__file__).resolve().parents[1] / "src" / "visual_agent" / "dashboard" / "static" / "app.js"
    text = app_js.read_text(encoding="utf-8")
    assert "执行证据" in text
    assert "Worker 日志尾部" in text
    assert "verification_command" in text
    assert "pacer_evidence" in text
    assert "需求合同" in text
    assert "requirement_contract" in text
    assert "_requirementContractBlock" in text
    assert "_intakePolicyLabel" in text
    assert "focusWorkbenchPanel" in text
    assert "switchWorkbenchView" in text
    assert "收口方式" in text
    assert "/api/goal/refine" in text
    assert "目标还没收口" in text
    assert "先补验收方案" in text
    assert "saveRelayConfig" in text
    assert "renderSubscriptionQuotaPanel" in text
    assert "renderPromotionReadinessPanel" in text
    assert "saveProfileConfig" in text
    assert "打开中转站" in text
    assert "verification_mode" in text
    assert "commandMode && m.stop_reason==='coverage_gap'" in text
    assert "commandMode&&m.stop_reason==='coverage_gap'" in text


def test_dashboard_goal_intake_refines_goal_into_dialogue(tmp_path, monkeypatch) -> None:
    captured = {}

    def fake_refine_goal(goal, *, answers=None, enable_model=True):
        captured["goal"] = goal
        captured["answers"] = list(answers or [])
        captured["enable_model"] = enable_model
        return {
            "source": "model",
            "already_clear": False,
            "clarifying_questions": ["要兼容手机端吗？"],
            "suggested_goal": "把登录页改成深色主题，并保持手机端布局可用",
            "acceptance_hint": "打开登录页检查配色和移动端布局",
            "input_goal": goal,
        }

    def fake_dialogue_lines(payload, answers=None):
        return [
            "我还需要确认这些点：",
            "- 要兼容手机端吗？",
            "建议改写：把登录页改成深色主题，并保持手机端布局可用",
        ]

    monkeypatch.setattr("visual_agent.dashboard.api.refine_goal", fake_refine_goal)
    monkeypatch.setattr("visual_agent.dashboard.api.intake_dialogue_lines", fake_dialogue_lines)

    payload = refine_goal_intake({"goal": "把登录页改成暗色主题", "answers": ["保留蓝色按钮"]})

    assert captured["goal"] == "把登录页改成暗色主题"
    assert captured["answers"] == ["保留蓝色按钮"]
    assert captured["enable_model"] is True
    assert payload["ok"] is True
    assert payload["clarifying_questions"] == ["要兼容手机端吗？"]
    assert "建议改写" in "\n".join(payload["dialogue_lines"])


def test_dashboard_goal_intake_does_not_swap_selected_agent_for_auto_backend(monkeypatch) -> None:
    captured = {}

    def fake_resolve_agent_llm(agent):
        captured["agent"] = agent
        return None

    def fake_refine_goal(goal, *, answers=None, enable_model=True, **kwargs):
        captured["goal"] = goal
        captured["answers"] = list(answers or [])
        captured["enable_model"] = enable_model
        captured["kwargs"] = kwargs
        return {
            "source": "deterministic",
            "already_clear": False,
            "clarifying_questions": ["完成标准是什么？"],
            "suggested_goal": goal,
            "acceptance_hint": "",
            "input_goal": goal,
        }

    monkeypatch.setattr("visual_agent.dashboard.api._resolve_agent_llm", fake_resolve_agent_llm)
    monkeypatch.setattr("visual_agent.dashboard.api.refine_goal", fake_refine_goal)
    monkeypatch.setattr("visual_agent.dashboard.api.shutil.which", lambda _exe: None)

    payload = refine_goal_intake({"goal": "改一下登录页", "agent": "codex", "use_model": True})

    assert captured["agent"] == "codex"
    assert captured["enable_model"] is False
    assert captured["kwargs"] == {}
    assert payload["intake_policy"] == "selected_agent_unavailable"
    assert payload["model_unavailable"] is True
    assert "未切换到其他模型" in "\n".join(payload["dialogue_lines"])


def test_dashboard_goal_intake_uses_codex_read_only_cli(monkeypatch) -> None:
    captured = {}

    class Completed:
        returncode = 0
        stdout = json.dumps(
            {
                "clarifying_questions": ["完成后页面上要看到什么？"],
                "suggested_goal": "把登录页改成暗色主题，并保留蓝色提交按钮",
                "acceptance_hint": "打开登录页确认背景为暗色且提交按钮仍为蓝色",
            },
            ensure_ascii=False,
        )
        stderr = ""

    monkeypatch.setattr("visual_agent.dashboard.api._resolve_agent_llm", lambda _agent: None)
    monkeypatch.setattr("visual_agent.dashboard.api.shutil.which", lambda exe: r"C:\Tools\codex.cmd" if exe == "codex" else None)

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")
        return Completed()

    monkeypatch.setattr("visual_agent.dashboard.api.subprocess.run", fake_run)

    payload = refine_goal_intake(
        {
            "goal": "把登录页改成暗色主题",
            "agent": "codex",
            "repo_root": r"D:\Projects\app",
            "test_command": "pytest -q",
            "use_model": True,
        }
    )

    assert payload["ok"] is True
    assert payload["source"] == "selected_agent_cli"
    assert payload["intake_policy"] == "selected_agent_cli"
    assert payload["model_id"] == "codex:cli"
    assert "codex.cmd" in " ".join(captured["argv"])
    assert "read-only" in captured["argv"]
    assert captured["argv"][-1] == "-"
    assert "把登录页改成暗色主题" not in " ".join(captured["argv"])
    assert "把登录页改成暗色主题" in captured["input"]
    assert "Do not edit files" in captured["input"]
    assert "暗色主题" in payload["suggested_goal"]


def test_dashboard_html_surfaces_live_execution_logs() -> None:
    assert "实时执行日志" in DASHBOARD_HTML
    assert "live_logs" in DASHBOARD_HTML
