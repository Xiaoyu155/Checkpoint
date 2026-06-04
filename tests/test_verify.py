from __future__ import annotations

from pathlib import Path

from visual_agent.verify import _failure_hint, _has_tag, run_verify, verify_to_markdown
from visual_agent.workspace import init_workspace
from visual_agent.models import ActionStatus
from visual_agent.workflow import WorkflowStepResult


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = str(ROOT / "examples" / "fixtures" / "login_page_observation.json").replace(chr(92), "/")


def write_workflow(workspace, name: str, *, tagged: bool = True, failing: bool = False, extra_tags: tuple[str, ...] = ()) -> Path:
    path = workspace.workflows_dir / f"{name}.yaml"
    tag_lines = ["verification", *extra_tags] if tagged else list(extra_tags)
    tags = "tags:\n" + "".join(f"  - {tag}\n" for tag in tag_lines) if tag_lines else ""
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


def test_run_verify_requires_all_requested_tags(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(workspace, "verification", tagged=True)
    path = write_workflow(workspace, "miniprogram_verification", tagged=True)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("  - verification\n", "  - verification\n  - miniprogram\n"), encoding="utf-8")

    report = run_verify(workspace, tags=("verification", "miniprogram"))

    assert report.total == 1
    assert report.results[0].name == "miniprogram_verification"


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
    assert "No matching verification workflows found" in report.suggested_prompt


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


def test_run_verify_can_target_specific_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(workspace, "slow_visual_contract")
    write_workflow(workspace, "fast_smoke_contract")

    report = run_verify(workspace, workflow_names=("fast_smoke_contract",))

    assert report.total == 1
    assert report.results[0].name == "fast_smoke_contract"


def test_run_verify_skips_slow_workflows_by_default(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(workspace, "slow_visual_contract", extra_tags=("slow",))
    write_workflow(workspace, "fast_smoke_contract")

    report = run_verify(workspace)

    assert report.total == 1
    assert report.results[0].name == "fast_smoke_contract"


def test_run_verify_includes_slow_workflows_when_requested(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(workspace, "slow_visual_contract", extra_tags=("slow",))
    write_workflow(workspace, "fast_smoke_contract")

    report = run_verify(workspace, include_slow=True)

    assert report.total == 2
    assert {item.name for item in report.results} == {"slow_visual_contract", "fast_smoke_contract"}


def test_run_verify_respects_custom_max_workflows(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    for index in range(3):
        write_workflow(workspace, f"verification_{index:02d}")

    report = run_verify(workspace, max_workflows=1)

    assert report.total == 1


def test_run_verify_supervised_profile(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(workspace, "verification")

    report = run_verify(workspace, run_profile="supervised")

    assert report.total == 1
    assert report.results[0].passed is True


def test_run_verify_passes_wait_lock_options(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(workspace, "verification")
    calls = []

    def fake_run_workspace_workflow(*_args, **kwargs):
        calls.append(kwargs)

        class Result:
            run_id = "run"
            steps = []

        return Result()

    monkeypatch.setattr("visual_agent.verify.run_workspace_workflow", fake_run_workspace_workflow)

    report = run_verify(workspace, wait_lock=True, lock_wait_seconds=12.0)

    assert report.total == 1
    assert calls[0]["queue_when_locked"] is True
    assert calls[0]["lock_wait_seconds"] == 12.0
