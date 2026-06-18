from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .models import ActionStatus
from .run_profile import MUTATING_ACTIONS

# Actions that exercise the product the way a user would. A run that executed
# none of these for real (dry-run skips do not count) can only claim page
# inspection, never product acceptance.
INTERACTION_ACTIONS = frozenset(MUTATING_ACTIONS)

INSPECTION_ONLY_WARNING = (
    "Page inspection only: no real user interaction was executed. "
    "This confirms the page renders, NOT that the product works. "
    "Run with a non-dry-run profile and include real interaction steps "
    "(click/type/submit) before claiming product acceptance."
)


@dataclass(frozen=True)
class InteractionProfile:
    real_interaction_count: int = 0
    skipped_interaction_count: int = 0
    invalid_interaction_count: int = 0

    @property
    def inspection_only(self) -> bool:
        return self.real_interaction_count == 0


@dataclass(frozen=True)
class OperationReceiptValidation:
    valid: bool
    reason: str = ""
    step_id: str = ""
    action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "step_id": self.step_id,
            "action": self.action,
        }


def profile_interactions(steps: Iterable[Any]) -> InteractionProfile:
    real = 0
    skipped = 0
    invalid = 0
    for step in steps:
        action = str(getattr(step, "action", "") or "")
        if action not in INTERACTION_ACTIONS:
            continue
        status = getattr(step, "status", None)
        if status == ActionStatus.SUCCESS:
            if has_valid_operation_receipt(step):
                real += 1
            else:
                invalid += 1
        elif status == ActionStatus.DRY_RUN:
            skipped += 1
    return InteractionProfile(real_interaction_count=real, skipped_interaction_count=skipped, invalid_interaction_count=invalid)


def has_valid_operation_receipt(step: Any) -> bool:
    return validate_operation_receipt(step).valid


def validate_operation_receipt(step: Any) -> OperationReceiptValidation:
    step_id = str(getattr(step, "id", "") or "")
    action = str(getattr(step, "action", "") or "")
    metadata = getattr(step, "metadata", None)
    if not isinstance(metadata, dict):
        return OperationReceiptValidation(False, "missing_metadata", step_id, action)
    receipt = metadata.get("operation_receipt")
    if not isinstance(receipt, dict):
        return OperationReceiptValidation(False, "missing_operation_receipt", step_id, action)
    if not bool(receipt.get("live")):
        return OperationReceiptValidation(False, "not_live", step_id, action)
    engine = str(receipt.get("engine") or "").strip().lower()
    if not bool(engine):
        return OperationReceiptValidation(False, "missing_engine", step_id, action)
    if engine == "mock":
        return OperationReceiptValidation(False, "mock_engine", step_id, action)
    evidence = receipt.get("evidence")
    if isinstance(evidence, dict):
        evidence_engine = str(evidence.get("engine") or "").strip().lower()
        if evidence_engine == "mock" or bool(evidence.get("synthetic")):
            return OperationReceiptValidation(False, "synthetic_evidence", step_id, action)
    after = receipt.get("after")
    if isinstance(after, dict):
        after_engine = str(after.get("engine") or "").strip().lower()
        if after_engine == "mock" or bool(after.get("synthetic")):
            return OperationReceiptValidation(False, "synthetic_post_action_observation", step_id, action)
    actionability = receipt.get("actionability")
    if not isinstance(actionability, dict) or not bool(actionability.get("checked")):
        return OperationReceiptValidation(False, "missing_actionability", step_id, action)
    try:
        count = int(actionability.get("count", 0))
    except (TypeError, ValueError):
        return OperationReceiptValidation(False, "invalid_target_count", step_id, action)
    if count != 1:
        return OperationReceiptValidation(False, "target_not_unique", step_id, action)
    if actionability.get("visible") is not True:
        return OperationReceiptValidation(False, "target_not_visible", step_id, action)
    if actionability.get("enabled") is not True:
        return OperationReceiptValidation(False, "target_not_enabled", step_id, action)
    if "editable" in actionability and actionability.get("editable") is not True:
        return OperationReceiptValidation(False, "target_not_editable", step_id, action)
    if not bool(receipt.get("observed_after_action")):
        return OperationReceiptValidation(False, "missing_post_action_observation", step_id, action)
    return OperationReceiptValidation(True, "", step_id, action)


