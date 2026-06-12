from __future__ import annotations

import json
from pathlib import Path
from typing import Any


WORKSPACE_DIRS = ("workflows", "inputs", "fixtures", "runs", "reports", "regression_tests", "queue")
WORKSPACE_RISK_POLICY_PROFILES = ("planner", "local", "ci")
WORKSPACE_RISK_ATTENTION_TREND_DIRECTIONS = (
    "worsening",
    "mixed",
    "improving",
    "stable",
    "insufficient_history",
    "unknown",
)
WORKSPACE_REPAIR_RISK_LEVELS = ("unknown", "low", "medium", "high")
DEFAULT_AUTO_REPAIR_POLICY = {
    "min_confidence": 0.75,
    "max_risk_level": "medium",
    "allow_force": True,
}


def workspace_root(workspace: Any) -> Path:
    if isinstance(workspace, (str, Path)):
        return Path(workspace).resolve()
    return Path(workspace.root).resolve() if hasattr(workspace, "root") else Path(workspace).resolve()


def load_workspace_manifest(workspace: Any) -> dict[str, Any]:
    manifest_path = workspace_root(workspace) / "workspace.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def load_workspace_gui_action_history_risk_config(workspace: Any) -> dict[str, Any]:
    manifest = load_workspace_manifest(workspace)
    quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
    config = quality.get("gui_action_history") if isinstance(quality.get("gui_action_history"), dict) else {}
    return config


def load_workspace_auto_repair_policy(workspace: Any | str | Path) -> dict[str, Any]:
    root = workspace_root(workspace)
    manifest = load_workspace_manifest(root) if root.exists() else {}
    raw = manifest.get("auto_repair") if isinstance(manifest.get("auto_repair"), dict) else {}
    policy = dict(DEFAULT_AUTO_REPAIR_POLICY)
    if "min_confidence" in raw:
        try:
            policy["min_confidence"] = min(1.0, max(0.0, float(raw["min_confidence"])))
        except (TypeError, ValueError):
            pass
    if str(raw.get("max_risk_level") or "").lower() in WORKSPACE_REPAIR_RISK_LEVELS:
        policy["max_risk_level"] = str(raw.get("max_risk_level")).lower()
    if "allow_force" in raw:
        policy["allow_force"] = bool(raw.get("allow_force"))
    return {
        **policy,
        "source": "workspace.json" if isinstance(raw, dict) and raw else "defaults",
    }


def build_workspace_risk_policy_template() -> dict[str, Any]:
    return {
        "auto_repair": dict(DEFAULT_AUTO_REPAIR_POLICY),
        "quality": {
            "gui_action_history": {
                "error_rate_threshold": 0.25,
                "history_limit": 50,
                "failed_action_limit": 2,
                "profiles": {
                    "planner": {
                        "error_rate_threshold": 0.25,
                        "history_limit": 50,
                        "failed_action_limit": 2,
                    },
                    "local": {
                        "error_rate_threshold": 0.3,
                        "history_limit": 50,
                        "failed_action_limit": 3,
                    },
                    "ci": {
                        "error_rate_threshold": 0.15,
                        "history_limit": 100,
                        "failed_action_limit": 1,
                    },
                },
                "health": {
                    "attention_trend_directions": ["worsening"],
                },
            },
        },
    }


