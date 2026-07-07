from visual_agent.models import ActionStatus
from visual_agent.product_issues import build_product_issues, product_issues_to_markdown, write_product_issues
from visual_agent.workspace import init_workspace, run_workspace_workflow


def test_product_issues_group_failed_workspace_reports(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    workflow = workspace.workflows_dir / "product_issue.yaml"
    workflow.write_text(
        """
schema_version: 1
min_runtime_version: "0.1.0"
name: product_issue
version: 1
steps:
  - id: observe
    action: observe_html
    path: fixtures/login_demo.html
  - id: assert_contract
    action: assert_product_contract
    required_sections:
      - 购买服务
    must_have_actions:
      - 立即购买
""".strip(),
        encoding="utf-8",
    )

    result = run_workspace_workflow(workspace, "product_issue", dry_run=True, preflight=False)
    payload = build_product_issues(workspace)
    path = write_product_issues(workspace)
    markdown = product_issues_to_markdown(payload)

    assert result.steps[-1].status == ActionStatus.FAILED
    assert path.exists()
    assert payload["total_failed_reports"] == 1
    assert payload["total_issues"] == 1
    assert payload["issues"][0]["workflow_name"] == "product_issue"
    assert payload["issues"][0]["failed_step"] == "assert_contract"
    assert "购买服务" in payload["issues"][0]["message"]
    assert "Product Issues" in markdown
    assert "product_issue / assert_contract" in markdown