# --- acceptance levels -------------------------------------------------------
#
# A run earns a level only when it also earned every level below it. Levels
# measure how much real product behavior the run actually proved; the run
# verdict (passed/failed) is a separate question. Below L3 a green run is page
# inspection, not product acceptance.

ACCEPTANCE_LEVEL_NAMES: dict[int, str] = {
    0: "opens",
    1: "content_verified",
    2: "real_interaction",
    3: "data_round_trip",
    4: "visual_quality",
    5: "cross_platform",
}
PRODUCT_ACCEPTANCE_MIN_LEVEL = 3

# Observations of the real product vs. canned simulations. A run whose only
# evidence is a fixture can never claim more than content checks against that
# fixture, so it is capped at L1 and flagged as simulated.
LIVE_OBSERVE_ACTIONS = frozenset(
    {"observe_browser", "observe_dom", "observe_screen", "observe_ocr", "observe_uia", "observe_vision"}
)
SIMULATED_OBSERVE_ACTIONS = frozenset({"observe_fixture", "observe_html", "observe_state"})

CONTENT_ASSERTION_ACTIONS = frozenset(
    {
        "assert_text",
        "assert_text_contract",
        "wait_for_text",
        "assert_visual_text",
        "assert_element_exists",
        "assert_count",
        "assert_attribute",
        "assert_browser_ready",
        "assert_product_contract",
    }
)
# Assertions that can verify the outcome of an interaction (page content,
# network response, or awaited state change).
OUTCOME_ASSERTION_ACTIONS = CONTENT_ASSERTION_ACTIONS | frozenset({"assert_response", "wait_for", "assert_no_error"})
STRICT_OUTCOME_ASSERTION_ACTIONS = frozenset({"assert_text_contract", "assert_product_contract"})

_LEVEL_REQUIREMENTS: dict[int, str] = {
    0: "open the product with a successful observe step",
    1: "assert that expected content is present (assert_text or similar)",
    2: "execute at least one real interaction with a valid operation receipt",
    3: "assert the interaction outcome after acting (content, state, or network response)",
    4: "pass the visual quality audit with no blocking findings",
    5: "aggregate passing runs from at least two platforms or viewports",
}


@dataclass(frozen=True)
class AcceptanceGrade:
    level: int
    simulated: bool = False
    missing: str = ""
    valid_operation_receipts: int = 0
    invalid_operation_receipts: int = 0
    operation_receipt_failures: tuple[dict[str, Any], ...] = ()
    product_acceptance_blockers: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"L{self.level}" if self.level >= 0 else "none"

    @property
    def name(self) -> str:
        if self.level < 0:
            return "not_opened"
        return ACCEPTANCE_LEVEL_NAMES.get(self.level, "unknown")

    @property
    def is_product_acceptance(self) -> bool:
        return self.level >= PRODUCT_ACCEPTANCE_MIN_LEVEL and not self.simulated and not self.product_acceptance_blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "label": self.label,
            "name": self.name,
            "simulated": self.simulated,
            "is_product_acceptance": self.is_product_acceptance,
            "valid_operation_receipts": self.valid_operation_receipts,
            "invalid_operation_receipts": self.invalid_operation_receipts,
            "operation_receipt_failures": list(self.operation_receipt_failures),
            "product_acceptance_blockers": list(self.product_acceptance_blockers),
            "missing_for_next_level": self.missing,
            "scale": {f"L{key}": value for key, value in ACCEPTANCE_LEVEL_NAMES.items()},
        }


CROSS_PLATFORM_MIN_LEVEL = 4
PLATFORM_TAG_PREFIX = "platform:"
FAMILY_TAG_PREFIX = "family:"


