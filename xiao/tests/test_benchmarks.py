from __future__ import annotations

import json
from pathlib import Path

from visual_agent.benchmarks import benchmark_plan_to_markdown, build_benchmark_plan, build_benchmark_workflow_draft
from visual_agent.cli import main
from visual_agent.workflow import parse_workflow_file
from visual_agent.workspace import init_workspace


def test_build_benchmark_plan_returns_scenarios_and_acceptance() -> None:
    payload = build_benchmark_plan(benchmark_id="healenium_locator_repair")

    assert payload["status"] == "ready"
    assert payload["benchmark_count"] == 1
    assert payload["scenario_count"] >= 1
    assert payload["scenarios"][0]["workflow_name"].startswith("benchmark_healenium_locator_repair")
    assert "selector_repair" in payload["scenarios"][0]["capabilities"]
    assert "auto-repair --dry-run" in payload["acceptance"]["repair"]


def test_benchmark_plan_markdown_lists_workflows() -> None:
    markdown = benchmark_plan_to_markdown(build_benchmark_plan(category="self_healing_tests"))

    assert "Benchmark Plan" in markdown
    assert "benchmark_healenium_locator_repair" in markdown
    assert "Acceptance Commands" in markdown


def test_benchmark_plan_cli_outputs_json(tmp_path, capsys) -> None:
    code = main(["benchmark-plan", "--benchmark-id", "stagehand_act_extract", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["benchmark_count"] == 1
    assert payload["scenarios"][0]["benchmark_id"] == "stagehand_act_extract"


def test_benchmark_plan_cli_returns_one_for_missing_id(capsys) -> None:
    code = main(["benchmark-plan", "--benchmark-id", "missing", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["status"] == "not_found"


def test_benchmark_workflow_draft_dry_run_returns_yaml(tmp_path) -> None:
    payload = build_benchmark_workflow_draft(
        scenario_id="healenium_locator_repair_1",
        workspace_root=tmp_path / "workspace",
    )

    assert payload["status"] == "success"
    assert payload["saved_to"] is None
    assert "observe_fixture" in payload["yaml"]
    assert "selector_repair" in payload["yaml"]


def test_benchmark_workflow_draft_save_writes_parseable_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    payload = build_benchmark_workflow_draft(
        scenario_id="stagehand_act_extract_1",
        workspace_root=workspace.root,
        dry_run=False,
    )

    saved_to = Path(payload["saved_to"])
    workflow = parse_workflow_file(saved_to)

    assert payload["status"] == "success"
    assert saved_to.exists()
    assert workflow.name == payload["workflow_name"]
    assert "benchmark" in workflow.tags


def test_benchmark_draft_cli_can_save_workflow(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    code = main(
        [
            "benchmark-draft",
            "--workspace-root",
            str(workspace.root),
            "--scenario-id",
            "stagehand_act_extract_1",
            "--save",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert Path(payload["saved_to"]).exists()
