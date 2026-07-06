from __future__ import annotations

import json

from visual_agent.dashboard import DASHBOARD_HTML, build_dashboard_data, build_mission_detail, start_worker, stop_worker
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


def test_dashboard_data_reads_status_file(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    (tmp_path / ".visual-agent-status.md").write_text("## Status: PASSING\n", encoding="utf-8")

    data = build_dashboard_data(workspace)

    assert data["status"]["state"] == "PASSING"


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


def test_dashboard_html_surfaces_live_execution_logs() -> None:
    assert "实时执行日志" in DASHBOARD_HTML
    assert "live_logs" in DASHBOARD_HTML
