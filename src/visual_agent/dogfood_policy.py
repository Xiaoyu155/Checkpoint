from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


DOGFOOD_POLICY_SCHEMA_VERSION = 1
DOGFOOD_POLICY_PATH = Path(".pacer/dogfood.json")
MAX_DOGFOOD_POLICY_BYTES = 256 * 1024


def dogfood_policy_digest(policy: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        policy,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_dogfood_policy(value: Any) -> dict[str, Any]:
    policy = value if isinstance(value, Mapping) else {}
    reasons: list[str] = []
    if policy.get("schema_version") != DOGFOOD_POLICY_SCHEMA_VERSION:
        reasons.append("dogfood_policy_schema_unsupported")
    target = _integer(policy.get("target_score"))
    release_target = _integer(policy.get("release_score"))
    runs = _integer(policy.get("required_independent_runs"))
    if target is None or not 95 <= target <= 100:
        reasons.append("dogfood_policy_target_below_95")
    if release_target is None or not 95 <= release_target <= 100:
        reasons.append("dogfood_policy_release_score_invalid")
    if runs is None or runs < 3:
        reasons.append("dogfood_policy_independent_runs_insufficient")
    if policy.get("same_candidate_required") is not True:
        reasons.append("dogfood_policy_candidate_not_immutable")

    attestation = policy.get("attestation")
    attestation = attestation if isinstance(attestation, Mapping) else {}
    if str(attestation.get("provider") or "") != "github-artifact-attestation":
        reasons.append("dogfood_policy_standard_attestation_required")
    if not str(attestation.get("repository") or "").strip():
        reasons.append("dogfood_policy_attestation_repository_missing")
    if not str(attestation.get("signer_workflow") or "").strip():
        reasons.append("dogfood_policy_signer_workflow_missing")
    if attestation.get("attest_evidence") is not True or attestation.get("attest_candidate") is not True:
        reasons.append("dogfood_policy_subject_coverage_incomplete")

    lanes = policy.get("lanes")
    lanes = lanes if isinstance(lanes, Mapping) else {}
    expected_lanes = {"local", "ci", "release"}
    if set(lanes) != expected_lanes:
        reasons.append("dogfood_policy_lanes_incomplete")
    else:
        for name, minimum in (("local", 85), ("ci", 95), ("release", 100)):
            lane = lanes.get(name)
            lane = lane if isinstance(lane, Mapping) else {}
            if lane.get("required") is not True:
                reasons.append(f"dogfood_policy_{name}_lane_optional")
            score = _integer(lane.get("minimum_score"))
            if score is None or score < minimum:
                reasons.append(f"dogfood_policy_{name}_score_too_low")

    references = policy.get("github_references")
    if not isinstance(references, list) or len(references) < 4:
        reasons.append("dogfood_policy_references_incomplete")
    else:
        for item in references:
            reference = item if isinstance(item, Mapping) else {}
            if not _sha(str(reference.get("commit") or "")):
                reasons.append("dogfood_policy_reference_not_pinned")
                break
            if not _safe_repo_path(reference.get("path")):
                reasons.append("dogfood_policy_reference_path_invalid")
                break

    unique = list(dict.fromkeys(reasons))
    return {
        "schema_version": DOGFOOD_POLICY_SCHEMA_VERSION,
        "status": "passed" if not unique else "failed",
        "passed": not unique,
        "reason_codes": unique,
        "target_score": target or 0,
        "release_score": release_target or 0,
        "required_independent_runs": runs or 0,
        "same_candidate_required": policy.get("same_candidate_required") is True,
        "policy_digest": dogfood_policy_digest(policy) if policy else "",
    }


def load_dogfood_policy(repo_root: str | Path) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve()
    path = root / DOGFOOD_POLICY_PATH
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("dogfood policy cannot use symbolic links")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("dogfood policy is unavailable") from exc
    if len(raw) > MAX_DOGFOOD_POLICY_BYTES:
        raise ValueError("dogfood policy exceeds the size limit")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("dogfood policy is invalid JSON") from exc
    result = validate_dogfood_policy(payload)
    result["policy_path"] = DOGFOOD_POLICY_PATH.as_posix()
    return result


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _sha(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value.lower())


def _safe_repo_path(value: Any) -> bool:
    path = PurePosixPath(str(value or "").replace("\\", "/"))
    return bool(path.parts) and not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)
