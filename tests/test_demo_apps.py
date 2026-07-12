from __future__ import annotations

from pathlib import Path

from visual_agent.workflow import parse_workflow_file


def test_demo_app_workflows_parse() -> None:
    root = Path("examples/demo-app/workflows")
    workflow_files = sorted(root.glob("*.yaml"))

    assert len(workflow_files) == 7
    for path in workflow_files:
        workflow = parse_workflow_file(path)
        assert workflow.name.startswith("demo_app_")
        assert "demo_app" in workflow.tags


def test_nextjs_demo_workflows_parse() -> None:
    root = Path("examples/nextjs-demo/workflows")
    workflow_files = sorted(root.glob("*.yaml"))

    assert len(workflow_files) == 7
    for path in workflow_files:
        workflow = parse_workflow_file(path)
        assert workflow.name.startswith("nextjs_demo_")
        assert "nextjs_demo" in workflow.tags
