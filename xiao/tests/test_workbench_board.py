from __future__ import annotations

from visual_agent.chief_plans_store import save_plan, save_verification
from visual_agent.missions import append_round, create_mission, default_budget_policy, load_mission
from visual_agent.workbench_board import (
    attach_board_fields,
    board_column,
    merge_mission_now,
    mission_merge_state,
    mission_review_payload,
)
from visual_agent.workspace import init_workspace


class TestBoardColumn:
    def test_created_is_todo(self):
        assert board_column("created") == "todo"

    def test_running_is_in_progress(self):
        assert board_column("running") == "in_progress"
        assert board_column("preview_running") == "in_progress"
        assert board_column("background_running") == "in_progress"

    def test_verified_unmerged_is_pending_merge(self):
        assert board_column("verified") == "pending_merge"

    def test_verified_merged_is_done(self):
        assert board_column("verified", merge_state="merged") == "done"
        assert board_column("verified", merge_state="nothing_to_merge") == "done"

    def test_merged_status_is_done(self):
        assert board_column("merged") == "done"

    def test_stopped_and_failed_land_in_review(self):
        assert board_column("stopped") == "in_review"
        assert board_column("failed") == "in_review"
        assert board_column("preview") == "in_review"

    def test_unknown_defaults_to_review(self):
        assert board_column("") == "in_review"
        assert board_column("whatever") == "in_review"


class TestMergeState:
    def test_no_rounds_file_is_empty(self, tmp_path):
        ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
        assert mission_merge_state(ws.root, "missing") == ""

    def test_reads_last_merge_round(self, tmp_path):
        ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
        create_mission(
            workspace_root=ws.root, objective="x", repo_root=tmp_path,
            plan_id="p1", budget_policy=default_budget_policy(), mission_id="m1",
        )
        append_round(ws.root, "m1", {"round": 1, "type": "verification", "status": "pass"})
        append_round(ws.root, "m1", {"round": 2, "type": "merge", "status": "conflict"})
        append_round(ws.root, "m1", {"round": 3, "type": "merge", "status": "merged"})
        assert mission_merge_state(ws.root, "m1") == "merged"

    def test_ignores_malformed_lines(self, tmp_path):
        ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
        create_mission(
            workspace_root=ws.root, objective="x", repo_root=tmp_path,
            plan_id="p1", budget_policy=default_budget_policy(), mission_id="m1",
        )
        rounds = ws.root / "missions" / "m1" / "rounds.jsonl"
        rounds.write_text('not json\n{"type": "merge", "status": "merged"}\n', encoding="utf-8")
        assert mission_merge_state(ws.root, "m1") == "merged"


class TestAttachBoardFields:
    def test_verified_can_merge(self, tmp_path):
        ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
        create_mission(
            workspace_root=ws.root, objective="x", repo_root=tmp_path,
            plan_id="p1", budget_policy=default_budget_policy(),
            mission_id="m1", status="verified",
        )
        items = attach_board_fields(ws.root, [{"mission_id": "m1", "status": "verified"}])
        assert items[0]["board_column"] == "pending_merge"
        assert items[0]["can_merge"] is True

    def test_already_merged_cannot_merge_again(self, tmp_path):
        ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
        create_mission(
            workspace_root=ws.root, objective="x", repo_root=tmp_path,
            plan_id="p1", budget_policy=default_budget_policy(),
            mission_id="m1", status="verified",
        )
        append_round(ws.root, "m1", {"round": 1, "type": "merge", "status": "merged"})
        items = attach_board_fields(ws.root, [{"mission_id": "m1", "status": "verified"}])
        assert items[0]["board_column"] == "done"
        assert items[0]["can_merge"] is False


