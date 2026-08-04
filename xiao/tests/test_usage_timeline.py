from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from visual_agent.usage_timeline import (
    collect_usage_timeline,
    discover_workspace_roots,
    usage_timeline_to_markdown,
)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _mission(workspace: Path, mission_id: str, *, updated_at: str, status: str = "verified") -> None:
    _write(
        workspace / "missions" / mission_id / "mission.json",
        {
            "mission_id": mission_id,
            "objective": f"objective for {mission_id}",
            "status": status,
            "created_at": updated_at,
            "updated_at": updated_at,
        },
    )


def _journey(workspace: Path, mission_id: str, *, routing: str = "passed", delivered: bool = True) -> None:
    _write(
        workspace / "missions" / mission_id / "journey.json",
        {
            "mission_id": mission_id,
            "status": "completed" if delivered else "verified_pending_delivery",
            "can_claim_verified": True,
            "can_claim_delivered": delivered,
            "next_action": "查看最终报告。",
            "reason_codes": [],
            "phases": [
                {
                    "id": "routing",
                    "status": routing,
                    "details": {"actual_agent": "codex", "provider": "openai", "model": "gpt-5.5"},
                },
                {"id": "memory", "status": "passed", "details": {"selected_entries": 3}},
                {"id": "managed", "status": "passed", "details": {}},
                {"id": "acceptance", "status": "passed", "details": {}},
                {"id": "delivery", "status": "passed" if delivered else "ready", "details": {}},
            ],
        },
    )


def test_discover_workspace_roots_finds_sibling_projects(tmp_path: Path) -> None:
    (tmp_path / ".agent-workspace").mkdir()
    (tmp_path / "project-a" / ".agent-workspace").mkdir(parents=True)
    (tmp_path / "project-b" / "nested" / ".agent-workspace").mkdir(parents=True)
    (tmp_path / "project-a" / "node_modules" / ".agent-workspace").mkdir(parents=True)

    found = {path.parent.name for path in discover_workspace_roots(tmp_path)}

    assert found == {tmp_path.name, "project-a", "nested"}


def test_collect_usage_timeline_orders_and_totals(tmp_path: Path) -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    first = tmp_path / "project-a" / ".agent-workspace"
    second = tmp_path / "project-b" / ".agent-workspace"
    _mission(first, "20260803-old", updated_at=(now - timedelta(days=1)).isoformat())
    _journey(first, "20260803-old", delivered=True)
    _mission(second, "20260804-new", updated_at=(now - timedelta(hours=1)).isoformat())
    _journey(second, "20260804-new", delivered=False)

    payload = collect_usage_timeline([first, second], days=14, now=now)

    assert [entry["mission_id"] for entry in payload["entries"]] == ["20260804-new", "20260803-old"]
    assert payload["totals"]["missions"] == 2
    assert payload["totals"]["verified"] == 2
    assert payload["totals"]["delivered"] == 1
    assert payload["totals"]["projects"] == 2
    assert payload["totals"]["memory_entries_injected"] == 6
    assert payload["entries"][0]["routing"] == "codex / openai / gpt-5.5"


def test_collect_usage_timeline_respects_days_window(tmp_path: Path) -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    workspace = tmp_path / "project" / ".agent-workspace"
    _mission(workspace, "20260701-stale", updated_at=(now - timedelta(days=30)).isoformat())
    _journey(workspace, "20260701-stale")
    _mission(workspace, "20260803-fresh", updated_at=(now - timedelta(days=1)).isoformat())
    _journey(workspace, "20260803-fresh")

    payload = collect_usage_timeline([workspace], days=7, now=now)

    assert [entry["mission_id"] for entry in payload["entries"]] == ["20260803-fresh"]


def test_collect_usage_timeline_limit_marks_truncated(tmp_path: Path) -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    workspace = tmp_path / "project" / ".agent-workspace"
    for index in range(4):
        mission_id = f"2026080{index}-mission"
        _mission(workspace, mission_id, updated_at=(now - timedelta(hours=index)).isoformat())
        _journey(workspace, mission_id)

    payload = collect_usage_timeline([workspace], days=14, limit=2, now=now)

    assert payload["truncated"] is True
    assert len(payload["entries"]) == 2


def test_timeline_markdown_shows_chain_and_evidence_path(tmp_path: Path) -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    workspace = tmp_path / "project" / ".agent-workspace"
    _mission(workspace, "20260804-one", updated_at=now.isoformat())
    _journey(workspace, "20260804-one", routing="incomplete")

    text = usage_timeline_to_markdown(collect_usage_timeline([workspace], days=14, now=now))

    assert "Pacer 使用时间线" in text
    assert "!路由" in text
    assert "20260804-one" in text


def test_timeline_markdown_handles_empty_history(tmp_path: Path) -> None:
    payload = collect_usage_timeline([tmp_path / "missing" / ".agent-workspace"], days=14)

    assert payload["entries"] == []
    assert "没有任务记录" in usage_timeline_to_markdown(payload)
