from __future__ import annotations

from visual_agent.acceptance import (
    PRODUCT_ACCEPTANCE_MIN_LEVEL,
    aggregate_cross_platform,
    grade_run,
    validate_operation_receipt,
)
from visual_agent.models import ActionStatus
from visual_agent.workflow import WorkflowStepResult


def step(
    step_id: str,
    action: str,
    status: ActionStatus = ActionStatus.SUCCESS,
    *,
    receipt: bool = False,
) -> WorkflowStepResult:
    metadata = {}
    if receipt:
        metadata["operation_receipt"] = {
            "schema_version": 1,
            "engine": "playwright",
            "live": True,
            "observed_after_action": True,
            "after": {"url": "https://example.test/after", "screenshot_path": "after.png"},
            "post_action_assertion": {"type": "text", "expected": "Saved", "status": "matched"},
            "actionability": {"checked": True, "count": 1, "visible": True, "enabled": True},
        }
    return WorkflowStepResult(id=step_id, action=action, status=status, metadata=metadata)


def contract_step(
    step_id: str,
    *,
    forbidden_any: list[str] | None = None,
    status: ActionStatus = ActionStatus.SUCCESS,
) -> WorkflowStepResult:
    return WorkflowStepResult(
        id=step_id,
        action="assert_text_contract",
        status=status,
        metadata={
            "text_contract": {
                "passed": status == ActionStatus.SUCCESS,
                "required_all": ["Saved"],
                "forbidden_any": forbidden_any or [],
            }
        },
    )


def test_no_observation_means_no_level() -> None:
    grade = grade_run([step("assert", "assert_text")])

    assert grade.level == -1
    assert grade.label == "none"
    assert grade.name == "not_opened"
    assert grade.is_product_acceptance is False


def test_open_without_content_assertion_is_l0() -> None:
    grade = grade_run([step("observe", "observe_browser")])

    assert grade.level == 0
    assert grade.name == "opens"
    assert "assert" in grade.missing


def test_observe_and_assert_is_l1() -> None:
    grade = grade_run([step("observe", "observe_browser"), step("assert", "assert_text")])

    assert grade.level == 1
    assert grade.name == "content_verified"
    assert "interaction" in grade.missing
    assert grade.is_product_acceptance is False


def test_fixture_only_runs_are_simulated_and_capped_at_l1() -> None:
    grade = grade_run(
        [
            step("observe", "observe_fixture"),
            step("assert", "assert_text"),
            step("click", "click"),
            step("verify", "assert_text"),
        ]
    )

    assert grade.level == 1
    assert grade.simulated is True
    assert grade.is_product_acceptance is False
    assert "live observe" in grade.missing


def test_interaction_without_outcome_assertion_is_l2() -> None:
    grade = grade_run(
        [
            step("observe", "observe_browser"),
            step("assert", "assert_text"),
            step("click", "click", receipt=True),
        ]
    )

    assert grade.level == 2
    assert grade.name == "real_interaction"


def test_dry_run_interaction_does_not_count() -> None:
    grade = grade_run(
        [
            step("observe", "observe_browser"),
            step("assert", "assert_text"),
            step("click", "click", ActionStatus.DRY_RUN),
            step("verify", "assert_text"),
        ]
    )

    assert grade.level == 1


def test_successful_interaction_without_operation_receipt_does_not_count_as_real() -> None:
    click = step("click", "click")
    grade = grade_run(
        [
            step("observe", "observe_browser"),
            step("assert", "assert_text"),
            click,
            step("verify", "assert_text"),
        ]
    )

    assert grade.level == 1
    assert "operation receipt" in grade.missing
    assert grade.invalid_operation_receipts == 1
    assert grade.operation_receipt_failures[0]["reason"] == "missing_operation_receipt"
    assert validate_operation_receipt(click).reason == "missing_operation_receipt"


def test_live_receipt_without_actionability_does_not_count_as_real() -> None:
    bad_receipt = WorkflowStepResult(
        id="click",
        action="click",
        status=ActionStatus.SUCCESS,
        metadata={"operation_receipt": {"schema_version": 1, "engine": "playwright", "live": True}},
    )
    grade = grade_run(
        [
            step("observe", "observe_browser"),
            step("assert", "assert_text"),
            bad_receipt,
            step("verify", "assert_text"),
        ]
    )

    assert grade.level == 1
    assert grade.invalid_operation_receipts == 1
    assert grade.operation_receipt_failures[0]["reason"] == "missing_actionability"


