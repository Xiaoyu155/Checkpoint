from __future__ import annotations

import hashlib
import hmac
import json
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .release_gate import release_manifest_digest, run_release_matrix, validate_release_manifest


RELEASE_EVIDENCE_SCHEMA_VERSION = 1
RELEASE_EVIDENCE_ATTESTATION_ALGORITHM = "hmac-sha256"
MAX_RELEASE_EVIDENCE_BYTES = 4 * 1024 * 1024


def release_evidence_bundle_digest(value: Mapping[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "attestation"}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def attest_release_evidence_bundle(
    value: Mapping[str, Any],
    *,
    key_id: str,
    key: str | bytes,
) -> dict[str, Any]:
    clean_key_id = str(key_id or "").strip()
    key_bytes = key if isinstance(key, bytes) else str(key or "").encode("utf-8")
    if not clean_key_id or not key_bytes:
        raise ValueError("release evidence attestation key_id and key are required")
    payload = deepcopy(dict(value))
    digest = release_evidence_bundle_digest(payload)
    payload["attestation"] = {
        "algorithm": RELEASE_EVIDENCE_ATTESTATION_ALGORITHM,
        "key_id": clean_key_id,
        "bundle_digest": digest,
        "signature": hmac.new(key_bytes, digest.encode("ascii"), hashlib.sha256).hexdigest(),
    }
    return payload


def load_release_evidence_bundle(
    *,
    manifest: Mapping[str, Any],
    expected_manifest_digest: str,
    evidence_root: str | Path,
    bundle_path: str | Path,
    attestation_keys: Mapping[str, str | bytes] | None,
) -> dict[str, Any]:
    root = Path(evidence_root).expanduser().resolve()
    manifest_assessment = validate_release_manifest(manifest)
    expected_digest = str(expected_manifest_digest or "").strip().lower()
    try:
        actual_manifest_digest = release_manifest_digest(manifest)
    except (TypeError, ValueError):
        actual_manifest_digest = ""
    reasons = list(manifest_assessment.get("reason_codes") or [])
    if not expected_digest or not actual_manifest_digest or not hmac.compare_digest(
        expected_digest,
        actual_manifest_digest,
    ):
        reasons.append("release_bundle_manifest_digest_mismatch")
    path = _protected_path(root, bundle_path)
    if path is None:
        reasons.append("release_bundle_path_invalid")
        return _bundle_result(reasons=reasons, digest="", cases=[])
    try:
        raw = path.read_bytes()
    except OSError:
        reasons.append("release_bundle_unavailable")
        return _bundle_result(reasons=reasons, digest="", cases=[])
    if len(raw) > MAX_RELEASE_EVIDENCE_BYTES:
        reasons.append("release_bundle_too_large")
        return _bundle_result(reasons=reasons, digest="", cases=[])
    try:
        bundle = json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=_reject_nonfinite_json_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        reasons.append("release_bundle_not_json")
        return _bundle_result(reasons=reasons, digest="", cases=[])
    if not isinstance(bundle, dict):
        reasons.append("release_bundle_not_object")
        return _bundle_result(reasons=reasons, digest="", cases=[])
    try:
        bundle_digest = release_evidence_bundle_digest(bundle)
    except (TypeError, ValueError):
        reasons.append("release_bundle_not_canonical_json")
        return _bundle_result(reasons=reasons, digest="", cases=[])
    if bundle.get("schema_version") != RELEASE_EVIDENCE_SCHEMA_VERSION:
        reasons.append("release_bundle_schema_unsupported")
    if str(bundle.get("manifest_digest") or "").strip().lower() != actual_manifest_digest:
        reasons.append("release_bundle_manifest_digest_mismatch")
    reasons.extend(
        _attestation_reasons(
            bundle,
            bundle_digest=bundle_digest,
            expected_key_id=str(manifest.get("release_attestation_key_id") or ""),
            attestation_keys=attestation_keys,
        )
    )

    expected_ids = [
        str(item.get("case_id") or "")
        for item in manifest.get("cases") or []
        if isinstance(item, dict)
    ]
    entries = bundle.get("cases") if isinstance(bundle.get("cases"), list) else []
    observed_ids = [
        str(item.get("case_id") or "") for item in entries if isinstance(item, dict)
    ]
    if observed_ids != expected_ids or len(entries) != len(expected_ids):
        reasons.append("release_bundle_case_order_or_coverage_mismatch")
    payloads: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    seen_digests: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            reasons.append("release_bundle_case_invalid")
            continue
        case_id = str(entry.get("case_id") or "").strip()
        result_path = _protected_path(root, entry.get("result_path"))
        expected_sha = str(entry.get("result_sha256") or "").strip().lower()
        if result_path is None:
            reasons.append("release_bundle_result_path_invalid")
            continue
        if result_path in seen_paths:
            reasons.append("release_bundle_result_path_duplicate")
        seen_paths.add(result_path)
        if expected_sha in seen_digests:
            reasons.append("release_bundle_result_digest_duplicate")
        seen_digests.add(expected_sha)
        try:
            result_raw = result_path.read_bytes()
        except OSError:
            reasons.append("release_bundle_result_unavailable")
            continue
        if len(result_raw) > MAX_RELEASE_EVIDENCE_BYTES:
            reasons.append("release_bundle_result_too_large")
            continue
        actual_sha = hashlib.sha256(result_raw).hexdigest()
        if not _is_sha256(expected_sha) or not hmac.compare_digest(expected_sha, actual_sha):
            reasons.append("release_bundle_result_digest_mismatch")
            continue
        try:
            payload = json.loads(
                result_raw.decode("utf-8-sig"),
                parse_constant=_reject_nonfinite_json_number,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            reasons.append("release_bundle_result_not_json")
            continue
        if not isinstance(payload, dict):
            reasons.append("release_bundle_result_not_object")
            continue
        payloads.append({"case_id": case_id, "payload": payload})
    return _bundle_result(
        reasons=reasons,
        digest=bundle_digest,
        cases=payloads,
        key_id=str((bundle.get("attestation") or {}).get("key_id") or "")
        if isinstance(bundle.get("attestation"), dict)
        else "",
    )


def run_release_evidence_bundle(
    *,
    manifest: Mapping[str, Any],
    expected_manifest_digest: str,
    repo_root: str | Path,
    evidence_root: str | Path,
    bundle_path: str | Path,
    release_attestation_keys: Mapping[str, str | bytes] | None,
    dogfood_attestation_keys: Mapping[str, str | bytes] | None = None,
    dogfood_artifact_roots: tuple[str | Path, ...] = (),
) -> dict[str, Any]:
    loaded = load_release_evidence_bundle(
        manifest=manifest,
        expected_manifest_digest=expected_manifest_digest,
        evidence_root=evidence_root,
        bundle_path=bundle_path,
        attestation_keys=release_attestation_keys,
    )
    if not loaded["passed"]:
        return {
            "schema_version": 1,
            "status": "failed",
            "passed": False,
            "clean": False,
            "manifest_digest": str(expected_manifest_digest or ""),
            "bundle_digest": str(loaded.get("bundle_digest") or ""),
            "executed_count": 0,
            "total_count": len(manifest.get("cases") or []),
            "stopped_at": "evidence_preflight",
            "reason_codes": list(loaded.get("reason_codes") or []),
            "results": [],
        }
    payloads = {
        str(item.get("case_id") or ""): dict(item.get("payload") or {})
        for item in loaded.get("cases") or []
        if isinstance(item, dict)
    }
    runners = {
        case_id: (lambda payload=payload: deepcopy(payload))
        for case_id, payload in payloads.items()
    }
    roots = tuple(dict.fromkeys([Path(evidence_root).expanduser().resolve(), *dogfood_artifact_roots]))
    artifact_roots_by_case = {
        str(item.get("case_id") or ""): roots
        for item in manifest.get("cases") or []
        if isinstance(item, dict) and str(item.get("kind") or "") == "dogfood"
    }
    result = run_release_matrix(
        manifest,
        expected_manifest_digest=expected_manifest_digest,
        runners=runners,
        dogfood_attestation_keys=dogfood_attestation_keys or release_attestation_keys,
        dogfood_artifact_roots=artifact_roots_by_case,
        release_root=repo_root,
    )
    result["bundle_digest"] = str(loaded.get("bundle_digest") or "")
    result["bundle_attestation_key_id"] = str(loaded.get("attestation_key_id") or "")
    return result


def _attestation_reasons(
    bundle: Mapping[str, Any],
    *,
    bundle_digest: str,
    expected_key_id: str,
    attestation_keys: Mapping[str, str | bytes] | None,
) -> list[str]:
    envelope = bundle.get("attestation")
    if not isinstance(envelope, Mapping):
        return ["release_bundle_attestation_missing"]
    reasons: list[str] = []
    algorithm = str(envelope.get("algorithm") or "").strip().lower()
    key_id = str(envelope.get("key_id") or "").strip()
    declared_digest = str(envelope.get("bundle_digest") or "").strip().lower()
    signature = str(envelope.get("signature") or "").strip().lower()
    if algorithm != RELEASE_EVIDENCE_ATTESTATION_ALGORITHM:
        reasons.append("release_bundle_attestation_algorithm_invalid")
    if not key_id or key_id != expected_key_id:
        reasons.append("release_bundle_attestation_key_mismatch")
    if not _is_sha256(declared_digest) or not hmac.compare_digest(declared_digest, bundle_digest):
        reasons.append("release_bundle_attestation_digest_mismatch")
    keys = attestation_keys if isinstance(attestation_keys, Mapping) else {}
    raw_key = keys.get(key_id, b"")
    key = raw_key if isinstance(raw_key, bytes) else str(raw_key or "").encode("utf-8")
    if not key:
        reasons.append("release_bundle_attestation_key_unavailable")
    elif _is_sha256(signature):
        expected = hmac.new(key, bundle_digest.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            reasons.append("release_bundle_attestation_signature_mismatch")
    else:
        reasons.append("release_bundle_attestation_signature_invalid")
    return reasons


def _protected_path(root: Path, value: Any) -> Path | None:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return None
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"", ".."} for part in relative.parts):
        return None
    if relative.parts and relative.parts[0].endswith(":"):
        return None
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return None
    try:
        resolved = current.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def _bundle_result(
    *,
    reasons: list[str],
    digest: str,
    cases: list[dict[str, Any]],
    key_id: str = "",
) -> dict[str, Any]:
    unique = list(dict.fromkeys(str(item) for item in reasons if str(item)))
    return {
        "schema_version": RELEASE_EVIDENCE_SCHEMA_VERSION,
        "status": "passed" if not unique else "failed",
        "passed": not unique,
        "bundle_digest": digest,
        "attestation_key_id": key_id,
        "reason_codes": unique,
        "cases": cases,
    }


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _reject_nonfinite_json_number(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")
