from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any, Literal


TierName = Literal["free", "pro", "team", "enterprise"]
VALID_TIERS: tuple[TierName, ...] = ("free", "pro", "team", "enterprise")

FREE_FEATURES = frozenset(
    {
        "local_run",
        "mcp_server",
        "codex_check",
        "basic_report",
        "context_snapshot",
        "generate_workflow",
        "vscode_extension",
    }
)

PRO_FEATURES = frozenset(
    {
        "ci_github_check",
        "workflow_history_30d",
        "priority_support",
    }
)

CLOUD_RUN_FREE_MONTHLY_LIMIT = 5

TEAM_FEATURES = frozenset(
    {
        "team_workspace",
        "shared_workflow_library",
        "workflow_history_unlimited",
        "audit_log_export",
    }
)


@dataclass(frozen=True)
class License:
    tier: TierName
    seats: int = 1
    expires_at: float | None = None
    source: str = "default"
    key_present: bool = False


class FeatureGatedError(Exception):
    def __init__(self, feature: str, *, required_tier: TierName | str = "paid", current_tier: TierName | str = "free"):
        self.feature = feature
        self.required_tier = str(required_tier)
        self.current_tier = str(current_tier)
        super().__init__(
            f"Feature '{feature}' requires the {self.required_tier} plan. "
            f"Current tier: {self.current_tier}. "
            "Visit https://visualagent.dev/upgrade to unlock."
        )


def get_license() -> License:
    """Return local license metadata from env or a local JSON file.

    License reads are intentionally local-only. Feature gates are enforced with
    local tier metadata and workspace usage counters until remote validation is
    activated.
    """
    env_license = _license_from_env()
    if env_license:
        return env_license
    file_license = _license_from_file(default_license_path())
    if file_license:
        return file_license
    return License(tier="free")


def check_feature(feature: str, license_: License | None = None) -> bool:
    lic = license_ or get_license()
    if _is_expired(lic):
        lic = License(tier="free", source=lic.source, key_present=lic.key_present)
    if feature == "cloud_run":
        return True
    if feature in FREE_FEATURES:
        return True
    if lic.tier in ("pro", "team", "enterprise") and feature in PRO_FEATURES:
        return True
    if lic.tier in ("team", "enterprise") and feature in TEAM_FEATURES:
        return True
    return lic.tier == "enterprise"


def require_feature(feature: str, license_: License | None = None) -> None:
    lic = license_ or get_license()
    effective_license = lic
    if _is_expired(effective_license):
        effective_license = License(tier="free", source=lic.source, key_present=lic.key_present)
    if check_feature(feature, effective_license):
        return None
    raise FeatureGatedError(
        feature,
        required_tier=_feature_min_tier(feature),
        current_tier=effective_license.tier,
    )


def _feature_min_tier(feature: str) -> TierName | str:
    if feature == "cloud_run":
        return "free"
    if feature in FREE_FEATURES:
        return "free"
    if feature in PRO_FEATURES:
        return "pro"
    if feature in TEAM_FEATURES:
        return "team"
    return "enterprise"


def monthly_feature_limit(feature: str, license_: License | None = None) -> int | None:
    lic = license_ or get_license()
    if _is_expired(lic):
        lic = License(tier="free", source=lic.source, key_present=lic.key_present)
    if feature == "cloud_run" and lic.tier == "free":
        return CLOUD_RUN_FREE_MONTHLY_LIMIT
    return None


def default_license_path() -> Path:
    override = os.environ.get("VISUAL_AGENT_LICENSE_FILE")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("VISUAL_AGENT_HOME")
    if home:
        return Path(home).expanduser() / "license.json"
    return Path.home() / ".visual-agent" / "license.json"


def _license_from_env() -> License | None:
    tier = _normalize_tier(os.environ.get("VISUAL_AGENT_LICENSE_TIER"))
    key_present = bool(os.environ.get("VISUAL_AGENT_LICENSE_KEY"))
    if tier:
        return License(
            tier=tier,
            seats=_positive_int(os.environ.get("VISUAL_AGENT_LICENSE_SEATS"), default=1),
            expires_at=_optional_float(os.environ.get("VISUAL_AGENT_LICENSE_EXPIRES_AT")),
            source="env",
            key_present=key_present,
        )
    if key_present:
        return License(tier="free", source="env", key_present=True)
    return None


def _license_from_file(path: Path) -> License | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    tier = _normalize_tier(raw.get("tier"))
    if tier is None:
        return None
    return License(
        tier=tier,
        seats=_positive_int(raw.get("seats"), default=1),
        expires_at=_optional_float(raw.get("expires_at")),
        source=str(path),
        key_present=bool(raw.get("key") or raw.get("license_key")),
    )


def _normalize_tier(value: Any) -> TierName | None:
    tier = str(value or "").strip().lower()
    if tier in VALID_TIERS:
        return tier  # type: ignore[return-value]
    return None


def _is_expired(license_: License) -> bool:
    return license_.expires_at is not None and license_.expires_at < time()


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