def test_non_unique_action_target_does_not_count_as_real() -> None:
    bad_receipt = WorkflowStepResult(
        id="click",
        action="click",
        status=ActionStatus.SUCCESS,
        metadata={
            "operation_receipt": {
                "schema_version": 1,
                "engine": "playwright",
                "live": True,
                "observed_after_action": True,
                "actionability": {"checked": True, "count": 2, "visible": True, "enabled": True},
            }
        },
    )
    grade = grade_run(
        [
            step("observe", "observe_browser"),
            step("assert", "assert_text"),
            bad_receipt,
            step("verify", "assert_text"),
        ]
    )

    assert grade.level == 1
    assert grade.operation_receipt_failures[0]["reason"] == "target_not_unique"


def test_hidden_or_disabled_action_target_does_not_count_as_real() -> None:
    hidden = WorkflowStepResult(
        id="click_hidden",
        action="click",
        status=ActionStatus.SUCCESS,
        metadata={
            "operation_receipt": {
                "schema_version": 1,
                "engine": "playwright",
                "live": True,
                "observed_after_action": True,
                "actionability": {"checked": True, "count": 1, "visible": False, "enabled": True},
            }
        },
    )
    disabled = WorkflowStepResult(
        id="click_disabled",
        action="click",
        status=ActionStatus.SUCCESS,
        metadata={
            "operation_receipt": {
                "schema_version": 1,
                "engine": "playwright",
                "live": True,
                "observed_after_action": True,
                "actionability": {"checked": True, "count": 1, "visible": True, "enabled": False},
            }
        },
    )

    assert validate_operation_receipt(hidden).reason == "target_not_visible"
    assert validate_operation_receipt(disabled).reason == "target_not_enabled"


def test_missing_post_action_observation_does_not_count_as_real() -> None:
    bad_receipt = WorkflowStepResult(
        id="click",
        action="click",
        status=ActionStatus.SUCCESS,
        metadata={
            "operation_receipt": {
                "schema_version": 1,
                "engine": "playwright",
                "live": True,
                "observed_after_action": False,
                "actionability": {"checked": True, "count": 1, "visible": True, "enabled": True},
            }
        },
    )

    assert validate_operation_receipt(bad_receipt).reason == "missing_post_action_observation"


def test_synthetic_evidence_does_not_count_as_real() -> None:
    synthetic = WorkflowStepResult(
        id="click",
        action="click_text",
        status=ActionStatus.SUCCESS,
        metadata={
            "operation_receipt": {
                "schema_version": 1,
                "engine": "ocr",
                "live": True,
                "evidence": {"engine": "mock"},
                "observed_after_action": True,
                "actionability": {"checked": True, "count": 1, "visible": True, "enabled": True},
            }
        },
    )

    assert validate_operation_receipt(synthetic).reason == "synthetic_evidence"


def test_synthetic_post_action_observation_does_not_count_as_real() -> None:
    synthetic = WorkflowStepResult(
        id="click",
        action="click_text",
        status=ActionStatus.SUCCESS,
        metadata={
            "operation_receipt": {
                "schema_version": 1,
                "engine": "ocr",
                "live": True,
                "evidence": {"engine": "screen-ocr"},
                "after": {"engine": "mock"},
                "observed_after_action": True,
                "actionability": {"checked": True, "count": 1, "visible": True, "enabled": True},
            }
        },
    )

    assert validate_operation_receipt(synthetic).reason == "synthetic_post_action_observation"


def test_real_non_browser_receipt_counts_as_real() -> None:
    real = WorkflowStepResult(
        id="click",
        action="click_text",
        status=ActionStatus.SUCCESS,
        metadata={
            "operation_receipt": {
                "schema_version": 1,
                "engine": "ocr",
                "live": True,
                "evidence": {"engine": "screen-ocr"},
                "after": {"engine": "screen-ocr"},
                "observed_after_action": True,
                "actionability": {
                    "checked": True,
                    "mode": "screen_point",
                    "count": 1,
                    "visible": True,
                    "enabled": True,
                    "point": {"x": 10, "y": 20},
                },
            }
        },
    )

    grade = grade_run(
        [
            step("observe", "observe_ocr"),
            step("assert", "assert_text"),
            real,
            step("verify", "assert_text"),
        ]
    )

    assert validate_operation_receipt(real).valid is True
    assert grade.level == 3
    assert grade.valid_operation_receipts == 1


def test_interaction_with_outcome_assertion_is_l3_product_acceptance() -> None:
    grade = grade_run(
        [
            step("observe", "observe_browser"),
            step("assert", "assert_text"),
            step("click", "click", receipt=True),
            step("verify", "wait_for"),
        ]
    )

    assert grade.level == 3
    assert grade.name == "data_round_trip"
    assert grade.level >= PRODUCT_ACCEPTANCE_MIN_LEVEL
    assert grade.is_product_acceptance is False
    assert "missing_post_interaction_contract_assertion" in grade.product_acceptance_blockers
    assert "visual" in grade.missing


