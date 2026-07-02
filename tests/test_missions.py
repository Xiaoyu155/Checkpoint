from __future__ import annotations

from visual_agent.missions import (
    append_round,
    create_mission,
    default_budget_policy,
    list_missions,
    load_mission,
    load_rounds,
    write_final_report,
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
