from __future__ import annotations

import json

from visual_agent.chief_queue import list_mission_queue_items
from visual_agent.cli import main
from visual_agent.program_scheduler import start_program
from visual_agent.programs import create_program_from_plan, load_program
from visual_agent.workspace import init_workspace


def test_start_program_creates_preview_and_queues_first_task(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = tmp_path / "plan.md"
    plan.write_text("- [ ] Implement voice overlay\n- [ ] Update docs\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    program = create_program_from_plan(source_file=plan, workspace_root=workspace.root, repo_root=tmp_path, sequential=True)

    payload = start_program(workspace_root=workspace.root, program_id=program["program_id"], hours=5)

    assert payload["queued_items"]
    assert payload["queued_items"][0]["task_id"] == "task-001"
    saved = load_program(workspace.root, program["program_id"])
    assert saved["tasks"][0]["status"] == "queued"
    assert saved["tasks"][1]["status"] == "pending"
    assert list_mission_queue_items(workspace.root)["pending_items"] == 1


def test_autopilot_cli_creates_program_and_queue(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = tmp_path / "plan.md"
    plan.write_text("- [ ] Implement voice overlay\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    code = main(
        [
            "autopilot",
            "--file",
            str(plan),
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["program_id"]
    assert payload["queued_items"]
