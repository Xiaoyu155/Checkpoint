from __future__ import annotations

import json
from pathlib import Path

from visual_agent.cli import main
from visual_agent.workflow import parse_workflow_file
from visual_agent.workflow_generator import generate_workflow_yaml
from visual_agent.workspace import Workspace, discover_workflows


def test_generate_workflow_dry_run_template_is_valid(tmp_path: Path) -> None:
    result = generate_workflow_yaml(
        description="Verify user login shows dashboard",
        workspace_root=tmp_path / ".agent-workspace",
        dry_run=True,
    )

    assert result["status"] == "success"
    assert result["source"] in {"template_fallback", "anthropic"}
    assert "observe_browser" in result["yaml"]
    assert "assert_browser_ready" in result["yaml"]
    assert "visibility: private" in result["yaml"]


def test_generate_workflow_saves_to_project_workflows(tmp_path: Path) -> None:
    workspace_root = tmp_path / ".agent-workspace"
    workspace_root.mkdir()

    result = generate_workflow_yaml(
        description="Verify checkout success message",
        workspace_root=workspace_root,
    )

    assert result["status"] == "success"
    saved_to = Path(str(result["saved_to"]))
    assert saved_to.exists()
    assert saved_to.parent == tmp_path / "workflows"
    workflow = parse_workflow_file(saved_to)
    assert workflow.visibility == "private"
    assert "verification" in workflow.tags


def test_cli_generate_workflow_dry_run_outputs_json(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "generate-workflow",
            "--workspace-root",
            str(tmp_path / ".agent-workspace"),
            "--description",
            "Verify settings page loads",
            "--dry-run",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "success"
    assert payload["saved_to"] is None
    assert "assert_browser_ready" in payload["yaml"]
    assert "assert_text" in payload["yaml"]


def test_workflow_ref_reads_visibility_author_description_and_license(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / ".agent-workspace")
    workspace.workflows_dir.mkdir(parents=True)
    workflow_path = workspace.workflows_dir / "public_demo.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: public_demo\n"
        "version: 1\n"
        "description: Public demo workflow\n"
        "tags: [verification]\n"
        "visibility: public\n"
        "author: demo\n"
        "license: cc-by-4.0\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_ocr\n"
        "    mock_text: ready\n",
        encoding="utf-8",
    )

    refs = discover_workflows(workspace)

    assert len(refs) == 1
    assert refs[0].visibility == "public"
    assert refs[0].author == "demo"
    assert refs[0].description == "Public demo workflow"
    assert refs[0].license == "cc-by-4.0"