def aggregate_cross_platform(entries: Iterable[tuple[str, Iterable[str], int, bool]]) -> list[dict[str, Any]]:
    """Aggregate per-run grades into L5 (cross-platform) evidence.

    ``entries`` are ``(workflow_name, tags, level, passed)``. A workflow joins a
    family via an explicit ``family:<name>`` tag and declares its platform via
    ``platform:<name>``. A family earns L5 only when runs on at least two
    distinct platforms each reached L4. Explicit tags only — no name guessing.
    """
    families: dict[str, dict[str, int]] = {}
    for name, tags, level, passed in entries:
        if not passed:
            continue
        tag_list = [str(tag) for tag in tags]
        platform = next((tag[len(PLATFORM_TAG_PREFIX):] for tag in tag_list if tag.startswith(PLATFORM_TAG_PREFIX)), "")
        family = next((tag[len(FAMILY_TAG_PREFIX):] for tag in tag_list if tag.startswith(FAMILY_TAG_PREFIX)), "")
        if not platform or not family:
            continue
        platforms = families.setdefault(family, {})
        platforms[platform] = max(platforms.get(platform, -1), int(level))
    results = []
    for family, platforms in sorted(families.items()):
        qualified = sorted(platform for platform, level in platforms.items() if level >= CROSS_PLATFORM_MIN_LEVEL)
        achieved = len(qualified) >= 2
        results.append(
            {
                "family": family,
                "platforms": dict(sorted(platforms.items())),
                "qualified_platforms": qualified,
                "achieved": achieved,
                "label": "L5" if achieved else f"L{max(platforms.values(), default=0)}",
            }
        )
    return results


def grade_run(steps: Iterable[Any], *, visual_passed: bool | None = None) -> AcceptanceGrade:
    """Grade how much real product behavior a run proved (L0-L5)."""
    step_list = list(steps)
    receipt_validations = tuple(
        validate_operation_receipt(step)
        for step in step_list
        if str(getattr(step, "action", "") or "") in INTERACTION_ACTIONS
        and getattr(step, "status", None) == ActionStatus.SUCCESS
    )
    operation_receipts = sum(1 for item in receipt_validations if item.valid)
    invalid_operation_receipts = sum(1 for item in receipt_validations if not item.valid)
    operation_receipt_failures = tuple(item.to_dict() for item in receipt_validations if not item.valid)

    def grade(
        level: int,
        *,
        simulated: bool = False,
        missing: str = "",
        product_acceptance_blockers: tuple[str, ...] = (),
    ) -> AcceptanceGrade:
        return AcceptanceGrade(
            level=level,
            simulated=simulated,
            missing=missing,
            valid_operation_receipts=operation_receipts,
            invalid_operation_receipts=invalid_operation_receipts,
            operation_receipt_failures=operation_receipt_failures,
            product_acceptance_blockers=product_acceptance_blockers,
        )

    succeeded = [
        (index, str(getattr(step, "action", "") or ""))
        for index, step in enumerate(step_list)
        if getattr(step, "status", None) == ActionStatus.SUCCESS
    ]
    opened_live = any(action in LIVE_OBSERVE_ACTIONS for _, action in succeeded)
    opened_simulated = any(action in SIMULATED_OBSERVE_ACTIONS for _, action in succeeded)
    simulated = opened_simulated and not opened_live

    if not opened_live and not opened_simulated:
        return grade(-1, missing=_LEVEL_REQUIREMENTS[0])

    content_verified = any(action in CONTENT_ASSERTION_ACTIONS for _, action in succeeded)
    if not content_verified:
        return grade(0, simulated=simulated, missing=_LEVEL_REQUIREMENTS[1])
    if simulated:
        # fixture evidence cannot prove the real product works
        return grade(1, simulated=True, missing="replace fixture observations with a live observe step")

    first_interaction_index = next(
        (
            index
            for index, step in enumerate(step_list)
            if str(getattr(step, "action", "") or "") in INTERACTION_ACTIONS
            and getattr(step, "status", None) == ActionStatus.SUCCESS
            and has_valid_operation_receipt(step)
        ),
        None,
    )
    if first_interaction_index is None:
        return grade(1, missing=_LEVEL_REQUIREMENTS[2])

    outcome_verified = any(
        index > first_interaction_index and action in OUTCOME_ASSERTION_ACTIONS for index, action in succeeded
    )
    if not outcome_verified:
        return grade(2, missing=_LEVEL_REQUIREMENTS[3])

    product_acceptance_blockers = strict_product_acceptance_blockers(
        step_list,
        first_interaction_index=first_interaction_index,
        receipt_validations=receipt_validations,
    )

    explicit_visual_passed = any(action == "assert_visual_quality" for _, action in succeeded)
    if not (visual_passed is True or explicit_visual_passed):
        return grade(3, missing=_LEVEL_REQUIREMENTS[4], product_acceptance_blockers=product_acceptance_blockers)

    # L5 needs evidence across platforms, which a single run cannot supply
    return grade(4, missing=_LEVEL_REQUIREMENTS[5], product_acceptance_blockers=product_acceptance_blockers)


