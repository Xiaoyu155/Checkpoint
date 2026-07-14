from __future__ import annotations

import json
from pathlib import Path

from visual_agent.cli import main
from visual_agent.programs import create_program_from_plan, load_program, ready_program_tasks
from visual_agent.workspace import init_workspace


def test_create_program_from_plan_writes_task_graph(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = tmp_path / "plan.md"
    plan.write_text("- [ ] Implement voice overlay\n- [ ] Update docs\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}', encoding="utf-8")

    program = create_program_from_plan(source_file=plan, workspace_root=workspace.root, repo_root=tmp_path, agent="codex")

    assert program["program_id"]
    assert len(program["tasks"]) == 2
    assert program["tasks"][0]["test_command"] == "npm test"
    assert program["tasks"][1]["depends_on"] == ["task-001"]
    assert Path(workspace.root / "programs" / program["program_id"] / "daily_plan.md").exists()


def test_ready_program_tasks_respects_dependencies(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = tmp_path / "plan.md"
    plan.write_text("- [ ] First task\n- [ ] Second task\n", encoding="utf-8")
    program = create_program_from_plan(source_file=plan, workspace_root=workspace.root, repo_root=tmp_path)

    ready = ready_program_tasks(program)

    assert [item["task_id"] for item in ready] == ["task-001"]


def test_program_cli_create_and_plan(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = tmp_path / "plan.md"
    plan.write_text("- [ ] Implement voice overlay\n", encoding="utf-8")

    code = main(["program", "create", "--file", str(plan), "--workspace-root", str(workspace.root), "--repo-root", str(tmp_path), "--format", "json"])
    created = json.loads(capsys.readouterr().out)
    plan_code = main(["program", "plan", "--program", created["program_id"], "--workspace-root", str(workspace.root), "--format", "json"])
    hourly = json.loads(capsys.readouterr().out)

    assert code == 0
    assert plan_code == 0
    assert load_program(workspace.root, created["program_id"]) is not None
    assert "scheduled" in hourly


def test_autonomous_program_imports_all_tasks_without_serial_dependencies(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = tmp_path / "plan.md"
    plan.write_text("".join(f"- [ ] Implement feature {index}\n" for index in range(1, 15)), encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    code = main(
        [
            "program",
            "create",
            "--file",
            str(plan),
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--autonomous",
            "--parallel",
            "--format",
            "json",
        ]
    )
    program = json.loads(capsys.readouterr().out)

    assert code == 0
    assert len(program["tasks"]) == 14
    assert all(task["depends_on"] == [] for task in program["tasks"])
    assert program["autonomy_policy"]["mode"] == "autonomous"
    assert program["autonomy_policy"]["dispatch_mode"] == "delegated"
    assert program["autonomy_policy"]["allow_dirty"] is False
    assert program["quota_policy"]["quota_mode"] == "unrestricted"
    assert program["autonomy_policy"]["model_routing"]["cheap_worker"] == "gpt-5.6-luna"


def test_program_cli_model_override_applies_to_every_worker_mode(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = tmp_path / "plan.md"
    plan.write_text("- [ ] Update docs\n", encoding="utf-8")

    code = main(
        [
            "program",
            "create",
            "--file",
            str(plan),
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--autonomous",
            "--model",
            "gpt-5.5",
            "--format",
            "json",
        ]
    )
    program = json.loads(capsys.readouterr().out)

    assert code == 0
    assert set(program["autonomy_policy"]["model_routing"].values()) == {"gpt-5.5"}


def test_autonomous_program_persists_closed_loop_contract(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = tmp_path / "plan.md"
    plan.write_text("- [ ] Implement the roadmap task\n", encoding="utf-8")

    program = create_program_from_plan(
        source_file=plan,
        workspace_root=workspace.root,
        repo_root=tmp_path,
        autonomous=True,
        codex_provider="openai",
        codex_failover_provider="custom",
        memory_mode="disabled",
    )
    policy = program["autonomy_policy"]["closed_loop"]

    assert policy["codex_provider"] == "openai"
    assert policy["codex_failover_provider"] == "custom"
    assert policy["memory_mode"] == "disabled"
    assert policy["acceptance_policy"] == "strict"
    assert policy["roadmap_mode"] == "locked"
    assert policy["source_plan_sha256"] == program["source_plan_sha256"]
