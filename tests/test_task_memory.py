from __future__ import annotations

import json
from pathlib import Path

from visual_agent.pacer_launch_context import initialize_active_launch, update_active_launch
from visual_agent.task_memory import (
    TASK_MEMORY_MAX_RECENT_EVENTS,
    append_task_memory_event,
    compact_task_memory,
    initialize_task_memory,
    read_task_memory,
    task_memory_paths,
)


def test_task_memory_keeps_append_only_log_and_compact_summary(tmp_path: Path) -> None:
    workspace = tmp_path / ".agent-workspace"
    memory_id = "launch-memory-test"
    initialize_task_memory(
        workspace,
        memory_id=memory_id,
        launch_id=memory_id,
        goal="持续开发并保存记忆",
        repo_root=tmp_path,
    )

    for index in range(TASK_MEMORY_MAX_RECENT_EVENTS + 12):
        append_task_memory_event(
            workspace,
            memory_id=memory_id,
            launch_id=memory_id,
            event_type="progress",
            data={"step": index, "secret": "sk-test-secret"},
        )

    summary = read_task_memory(workspace, memory_id=memory_id)
    log_path, summary_path = task_memory_paths(workspace, memory_id)
    assert summary["health"]["status"] == "healthy"
    assert summary["event_count"] == TASK_MEMORY_MAX_RECENT_EVENTS + 12
    assert len(summary["recent_events"]) == TASK_MEMORY_MAX_RECENT_EVENTS
    assert "sk-test-secret" not in log_path.read_text(encoding="utf-8")

    compacted = compact_task_memory(workspace, memory_id=memory_id, max_recent_events=8)
    assert compacted["event_count"] == TASK_MEMORY_MAX_RECENT_EVENTS + 12
    assert len(compacted["recent_events"]) == 8
    assert compacted["compression"]["full_log_retained"] is True
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == TASK_MEMORY_MAX_RECENT_EVENTS + 12
    assert json.loads(summary_path.read_text(encoding="utf-8"))["event_count"] == compacted["event_count"]


def test_launch_state_updates_are_recorded_in_required_task_memory(tmp_path: Path) -> None:
    workspace = tmp_path / ".agent-workspace"
    launch_id = "launch-state-memory"
    manifest = workspace / "pacer_native" / "launches" / f"{launch_id}.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")

    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": launch_id, "repo_root": str(tmp_path), "goal": "record state"},
    )
    before = read_task_memory(workspace, memory_id=launch_id)["event_count"]
    update_active_launch(
        workspace,
        expected_launch_id=launch_id,
        current_goal="record state progress",
    )
    after = read_task_memory(workspace, memory_id=launch_id)

    assert after["event_count"] == before + 1
    assert after["last_event"]["type"] == "launch_state_updated"
    assert after["health"]["status"] == "healthy"

