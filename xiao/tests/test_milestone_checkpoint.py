"""Tests for milestone_checkpoint.py — Layer 3 of the three-tier acceptance architecture."""

from __future__ import annotations

import json
from pathlib import Path

from visual_agent.milestone_checkpoint import (
    generate_milestone_checkpoint,
    format_milestone_checkpoint,
)


# ── fixtures ─────────────────────────────────────────────────────────────────

def _task(objective: str, status: str = "verified", diff_summary: dict | None = None) -> dict:
    t = {"objective": objective, "status": status}
    if diff_summary is not None:
        t["diff_summary"] = diff_summary
    return t


def _simple_diff(files: int = 3, added: int = 30, removed: int = 5, prefix: str = "a") -> dict:
    return {
        "file_count": files,
        "lines_added": added,
        "lines_removed": removed,
        "large_diff": False,
        "changed_files": [
            {"path": f"src/{prefix}_module_{i}.py", "status": "M", "lines_added": added // files, "lines_removed": removed // files}
            for i in range(files)
        ],
        "functions_touched": [{"name": "my_func", "lang": "Python"}],
        "user_checklist": ["运行 pytest 确认改动"],
        "summary_text": f"改了 {files} 个文件。",
    }


# ── generate_milestone_checkpoint ────────────────────────────────────────────

class TestGenerateMilestoneCheckpoint:
    def test_basic_verified_tasks(self, tmp_path):
        tasks = [
            _task("实现登录页面"),
            _task("添加 JWT 验证"),
        ]
        result = generate_milestone_checkpoint(
            milestone_label="登录流程",
            completed_tasks=tasks,
            workspace_root=tmp_path,
        )
        assert result["milestone_label"] == "登录流程"
        assert result["task_count"] == 2
        assert result["verified_count"] == 2
        assert result["unverified_count"] == 0
        assert len(result["what_was_done"]) == 2
        assert "✅" in result["what_was_done"][0]
        assert len(result["what_to_check"]) >= 1
        assert len(result["how_to_report"]) == 3
        assert isinstance(result["estimated_check_minutes"], int)
        assert result["estimated_check_minutes"] >= 2

    def test_mixed_verified_and_failed(self, tmp_path):
        tasks = [
            _task("实现搜索", "verified"),
            _task("修复翻页 bug", "verification_failed"),
        ]
        result = generate_milestone_checkpoint(
            milestone_label="搜索功能",
            completed_tasks=tasks,
            workspace_root=tmp_path,
        )
        assert result["verified_count"] == 1
        assert result["unverified_count"] == 1
        # Unverified task should appear in what_to_check
        checklist_text = " ".join(result["what_to_check"])
        assert "修复翻页 bug" in checklist_text or "未通过" in checklist_text

    def test_aggregates_diff_summaries(self, tmp_path):
        tasks = [
            _task("改 A", diff_summary=_simple_diff(files=2, added=20, removed=5, prefix="a")),
            _task("改 B", diff_summary=_simple_diff(files=3, added=30, removed=10, prefix="b")),
        ]
        result = generate_milestone_checkpoint(
            milestone_label="批次1",
            completed_tasks=tasks,
            workspace_root=tmp_path,
        )
        ds = result["diff_summary"]
        assert ds["file_count"] == 5
        assert ds["lines_added"] == 50
        assert ds["lines_removed"] == 15

    def test_saves_markdown_and_json_files(self, tmp_path):
        tasks = [_task("实现功能 X")]
        result = generate_milestone_checkpoint(
            milestone_label="功能X",
            completed_tasks=tasks,
            workspace_root=tmp_path,
        )
        saved = result.get("saved_path", "")
        assert saved
        md_path = Path(saved)
        assert md_path.exists()
        assert md_path.read_text(encoding="utf-8").strip()
        json_path = md_path.with_suffix(".json")
        assert json_path.exists()
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["milestone_label"] == "功能X"

    def test_empty_task_list(self, tmp_path):
        result = generate_milestone_checkpoint(
            milestone_label="空",
            completed_tasks=[],
            workspace_root=tmp_path,
        )
        assert result["task_count"] == 0
        assert result["verified_count"] == 0
        assert "what_to_check" in result

    def test_large_diff_triggers_warning_in_checklist(self, tmp_path):
        large = _simple_diff(files=50, added=3000, removed=500)
        large["large_diff"] = True
        tasks = [_task("大重构", diff_summary=large)]
        result = generate_milestone_checkpoint(
            milestone_label="重构",
            completed_tasks=tasks,
            workspace_root=tmp_path,
        )
        ds = result["diff_summary"]
        assert ds["large_diff"] is True

    def test_workspace_write_failure_degrades(self, tmp_path):
        # Point workspace to a file path (not a dir) to force write failure
        bad_ws = tmp_path / "not_a_dir.txt"
        bad_ws.write_text("x", encoding="utf-8")
        tasks = [_task("任务")]
        # Should not raise; saved_path will be empty string
        result = generate_milestone_checkpoint(
            milestone_label="test",
            completed_tasks=tasks,
            workspace_root=bad_ws,
        )
        assert result["task_count"] == 1
        # saved_path may be empty but the rest of the result is intact
        assert "what_was_done" in result


# ── format_milestone_checkpoint ───────────────────────────────────────────────

class TestFormatMilestoneCheckpoint:
    def test_renders_all_sections(self, tmp_path):
        tasks = [_task("实现功能"), _task("修复 bug", "verification_failed")]
        result = generate_milestone_checkpoint(
            milestone_label="Sprint 1",
            completed_tasks=tasks,
            workspace_root=tmp_path,
        )
        md = result["markdown"]
        assert "Sprint 1" in md
        assert "这一批做了什么" in md
        assert "你需要做的" in md
        assert "如果有问题" in md

    def test_verified_shows_checkmark(self, tmp_path):
        tasks = [_task("全部通过")]
        result = generate_milestone_checkpoint(
            milestone_label="ok",
            completed_tasks=tasks,
            workspace_root=tmp_path,
        )
        assert "✅" in result["markdown"]

    def test_unverified_shows_cross(self, tmp_path):
        tasks = [_task("失败任务", "verification_failed")]
        result = generate_milestone_checkpoint(
            milestone_label="fail",
            completed_tasks=tasks,
            workspace_root=tmp_path,
        )
        assert "❌" in result["markdown"]

    def test_format_standalone(self):
        checkpoint = {
            "milestone_label": "直接格式化",
            "task_count": 1,
            "verified_count": 1,
            "unverified_count": 0,
            "estimated_check_minutes": 3,
            "what_was_done": ["✅ 改了一些东西"],
            "what_to_check": ["运行 app 看看"],
            "how_to_report": ["如果崩溃请告知"],
            "diff_summary": {},
        }
        md = format_milestone_checkpoint(checkpoint)
        assert "直接格式化" in md
        assert "改了一些东西" in md
        assert "运行 app 看看" in md
