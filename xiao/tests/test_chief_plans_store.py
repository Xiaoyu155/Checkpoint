from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from visual_agent.chief_plans_store import (
    append_worker_record,
    list_plans,
    load_plan,
    load_verification,
    load_worker_records,
    make_plan_id,
    save_plan,
    save_verification,
)


def test_save_and_load_plan_roundtrip(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    plan = {"objective": "Fix checkout total", "status": "ready", "selected_workflows": ["checkout"]}

    saved = save_plan(plan, workspace_root=workspace)
    assert saved["plan_id"]

    loaded = load_plan(workspace, saved["plan_id"])
    assert loaded is not None
    assert loaded["objective"] == "Fix checkout total"
    assert loaded["plan_id"] == saved["plan_id"]
    assert loaded["saved_at"]


def test_list_plans_newest_first(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    save_plan({"objective": "one", "status": "ready"}, workspace_root=workspace, plan_id="20260101-000000-aaaaaa")
    save_plan({"objective": "two", "status": "ready"}, workspace_root=workspace, plan_id="20260201-000000-bbbbbb")

    summaries = list_plans(workspace)
    assert [item["plan_id"] for item in summaries] == ["20260201-000000-bbbbbb", "20260101-000000-aaaaaa"]
    assert summaries[0]["objective"] == "two"


def test_load_missing_plan_returns_none(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    assert load_plan(workspace, "does-not-exist") is None


def test_make_plan_id_is_unique_for_same_objective_and_moment() -> None:
    from datetime import datetime, timezone

    moment = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
    first = make_plan_id("Fix checkout", now=moment)
    second = make_plan_id("Fix checkout", now=moment)
    assert first != second
    assert first.startswith("20260702-120000-000000-")


def test_concurrent_generated_plans_do_not_overwrite_each_other(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"

    def save(index: int) -> dict[str, str]:
        return save_plan({"objective": "Fix checkout", "index": index}, workspace_root=workspace)

    with ThreadPoolExecutor(max_workers=8) as pool:
        saved = list(pool.map(save, range(32)))

    plan_ids = [item["plan_id"] for item in saved]
    assert len(set(plan_ids)) == 32
    assert sorted(load_plan(workspace, plan_id)["index"] for plan_id in plan_ids) == list(range(32))


@pytest.mark.parametrize("plan_id", ["", ".", "..", "../escape", r"..\escape", "C:escape", "bad id"])
def test_plan_storage_rejects_unsafe_ids(tmp_path, plan_id) -> None:
    with pytest.raises(ValueError, match="plan_id"):
        load_plan(tmp_path / ".agent-workspace", plan_id)


def test_worker_records_and_verification_roundtrip(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    plan_id = "20260702-120000-aaaaaa"

    append_worker_record(workspace, plan_id, {"track_id": "track_1_codex", "status": "completed"})
    append_worker_record(workspace, plan_id, {"track_id": "track_1_codex", "status": "verified"})
    records = load_worker_records(workspace, plan_id)
    assert [item["status"] for item in records] == ["completed", "verified"]

    save_verification(workspace, plan_id, {"verdict": "pass", "failed": 0})
    verification = load_verification(workspace, plan_id)
    assert verification is not None
    assert verification["verdict"] == "pass"
