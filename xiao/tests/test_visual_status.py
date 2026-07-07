from __future__ import annotations

import json
from pathlib import Path

from visual_agent.cli import main
from visual_agent.models import ActionStatus
from visual_agent.mcp_server import mcp_tools
from visual_agent.visual_status import read_run_history, read_status_file
from visual_agent.workspace import init_workspace, load_workspace_inputs, run_workspace_workflow


def test_workspace_run_writes_visual_status_and_history(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace")
    inputs = load_workspace_inputs(workspace, None, "demo_login.json")

    result = run_workspace_workflow(workspace, "local_html_form_workflow", inputs=inputs, dry_run=True)
    status = read_status_file(tmp_path)
    history = read_run_history(workspace.root)

    assert result.steps[-1].status == ActionStatus.DRY_RUN
    assert status is not None
    assert status.status == "PASSING"
    assert status.passing == ("local_html_form_workflow",)
    assert (tmp_path / ".visual-agent-status.md").exists()
    assert len(history) == 1
    assert history[0]["workflow_name"] == "local_html_form_workflow"
    assert history[0]["passed"] is True
    assert history[0]["step_count"] >= 1


def test_run_workflow_cli_writes_project_status_and_history(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    workflow_path = tmp_path / "smoke.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: smoke\n"
        "version: 1\n"
        "tags: [verification]\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_ocr\n"
        "    mock_text: Ready\n"
        "  - id: assert_ready\n"
        "    action: assert_text\n"
        "    text: Ready\n",
        encoding="utf-8",
    )

    code = main(["run-workflow", "--file", str(workflow_path), "--output-dir", str(tmp_path / ".runs")])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["workflow_name"] == "smoke"
    assert (tmp_path / ".visual-agent-status.md").exists()
    assert read_run_history(tmp_path / ".agent-workspace")[0]["workflow_name"] == "smoke"


def test_stats_and_export_runs_cli(tmp_path: Path, capsys) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace")
    inputs = load_workspace_inputs(workspace, None, "demo_login.json")
    run_workspace_workflow(workspace, "local_html_form_workflow", inputs=inputs, dry_run=True)

    code = main(["stats", "--workspace-root", str(workspace.root), "--format", "json"])
    stats = json.loads(capsys.readouterr().out)
    assert code == 0
    assert stats["total_runs"] == 1
    assert stats["pass_rate"] == 1.0

    output = tmp_path / "runs.csv"
    code = main(["export-runs", "--workspace-root", str(workspace.root), "--output", str(output), "--format", "csv"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "success"
    assert output.exists()
    assert "local_html_form_workflow" in output.read_text(encoding="utf-8")


def test_telemetry_is_opt_in(tmp_path: Path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace")
    inputs = load_workspace_inputs(workspace, None, "demo_login.json")

    run_workspace_workflow(workspace, "local_html_form_workflow", inputs=inputs, dry_run=True)
    assert not (workspace.root / "telemetry.jsonl").exists()

    monkeypatch.setenv("VISUAL_AGENT_TELEMETRY", "1")
    run_workspace_workflow(workspace, "local_html_form_workflow", inputs=inputs, dry_run=True)
    events = [json.loads(line) for line in (workspace.root / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()]

    assert events[-1]["event"] == "workflow_run"
    assert events[-1]["visibility"] == "private"
    assert events[-1]["passed"] is True
    assert "step_types" in events[-1]


def test_repeated_failure_is_marked_known_problem(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    workflow = workspace.workflows_dir / "failure.yaml"
    workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_ocr\n"
        "    mock_text: Ready\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: Missing\n",
        encoding="utf-8",
    )

    for _ in range(3):
        run_workspace_workflow(workspace, "failure", dry_run=True, preflight=False)

    history = read_run_history(workspace.root)

    assert history[-1]["known_problem"] is True
    assert history[-1]["known_label"] == "[KNOWN]"
    assert "[KNOWN]" in (tmp_path / ".visual-agent-status.md").read_text(encoding="utf-8")


def test_mcp_exposes_get_visual_status_tool() -> None:
    names = {tool.name for tool in mcp_tools()}

    assert "get_visual_status" in names


def test_root_cause_guess_marks_hydration_mismatch_as_known_issue() -> None:
    class Step:
        action = "assert_text"
        message = (
            "A tree hydrated but some attributes of the server rendered HTML didn't match the client properties. "
            "https://react.dev/link/hydration-mismatch"
        )
        metadata = {}

    from visual_agent.visual_status import root_cause_guess

    assert root_cause_guess(Step()) == "known_issue"
