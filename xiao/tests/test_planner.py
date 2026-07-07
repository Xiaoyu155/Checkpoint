from pathlib import Path

from visual_agent.planner import check_planner_draft
from visual_agent.workflow import Workflow, WorkflowStep
from visual_agent.workspace import init_workspace


def make_workflow(*steps: WorkflowStep) -> Workflow:
    return Workflow(name="draft", version=1, steps=steps)


def test_planner_draft_accepts_valid_dry_run_shape() -> None:
    workflow = make_workflow(
        WorkflowStep("observe", "observe_html", {"path": "fixtures/login.html"}),
        WorkflowStep("assert", "assert_text", {"text": "登录"}),
        WorkflowStep("click", "click", {"target": {"text": "登录", "role": "button"}}),
    )

    result = check_planner_draft(workflow)
    codes = {issue.code for issue in result.issues}

    assert result.valid is True
    assert result.allowed_to_execute is False
    assert result.dry_run_required is True
    assert "dry_run_required" in codes
    assert "click" in result.atomic_capabilities


def test_planner_draft_blocks_high_risk_capability_by_default() -> None:
    workflow = make_workflow(
        WorkflowStep("observe", "observe_browser", {"url": "https://example.com"}),
        WorkflowStep("save_auth", "save_storage_state", {"path": ".agent-auth/session.json"}),
    )

    result = check_planner_draft(workflow)

    assert result.valid is False
    assert any(issue.code == "high_risk_blocked" for issue in result.issues)


def test_planner_draft_can_allow_high_risk_with_explicit_flag() -> None:
    workflow = make_workflow(
        WorkflowStep("observe", "observe_browser", {"url": "https://example.com"}),
        WorkflowStep("assert", "assert_text", {"text": "已登录"}),
        WorkflowStep("save_auth", "save_storage_state", {"path": ".agent-auth/session.json"}),
    )

    result = check_planner_draft(workflow, allow_high_risk=True)

    assert result.valid is True
    assert result.allowed_to_execute is False


def test_planner_draft_rejects_non_planner_visible_actions() -> None:
    workflow = make_workflow(
        WorkflowStep("observe", "observe_screen", {}),
        WorkflowStep("assert", "assert_text", {"text": "登录"}),
    )

    result = check_planner_draft(workflow)

    assert result.valid is False
    assert any(issue.code == "capability_not_planner_visible" for issue in result.issues)


def test_planner_draft_warns_without_assertion() -> None:
    workflow = make_workflow(
        WorkflowStep("observe", "observe_html", {"path": "fixtures/login.html"}),
        WorkflowStep("wait", "wait_for", {"condition": "text", "text": "登录"}),
    )

    result = check_planner_draft(workflow)

    assert result.valid is True
    assert any(issue.code == "missing_assertion" for issue in result.issues)


def test_workspace_plan_paths_must_stay_inside_workspace(tmp_path: Path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace", with_demo=False)
    workflow = make_workflow(
        WorkflowStep("observe", "observe_html", {"path": "../outside.html"}),
        WorkflowStep("assert", "assert_text", {"text": "登录"}),
    )

    result = check_planner_draft(workflow, workspace=workspace)

    assert result.valid is False
    assert any(issue.code == "path_outside_workspace" for issue in result.issues)