def build_workspace_risk_policy_apply_plan(
    workspace: Any,
    *,
    overwrite: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    root = workspace_root(workspace)
    manifest_path = root / "workspace.json"
    manifest = load_workspace_manifest(root)
    if not manifest:
        manifest = {
            "name": root.name,
            "version": 1,
            "dirs": list(WORKSPACE_DIRS),
        }
    before_quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
    template = build_workspace_risk_policy_template()
    template_quality = template["quality"]
    proposed_quality = (
        merge_json_object(before_quality, template_quality)
        if overwrite
        else merge_json_object(template_quality, before_quality)
    )
    proposed_manifest = dict(manifest)
    proposed_manifest["quality"] = proposed_quality
    before_auto_repair = manifest.get("auto_repair") if isinstance(manifest.get("auto_repair"), dict) else {}
    template_auto_repair = template["auto_repair"]
    proposed_auto_repair = (
        merge_json_object(before_auto_repair, template_auto_repair)
        if overwrite
        else merge_json_object(template_auto_repair, before_auto_repair)
    )
    proposed_manifest["auto_repair"] = proposed_auto_repair
    changed_paths = diff_json_paths(before_quality, proposed_quality, path="quality")
    changed_paths.extend(diff_json_paths(before_auto_repair, proposed_auto_repair, path="auto_repair"))
    if apply and changed_paths:
        manifest_path.write_text(json.dumps(proposed_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    validation_before = validate_workspace_risk_policy(root)
    validation_after = validate_risk_policy_manifest(proposed_manifest, workspace=root, manifest_path=manifest_path)
    return {
        "schema_version": 1,
        "workspace_root": str(root),
        "manifest_path": str(manifest_path),
        "mode": "overwrite" if overwrite else "fill_missing",
        "applied": bool(apply and changed_paths),
        "changed": bool(changed_paths),
        "changed_paths": changed_paths,
        "patch": {"auto_repair": proposed_auto_repair, "quality": proposed_quality},
        "validation_before": {
            "status": validation_before["status"],
            "error_count": validation_before["error_count"],
            "warning_count": validation_before["warning_count"],
        },
        "validation_after": {
            "status": validation_after["status"],
            "error_count": validation_after["error_count"],
            "warning_count": validation_after["warning_count"],
        },
    }


def merge_json_object(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = merge_json_object(existing, value)
        else:
            merged[key] = value
    return merged


def diff_json_paths(before: Any, after: Any, *, path: str) -> list[str]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        paths: list[str] = []
        for key in sorted(set(before) | set(after)):
            paths.extend(diff_json_paths(before.get(key), after.get(key), path=f"{path}.{key}"))
        return paths
    return [path]


def validate_workspace_risk_policy(workspace: Any) -> dict[str, Any]:
    root = workspace_root(workspace)
    manifest_path = root / "workspace.json"
    manifest: Any = {}
    if manifest_path.exists():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return invalid_workspace_manifest_risk_policy_result(root, manifest_path, exc.msg)
        manifest = payload
    return validate_risk_policy_manifest(manifest, workspace=root, manifest_path=manifest_path)


def invalid_workspace_manifest_risk_policy_result(workspace: Any, manifest_path: Path, message: str) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    add_risk_policy_issue(
        issues,
        "error",
        "workspace_manifest_invalid_json",
        "workspace.json",
        f"Workspace manifest is not valid JSON: {message}.",
        "Fix workspace.json syntax, then rerun workspace-risk-policy-check.",
    )
    return workspace_risk_policy_result(workspace, manifest_path, issues)


def validate_risk_policy_manifest(
    manifest: Any,
    *,
    workspace: Any,
    manifest_path: Path,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if not manifest_path.exists():
        add_risk_policy_issue(
            issues,
            "error",
            "workspace_manifest_missing",
            "workspace.json",
            "Workspace manifest is missing.",
            "Run init or restore workspace.json before applying risk policy.",
        )
    if manifest and not isinstance(manifest, dict):
        add_risk_policy_issue(
            issues,
            "error",
            "workspace_manifest_not_object",
            "workspace.json",
            "Workspace manifest must be a JSON object.",
            "Replace workspace.json with an object containing workspace metadata.",
        )
        manifest = {}

    quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else None
    if quality is None:
        if "quality" in manifest:
            add_risk_policy_issue(
                issues,
                "error",
                "quality_policy_not_object",
                "quality",
                "quality must be a JSON object.",
                "Use workspace-risk-policy-template and copy its quality object.",
            )
        elif manifest:
            add_risk_policy_issue(
                issues,
                "warning",
                "quality_policy_missing",
                "quality",
                "No workspace quality policy is configured; built-in defaults will be used.",
                "Run workspace-risk-policy-template and copy the quality object into workspace.json.",
            )
        quality = {}
    config = quality.get("gui_action_history") if isinstance(quality.get("gui_action_history"), dict) else None
    if config is None:
        if "gui_action_history" in quality:
            add_risk_policy_issue(
                issues,
                "error",
                "gui_action_history_policy_not_object",
                "quality.gui_action_history",
                "quality.gui_action_history must be a JSON object.",
                "Use workspace-risk-policy-template for the expected structure.",
            )
        elif quality:
            add_risk_policy_issue(
                issues,
                "warning",
                "gui_action_history_policy_missing",
                "quality.gui_action_history",
                "No GUI action history risk policy is configured; built-in defaults will be used.",
                "Copy quality.gui_action_history from workspace-risk-policy-template.",
            )
        config = {}
    validate_gui_action_history_policy_config(issues, config, "quality.gui_action_history")
    auto_repair = manifest.get("auto_repair") if isinstance(manifest.get("auto_repair"), dict) else None
    if auto_repair is None:
        if "auto_repair" in manifest:
            add_risk_policy_issue(
                issues,
                "error",
                "auto_repair_policy_not_object",
                "auto_repair",
                "auto_repair must be a JSON object.",
                "Use workspace-risk-policy-template for the expected auto_repair structure.",
            )
        elif manifest:
            add_risk_policy_issue(
                issues,
                "warning",
                "auto_repair_policy_missing",
                "auto_repair",
                "No auto_repair policy is configured; built-in defaults will be used.",
                "Copy auto_repair from workspace-risk-policy-template.",
            )
        auto_repair = {}
    validate_auto_repair_policy_config(issues, auto_repair, "auto_repair")
    return workspace_risk_policy_result(workspace, manifest_path, issues)


def workspace_risk_policy_result(workspace: Any, manifest_path: Path, issues: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [issue for issue in issues if issue["level"] == "error"]
    warnings = [issue for issue in issues if issue["level"] == "warning"]
    root = workspace_root(workspace)
    return {
        "schema_version": 1,
        "workspace_root": str(root),
        "manifest_path": str(manifest_path),
        "ok": not errors,
        "status": "error" if errors else "warning" if warnings else "ok",
        "error_count": len(errors),
        "warning_count": len(warnings),
        "issues": issues,
        "supported_profiles": list(WORKSPACE_RISK_POLICY_PROFILES),
        "supported_attention_trend_directions": list(WORKSPACE_RISK_ATTENTION_TREND_DIRECTIONS),
        "supported_repair_risk_levels": list(WORKSPACE_REPAIR_RISK_LEVELS),
    }


def validate_auto_repair_policy_config(issues: list[dict[str, Any]], config: dict[str, Any], path: str) -> None:
    validate_risk_float(issues, config, "min_confidence", path, minimum=0.0, maximum=1.0)
    if "allow_force" in config and not isinstance(config.get("allow_force"), bool):
        add_risk_policy_issue(
            issues,
            "error",
            "auto_repair_allow_force_invalid",
            f"{path}.allow_force",
            "allow_force must be a boolean.",
            "Set allow_force to true or false.",
        )
    if "max_risk_level" in config:
        value = config.get("max_risk_level")
        if not isinstance(value, str) or value not in WORKSPACE_REPAIR_RISK_LEVELS:
            add_risk_policy_issue(
                issues,
                "error",
                "auto_repair_max_risk_level_invalid",
                f"{path}.max_risk_level",
                "max_risk_level must be one of the supported repair risk levels.",
                "Use one of: " + ", ".join(WORKSPACE_REPAIR_RISK_LEVELS) + ".",
            )


def validate_gui_action_history_policy_config(issues: list[dict[str, Any]], config: dict[str, Any], path: str) -> None:
    validate_risk_float(issues, config, "error_rate_threshold", path, minimum=0.0, maximum=1.0)
    validate_risk_int(issues, config, "history_limit", path, minimum=1)
    validate_risk_int(issues, config, "limit", path, minimum=1)
    validate_risk_int(issues, config, "failed_action_limit", path, minimum=0)
    profiles = config.get("profiles")
    if "profiles" in config and not isinstance(profiles, dict):
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_profiles_not_object",
            f"{path}.profiles",
            "profiles must be a JSON object.",
            "Use profiles.planner, profiles.local, and profiles.ci objects.",
        )
    elif isinstance(profiles, dict):
        for profile, profile_config in profiles.items():
            profile_path = f"{path}.profiles.{profile}"
            if profile not in WORKSPACE_RISK_POLICY_PROFILES:
                add_risk_policy_issue(
                    issues,
                    "warning",
                    "risk_policy_unknown_profile",
                    profile_path,
                    f"Unknown profile '{profile}' will not be consumed by current quality gates.",
                    "Use planner, local, or ci profile names.",
                )
            if not isinstance(profile_config, dict):
                add_risk_policy_issue(
                    issues,
                    "error",
                    "risk_policy_profile_not_object",
                    profile_path,
                    "Profile override must be a JSON object.",
                    "Replace the profile value with threshold fields.",
                )
                continue
            validate_risk_float(issues, profile_config, "error_rate_threshold", profile_path, minimum=0.0, maximum=1.0)
            validate_risk_int(issues, profile_config, "history_limit", profile_path, minimum=1)
            validate_risk_int(issues, profile_config, "limit", profile_path, minimum=1)
            validate_risk_int(issues, profile_config, "failed_action_limit", profile_path, minimum=0)
    health = config.get("health")
    if "health" in config and not isinstance(health, dict):
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_health_not_object",
            f"{path}.health",
            "health must be a JSON object.",
            "Use health.attention_trend_directions as a list of trend direction strings.",
        )
    elif isinstance(health, dict):
        validate_attention_trend_directions(issues, health, f"{path}.health")


def validate_attention_trend_directions(issues: list[dict[str, Any]], config: dict[str, Any], path: str) -> None:
    directions = config.get("attention_trend_directions")
    if "attention_trend_directions" not in config:
        return
    if not isinstance(directions, list):
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_attention_trends_not_list",
            f"{path}.attention_trend_directions",
            "attention_trend_directions must be a list.",
            "Use values such as ['worsening'] or ['worsening', 'mixed'].",
        )
        return
    if not directions:
        add_risk_policy_issue(
            issues,
            "warning",
            "risk_policy_attention_trends_empty",
            f"{path}.attention_trend_directions",
            "No risk trend direction will trigger dashboard attention.",
            "Keep ['worsening'] unless the workspace deliberately disables trend health attention.",
        )
        return
    supported = set(WORKSPACE_RISK_ATTENTION_TREND_DIRECTIONS)
    seen: set[str] = set()
    for index, item in enumerate(directions):
        item_path = f"{path}.attention_trend_directions[{index}]"
        if not isinstance(item, str) or not item.strip():
            add_risk_policy_issue(
                issues,
                "error",
                "risk_policy_attention_trend_invalid",
                item_path,
                "Each attention trend direction must be a non-empty string.",
                "Use one of the supported trend direction names.",
            )
            continue
        direction = item.strip()
        if direction in seen:
            add_risk_policy_issue(
                issues,
                "warning",
                "risk_policy_attention_trend_duplicate",
                item_path,
                f"Duplicate attention trend direction '{direction}'.",
                "Keep each direction only once.",
            )
        seen.add(direction)
        if direction not in supported:
            add_risk_policy_issue(
                issues,
                "error",
                "risk_policy_attention_trend_unsupported",
                item_path,
                f"Unsupported attention trend direction '{direction}'.",
                "Use one of: " + ", ".join(WORKSPACE_RISK_ATTENTION_TREND_DIRECTIONS) + ".",
            )


def validate_risk_float(
    issues: list[dict[str, Any]],
    config: dict[str, Any],
    key: str,
    path: str,
    *,
    minimum: float,
    maximum: float,
) -> None:
    if key not in config:
        return
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_float_invalid",
            f"{path}.{key}",
            f"{key} must be a number between {minimum:g} and {maximum:g}.",
            f"Set {key} to a numeric value such as 0.2.",
        )
        return
    if float(value) < minimum or float(value) > maximum:
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_float_out_of_range",
            f"{path}.{key}",
            f"{key} must be between {minimum:g} and {maximum:g}.",
            f"Set {key} within the supported range.",
        )


def validate_risk_int(
    issues: list[dict[str, Any]],
    config: dict[str, Any],
    key: str,
    path: str,
    *,
    minimum: int,
) -> None:
    if key not in config:
        return
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, int):
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_int_invalid",
            f"{path}.{key}",
            f"{key} must be an integer greater than or equal to {minimum}.",
            f"Set {key} to an integer value.",
        )
        return
    if value < minimum:
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_int_out_of_range",
            f"{path}.{key}",
            f"{key} must be greater than or equal to {minimum}.",
            f"Increase {key} to at least {minimum}.",
        )


def add_risk_policy_issue(
    issues: list[dict[str, Any]],
    level: str,
    code: str,
    path: str,
    message: str,
    suggestion: str,
) -> None:
    issues.append(
        {
            "level": level,
            "code": code,
            "path": path,
            "message": message,
            "suggestion": suggestion,
        }
    )