def test_strict_contract_interaction_is_product_acceptance() -> None:
    grade = grade_run(
        [
            step("observe", "observe_browser"),
            contract_step("assert_before", forbidden_any=["Error"]),
            step("click", "click", receipt=True),
            contract_step("verify_after"),
        ]
    )

    assert grade.level == 3
    assert grade.product_acceptance_blockers == ()
    assert grade.is_product_acceptance is True


def test_assertion_before_interaction_does_not_close_the_loop() -> None:
    grade = grade_run(
        [
            step("observe", "observe_browser"),
            step("assert", "assert_text"),
            step("click", "click", receipt=True),
        ]
    )

    # the only assertion ran before the click, so the outcome was never verified
    assert grade.level == 2


def test_visual_pass_promotes_to_l4() -> None:
    steps = [
        step("observe", "observe_browser"),
        step("assert", "assert_text"),
        step("click", "click", receipt=True),
        step("verify", "assert_text"),
    ]

    assert grade_run(steps, visual_passed=True).level == 4
    assert grade_run(steps, visual_passed=False).level == 3
    assert grade_run(steps, visual_passed=None).level == 3


def test_explicit_visual_step_counts_for_l4() -> None:
    grade = grade_run(
        [
            step("observe", "observe_browser"),
            step("assert", "assert_text"),
            step("click", "click", receipt=True),
            step("verify", "assert_text"),
            step("visual", "assert_visual_quality"),
        ]
    )

    assert grade.level == 4
    assert grade.name == "visual_quality"
    assert "platform" in grade.missing


def test_failed_steps_do_not_earn_levels() -> None:
    grade = grade_run(
        [
            step("observe", "observe_browser"),
            step("assert", "assert_text", ActionStatus.FAILED),
        ]
    )

    assert grade.level == 0


def test_cross_platform_l5_requires_two_platforms_at_l4() -> None:
    entries = [
        ("checkout_mobile", ("verification", "family:checkout", "platform:mobile"), 4, True),
        ("checkout_desktop", ("verification", "family:checkout", "platform:desktop"), 4, True),
        ("profile_mobile", ("verification", "family:profile", "platform:mobile"), 4, True),
    ]

    results = {item["family"]: item for item in aggregate_cross_platform(entries)}

    assert results["checkout"]["achieved"] is True
    assert results["checkout"]["label"] == "L5"
    assert results["checkout"]["qualified_platforms"] == ["desktop", "mobile"]
    # only one platform: no L5
    assert results["profile"]["achieved"] is False
    assert results["profile"]["label"] == "L4"


def test_cross_platform_ignores_low_levels_failures_and_untagged() -> None:
    entries = [
        ("checkout_mobile", ("family:checkout", "platform:mobile"), 3, True),    # below L4
        ("checkout_desktop", ("family:checkout", "platform:desktop"), 4, False),  # failed run
        ("checkout_web", ("family:checkout",), 4, True),                          # no platform tag
        ("orphan", ("platform:desktop",), 4, True),                               # no family tag
    ]

    results = {item["family"]: item for item in aggregate_cross_platform(entries)}

    assert results["checkout"]["achieved"] is False
    assert "orphan" not in results


def test_cross_platform_takes_best_level_per_platform() -> None:
    entries = [
        ("checkout_mobile_a", ("family:checkout", "platform:mobile"), 2, True),
        ("checkout_mobile_b", ("family:checkout", "platform:mobile"), 4, True),
        ("checkout_desktop", ("family:checkout", "platform:desktop"), 4, True),
    ]

    results = aggregate_cross_platform(entries)

    assert results[0]["achieved"] is True
    assert results[0]["platforms"]["mobile"] == 4


def test_to_dict_is_machine_readable() -> None:
    payload = grade_run(
        [
            step("observe", "observe_browser"),
            contract_step("assert_before", forbidden_any=["Error"]),
            step("click", "click", receipt=True),
            contract_step("verify_after"),
        ]
    ).to_dict()

    assert payload["label"] == "L3"
    assert payload["is_product_acceptance"] is True
    assert payload["product_acceptance_blockers"] == []
    assert payload["valid_operation_receipts"] == 1
    assert payload["invalid_operation_receipts"] == 0
    assert payload["operation_receipt_failures"] == []
    assert payload["scale"]["L5"] == "cross_platform"
    assert payload["missing_for_next_level"]
