from visual_agent.reports import (
    list_run_summaries,
    load_run_report,
    load_run_summary,
    run_report_to_dict,
    run_report_to_markdown,
    run_summary_to_dict,
)
from visual_agent.workflow import WorkflowRuntime, parse_workflow_file


def test_run_summary_reports_successful_dry_run(tmp_path) -> None:
    workflow = parse_workflow_file("examples/local_html_form_workflow.yaml")
    result = WorkflowRuntime(output_dir=tmp_path).run(
        workflow,
        dry_run=True,
        inputs={"username": "demo_user", "password": "demo_password"},
    )

    summary = load_run_summary(result.run_dir)

    assert summary.status == "success"
    assert summary.workflow_schema_version == 1
    assert summary.runtime_version == "0.1.0"
    assert summary.run_profile == "dry-run"
    assert summary.total_steps == 6
    assert summary.dry_run_actions == 3
    assert run_summary_to_dict(summary)["run_id"] == result.run_id


def test_list_run_summaries_returns_newest_first(tmp_path) -> None:
    workflow = parse_workflow_file("examples/minimal_testable_workflow.yaml")
    first = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)
    second = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)

    summaries = list_run_summaries(tmp_path)

    assert summaries[0].run_id == second.run_id
    assert summaries[1].run_id == first.run_id


def test_run_report_includes_steps_schema_and_markdown(tmp_path) -> None:
    workflow = parse_workflow_file("examples/ocr_failure_diagnosis_workflow.yaml")
    result = WorkflowRuntime(output_dir=tmp_path).run(workflow, dry_run=True)

    report = load_run_report(result.run_dir)
    payload = run_report_to_dict(report)
    markdown = run_report_to_markdown(report)

    assert report.schema_version == 1
    assert report.workflow_schema_version == 1
    assert report.runtime_version == "0.1.0"
    assert report.run_profile == "dry-run"
    assert report.run_lock is not None
    assert report.status == "failed"
    assert report.failed_step == "assert_missing_text"
    assert payload["steps"][-1]["failure_diagnosis"]["expected"] == "expected text: 不存在的成功提示"
    assert payload["steps"][-1]["failure_artifacts"]["screenshot"]
    assert payload["steps"][-1]["failure_artifacts"]["dom_excerpt"]
    assert payload["steps"][0]["observation_summary"]["screenshot_path"]
    assert payload["steps"][0]["observation_summary"]["visible_text"]
    assert payload["run_lock"]["owner"].startswith("ocr_failure_diagnosis_demo:")
    assert payload["steps"][-1]["artifact_paths"]
    assert "# Run Report: ocr_failure_diagnosis_demo" in markdown
    assert "Lock owner" in markdown
    assert "Failure expected" in markdown
    assert "Failure screenshot" in markdown
    assert "DOM excerpt" in markdown
    assert "Visible text" in markdown
    assert "Screenshot" in markdown


def test_run_report_surfaces_selector_resolution_metadata(tmp_path) -> None:
    workflow = parse_workflow_file("examples/local_html_form_workflow.yaml")
    result = WorkflowRuntime(output_dir=tmp_path).run(
        workflow,
        dry_run=True,
        inputs={"username": "demo_user", "password": "demo_password"},
    )

    report = load_run_report(result.run_dir)
    payload = run_report_to_dict(report)
    markdown = run_report_to_markdown(report)
    click_step = next(step for step in payload["steps"] if step["id"] == "click_login")

    assert click_step["selector_resolution"]["selected_provider"] == "dom"
    assert click_step["selector_resolution"]["fallback_path"] == ["dom"]
    assert click_step["selector_resolution"]["confidence_level"] == "high"
    assert "Selector: level `high`" in markdown
    assert "fallback path `dom`" in markdown
