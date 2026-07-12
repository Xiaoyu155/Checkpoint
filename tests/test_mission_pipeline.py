from __future__ import annotations

import json

import pytest

from visual_agent.mission_pipeline import MissionPipeline, SpecValidationError, SpecValidator


def test_spec_validator_requires_scope_plan_test():
    validator = SpecValidator()

    with pytest.raises(SpecValidationError) as excinfo:
        validator.validate({"scope": ["src"], "plan": ["edit"], "test": ["pytest -q"], "risk": ["low"]})

    assert excinfo.value.field == "spec.rollback"
    assert excinfo.value.to_response()["error_code"] == "spec_validation_failed"


def test_mission_pipeline_writes_state_json(tmp_path):
    spec = SpecValidator().validate(
        {
            "scope": ["src/visual_agent"],
            "plan": ["Wrap workbench launch"],
            "test": ["python -m pytest tests/test_mission_pipeline.py -q"],
            "risk": ["Low-risk wrapper change."],
            "rollback": ["Revert wrapper change."],
        }
    )
    pipeline = MissionPipeline(tmp_path / ".agent-workspace", launch_id="launch-1")

    state = pipeline.begin(spec=spec, execute=False, request={"goal": "Wrap workbench launch"})
    pipeline.attach_mission(state, "mission-1")
    pipeline.transition(state, "VERIFIED", "test_complete")

    launch_payload = json.loads(pipeline.path.read_text(encoding="utf-8"))
    mission_payload = json.loads((tmp_path / ".agent-workspace" / "missions" / "mission-1" / "state.json").read_text(encoding="utf-8"))
    assert launch_payload["current_state"] == "VERIFIED"
    assert mission_payload["mission_id"] == "mission-1"
    assert mission_payload["context"]["spec"]["plan"] == ["Wrap workbench launch"]
