from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


RunProfileName = Literal["dry-run", "supervised", "semi-auto", "approved"]

HIGH_RISK_ACTIONS = {"save_storage_state"}
MUTATING_ACTIONS = {
    "click",
    "type",
    "paste",
    "press_key",
    "refresh_browser",
    "click_text",
    "request_api",
    "expect_download",
    "save_storage_state",
}


@dataclass(frozen=True)
class RunProfilePolicy:
    name: RunProfileName
    force_dry_run: bool
    allow_low_and_medium_risk: bool
    allow_high_risk: bool


RUN_PROFILES = {
    "dry-run": RunProfilePolicy(
        name="dry-run",
        force_dry_run=True,
        allow_low_and_medium_risk=False,
        allow_high_risk=False,
    ),
    "supervised": RunProfilePolicy(
        name="supervised",
        force_dry_run=False,
        allow_low_and_medium_risk=True,
        allow_high_risk=False,
    ),
    "semi-auto": RunProfilePolicy(
        name="semi-auto",
        force_dry_run=False,
        allow_low_and_medium_risk=True,
        allow_high_risk=False,
    ),
    "approved": RunProfilePolicy(
        name="approved",
        force_dry_run=False,
        allow_low_and_medium_risk=True,
        allow_high_risk=True,
    ),
}


def normalize_run_profile(value: str | None, *, dry_run: bool = True) -> RunProfileName:
    if value is None:
        return "dry-run" if dry_run else "approved"
    normalized = value.strip().lower()
    if normalized not in RUN_PROFILES:
        raise ValueError(f"Unsupported run profile: {value}")
    return normalized  # type: ignore[return-value]


def policy_for_profile(profile: RunProfileName) -> RunProfilePolicy:
    return RUN_PROFILES[profile]


def step_should_dry_run(profile: RunProfileName, action: str, params: dict) -> bool:
    policy = policy_for_profile(profile)
    if "dry_run" in params:
        return bool(params["dry_run"])
    if action in MUTATING_ACTIONS:
        return policy.force_dry_run
    return False


def ensure_step_allowed(profile: RunProfileName, action: str, params: dict) -> None:
    policy = policy_for_profile(profile)
    if action not in MUTATING_ACTIONS:
        return
    if policy.force_dry_run:
        return
    if action in HIGH_RISK_ACTIONS and not policy.allow_high_risk:
        raise PermissionError(f"Run profile '{profile}' blocks high-risk action: {action}")
    if action in HIGH_RISK_ACTIONS and params.get("require_confirm") is not True:
        raise PermissionError(f"High-risk action requires require_confirm: true under run profile '{profile}'.")
