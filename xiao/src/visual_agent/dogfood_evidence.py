"""Strict evidence assessment for Pacer developing Pacer.

Full dogfood is a generation chain, not a source-discipline label: an installed
wheel A must manage a change in the Pacer repository, produce wheel B, install B
in a fresh environment, and use B to complete a trusted self-check.  Every
cross-stage identity is matched here and missing evidence fails closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import zipfile
from configparser import ConfigParser
from copy import deepcopy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Mapping

from .dogfood_quality import DOGFOOD_TARGET_SCORE, assess_dogfood_quality
from .github_attestation import AttestationRunner, verify_github_artifact_attestations


DOGFOOD_EVIDENCE_SCHEMA_VERSION = 1
DOGFOOD_EVIDENCE_PATH = Path(".pacer/dogfood-evidence.json")
MAX_DOGFOOD_EVIDENCE_BYTES = 2 * 1024 * 1024
MAX_DOGFOOD_JSON_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_DOGFOOD_WHEEL_BYTES = 512 * 1024 * 1024
DOGFOOD_ATTESTATION_ALGORITHM = "hmac-sha256"
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_DRIVE_PATH = re.compile(r"^[A-Za-z]:/")

_ARTIFACT_FIELDS = (
    ("orchestrator", "input_wheel_path", "input_wheel_sha256", "input_wheel"),
    ("contract", "task_contract_path", "task_contract_digest", "task_contract"),
    (
        "contract",
        "acceptance_contract_path",
        "acceptance_contract_digest",
        "acceptance_contract",
    ),
    ("verification", "receipt_path", "receipt_digest", "verification_receipt"),
    ("candidate", "wheel_path", "wheel_sha256", "candidate_wheel"),
    (
        "bootstrap",
        "self_check_receipt_path",
        "self_check_receipt_digest",
        "self_check_receipt",
    ),
)


def assess_dogfood_evidence(
    value: Any,
    *,
    repo_root: str | Path | None = None,
    artifact_roots: tuple[str | Path, ...] = (),
    require_artifacts: bool = True,
    attestation_keys: Mapping[str, str | bytes] | None = None,
    standard_provenance: Mapping[str, Any] | None = None,
    target_score: int = DOGFOOD_TARGET_SCORE,
) -> dict[str, Any]:
    evidence = value if isinstance(value, dict) else {}
    if not evidence:
        return _result(
            status="indeterminate",
            reasons=["dogfood_evidence_missing"],
            evidence_digest="",
        )

    missing: list[str] = []
    failures: list[str] = []
    if evidence.get("schema_version") != DOGFOOD_EVIDENCE_SCHEMA_VERSION:
        failures.append("dogfood_schema_unsupported")

    source = _section(evidence, "source_repo", missing)
    orchestrator = _section(evidence, "orchestrator", missing)
    contract = _section(evidence, "contract", missing)
    verification = _section(evidence, "verification", missing)
    candidate = _section(evidence, "candidate", missing)
    bootstrap = _section(evidence, "bootstrap", missing)

    attestation = _verify_attestation(
        evidence,
        attestation_keys=attestation_keys,
        standard_provenance=standard_provenance,
    )
    failures.extend(attestation["failure_codes"])
    missing.extend(attestation["missing_codes"])

    product = _required_text(source, "product", "dogfood_source_product_missing", missing)
    package_name = _required_text(
        source,
        "package_name",
        "dogfood_source_package_missing",
        missing,
    )
    entrypoint = _required_text(
        source,
        "pacer_entrypoint",
        "dogfood_source_entrypoint_missing",
        missing,
    )
    _required_text(source, "canonical_root", "dogfood_source_root_missing", missing)
    repo_digest = _required_digest(
        source,
        "repo_identity_digest",
        "dogfood_repo_identity_missing",
        "dogfood_repo_identity_invalid",
        missing,
        failures,
    )
    _required_text(source, "baseline_commit", "dogfood_baseline_commit_missing", missing)
    _required_digest(
        source,
        "baseline_changes_digest",
        "dogfood_baseline_digest_missing",
        "dogfood_baseline_digest_invalid",
        missing,
        failures,
    )
    change_set_digest = _required_digest(
        source,
        "change_set_digest",
        "dogfood_changeset_digest_missing",
        "dogfood_changeset_digest_invalid",
        missing,
        failures,
    )
    _require_true(
        source,
        "scan_complete",
        "dogfood_scan_complete_missing",
        "dogfood_scan_incomplete",
        missing,
        failures,
    )
    _require_true(
        source,
        "protected_paths_unchanged",
        "dogfood_protected_paths_missing",
        "dogfood_protected_path_changed",
        missing,
        failures,
    )
    if "out_of_band_changes" not in source:
        missing.append("dogfood_out_of_band_status_missing")
    elif source.get("out_of_band_changes") is not False:
        failures.append("dogfood_out_of_band_changes_detected")
    attribution = _required_text(
        source,
        "source_attribution",
        "dogfood_source_attribution_missing",
        missing,
    )
    changed_files = source.get("changed_files")
    if not isinstance(changed_files, list) or not changed_files:
        missing.append("dogfood_changed_files_missing")
        normalized_files: list[str] = []
    else:
        normalized_files = [_normalize_repo_path(item) for item in changed_files]
        if any(not item for item in normalized_files):
            failures.append("dogfood_changed_file_path_invalid")
    if normalized_files and not any(_is_pacer_product_path(path) for path in normalized_files):
        failures.append("dogfood_pacer_product_change_missing")
    if product and product.casefold() != "pacer":
        failures.append("dogfood_source_is_not_pacer")
    if package_name and package_name.casefold() != "visual-agent":
        failures.append("dogfood_package_identity_mismatch")
    if entrypoint and entrypoint != "visual_agent.cli:main":
        failures.append("dogfood_entrypoint_identity_mismatch")
    if attribution and attribution != "pacer_worker":
        failures.append("dogfood_source_attribution_untrusted")

    input_wheel = _required_digest(
        orchestrator,
        "input_wheel_sha256",
        "dogfood_input_wheel_digest_missing",
        "dogfood_input_wheel_digest_invalid",
        missing,
        failures,
    )
    input_wheel_path = _required_text(
        orchestrator,
        "input_wheel_path",
        "dogfood_input_wheel_path_missing",
        missing,
    )
    _required_text(orchestrator, "input_version", "dogfood_input_version_missing", missing)
    _required_text(orchestrator, "launch_id", "dogfood_launch_id_missing", missing)
    _required_text(orchestrator, "mission_id", "dogfood_mission_id_missing", missing)
    orchestrator_repo_digest = _required_digest(
        orchestrator,
        "repo_identity_digest",
        "dogfood_orchestrator_repo_identity_missing",
        "dogfood_orchestrator_repo_identity_invalid",
        missing,
        failures,
    )
    sessions = orchestrator.get("worker_session_ids")
    if not isinstance(sessions, list) or not any(str(item).strip() for item in sessions):
        missing.append("dogfood_worker_session_missing")
    if input_wheel_path and not input_wheel_path.casefold().endswith(".whl"):
        failures.append("dogfood_input_artifact_not_wheel")
    _match_digest(
        repo_digest,
        orchestrator_repo_digest,
        "dogfood_orchestrator_repo_identity_mismatch",
        failures,
    )

    task_contract_digest = _required_digest(
        contract,
        "task_contract_digest",
        "dogfood_task_contract_missing",
        "dogfood_task_contract_invalid",
        missing,
        failures,
    )
    acceptance_contract_digest = _required_digest(
        contract,
        "acceptance_contract_digest",
        "dogfood_acceptance_contract_missing",
        "dogfood_acceptance_contract_invalid",
        missing,
        failures,
    )

    verification_status = _required_text(
        verification,
        "status",
        "dogfood_verification_status_missing",
        missing,
    )
    trust = _required_text(
        verification,
        "trust",
        "dogfood_verification_trust_missing",
        missing,
    )
    _required_text(
        verification,
        "batch_run_id",
        "dogfood_verification_batch_missing",
        missing,
    )
    _required_digest(
        verification,
        "receipt_digest",
        "dogfood_verification_receipt_missing",
        "dogfood_verification_receipt_invalid",
        missing,
        failures,
    )
    verified_contract_digest = _required_digest(
        verification,
        "acceptance_contract_digest",
        "dogfood_verified_contract_missing",
        "dogfood_verified_contract_invalid",
        missing,
        failures,
    )
    verified_change_set = _required_digest(
        verification,
        "change_set_digest",
        "dogfood_verified_changeset_missing",
        "dogfood_verified_changeset_invalid",
        missing,
        failures,
    )
    if verification_status and verification_status != "passed":
        failures.append("dogfood_verification_not_passed")
    if trust and trust != "yes":
        failures.append("dogfood_verification_untrusted")
    _require_zero(
        verification,
        "evidence_resubmissions",
        "dogfood_evidence_resubmissions_missing",
        "dogfood_evidence_resubmitted",
        missing,
        failures,
    )
    warnings = verification.get("warnings")
    if not isinstance(warnings, list):
        missing.append("dogfood_verification_warnings_missing")
    elif warnings:
        failures.append("dogfood_verification_warning_present")
    _match_digest(
        acceptance_contract_digest,
        verified_contract_digest,
        "dogfood_acceptance_contract_mismatch",
        failures,
    )
    _match_digest(
        change_set_digest,
        verified_change_set,
        "dogfood_verified_changeset_mismatch",
        failures,
    )

    candidate_wheel = _required_digest(
        candidate,
        "wheel_sha256",
        "dogfood_candidate_wheel_digest_missing",
        "dogfood_candidate_wheel_digest_invalid",
        missing,
        failures,
    )
    candidate_wheel_path = _required_text(
        candidate,
        "wheel_path",
        "dogfood_candidate_wheel_path_missing",
        missing,
    )
    _required_text(candidate, "version", "dogfood_candidate_version_missing", missing)
    built_from_digest = _required_digest(
        candidate,
        "built_from_change_set_digest",
        "dogfood_candidate_changeset_missing",
        "dogfood_candidate_changeset_invalid",
        missing,
        failures,
    )
    _require_true(
        candidate,
        "fresh_install",
        "dogfood_fresh_install_missing",
        "dogfood_fresh_install_unverified",
        missing,
        failures,
    )
    _required_text(candidate, "fresh_env_id", "dogfood_fresh_env_missing", missing)
    pip_check = _required_text(
        candidate,
        "pip_check_status",
        "dogfood_pip_check_missing",
        missing,
    )
    if candidate_wheel_path and not candidate_wheel_path.casefold().endswith(".whl"):
        failures.append("dogfood_candidate_artifact_not_wheel")
    if input_wheel and candidate_wheel and input_wheel == candidate_wheel:
        failures.append("dogfood_candidate_matches_input_wheel")
    if pip_check and pip_check != "passed":
        failures.append("dogfood_pip_check_not_passed")
    _match_digest(
        change_set_digest,
        built_from_digest,
        "dogfood_candidate_changeset_mismatch",
        failures,
    )

    parent_wheel = _required_digest(
        bootstrap,
        "parent_wheel_sha256",
        "dogfood_bootstrap_parent_missing",
        "dogfood_bootstrap_parent_invalid",
        missing,
        failures,
    )
    installed_wheel = _required_digest(
        bootstrap,
        "installed_wheel_sha256",
        "dogfood_bootstrap_installed_missing",
        "dogfood_bootstrap_installed_invalid",
        missing,
        failures,
    )
    self_check_artifact = _required_digest(
        bootstrap,
        "self_check_artifact_sha256",
        "dogfood_self_check_artifact_missing",
        "dogfood_self_check_artifact_invalid",
        missing,
        failures,
    )
    bootstrap_repo_digest = _required_digest(
        bootstrap,
        "source_repo_identity_digest",
        "dogfood_bootstrap_repo_identity_missing",
        "dogfood_bootstrap_repo_identity_invalid",
        missing,
        failures,
    )
    self_check_status = _required_text(
        bootstrap,
        "self_check_status",
        "dogfood_self_check_status_missing",
        missing,
    )
    _required_digest(
        bootstrap,
        "self_check_receipt_digest",
        "dogfood_self_check_receipt_missing",
        "dogfood_self_check_receipt_invalid",
        missing,
        failures,
    )
    _required_text(
        bootstrap,
        "self_check_launch_id",
        "dogfood_self_check_launch_missing",
        missing,
    )
    if self_check_status and self_check_status != "passed":
        failures.append("dogfood_self_check_not_passed")
    _match_digest(input_wheel, parent_wheel, "dogfood_parent_wheel_mismatch", failures)
    _match_digest(candidate_wheel, installed_wheel, "dogfood_installed_wheel_mismatch", failures)
    _match_digest(
        candidate_wheel,
        self_check_artifact,
        "dogfood_self_check_artifact_mismatch",
        failures,
    )
    _match_digest(
        repo_digest,
        bootstrap_repo_digest,
        "dogfood_bootstrap_repo_identity_mismatch",
        failures,
    )

    artifact_verification = _verify_artifact_files(
        evidence,
        repo_root=repo_root,
        artifact_roots=artifact_roots,
        required=require_artifacts,
    )
    if require_artifacts:
        failures.extend(artifact_verification["failure_codes"])
        missing.extend(artifact_verification["missing_codes"])

    evidence_digest = ""
    try:
        evidence_digest = dogfood_evidence_digest(evidence)
    except (TypeError, ValueError):
        failures.append("dogfood_evidence_not_json")

    reasons = list(dict.fromkeys(failures + missing))
    status = "failed" if failures else "partial" if missing else "passed"
    result = _result(status=status, reasons=reasons, evidence_digest=evidence_digest)
    result.update(
        {
            "generation": {
                "parent_wheel_sha256": input_wheel,
                "candidate_wheel_sha256": candidate_wheel,
                "repo_identity_digest": repo_digest,
                "task_contract_digest": task_contract_digest,
                "change_set_digest": change_set_digest,
                "orchestrator_launch_id": str(orchestrator.get("launch_id") or ""),
                "verification_batch_run_id": str(verification.get("batch_run_id") or ""),
                "self_check_launch_id": str(bootstrap.get("self_check_launch_id") or ""),
            },
            "self_change_attributed": attribution == "pacer_worker"
            and source.get("out_of_band_changes") is False,
            "installed_artifact_verified": bool(
                candidate_wheel
                and installed_wheel == candidate_wheel
                and self_check_artifact == candidate_wheel
                and pip_check == "passed"
                and self_check_status == "passed"
                and artifact_verification["passed"]
            ),
            "artifact_files_verified": bool(artifact_verification["passed"]),
            "artifact_verification": artifact_verification,
            "attestation": attestation["summary"],
            "provenance": dict(standard_provenance)
            if isinstance(standard_provenance, Mapping)
            else {},
        }
    )
    result["quality"] = assess_dogfood_quality(
        result,
        provenance=standard_provenance,
        target_score=target_score,
    )
    return result


def dogfood_evidence_digest(value: Mapping[str, Any]) -> str:
    """Return the stable digest covered by the external attestation.

    The signature envelope is excluded so re-encoding the same signature cannot
    manufacture a new clean-streak sample.
    """
    payload = {key: item for key, item in value.items() if key != "attestation"}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def attest_dogfood_evidence(
    value: Mapping[str, Any],
    *,
    key_id: str,
    key: str | bytes,
) -> dict[str, Any]:
    """Create an HMAC envelope using a key kept outside the repository."""
    clean_key_id = str(key_id or "").strip()
    key_bytes = _attestation_key_bytes(key)
    if not clean_key_id:
        raise ValueError("dogfood attestation key_id is required")
    if not key_bytes:
        raise ValueError("dogfood attestation key is required")
    payload = deepcopy(dict(value))
    digest = dogfood_evidence_digest(payload)
    payload["attestation"] = {
        "algorithm": DOGFOOD_ATTESTATION_ALGORITHM,
        "key_id": clean_key_id,
        "evidence_digest": digest,
        "signature": hmac.new(key_bytes, digest.encode("ascii"), hashlib.sha256).hexdigest(),
    }
    return payload


def load_dogfood_evidence(
    repo_root: str | Path,
    *,
    evidence_path: str | Path | None = None,
    artifact_roots: tuple[str | Path, ...] = (),
    attestation_keys: Mapping[str, str | bytes] | None = None,
    github_repository: str = "",
    github_signer_workflow: str = "",
    github_run_id: str = "",
    github_run_attempt: str = "",
    require_github_provenance: bool = False,
    target_score: int = DOGFOOD_TARGET_SCORE,
    github_attestation_runner: AttestationRunner | None = None,
) -> dict[str, Any]:
    """Load the canonical repository evidence file and verify referenced artifacts."""
    root = Path(repo_root).expanduser().resolve()
    canonical = _protected_repository_path(root, DOGFOOD_EVIDENCE_PATH)
    supplied_raw = Path(evidence_path).expanduser() if evidence_path else canonical
    if evidence_path and not supplied_raw.is_absolute():
        supplied_raw = root / supplied_raw
    try:
        supplied = supplied_raw.resolve(strict=False)
    except OSError as exc:
        raise ValueError("dogfood evidence path is invalid") from exc
    if supplied != canonical.resolve(strict=False):
        raise ValueError("dogfood evidence must be .pacer/dogfood-evidence.json")
    try:
        size = supplied.stat().st_size
    except OSError as exc:
        raise ValueError("dogfood evidence is unavailable") from exc
    if size > MAX_DOGFOOD_EVIDENCE_BYTES:
        raise ValueError("dogfood evidence exceeds the size limit")
    try:
        payload = json.loads(supplied.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid dogfood evidence") from exc
    if not isinstance(payload, dict):
        raise ValueError("dogfood evidence must contain a JSON object")
    provenance: dict[str, Any] = {}
    if github_repository:
        candidate_path = _candidate_artifact_for_attestation(
            payload,
            repo_root=root,
            artifact_roots=artifact_roots,
        )
        provenance_kwargs: dict[str, Any] = {}
        if github_attestation_runner is not None:
            provenance_kwargs["runner"] = github_attestation_runner
        provenance = verify_github_artifact_attestations(
            [supplied, candidate_path],
            repository=github_repository,
            signer_workflow=github_signer_workflow,
            run_id=github_run_id,
            run_attempt=github_run_attempt,
            **provenance_kwargs,
        )
    assessment = assess_dogfood_evidence(
        payload,
        repo_root=root,
        artifact_roots=artifact_roots,
        require_artifacts=True,
        attestation_keys=attestation_keys,
        standard_provenance=provenance,
        target_score=target_score,
    )
    if require_github_provenance and provenance.get("verified") is not True:
        assessment["status"] = "failed"
        assessment["passed"] = False
        assessment["pacer_on_pacer"] = False
        assessment["reason_codes"] = list(
            dict.fromkeys(
                [
                    *assessment.get("reason_codes", []),
                    *provenance.get("reason_codes", []),
                    "dogfood_github_provenance_required",
                ]
            )
        )
        assessment["quality"] = assess_dogfood_quality(
            assessment,
            provenance=provenance,
            target_score=target_score,
        )
    assessment["evidence_path"] = DOGFOOD_EVIDENCE_PATH.as_posix()
    try:
        assessment["evidence_file_sha256"] = _file_sha256(supplied)
    except OSError as exc:
        raise ValueError("dogfood evidence is unreadable") from exc
    return assessment


def _result(*, status: str, reasons: list[str], evidence_digest: str) -> dict[str, Any]:
    passed = status == "passed"
    return {
        "schema_version": DOGFOOD_EVIDENCE_SCHEMA_VERSION,
        "status": status,
        "passed": passed,
        "pacer_on_pacer": passed,
        "evidence_digest": evidence_digest,
        "reason_codes": list(dict.fromkeys(reasons)),
    }


def _section(evidence: dict[str, Any], key: str, missing: list[str]) -> dict[str, Any]:
    value = evidence.get(key)
    if not isinstance(value, dict):
        missing.append(f"dogfood_{key}_missing")
        return {}
    return value


def _required_text(
    section: dict[str, Any],
    key: str,
    missing_code: str,
    missing: list[str],
) -> str:
    value = str(section.get(key) or "").strip()
    if not value:
        missing.append(missing_code)
    return value


def _required_digest(
    section: dict[str, Any],
    key: str,
    missing_code: str,
    invalid_code: str,
    missing: list[str],
    failures: list[str],
) -> str:
    value = str(section.get(key) or "").strip().lower()
    if not value:
        missing.append(missing_code)
    elif not _SHA256.fullmatch(value):
        failures.append(invalid_code)
    return value


def _require_true(
    section: dict[str, Any],
    key: str,
    missing_code: str,
    failure_code: str,
    missing: list[str],
    failures: list[str],
) -> None:
    if key not in section:
        missing.append(missing_code)
    elif section.get(key) is not True:
        failures.append(failure_code)


def _require_zero(
    section: dict[str, Any],
    key: str,
    missing_code: str,
    failure_code: str,
    missing: list[str],
    failures: list[str],
) -> None:
    if key not in section:
        missing.append(missing_code)
        return
    value = section.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value != 0:
        failures.append(failure_code)


def _match_digest(left: str, right: str, failure_code: str, failures: list[str]) -> None:
    if left and right and left != right:
        failures.append(failure_code)


def _verify_artifact_files(
    evidence: dict[str, Any],
    *,
    repo_root: str | Path | None,
    artifact_roots: tuple[str | Path, ...],
    required: bool,
) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve() if repo_root is not None else None
    allowed_roots: list[Path] = []
    if root is not None:
        allowed_roots.append(root)
    allowed_roots.extend(Path(item).expanduser().resolve() for item in artifact_roots)
    allowed_roots = list(dict.fromkeys(allowed_roots))
    missing: list[str] = []
    failures: list[str] = []
    artifacts: dict[str, dict[str, Any]] = {}
    parsed_artifacts: dict[str, dict[str, Any]] = {}
    seen_paths: set[Path] = set()
    if not required:
        return {
            "status": "not_required",
            "passed": False,
            "missing_codes": [],
            "failure_codes": [],
            "artifacts": {},
        }
    if not allowed_roots:
        return {
            "status": "partial",
            "passed": False,
            "missing_codes": ["dogfood_artifact_root_missing"],
            "failure_codes": [],
            "artifacts": {},
        }
    for section_name, path_key, digest_key, role in _ARTIFACT_FIELDS:
        section = evidence.get(section_name)
        section = section if isinstance(section, dict) else {}
        raw_path = str(section.get(path_key) or "").strip()
        expected = str(section.get(digest_key) or "").strip().lower()
        if not raw_path:
            missing.append(f"dogfood_{role}_path_missing")
            artifacts[role] = {"status": "missing", "path": "", "sha256": ""}
            continue
        candidate = Path(raw_path).expanduser()
        if ".." in candidate.parts:
            failures.append(f"dogfood_{role}_path_invalid")
            artifacts[role] = {"status": "rejected", "path": role, "sha256": ""}
            continue
        if not candidate.is_absolute():
            candidate = (root / candidate) if root is not None else candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            failures.append(f"dogfood_{role}_file_missing")
            artifacts[role] = {"status": "missing", "path": str(candidate), "sha256": ""}
            continue
        if not resolved.is_file():
            failures.append(f"dogfood_{role}_not_file")
            artifacts[role] = {"status": "invalid", "path": str(resolved), "sha256": ""}
            continue
        if not any(_is_within(resolved, allowed) for allowed in allowed_roots):
            failures.append(f"dogfood_{role}_outside_allowed_roots")
            artifacts[role] = {"status": "rejected", "path": str(resolved), "sha256": ""}
            continue
        if _path_uses_symlink(candidate, allowed_roots):
            failures.append(f"dogfood_{role}_symlink_rejected")
            artifacts[role] = {"status": "rejected", "path": _artifact_label(resolved, allowed_roots), "sha256": ""}
            continue
        if resolved in seen_paths:
            failures.append("dogfood_artifact_path_duplicate")
        seen_paths.add(resolved)
        size_limit = MAX_DOGFOOD_WHEEL_BYTES if role in {"input_wheel", "candidate_wheel"} else MAX_DOGFOOD_JSON_ARTIFACT_BYTES
        try:
            size = resolved.stat().st_size
        except OSError:
            failures.append(f"dogfood_{role}_file_unreadable")
            artifacts[role] = {"status": "unreadable", "path": _artifact_label(resolved, allowed_roots), "sha256": ""}
            continue
        if size > size_limit:
            failures.append(f"dogfood_{role}_file_too_large")
            artifacts[role] = {"status": "rejected", "path": _artifact_label(resolved, allowed_roots), "sha256": ""}
            continue
        try:
            actual = _file_sha256(resolved)
        except OSError:
            failures.append(f"dogfood_{role}_file_unreadable")
            artifacts[role] = {"status": "unreadable", "path": _artifact_label(resolved, allowed_roots), "sha256": ""}
            continue
        matched = bool(_SHA256.fullmatch(expected)) and actual == expected
        if not matched:
            failures.append(f"dogfood_{role}_digest_mismatch")
        semantic_failures: list[str] = []
        parsed: dict[str, Any] | None = None
        if matched:
            semantic_failures, parsed = _verify_artifact_semantics(role, resolved, evidence)
            failures.extend(semantic_failures)
            if parsed is not None:
                parsed_artifacts[role] = parsed
        verified = matched and not semantic_failures
        artifacts[role] = {
            "status": "verified" if verified else "invalid" if matched else "mismatched",
            "path": _artifact_label(resolved, allowed_roots),
            "sha256": actual,
        }
    failures.extend(_verify_cross_artifact_bindings(parsed_artifacts))
    missing = list(dict.fromkeys(missing))
    failures = list(dict.fromkeys(failures))
    passed = not missing and not failures and len(artifacts) == len(_ARTIFACT_FIELDS)
    return {
        "status": "passed" if passed else "failed" if failures else "partial",
        "passed": passed,
        "missing_codes": missing,
        "failure_codes": failures,
        "artifacts": artifacts,
    }


def _candidate_artifact_for_attestation(
    evidence: Mapping[str, Any],
    *,
    repo_root: Path,
    artifact_roots: tuple[str | Path, ...],
) -> Path:
    candidate = evidence.get("candidate")
    candidate = candidate if isinstance(candidate, Mapping) else {}
    raw_path = str(candidate.get("wheel_path") or "").strip()
    if not raw_path:
        raise ValueError("dogfood candidate wheel path is missing")
    supplied = Path(raw_path).expanduser()
    if ".." in supplied.parts:
        raise ValueError("dogfood candidate wheel path is invalid")
    if not supplied.is_absolute():
        supplied = repo_root / supplied
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise ValueError("dogfood candidate wheel is unavailable") from exc
    roots = [repo_root, *(Path(item).expanduser().resolve() for item in artifact_roots)]
    if not any(_is_within(resolved, root) for root in roots):
        raise ValueError("dogfood candidate wheel is outside trusted artifact roots")
    if _path_uses_symlink(supplied, roots):
        raise ValueError("dogfood candidate wheel cannot use symbolic links")
    return resolved


def _verify_attestation(
    evidence: dict[str, Any],
    *,
    attestation_keys: Mapping[str, str | bytes] | None,
    standard_provenance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(standard_provenance, Mapping) and standard_provenance.get("verified") is True:
        return {
            "missing_codes": [],
            "failure_codes": [],
            "summary": {
                "status": "verified",
                "provider": "github-artifact-attestation",
                "key_id": str(standard_provenance.get("key_id") or ""),
                "evidence_digest": dogfood_evidence_digest(evidence),
            },
        }
    envelope = evidence.get("attestation")
    if not isinstance(envelope, dict):
        return {
            "missing_codes": ["dogfood_attestation_missing"],
            "failure_codes": [],
            "summary": {"status": "missing", "key_id": "", "evidence_digest": ""},
        }
    missing: list[str] = []
    failures: list[str] = []
    algorithm = str(envelope.get("algorithm") or "").strip().lower()
    key_id = str(envelope.get("key_id") or "").strip()
    declared_digest = str(envelope.get("evidence_digest") or "").strip().lower()
    signature = str(envelope.get("signature") or "").strip().lower()
    if not algorithm:
        missing.append("dogfood_attestation_algorithm_missing")
    elif algorithm != DOGFOOD_ATTESTATION_ALGORITHM:
        failures.append("dogfood_attestation_algorithm_unsupported")
    if not key_id:
        missing.append("dogfood_attestation_key_id_missing")
    if not declared_digest:
        missing.append("dogfood_attestation_digest_missing")
    elif not _SHA256.fullmatch(declared_digest):
        failures.append("dogfood_attestation_digest_invalid")
    if not signature:
        missing.append("dogfood_attestation_signature_missing")
    elif not _SHA256.fullmatch(signature):
        failures.append("dogfood_attestation_signature_invalid")
    try:
        actual_digest = dogfood_evidence_digest(evidence)
    except (TypeError, ValueError):
        actual_digest = ""
        failures.append("dogfood_evidence_not_json")
    if declared_digest and actual_digest and not hmac.compare_digest(declared_digest, actual_digest):
        failures.append("dogfood_attestation_digest_mismatch")
    keys = attestation_keys if isinstance(attestation_keys, Mapping) else {}
    key_bytes = _attestation_key_bytes(keys.get(key_id, b"")) if key_id else b""
    if key_id and not key_bytes:
        failures.append("dogfood_attestation_key_unavailable")
    if key_bytes and actual_digest and _SHA256.fullmatch(signature):
        expected = hmac.new(key_bytes, actual_digest.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            failures.append("dogfood_attestation_signature_mismatch")
    passed = not missing and not failures
    return {
        "missing_codes": list(dict.fromkeys(missing)),
        "failure_codes": list(dict.fromkeys(failures)),
        "summary": {
            "status": "verified" if passed else "failed" if failures else "partial",
            "key_id": key_id,
            "evidence_digest": actual_digest,
        },
    }


def _attestation_key_bytes(value: str | bytes | Any) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value or "").encode("utf-8")


def _verify_artifact_semantics(
    role: str,
    path: Path,
    evidence: dict[str, Any],
) -> tuple[list[str], dict[str, Any] | None]:
    if role in {"input_wheel", "candidate_wheel"}:
        expected_version = str(
            (_section_value(evidence, "orchestrator", "input_version") if role == "input_wheel" else _section_value(evidence, "candidate", "version"))
            or ""
        )
        return _verify_wheel(path, role=role, expected_version=expected_version), None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [f"dogfood_{role}_not_json"], None
    if not isinstance(payload, dict):
        return [f"dogfood_{role}_not_object"], None
    failures: list[str] = []
    if payload.get("schema_version") != 1:
        failures.append(f"dogfood_{role}_schema_invalid")
    if role == "task_contract":
        if not _SHA256.fullmatch(str(payload.get("goal_digest") or "")):
            failures.append("dogfood_task_contract_goal_digest_invalid")
        requirements = payload.get("requirements")
        if not isinstance(requirements, list) or not requirements:
            failures.append("dogfood_task_contract_requirements_missing")
    elif role == "acceptance_contract":
        if not isinstance(payload.get("observable_outcomes"), list) or not payload.get("observable_outcomes"):
            failures.append("dogfood_acceptance_contract_outcomes_missing")
        if not isinstance(payload.get("verification"), dict):
            failures.append("dogfood_acceptance_contract_verification_missing")
        embedded_digest = str(payload.get("digest") or "").strip().lower()
        if embedded_digest:
            canonical = json.dumps(
                {key: item for key, item in payload.items() if key != "digest"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(embedded_digest, actual):
                failures.append("dogfood_acceptance_contract_embedded_digest_mismatch")
    elif role == "verification_receipt":
        failures.extend(
            _receipt_binding_failures(
                payload,
                expected={
                    "status": "passed",
                    "trust": "yes",
                    "launch_id": _section_value(evidence, "orchestrator", "launch_id"),
                    "batch_run_id": _section_value(evidence, "verification", "batch_run_id"),
                    "task_contract_digest": _section_value(evidence, "contract", "task_contract_digest"),
                    "acceptance_contract_digest": _section_value(evidence, "contract", "acceptance_contract_digest"),
                    "change_set_digest": _section_value(evidence, "source_repo", "change_set_digest"),
                },
                prefix="dogfood_verification_receipt",
            )
        )
    elif role == "self_check_receipt":
        failures.extend(
            _receipt_binding_failures(
                payload,
                expected={
                    "status": "passed",
                    "trust": "yes",
                    "launch_id": _section_value(evidence, "bootstrap", "self_check_launch_id"),
                    "installed_wheel_sha256": _section_value(evidence, "candidate", "wheel_sha256"),
                    "source_repo_identity_digest": _section_value(evidence, "source_repo", "repo_identity_digest"),
                },
                prefix="dogfood_self_check_receipt",
            )
        )
    return failures, payload


def _verify_wheel(path: Path, *, role: str, expected_version: str) -> list[str]:
    failures: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
            record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
            entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
            if len(metadata_names) != 1 or len(wheel_names) != 1 or len(record_names) != 1:
                return [f"dogfood_{role}_wheel_structure_invalid"]
            if archive.getinfo(metadata_names[0]).file_size > 256 * 1024:
                return [f"dogfood_{role}_wheel_metadata_too_large"]
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
            package = str(metadata.get("Name") or "").strip().replace("_", "-").casefold()
            version = str(metadata.get("Version") or "").strip()
            if package != "visual-agent":
                failures.append(f"dogfood_{role}_wheel_package_mismatch")
            if expected_version and version != expected_version:
                failures.append(f"dogfood_{role}_wheel_version_mismatch")
            entrypoint_ok = False
            for name in entry_names:
                if archive.getinfo(name).file_size > 256 * 1024:
                    continue
                parser = ConfigParser()
                parser.read_string(archive.read(name).decode("utf-8"))
                if parser.has_section("console_scripts") and parser.get("console_scripts", "pacer", fallback="").strip() == "visual_agent.cli:main":
                    entrypoint_ok = True
                    break
            if not entrypoint_ok:
                failures.append(f"dogfood_{role}_wheel_entrypoint_missing")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, ValueError):
        return [f"dogfood_{role}_not_wheel"]
    return failures


def _receipt_binding_failures(
    payload: dict[str, Any],
    *,
    expected: Mapping[str, Any],
    prefix: str,
) -> list[str]:
    failures: list[str] = []
    for key, expected_value in expected.items():
        actual = str(payload.get(key) or "").strip()
        wanted = str(expected_value or "").strip()
        if not actual:
            failures.append(f"{prefix}_{key}_missing")
        elif not wanted or actual != wanted:
            failures.append(f"{prefix}_{key}_mismatch")
    return failures


def _verify_cross_artifact_bindings(payloads: Mapping[str, dict[str, Any]]) -> list[str]:
    task_contract = payloads.get("task_contract")
    acceptance_contract = payloads.get("acceptance_contract")
    if not task_contract or not acceptance_contract:
        return []
    embedded = task_contract.get("acceptance_contract")
    if not isinstance(embedded, dict) or embedded != acceptance_contract:
        return ["dogfood_task_acceptance_contract_mismatch"]
    return []


def _section_value(evidence: Mapping[str, Any], section: str, key: str) -> Any:
    value = evidence.get(section)
    return value.get(key) if isinstance(value, Mapping) else None


def _artifact_label(path: Path, roots: list[Path]) -> str:
    for index, root in enumerate(roots):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        prefix = "repo" if index == 0 else f"artifact-root-{index}"
        return f"{prefix}/{relative.as_posix()}"
    return path.name


def _path_uses_symlink(path: Path, roots: list[Path]) -> bool:
    absolute = path.absolute()
    for root in roots:
        try:
            relative = absolute.relative_to(root.absolute())
        except ValueError:
            continue
        current = root.absolute()
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return True
        return False
    return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _protected_repository_path(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("dogfood evidence path must stay inside the repository")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("dogfood evidence path cannot use symbolic links")
    try:
        current.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError("dogfood evidence path must stay inside the repository") from exc
    return current


def _normalize_repo_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if (
        not path
        or path.startswith("/")
        or _DRIVE_PATH.match(path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        return ""
    return path


def _is_pacer_product_path(path: str) -> bool:
    return path == "pyproject.toml" or path.startswith("src/visual_agent/")
