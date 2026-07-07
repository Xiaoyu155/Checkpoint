from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from visual_agent.chief_plans_store import append_worker_record, save_plan, save_verification
from visual_agent.dashboard.api import refine_goal_intake, run_chat
from visual_agent.dashboard import DASHBOARD_HTML, _bind_dashboard_server, build_dashboard_data, build_mission_detail, start_worker, stop_worker
from visual_agent.missions import missions_dir, save_mission


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
    assert me["saved_minutes"] == 0.0
    assert me["efficiency_gain_percent"] == 0
    assert me["capability_score"] == 0.0
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
    assert "mimo unified diff applied" in detail["pacer_evidence"]["log_tail"]


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

    def fake_popen(cmd, stdout, stderr):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    result = start_worker(tmp_path / ".agent-workspace")
    stop_worker()

    assert result["ok"] is True
    assert captured["cmd"][1:4] == ["-m", "visual_agent.cli", "mission"]


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
    assert "额度节省" in DASHBOARD_HTML
    assert "时间节省" in DASHBOARD_HTML
    assert "综合效率" in DASHBOARD_HTML
    assert "能力评分" in DASHBOARD_HTML
    assert "mSavedUsd" in DASHBOARD_HTML
    assert "mSavedMin" in DASHBOARD_HTML
    assert "mEfficiency" in DASHBOARD_HTML


def test_dashboard_html_has_professional_overview_strip() -> None:
    assert "工作台总览" in DASHBOARD_HTML
    assert "后端节省" in DASHBOARD_HTML
    assert "pageMeta" in DASHBOARD_HTML


def test_dashboard_app_js_exposes_execution_evidence_surface() -> None:
    app_js = Path(__file__).resolve().parents[1] / "src" / "visual_agent" / "dashboard" / "static" / "app.js"
    text = app_js.read_text(encoding="utf-8")
    assert "执行证据" in text
    assert "Worker 日志尾部" in text
    assert "verification_command" in text
    assert "pacer_evidence" in text
    assert "/api/goal/refine" in text
    assert "目标还没收口" in text
    assert "先补验收方案" in text


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


def test_dashboard_html_surfaces_live_execution_logs() -> None:
    assert "实时执行日志" in DASHBOARD_HTML
    assert "live_logs" in DASHBOARD_HTML
