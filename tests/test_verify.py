from __future__ import annotations

from pathlib import Path

from visual_agent.verify import _failure_hint, _has_tag, run_verify, verify_to_markdown
from visual_agent.workspace import init_workspace
from visual_agent.models import ActionStatus
from visual_agent.workflow import WorkflowStepResult


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = str(ROOT / "examples" / "fixtures" / "login_page_observation.json").replace(chr(92), "/")


def write_workflow(workspace, name: str, *, tagged: bool = True, failing: bool = False) -> Path:
    path = workspace.workflows_dir / f"{name}.yaml"
    tags = "tags:\n  - verification\n" if tagged else ""
    assert_step = "  - id: assert_title\n    action: assert_text\n    text: missing text\n" if failing else (
        "  - id: assert_title\n    action: assert_text\n    text: 客户管理系统\n"
    )
    path.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        f"name: {name}\n"
        "version: 1\n"
        f"{tags}"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {FIXTURE}\n"
        f"{assert_step}",
        encoding="utf-8",
    )
    return path


def test_run_verify_runs_tagged_workflows_only(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(workspace, "verification", tagged=True)
    write_workflow(workspace, "untagged", tagged=False)

    report = run_verify(workspace)
    markdown = verify_to_markdown(report)

    assert report.total == 1
    assert report.passed == 1
    assert report.failed == 0
    assert len(markdown) <= 3200


def test_run_verify_with_failing_workflow_reports_failure(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(workspace, "verification", failing=True)

    report = run_verify(workspace)

    assert report.total == 1
    assert report.passed == 0
    assert report.failed == 1
    assert report.results[0].failed_step == "assert_title"
    assert report.results[0].hint


def test_run_verify_no_tagged_workflows_returns_empty(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(workspace, "untagged", tagged=False)

    report = run_verify(workspace)

    assert report.total == 0
    assert report.passed == 0
    assert report.failed == 0
    assert "No verification-tagged workflows found" in report.suggested_prompt


def test_verify_to_markdown_contains_failed_section_when_failure(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(workspace, "verification", failing=True)

    markdown = verify_to_markdown(run_verify(workspace))

    assert "### Failed" in markdown
    assert "verification" in markdown
    assert "assert_title" in markdown


def test_verify_to_markdown_within_budget(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    for index in range(12):
        write_workflow(workspace, f"verification_{index}", failing=True)

    markdown = verify_to_markdown(run_verify(workspace))

    assert len(markdown) <= 3200


def test_verify_failure_hint_fallback_to_message() -> None:
    failed = WorkflowStepResult(
        id="assert_title",
        action="assert_text",
        status=ActionStatus.FAILED,
        message="plain failure message",
        metadata={},
    )

    assert _failure_hint(failed) == "plain failure message"


def test_has_tag_returns_false_when_parse_fails(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    bad = workspace.workflows_dir / "bad.yaml"
    bad.write_text("not: [valid", encoding="utf-8")
    ref = next(item for item in workspace.workflows_dir.rglob("bad.yaml"))

    class Ref:
        path = ref

    assert _has_tag(Ref(), ("verification",)) is False


def test_run_verify_caps_at_ten_workflows(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    for index in range(12):
        write_workflow(workspace, f"verification_{index:02d}")

    report = run_verify(workspace)

    assert report.total == 10
    assert report.passed == 10


def test_run_verify_supervised_profile(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(workspace, "verification")

    report = run_verify(workspace, run_profile="supervised")

    assert report.total == 1
    assert report.results[0].passed is True
