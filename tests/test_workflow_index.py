from __future__ import annotations

import json

from visual_agent.models import ActionStatus
from visual_agent.workflow_index import INDEX_FILE, list_public_workflows, load_workflow_index, mark_workflow_public
from visual_agent.workspace import find_workflow, init_workspace, load_workspace_inputs, run_workspace_workflow


def test_run_workspace_workflow_updates_workflow_index(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    inputs = load_workspace_inputs(workspace, None, "demo_login.json")

    result = run_workspace_workflow(workspace, "local_html_form_workflow", inputs=inputs, dry_run=True)
    index = load_workflow_index(workspace.root)

    assert result.steps[-1].status == ActionStatus.DRY_RUN
    assert (workspace.root / INDEX_FILE).exists()
    assert index["local_html_form_workflow"]["name"] == "local_html_form_workflow"
    assert index["local_html_form_workflow"]["visibility"] == "private"
    assert index["local_html_form_workflow"]["path"] == "workflows/local_html_form_workflow.yaml"


def test_mark_workflow_public_and_list_public_workflows(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    ref = find_workflow(workspace, "local_html_form_workflow")

    index_path = mark_workflow_public(workspace.root, ref)
    public = list_public_workflows(workspace.root)

    assert index_path == workspace.root / INDEX_FILE
    assert [item["name"] for item in public] == ["local_html_form_workflow"]
    assert public[0]["visibility"] == "public"


def test_load_workflow_index_tolerates_corrupt_file(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    (workspace.root / INDEX_FILE).write_text("{not-json", encoding="utf-8")

    assert load_workflow_index(workspace.root) == {}


def test_workflow_index_json_is_plain_object(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    ref = find_workflow(workspace, "local_html_form_workflow")

    mark_workflow_public(workspace.root, ref)
    payload = json.loads((workspace.root / INDEX_FILE).read_text(encoding="utf-8"))

    assert isinstance(payload, dict)
    assert isinstance(payload["local_html_form_workflow"]["tags"], list)
