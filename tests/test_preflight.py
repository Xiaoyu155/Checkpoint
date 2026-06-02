from visual_agent.preflight import run_preflight
from visual_agent.workflow import workflow_from_dict


def test_preflight_accepts_valid_workflow() -> None:
    workflow = workflow_from_dict(
        {
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "name": "ok",
            "steps": [
                {"id": "observe", "action": "observe_fixture", "path": "examples/fixtures/login_page_observation.json"},
                {"id": "assert", "action": "assert_text", "text": "客户管理系统"},
            ],
        }
    )

    result = run_preflight(workflow)

    assert result.ok
    assert result.validation.valid
    assert result.missing_required_capabilities == ()
    assert result.unavailable_used_capabilities == ()


def test_preflight_strict_rejects_missing_assertion() -> None:
    workflow = workflow_from_dict(
        {
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "name": "missing-assert",
            "steps": [
                {"id": "observe", "action": "observe_fixture", "path": "examples/fixtures/login_page_observation.json"},
            ],
        }
    )

    result = run_preflight(workflow, strict=True)

    assert not result.ok
    assert any("verification assertion" in issue.message for issue in result.validation.issues)


def test_preflight_blocks_unavailable_capability_used_by_workflow(monkeypatch) -> None:
    def fake_module_available(module_name: str | None) -> bool:
        if module_name == "uiautomation":
            return False
        return True

    monkeypatch.setattr("visual_agent.capabilities.module_available", fake_module_available)
    workflow = workflow_from_dict(
        {
            "schema_version": 1,
            "min_runtime_version": "0.1.0",
            "name": "uia",
            "steps": [
                {"id": "observe", "action": "observe_uia"},
                {"id": "assert", "action": "assert_text", "text": "确定"},
            ],
        }
    )

    result = run_preflight(workflow)

    assert not result.ok
    assert any(capability.name == "observe_uia" for capability in result.unavailable_used_capabilities)
