from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from .pacer_verification import classify_verification_step


ACCEPTANCE_CONTRACT_SCHEMA_VERSION = 1
ACCEPTANCE_MANIFEST_PATHS = (".pacer/acceptance.json", "pacer.acceptance.json")
_COMMAND_START = re.compile(
    r"(?i)(?:^|\s)((?:python(?:\.exe)?|pytest|ruff|mypy|npm|pnpm|yarn|cargo|go|dotnet|mvn|gradle|"
    r"robot|checkpoint|visual-agent)(?:\s+[^，；;。!?！？]+?))(?=\s*(?:验证|验收|以确认|$))"
)


def build_acceptance_contract(
    *,
    goal: str,
    task_contract: dict[str, Any],
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    manifest, manifest_path = _load_repository_manifest(repo_root)
    if manifest is not None:
        return _normalize_repository_contract(manifest, manifest_path=manifest_path)

    requirements = (
        task_contract.get("requirements")
        if isinstance(task_contract.get("requirements"), list)
        else []
    )
    outcomes = [
        str(item.get("text") or "").strip()
        for item in requirements
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    roles = _unique_strings(
        str(item.get("required_artifact_role") or "")
        for item in requirements
        if isinstance(item, dict) and bool(item.get("requires_source_change"))
    )
    commands = _explicit_verification_commands(goal)
    step_classes = _command_classes(commands)
    if not step_classes:
        step_classes = _default_step_classes(task_contract)
    source = "user_goal" if commands else "template"
    adequacy = "sufficient" if commands and outcomes and (roles or not task_contract.get("requires_source_change")) else "insufficient"
    payload = {
        "schema_version": ACCEPTANCE_CONTRACT_SCHEMA_VERSION,
        "standard_source": source,
        "source_path": "",
        "observable_outcomes": outcomes[:20],
        "required_artifact_roles": roles[:10],
        "protected_paths": [str(item) for item in task_contract.get("protected_paths") or []][:20],
        "verification": {
            "required_step_classes": step_classes,
            "required_commands": commands,
        },
        "boundary_cases": _boundary_cases(outcomes),
        "manual_checks": [],
        "adequacy": adequacy,
        "reason_codes": [] if adequacy == "sufficient" else ["acceptance_standard_template_only"],
    }
    return _with_digest(payload)


def assess_acceptance_contract(
    contract: Any,
    *,
    requested_steps: Any,
    final_phase: bool,
) -> dict[str, Any]:
    standard = contract if isinstance(contract, dict) else {}
    verification = standard.get("verification") if isinstance(standard.get("verification"), dict) else {}
    steps = requested_steps if isinstance(requested_steps, list) else []
    step_classes = {
        classify_verification_step([str(value) for value in item.get("argv") or []])
        for item in steps
        if isinstance(item, dict) and isinstance(item.get("argv"), list)
    }
    required_classes = {
        str(value)
        for value in verification.get("required_step_classes") or []
        if str(value)
    }
    missing_classes = sorted(required_classes - step_classes)
    required_commands = [
        str(value) for value in verification.get("required_commands") or [] if str(value).strip()
    ]
    missing_commands = [
        command
        for command in required_commands
        if not any(_argv_matches_command(item.get("argv"), command) for item in steps if isinstance(item, dict))
    ]
    reasons = [str(value) for value in standard.get("reason_codes") or [] if str(value)]
    trusted_digest = str(standard.get("digest") or "")
    digest_verified = bool(
        trusted_digest
        and hmac.compare_digest(trusted_digest, acceptance_contract_digest(standard))
    )
    if not digest_verified:
        reasons.append(
            "acceptance_standard_digest_mismatch"
            if trusted_digest
            else "acceptance_standard_digest_missing"
        )
    if missing_classes:
        reasons.append("acceptance_required_step_class_missing")
    if missing_commands:
        reasons.append("acceptance_required_command_missing")
    if standard.get("manual_checks"):
        reasons.append("acceptance_manual_checks_pending")
    sufficient = str(standard.get("adequacy") or "") == "sufficient" and not reasons
    return {
        "schema_version": 1,
        "standard_source": str(standard.get("standard_source") or "unknown"),
        "standard_digest": trusted_digest,
        "digest_verified": digest_verified,
        "adequacy": "sufficient" if sufficient else "insufficient",
        "final_phase": bool(final_phase),
        "required_step_classes": sorted(required_classes),
        "observed_step_classes": sorted(step_classes),
        "missing_step_classes": missing_classes,
        "missing_commands": missing_commands,
        "reason_codes": list(dict.fromkeys(reasons)),
    }


def _load_repository_manifest(repo_root: str | Path | None) -> tuple[dict[str, Any] | None, str]:
    if repo_root is None:
        return None, ""
    root = Path(repo_root).expanduser().resolve()
    for relative in ACCEPTANCE_MANIFEST_PATHS:
        path = _protected_manifest_path(root, relative)
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid acceptance manifest {relative}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"acceptance manifest {relative} must contain a JSON object")
        return payload, relative
    return None, ""


def _protected_manifest_path(root: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("acceptance manifest path must stay inside the repository")
    path = root / relative_path
    current = root
    for part in relative_path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("acceptance manifest path cannot use symbolic links")
    try:
        path.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("acceptance manifest path must stay inside the repository") from exc
    return path


def _normalize_repository_contract(payload: dict[str, Any], *, manifest_path: str) -> dict[str, Any]:
    if int(payload.get("schema_version") or 0) != ACCEPTANCE_CONTRACT_SCHEMA_VERSION:
        raise ValueError("acceptance manifest schema_version must be 1")
    outcomes = _bounded_strings(payload.get("observable_outcomes"), limit=20, chars=500)
    roles = _bounded_strings(payload.get("required_artifact_roles"), limit=10, chars=80)
    protected = _bounded_strings(payload.get("protected_paths"), limit=20, chars=300)
    boundary = _bounded_strings(payload.get("boundary_cases"), limit=20, chars=500)
    manual = _bounded_strings(payload.get("manual_checks"), limit=20, chars=500)
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    classes = _bounded_strings(verification.get("required_step_classes"), limit=10, chars=40)
    commands = _bounded_strings(verification.get("required_commands"), limit=20, chars=500)
    if not outcomes:
        raise ValueError("acceptance manifest requires observable_outcomes")
    if not classes and not commands:
        raise ValueError("acceptance manifest requires verification step classes or commands")
    normalized = {
        "schema_version": ACCEPTANCE_CONTRACT_SCHEMA_VERSION,
        "standard_source": "repository",
        "source_path": manifest_path,
        "observable_outcomes": outcomes,
        "required_artifact_roles": roles,
        "protected_paths": protected,
        "verification": {
            "required_step_classes": classes or _command_classes(commands),
            "required_commands": commands,
        },
        "boundary_cases": boundary,
        "manual_checks": manual,
        "adequacy": "sufficient" if not manual else "insufficient",
        "reason_codes": [] if not manual else ["acceptance_manual_checks_pending"],
    }
    return _with_digest(normalized)


def _explicit_verification_commands(goal: str) -> list[str]:
    normalized = str(goal or "").replace("`", " ")
    commands = [" ".join(match.group(1).strip().split()) for match in _COMMAND_START.finditer(normalized)]
    return list(dict.fromkeys(command for command in commands if command))[:20]


def _command_classes(commands: list[str]) -> list[str]:
    classes: list[str] = []
    for command in commands:
        try:
            argv = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            argv = command.split()
        step_class = classify_verification_step(argv)
        if step_class and step_class not in classes:
            classes.append(step_class)
    return classes


def _default_step_classes(task_contract: dict[str, Any]) -> list[str]:
    intent = str(task_contract.get("intent") or "")
    if intent == "documentation_change":
        return ["build"]
    if intent == "read_only":
        return ["analyze"]
    return ["test"]


def _argv_matches_command(value: Any, command: str) -> bool:
    if not isinstance(value, list):
        return False
    actual = [_normalize_command_token(str(item)) for item in value if str(item)]
    try:
        expected_raw = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        expected_raw = command.split()
    expected = [_normalize_command_token(item) for item in expected_raw if item]
    if not actual or not expected:
        return False
    if actual[0] in {"python", "python.exe"} and expected[0] in {"python", "python.exe"}:
        actual[0] = expected[0] = "python"
    return actual == expected


def _normalize_command_token(value: str) -> str:
    normalized = str(value).strip().replace("\\", "/")
    if "/" in normalized and Path(normalized).name.lower().startswith("python"):
        return "python"
    return normalized.casefold()


def _boundary_cases(outcomes: list[str]) -> list[str]:
    markers = ("边界", "异常", "错误", "零", "空", "负数", "覆盖", "edge", "error", "invalid", "zero")
    return [item for item in outcomes if any(marker in item.casefold() for marker in markers)][:20]


def _with_digest(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "digest": acceptance_contract_digest(payload)}


def acceptance_contract_digest(payload: Any) -> str:
    contract = payload if isinstance(payload, dict) else {}
    canonical_payload = {key: value for key, value in contract.items() if key != "digest"}
    canonical = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _bounded_strings(value: Any, *, limit: int, chars: int) -> list[str]:
    rows = value if isinstance(value, list) else []
    return list(dict.fromkeys(str(item).strip()[:chars] for item in rows if str(item).strip()))[:limit]


def _unique_strings(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
