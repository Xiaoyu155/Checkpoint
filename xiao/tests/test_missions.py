from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from visual_agent.cli import main
from visual_agent.missions import (
    append_round,
    create_mission,
    default_budget_policy,
    list_missions,
    load_mission,
    load_rounds,
    make_mission_id,
    mission_dir,
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
    assert loaded["product"] == "Pacer"
    assert loaded["verification_engine"] == "Checkpoint"
    assert load_rounds(workspace, "mission-1")[0]["type"] == "preview"
    assert report["path"].endswith("final_report.md")
    summaries = list_missions(workspace)
    assert summaries[0]["mission_id"] == "mission-1"


def test_default_budget_policy_caps_repair_rounds_at_two() -> None:
    budget = default_budget_policy(max_rounds=5, max_wall_minutes=60)

    assert budget["max_rounds"] == 5
    # The execution loop supports at most two automatic repair rounds by default.
    assert budget["max_repair_rounds"] == 2
    assert budget["max_total_tokens"] == 120_000
    assert budget["max_same_failure_count"] == 2
    assert budget["model_policy"]["visual_review"] == "multimodal"


def test_mission_ids_are_unique_for_same_objective_and_moment() -> None:
    from datetime import datetime, timezone

    moment = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
    with ThreadPoolExecutor(max_workers=8) as pool:
        mission_ids = list(pool.map(lambda _index: make_mission_id("Fix checkout", now=moment), range(64)))

    assert len(set(mission_ids)) == 64
    assert all(item.startswith("20260702-120000-000000-") for item in mission_ids)


def test_create_mission_refuses_to_overwrite_explicit_id(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    _seed_mission(workspace, tmp_path, "m1", "First objective")

    with pytest.raises(FileExistsError, match="already exists"):
        _seed_mission(workspace, tmp_path, "m1", "Replacement objective")

    assert load_mission(workspace, "m1")["objective"] == "First objective"


@pytest.mark.parametrize(
    "mission_id",
    ["", ".", "..", "../escape", r"..\escape", "/absolute", r"C:\absolute", "bad id", "bad:id"],
)
def test_mission_storage_rejects_unsafe_ids(tmp_path, mission_id) -> None:
    workspace = tmp_path / ".agent-workspace"
    with pytest.raises(ValueError, match="mission_id"):
        mission_dir(workspace, mission_id)
    with pytest.raises(ValueError, match="mission_id"):
        load_mission(workspace, mission_id)
    assert not (tmp_path / "escape").exists()


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
