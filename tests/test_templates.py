from visual_agent.models import ActionStatus
from visual_agent.templates import get_template, install_template, list_templates
from visual_agent.workspace import init_workspace, load_workspace_inputs, run_workspace_workflow
import pytest


def test_list_templates_contains_business_templates() -> None:
    templates = list_templates()
    ids = {template.id for template in templates}

    assert {
        "login_form",
        "order_entry",
        "ecommerce_download",
        "external_readonly_probe",
        "desktop_ocr_real_acceptance",
    }.issubset(ids)


def test_get_template_reads_manifest() -> None:
    template = get_template("order_entry")

    assert template.name == "订单录入 ERP"
    assert template.workflow == "order_entry.yaml"
    assert "erp" in template.tags


def test_install_template_into_workspace_and_run_order_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    result = install_template(workspace, "order_entry")

    assert (workspace.workflows_dir / "order_entry.yaml").exists()
    assert (workspace.inputs_dir / "order_entry_inputs.json").exists()
    assert result["copied"]

    inputs = load_workspace_inputs(workspace, None, "order_entry_inputs.json")
    run = run_workspace_workflow(workspace, "order_entry", inputs=inputs, dry_run=True)

    assert run.steps[-1].status == ActionStatus.DRY_RUN
    assert run.steps[-1].resolved_target is not None
    assert run.steps[-1].resolved_target.evidence.handle == "#save"


def test_install_template_into_workspace_and_run_ecommerce_download(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    install_template(workspace, "ecommerce_download")
    inputs = load_workspace_inputs(workspace, None, "ecommerce_download_inputs.json")

    run = run_workspace_workflow(workspace, "ecommerce_download", inputs=inputs, dry_run=True)

    assert run.steps[-1].status == ActionStatus.DRY_RUN
    assert run.steps[-1].resolved_target is not None
    assert run.steps[-1].resolved_target.evidence.handle == "#export"


def test_external_readonly_probe_template_is_observe_assert_only(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    result = install_template(workspace, "external_readonly_probe")
    workflow_path = workspace.workflows_dir / "external_readonly_probe.yaml"
    inputs_path = workspace.inputs_dir / "external_readonly_probe_inputs.json"

    assert workflow_path.exists()
    assert inputs_path.exists()
    assert any(path.endswith("external_readonly_probe.html") for path in result["copied"])

    import yaml

    payload = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    actions = {step["action"] for step in payload["steps"]}

    assert actions == {"observe_browser", "assert_text", "wait_for"}
    assert actions.isdisjoint({"click", "type", "paste", "expect_download", "save_storage_state"})
    assert payload["steps"][0]["url_from"] == "input.url"
    assert payload["steps"][1]["text_from"] == "input.assert_text"


def test_install_template_and_run_external_readonly_probe(tmp_path) -> None:
    pytest.importorskip("playwright")
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    install_template(workspace, "external_readonly_probe")
    inputs = load_workspace_inputs(workspace, None, "external_readonly_probe_inputs.json")

    run = run_workspace_workflow(workspace, "external_readonly_probe", inputs=inputs, dry_run=True)

    assert [step.action for step in run.steps] == ["observe_browser", "assert_text", "wait_for"]
    assert all(step.status == ActionStatus.SUCCESS for step in run.steps)


def test_desktop_ocr_real_acceptance_template_installs_actionable_skeleton(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    result = install_template(workspace, "desktop_ocr_real_acceptance")
    workflow_path = workspace.workflows_dir / "desktop_ocr_real_acceptance.yaml"
    inputs_path = workspace.inputs_dir / "desktop_ocr_real_acceptance_inputs.json"

    assert workflow_path.exists()
    assert inputs_path.exists()
    assert result["copied"]

    import yaml

    payload = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    inputs = load_workspace_inputs(workspace, None, "desktop_ocr_real_acceptance_inputs.json")
    actions = [step["action"] for step in payload["steps"]]
    type_amount = next(step for step in payload["steps"] if step["id"] == "type_amount")
    click = next(step for step in payload["steps"] if step["id"] == "click_approve")

    assert actions == ["observe_ocr", "assert_text_contract", "type", "click_text", "assert_text_contract"]
    assert inputs["engine"] == "tesseract"
    assert inputs["amount_value"] == "REPLACE_WITH_VALUE_TO_TYPE"
    assert inputs["required_before"] == [
        "REPLACE_WITH_VISIBLE_BUSINESS_TEXT",
        "REPLACE_WITH_VISIBLE_INPUT_TEXT",
        "REPLACE_WITH_VISIBLE_CLICK_TEXT",
    ]
    assert payload["steps"][1]["required_all_from"] == "input.required_before"
    assert payload["steps"][1]["forbidden_any_from"] == "input.forbidden_before"
    assert payload["steps"][4]["required_all_from"] == "input.required_after"
    assert payload["steps"][4]["forbidden_any_from"] == "input.forbidden_after"
    assert type_amount["allow_desktop_input"] is True
    assert type_amount["post_action_observe"]["assert_text_from"] == "input.expected_amount_text"
    assert click["post_action_observe"]["assert_text_from"] == "input.expected_after_text"
    assert "real-acceptance" in payload["tags"]
