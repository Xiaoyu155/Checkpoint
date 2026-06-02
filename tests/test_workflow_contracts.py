import json
from pathlib import Path

from visual_agent.validation import validate_workflow_file
from visual_agent.workflow import WorkflowRuntime, parse_workflow_file


EXAMPLE_WORKFLOWS = tuple(sorted(Path("examples").glob("*_workflow.yaml"))) + (
    Path("examples/screen_click_workflow.json"),
)
TEMPLATE_WORKFLOWS = tuple(sorted(Path("templates").glob("*/*.yaml")))

RUNNABLE_EXAMPLES = (
    Path("examples/minimal_testable_workflow.yaml"),
    Path("examples/minimal_form_workflow.yaml"),
    Path("examples/minimal_wait_retry_workflow.yaml"),
    Path("examples/local_html_form_workflow.yaml"),
    Path("examples/local_business_backend_workflow.yaml"),
    Path("examples/windows_notepad_demo_workflow.yaml"),
    Path("examples/failure_diagnosis_workflow.yaml"),
    Path("examples/ocr_mock_workflow.yaml"),
    Path("examples/ocr_failure_diagnosis_workflow.yaml"),
    Path("examples/vision_mock_workflow.yaml"),
    Path("examples/vision_screenshot_workflow.yaml"),
)


def test_all_example_and_template_workflows_validate() -> None:
    paths = EXAMPLE_WORKFLOWS + TEMPLATE_WORKFLOWS
    assert paths

    invalid = []
    for path in paths:
        result = validate_workflow_file(path)
        if not result.valid:
            invalid.append((path, result.issues))

    assert invalid == []


def test_all_example_and_template_workflows_declare_schema_version() -> None:
    paths = EXAMPLE_WORKFLOWS + TEMPLATE_WORKFLOWS
    missing = []
    for path in paths:
        workflow = parse_workflow_file(path)
        if workflow.schema_version != 1:
            missing.append(path)

    assert missing == []


def test_runnable_example_workflows_dry_run_or_expected_failure(tmp_path) -> None:
    expected_failures = {
        "failure_diagnosis_demo": "assert_missing_text",
        "ocr_failure_diagnosis_demo": "assert_missing_text",
    }

    for path in RUNNABLE_EXAMPLES:
        workflow = parse_workflow_file(path)
        result = WorkflowRuntime(output_dir=tmp_path / path.stem).run(
            workflow,
            dry_run=True,
            synthetic_on_capture_fail=True,
            inputs={"username": "demo_user", "password": "demo_password"},
        )
        failed_steps = [step for step in result.steps if step.status.value == "failed"]
        expected_failed_step = expected_failures.get(workflow.name)
        if expected_failed_step:
            assert failed_steps and failed_steps[-1].id == expected_failed_step
            assert "failure_diagnosis" in failed_steps[-1].metadata
        else:
            assert failed_steps == []


def test_layered_workflow_index_points_to_valid_workflows() -> None:
    index_path = Path("examples/workflows/index.json")
    index = json.loads(index_path.read_text(encoding="utf-8"))

    assert index["schema_version"] == 1
    group_ids = {group["id"] for group in index["groups"]}
    assert group_ids == {"readonly", "form-fill", "download", "auth"}

    workflow_paths = []
    for group in index["groups"]:
        for workflow_ref in group["workflows"]:
            workflow_path = index_path.parent / workflow_ref
            workflow_paths.append(workflow_path)
            assert workflow_path.exists(), workflow_ref
            assert validate_workflow_file(workflow_path).valid, workflow_ref
            parse_workflow_file(workflow_path)

    assert workflow_paths
