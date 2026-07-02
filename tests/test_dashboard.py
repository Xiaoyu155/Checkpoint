from __future__ import annotations

import json

from visual_agent.dashboard import DASHBOARD_HTML, build_dashboard_data, build_mission_detail
from visual_agent.missions import missions_dir, save_mission


def test_dashboard_data_on_empty_workspace(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    data = build_dashboard_data(workspace)

    assert data["product"] == "DevPacer"
    assert data["engine"] == "Checkpoint"
    assert data["missions"] == []
    assert data["plans"] == []
    # Value panel is always present so the user can see benefit accrue.
    assert data["value"]["verified"] == 0
    assert data["value"]["tier_counts"] == {"cheap": 0, "standard": 0, "strong": 0}
    assert data["value"]["spent_usd"] == 0.0
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


def test_dashboard_html_is_self_contained() -> None:
    # No external script/style URLs — the page must work offline/local-only.
    assert "<title>DevPacer</title>" in DASHBOARD_HTML
    assert "http://" not in DASHBOARD_HTML.split("<script>")[0]