def strict_product_acceptance_blockers(
    steps: Iterable[Any],
    *,
    first_interaction_index: int,
    receipt_validations: Iterable[OperationReceiptValidation],
) -> tuple[str, ...]:
    step_list = list(steps)
    blockers: list[str] = []
    if any(not item.valid for item in receipt_validations):
        blockers.append("invalid_operation_receipts_present")
    if not any_strict_outcome_assertion_after(step_list, first_interaction_index):
        blockers.append("missing_post_interaction_contract_assertion")
    if not any_negative_text_contract(step_list):
        blockers.append("missing_negative_contract_assertion")
    if not all_valid_receipts_have_after_evidence(step_list):
        blockers.append("missing_after_action_evidence_artifact")
    if not all_valid_receipts_have_post_action_assertion(step_list):
        blockers.append("missing_post_action_assertion")
    return tuple(blockers)


def any_strict_outcome_assertion_after(steps: list[Any], first_interaction_index: int) -> bool:
    for index, step in enumerate(steps):
        if index <= first_interaction_index or getattr(step, "status", None) != ActionStatus.SUCCESS:
            continue
        if str(getattr(step, "action", "") or "") in STRICT_OUTCOME_ASSERTION_ACTIONS:
            return True
    return False


def any_negative_text_contract(steps: list[Any]) -> bool:
    for step in steps:
        if getattr(step, "status", None) != ActionStatus.SUCCESS:
            continue
        if str(getattr(step, "action", "") or "") != "assert_text_contract":
            continue
        metadata = getattr(step, "metadata", None)
        contract = metadata.get("text_contract") if isinstance(metadata, dict) else None
        if isinstance(contract, dict) and contract.get("passed") is not False and contract.get("forbidden_any"):
            return True
    return False


def all_valid_receipts_have_after_evidence(steps: list[Any]) -> bool:
    valid_receipts = operation_receipts_from_valid_steps(steps)
    if not valid_receipts:
        return False
    for receipt in valid_receipts:
        after = receipt.get("after")
        if not isinstance(after, dict):
            return False
        if not (after.get("screenshot_path") or after.get("source") or after.get("url")):
            return False
    return True


def all_valid_receipts_have_post_action_assertion(steps: list[Any]) -> bool:
    valid_receipts = operation_receipts_from_valid_steps(steps)
    if not valid_receipts:
        return False
    return all(bool(receipt.get("post_action_assertion")) for receipt in valid_receipts)


def operation_receipts_from_valid_steps(steps: list[Any]) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for step in steps:
        if str(getattr(step, "action", "") or "") not in INTERACTION_ACTIONS:
            continue
        if getattr(step, "status", None) != ActionStatus.SUCCESS:
            continue
        if not validate_operation_receipt(step).valid:
            continue
        metadata = getattr(step, "metadata", None)
        receipt = metadata.get("operation_receipt") if isinstance(metadata, dict) else None
        if isinstance(receipt, dict):
            receipts.append(receipt)
    return receipts
