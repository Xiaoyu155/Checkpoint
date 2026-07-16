from __future__ import annotations

from copy import deepcopy

import pytest

from visual_agent.dogfood_ci import validate_pacer_task_handoff


def _active() -> dict:
    return {
        "launch_id": "launch-clean",
        "status": "completed",
        "source_baseline_complete": True,
        "source_baseline_digest": "a" * 64,
        "completion_control": {
            "attempts": 1,
            "last_rejection_codes": [],
        },
    }


def _record() -> dict:
    return {
        "launch_id": "launch-clean",
        "status": "completed",
        "batch_run_id": "batch-clean",
        "task_review": {
            "verdict": "approved",
            "trust": "yes",
            "evidence_integrity": "verified",
            "acceptance_adequacy": "sufficient",
            "product_verdict": "pass",
            "evidence_origin": "server_derived",
            "warnings": [],
            "errors": [],
            "source_change_complete": True,
            "source_changes": [
                {
                    "path": "src/visual_agent/dogfood_provider_check.py",
                    "state": "modified",
                },
                {
                    "path": "tests/test_dogfood_provider_check.py",
                    "state": "modified",
                },
            ],
            "task_contract": {
                "schema_version": 2,
                "goal_digest": "b" * 64,
                "requirements": [{"id": "R01", "text": "Apply the candidate patch."}],
                "acceptance_contract": {
                    "schema_version": 1,
                    "observable_outcomes": ["The exact candidate patch is applied."],
                    "verification": {"required_step_classes": ["test"]},
                },
            },
        },
    }


_CHANGED = [
    "src/visual_agent/dogfood_provider_check.py",
    "tests/test_dogfood_provider_check.py",
]


def test_clean_single_attempt_pacer_handoff_is_accepted() -> None:
    result = validate_pacer_task_handoff(_active(), _record(), changed_files=_CHANGED)

    assert result["launch_id"] == "launch-clean"
    assert result["batch_run_id"] == "batch-clean"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("trust", "with_limits", "trust is not yes"),
        ("warnings", [{"code": "warning"}], "contains warnings or errors"),
        ("evidence_origin", "model_reported", "evidence_origin is not server_derived"),
    ],
)
def test_limited_or_warning_task_review_fails_closed(
    field: str,
    value: object,
    message: str,
) -> None:
    record = _record()
    record["task_review"][field] = value

    with pytest.raises(ValueError, match=message):
        validate_pacer_task_handoff(_active(), record, changed_files=_CHANGED)


def test_completion_resubmission_fails_closed() -> None:
    active = deepcopy(_active())
    active["completion_control"]["attempts"] = 2
    active["completion_control"]["last_rejection_codes"] = ["claim_invalid"]

    with pytest.raises(ValueError, match="resubmitted"):
        validate_pacer_task_handoff(active, _record(), changed_files=_CHANGED)


def test_out_of_band_or_missing_source_change_fails_closed() -> None:
    with pytest.raises(ValueError, match="do not match"):
        validate_pacer_task_handoff(
            _active(),
            _record(),
            changed_files=[*_CHANGED, "src/visual_agent/other.py"],
        )
