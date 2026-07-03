from __future__ import annotations

import json

from visual_agent.cli import main
from visual_agent.missions import (
    append_round,
    create_mission,
    default_budget_policy,
    list_missions,
    load_mission,
    load_rounds,
    write_final_report,
)


def _seed_mission(workspace, tmp_path, mission_id: str, objective: str) -> None:
    create_mission(
        workspace_root=workspace,
        objective=objective,
        repo_root=tmp_path,
        plan_id=f"plan-{mission_id}",
        budget_policy=default_budget_policy(),
        mission_id=mission_id,
    )


def test_mission_storage_roundtrip(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    budget = default_budget_policy(max_rounds=2, max_wall_minutes=30)

    mission = create_mission(
        workspace_root=workspace,
        objective="Fix checkout total",
        repo_root=tmp_path,
        plan_id="plan-1",
        budget_policy=budget,
        mission_id="mission-1",
    )
    append_round(workspace, "mission-1", {"round": 0, "type": "preview", "status": "preview"})
    report = write_final_report(workspace, "mission-1", "## Report")

    loaded = load_mission(workspace, "mission-1")
    assert loaded is not None
    assert loaded["mission_id"] == mission["mission_id"]
    assert loaded["product"] == "DevPacer"
    assert loaded["verification_engine"] == "Checkpoint"
    assert load_rounds(workspace, "mission-1")[0]["type"] == "preview"
    assert report["path"].endswith("final_report.md")
    summaries = list_missions(workspace)
    assert summaries[0]["mission_id"] == "mission-1"


def test_default_budget_policy_caps_first_repair_round() -> None:
    budget = default_budget_policy(max_rounds=5, max_wall_minutes=60)

    assert budget["max_rounds"] == 5
    # The first executable loop only supports one automatic repair round.
    assert budget["max_repair_rounds"] == 1
    assert budget["model_policy"]["visual_review"] == "multimodal"


def test_chief_missions_list_limit_applies_to_json(tmp_path, capsys) -> None:
    workspace = tmp_path / ".agent-workspace"
    _seed_mission(workspace, tmp_path, "20260703-000001-old", "Oldest mission")
    _seed_mission(workspace, tmp_path, "20260703-000002-middle", "Middle mission")
    _seed_mission(workspace, tmp_path, "20260703-000003-new", "Newest mission")

    code = main(["chief-missions", "list", "--workspace-root", str(workspace), "--limit", "2", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert [item["mission_id"] for item in payload] == ["20260703-000003-new", "20260703-000002-middle"]
    assert "20260703-000001-old" not in {item["mission_id"] for item in payload}


def test_chief_missions_list_limit_applies_to_markdown(tmp_path, capsys) -> None:
    workspace = tmp_path / ".agent-workspace"
    _seed_mission(workspace, tmp_path, "20260703-000001-old", "Oldest mission")
    _seed_mission(workspace, tmp_path, "20260703-000002-middle", "Middle mission")
    _seed_mission(workspace, tmp_path, "20260703-000003-new", "Newest mission")

    code = main(["chief-missions", "list", "--workspace-root", str(workspace), "--limit", "2", "--format", "markdown"])
    output = capsys.readouterr().out

    assert code == 0
    assert "20260703-000003-new" in output
    assert "20260703-000002-middle" in output
    assert "20260703-000001-old" not in output
    assert "Oldest mission" not in output