class TestMergeMissionNow:
    def test_missing_mission(self, tmp_path):
        ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
        result = merge_mission_now(ws.root, "nope")
        assert result["ok"] is False
        assert "找不到" in result["error"]

    def test_refuses_unverified(self, tmp_path):
        ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
        create_mission(
            workspace_root=ws.root, objective="x", repo_root=tmp_path,
            plan_id="p1", budget_policy=default_budget_policy(),
            mission_id="m1", status="stopped",
        )
        result = merge_mission_now(ws.root, "m1")
        assert result["ok"] is False
        assert "verified" in result["error"]

    def test_refuses_double_merge(self, tmp_path):
        ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
        create_mission(
            workspace_root=ws.root, objective="x", repo_root=tmp_path,
            plan_id="p1", budget_policy=default_budget_policy(),
            mission_id="m1", status="verified",
        )
        append_round(ws.root, "m1", {"round": 1, "type": "merge", "status": "merged"})
        result = merge_mission_now(ws.root, "m1")
        assert result["ok"] is False
        assert "已经合并" in result["error"]

    def test_missing_plan(self, tmp_path):
        ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
        create_mission(
            workspace_root=ws.root, objective="x", repo_root=tmp_path,
            plan_id="p-none", budget_policy=default_budget_policy(),
            mission_id="m1", status="verified",
        )
        result = merge_mission_now(ws.root, "m1")
        assert result["ok"] is False
        assert "计划" in result["error"]

    def test_missing_worktree_gives_manual_hint(self, tmp_path):
        ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
        save_plan(
            {
                "objective": "x",
                "repo_root": str(tmp_path),
                "worker_tracks": [{"id": "track_1_codex", "agent": "codex", "track_kind": "implementation"}],
            },
            workspace_root=ws.root,
            plan_id="p1",
        )
        create_mission(
            workspace_root=ws.root, objective="x", repo_root=tmp_path,
            plan_id="p1", budget_policy=default_budget_policy(),
            mission_id="m1", status="verified",
        )
        result = merge_mission_now(ws.root, "m1")
        assert result["ok"] is False
        assert "不存在" in result["error"]

    def test_merges_and_marks_mission(self, tmp_path, monkeypatch):
        ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
        save_plan(
            {
                "objective": "fix add",
                "repo_root": str(tmp_path),
                "worker_tracks": [{"id": "track_1_codex", "agent": "codex", "track_kind": "implementation"}],
            },
            workspace_root=ws.root,
            plan_id="p1",
        )
        create_mission(
            workspace_root=ws.root, objective="fix add", repo_root=tmp_path,
            plan_id="p1", budget_policy=default_budget_policy(),
            mission_id="m1", status="verified",
        )

        from visual_agent import chief_dispatch

        worktree = chief_dispatch.default_worktree_path(
            repo_root=tmp_path, plan_id="p1", track_id="track_1_codex"
        )
        worktree.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            chief_dispatch,
            "merge_worktree_branch",
            lambda **kwargs: {"status": "merged", "branch": kwargs["branch"], "target": "main", "commit": "abc123"},
        )

        result = merge_mission_now(ws.root, "m1")

        assert result["ok"] is True
        assert result["merge"]["status"] == "merged"
        assert load_mission(ws.root, "m1")["status"] == "merged"
        assert mission_merge_state(ws.root, "m1") == "merged"


class TestMissionReviewPayload:
    def test_empty_without_plan(self, tmp_path):
        ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
        assert mission_review_payload(ws.root, {"plan_id": ""}) == {}
        assert mission_review_payload(ws.root, {"plan_id": "missing"}) == {}

    def test_exposes_diff_summary_and_verdict(self, tmp_path):
        ws = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
        save_verification(
            ws.root,
            "p1",
            {
                "verdict": "pass",
                "command_verification": {"command": "pytest -q"},
                "markdown": "ok",
                "diff_summary": {"file_count": 2, "lines_added": 10, "lines_removed": 3},
                "warnings": ["large diff"],
                "recorded_at": "2026-07-03T00:00:00+00:00",
            },
        )
        review = mission_review_payload(ws.root, {"plan_id": "p1"})
        assert review["verdict"] == "pass"
        assert review["command"] == "pytest -q"
        assert review["diff_summary"]["file_count"] == 2
        assert review["warnings"] == ["large diff"]
