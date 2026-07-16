"""Fail-closed release-matrix orchestration primitives.

The runner callbacks are injected so this module never starts a process itself.
A release manifest is content-addressed before execution, all runners are
preflighted, and the first result that is failed or merely unstable stops the
matrix.  A retried pass is useful diagnostic evidence but is not a clean release.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from .dogfood_evidence import assess_dogfood_evidence
from .managed_state import ManagedState, managed_run_from_dict


RELEASE_GATE_SCHEMA_VERSION = 1
MAX_RELEASE_MANIFEST_BYTES = 2 * 1024 * 1024
REQUIRED_RELEASE_SCENARIOS = frozenset(
    {"implementation", "test", "documentation", "read_only", "fault_recovery"}
)
RELEASE_CASE_KINDS = frozenset({"deterministic", "managed_sample", "dogfood"})
ReleaseRunner = Callable[[], Mapping[str, Any]]


def release_manifest_digest(manifest: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def assess_release_manifest_file(
    manifest_path: str | Path,
    *,
    expected_manifest_digest: str,
) -> dict[str, Any]:
    supplied_path = Path(manifest_path).expanduser()
    path = supplied_path.resolve()
    path_label = supplied_path.name or ".pacer/release.json"
    expected = str(expected_manifest_digest or "").strip().lower()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {
            "schema_version": RELEASE_GATE_SCHEMA_VERSION,
            "status": "failed",
            "passed": False,
            "manifest_path": path_label,
            "manifest_digest": "",
            "digest_locked": False,
            "reason_codes": ["release_manifest_unavailable"],
            "error_type": type(exc).__name__,
        }
    if len(raw) > MAX_RELEASE_MANIFEST_BYTES:
        return {
            "schema_version": RELEASE_GATE_SCHEMA_VERSION,
            "status": "failed",
            "passed": False,
            "manifest_path": path_label,
            "manifest_digest": "",
            "digest_locked": False,
            "reason_codes": ["release_manifest_too_large"],
        }
    try:
        manifest = json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=_reject_nonfinite_json_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            "schema_version": RELEASE_GATE_SCHEMA_VERSION,
            "status": "failed",
            "passed": False,
            "manifest_path": path_label,
            "manifest_digest": "",
            "digest_locked": False,
            "reason_codes": ["release_manifest_not_json"],
            "error_type": type(exc).__name__,
        }
    if not isinstance(manifest, dict):
        return {
            "schema_version": RELEASE_GATE_SCHEMA_VERSION,
            "status": "failed",
            "passed": False,
            "manifest_path": path_label,
            "manifest_digest": "",
            "digest_locked": False,
            "reason_codes": ["release_manifest_not_object"],
        }
    actual = release_manifest_digest(manifest)
    digest_locked = bool(expected) and hmac.compare_digest(expected, actual)
    validation = validate_release_manifest(manifest)
    reasons = list(validation.get("reason_codes") or [])
    if not digest_locked:
        reasons.append(
            "release_manifest_digest_missing"
            if not expected
            else "release_manifest_digest_mismatch"
        )
    reasons = list(dict.fromkeys(reasons))
    passed = not reasons
    return {
        "schema_version": RELEASE_GATE_SCHEMA_VERSION,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "manifest_path": path_label,
        "manifest_digest": actual,
        "digest_locked": digest_locked,
        "reason_codes": reasons,
        "validation": validation,
    }


def validate_release_manifest(value: Any) -> dict[str, Any]:
    manifest = value if isinstance(value, dict) else {}
    reasons: list[str] = []
    if not manifest:
        return {
            "schema_version": RELEASE_GATE_SCHEMA_VERSION,
            "passed": False,
            "reason_codes": ["release_manifest_missing"],
        }
    if manifest.get("schema_version") != RELEASE_GATE_SCHEMA_VERSION:
        reasons.append("release_manifest_schema_unsupported")
    release_attestation_key_id = str(
        manifest.get("release_attestation_key_id") or ""
    ).strip()
    if not release_attestation_key_id:
        reasons.append("release_manifest_attestation_key_missing")

    raw_repositories = manifest.get("repositories") if isinstance(manifest.get("repositories"), list) else []
    repositories = _unique_strings(raw_repositories)
    scenarios = set(_unique_strings(manifest.get("scenarios")))
    if len(repositories) != len(raw_repositories):
        reasons.append("release_manifest_repository_duplicate_or_invalid")
    if len(repositories) < 3:
        reasons.append("release_manifest_requires_three_repositories")
    missing_scenarios = sorted(REQUIRED_RELEASE_SCENARIOS - scenarios)
    if missing_scenarios:
        reasons.append("release_manifest_scenarios_incomplete")
    performance_policy, performance_reasons = _normalize_performance_policy(
        manifest.get("performance_policy")
    )
    reasons.extend(performance_reasons)
    repository_roots = manifest.get("repository_roots")
    if not isinstance(repository_roots, dict):
        reasons.append("release_manifest_repository_roots_missing")
        repository_roots = {}
    normalized_roots: dict[str, str] = {}
    for repository in repositories:
        relative = _safe_relative_path(repository_roots.get(repository))
        if not relative:
            reasons.append("release_manifest_repository_root_invalid")
        else:
            normalized_roots[repository] = relative
    if len(set(normalized_roots.values())) != len(normalized_roots):
        reasons.append("release_manifest_repository_root_duplicate")

    cases = manifest.get("cases") if isinstance(manifest.get("cases"), list) else []
    if not cases:
        reasons.append("release_manifest_cases_missing")
    ids: set[str] = set()
    managed_pairs: set[tuple[str, str]] = set()
    duplicate_pairs: set[tuple[str, str]] = set()
    deterministic_count = 0
    dogfood_count = 0
    last_rank = -1
    ranks = {"deterministic": 0, "managed_sample": 1, "dogfood": 2}
    for item in cases:
        if not isinstance(item, dict):
            reasons.append("release_manifest_case_invalid")
            continue
        case_id = str(item.get("case_id") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if not case_id:
            reasons.append("release_manifest_case_id_missing")
        elif case_id in ids:
            reasons.append("release_manifest_case_id_duplicate")
        else:
            ids.add(case_id)
        if kind not in RELEASE_CASE_KINDS:
            reasons.append("release_manifest_case_kind_invalid")
            continue
        rank = ranks[kind]
        if rank < last_rank:
            reasons.append("release_manifest_case_order_invalid")
        last_rank = max(last_rank, rank)
        if kind == "deterministic":
            deterministic_count += 1
        elif kind == "dogfood":
            dogfood_count += 1
            if not str(item.get("attestation_key_id") or "").strip():
                reasons.append("release_manifest_dogfood_attestation_key_missing")
            if not _safe_relative_path(item.get("repository_root")):
                reasons.append("release_manifest_dogfood_repository_root_invalid")
            minimum_score = _positive_int(item.get("minimum_score"))
            if minimum_score is None or not 95 <= minimum_score <= 100:
                reasons.append("release_manifest_dogfood_score_invalid")
        else:
            repository = str(item.get("repository") or "").strip()
            scenario = str(item.get("scenario") or "").strip()
            if repository not in repositories:
                reasons.append("release_manifest_case_repository_invalid")
            if scenario not in REQUIRED_RELEASE_SCENARIOS:
                reasons.append("release_manifest_case_scenario_invalid")
            pair = (repository, scenario)
            if pair in managed_pairs:
                duplicate_pairs.add(pair)
            managed_pairs.add(pair)
    if deterministic_count < 1:
        reasons.append("release_manifest_deterministic_gate_missing")
    required_streak = _positive_int(manifest.get("required_clean_dogfood_streak"))
    if required_streak is None or required_streak < 3:
        reasons.append("release_manifest_dogfood_streak_invalid")
        required_streak = 3
    if dogfood_count < required_streak:
        reasons.append("release_manifest_dogfood_cases_incomplete")
    if duplicate_pairs:
        reasons.append("release_manifest_managed_case_duplicate")
    if len(repositories) >= 3:
        required_pairs = {
            (repository, scenario)
            for repository in repositories
            for scenario in REQUIRED_RELEASE_SCENARIOS
        }
        if required_pairs - managed_pairs:
            reasons.append("release_manifest_managed_matrix_incomplete")
    return {
        "schema_version": RELEASE_GATE_SCHEMA_VERSION,
        "passed": not reasons,
        "reason_codes": list(dict.fromkeys(reasons)),
        "case_ids": [str(item.get("case_id") or "") for item in cases if isinstance(item, dict)],
        "repositories": repositories,
        "repository_roots": normalized_roots,
        "scenarios": sorted(scenarios),
        "required_clean_dogfood_streak": required_streak,
        "release_attestation_key_id": release_attestation_key_id,
        "performance_policy": performance_policy,
    }


def assess_release_case(
    case: Mapping[str, Any],
    payload: Any,
    *,
    dogfood_attestation_keys: Mapping[str, str | bytes] | None = None,
    dogfood_artifact_roots: tuple[str | Path, ...] = (),
    performance_limits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    case_id = str(case.get("case_id") or "").strip()
    kind = str(case.get("kind") or "").strip()
    result = payload if isinstance(payload, Mapping) else {}
    failures: list[str] = []
    unstable: list[str] = []
    dogfood_assessment: dict[str, Any] = {}
    managed_summary: dict[str, Any] = {}
    status = str(result.get("status") or "").strip().lower()
    if status != "passed":
        failures.append("release_case_not_passed")
    if bool(result.get("timeout")) or status == "timeout":
        failures.append("release_case_timeout")
    http_status = result.get("http_status")
    if isinstance(http_status, int) and 500 <= http_status <= 599:
        failures.append("release_case_http_5xx")
    warnings = result.get("warnings")
    if not isinstance(warnings, list):
        failures.append("release_case_warnings_missing")
    elif warnings:
        failures.append("release_case_warning_present")
    retry_count = result.get("retry_count")
    if not isinstance(retry_count, int) or isinstance(retry_count, bool) or retry_count < 0:
        failures.append("release_case_retry_count_invalid")
    elif retry_count:
        unstable.append("release_case_retried")
    performance, performance_failures = _assess_case_performance(
        result,
        limits=performance_limits,
    )
    failures.extend(performance_failures)

    if kind == "deterministic":
        exit_code = result.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code != 0:
            failures.append("release_case_exit_code_untrusted")
    elif kind == "managed_sample":
        if str(result.get("trust") or "") != "yes":
            failures.append("release_case_trust_failed")
        if result.get("scan_complete") is not True:
            failures.append("release_case_scan_incomplete")
        _require_zero_result(
            result,
            "evidence_rejections",
            "release_case_evidence_rejections_invalid",
            "release_case_evidence_rejected",
            failures,
        )
        try:
            managed_record = managed_run_from_dict(result.get("managed_state"))
        except (TypeError, ValueError) as exc:
            failures.append("release_case_managed_state_invalid")
            managed_summary = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        else:
            managed_summary = {
                "run_id": managed_record.run_id,
                "state": managed_record.state.value,
                "revision": managed_record.revision,
                "idempotency_key": managed_record.idempotency_key,
            }
            if not managed_record.terminal or managed_record.state != ManagedState.SUCCEEDED:
                failures.append("release_case_managed_state_not_succeeded")
            runtime = (
                result.get("managed_runtime")
                if isinstance(result.get("managed_runtime"), Mapping)
                else {}
            )
            if runtime.get("transition_valid") is not True:
                failures.append("release_case_managed_transition_unverified")
            if str(runtime.get("idempotency_key") or "") != managed_record.idempotency_key:
                failures.append("release_case_managed_idempotency_mismatch")
            if str(runtime.get("budget_status") or "") != "within_budget":
                failures.append("release_case_managed_budget_unverified")
        _require_zero_result(
            result,
            "evidence_resubmissions",
            "release_case_evidence_resubmissions_invalid",
            "release_case_evidence_resubmitted",
            failures,
        )
    elif kind == "dogfood":
        raw_evidence = result.get("dogfood_evidence")
        standard_provenance = (
            result.get("dogfood_provenance")
            if isinstance(result.get("dogfood_provenance"), Mapping)
            else {}
        )
        try:
            dogfood_assessment = assess_dogfood_evidence(
                raw_evidence,
                repo_root=(str(case.get("repository_root") or "") or None),
                artifact_roots=dogfood_artifact_roots,
                require_artifacts=True,
                attestation_keys=dogfood_attestation_keys,
                standard_provenance=standard_provenance,
                target_score=int(case.get("minimum_score") or 95),
            )
        except (OSError, TypeError, ValueError) as exc:
            dogfood_assessment = {
                "status": "failed",
                "passed": False,
                "pacer_on_pacer": False,
                "reason_codes": ["dogfood_evidence_assessment_error"],
                "error_type": type(exc).__name__,
            }
        if not dogfood_assessment.get("passed") or not dogfood_assessment.get(
            "pacer_on_pacer"
        ):
            failures.append("release_case_dogfood_unverified")
        attestation_summary = (
            dogfood_assessment.get("attestation")
            if isinstance(dogfood_assessment.get("attestation"), dict)
            else {}
        )
        if str(attestation_summary.get("key_id") or "") != str(
            case.get("attestation_key_id") or ""
        ):
            failures.append("release_case_dogfood_attestation_key_mismatch")
        if str(attestation_summary.get("status") or "") != "verified":
            failures.append("release_case_dogfood_attestation_unverified")
        if not _is_sha256(str(dogfood_assessment.get("evidence_digest") or "")):
            failures.append("release_case_dogfood_digest_invalid")
        generation = (
            dogfood_assessment.get("generation")
            if isinstance(dogfood_assessment.get("generation"), dict)
            else {}
        )
        if not _is_sha256(str(generation.get("candidate_wheel_sha256") or "")):
            failures.append("release_case_dogfood_artifact_digest_invalid")
        quality = (
            dogfood_assessment.get("quality")
            if isinstance(dogfood_assessment.get("quality"), Mapping)
            else {}
        )
        if quality.get("meets_target") is not True:
            failures.append("release_case_dogfood_quality_below_target")
    else:
        failures.append("release_case_kind_invalid")

    failures = list(dict.fromkeys(failures))
    unstable = list(dict.fromkeys(unstable))
    clean = not failures and not unstable
    assessment_status = "passed" if clean else "failed" if failures else "unstable"
    assessment = {
        "schema_version": RELEASE_GATE_SCHEMA_VERSION,
        "case_id": case_id,
        "kind": kind,
        "status": assessment_status,
        "clean": clean,
        "reason_codes": failures + unstable,
        "reported_status": status,
        "performance": performance,
    }
    if kind == "dogfood":
        assessment["dogfood_status"] = str(dogfood_assessment.get("status") or "")
        assessment["evidence_digest"] = str(
            dogfood_assessment.get("evidence_digest") or ""
        )
        assessment["generation"] = (
            dict(dogfood_assessment["generation"])
            if isinstance(dogfood_assessment.get("generation"), dict)
            else {}
        )
        assessment["dogfood_reason_codes"] = list(
            dogfood_assessment.get("reason_codes") or []
        )
        assessment["attestation"] = (
            dict(dogfood_assessment["attestation"])
            if isinstance(dogfood_assessment.get("attestation"), dict)
            else {}
        )
        assessment["provenance"] = (
            dict(dogfood_assessment["provenance"])
            if isinstance(dogfood_assessment.get("provenance"), dict)
            else {}
        )
        assessment["quality"] = (
            dict(dogfood_assessment["quality"])
            if isinstance(dogfood_assessment.get("quality"), dict)
            else {}
        )
    elif kind == "managed_sample":
        assessment["managed_state"] = managed_summary
    return assessment


def run_release_matrix(
    manifest: Mapping[str, Any],
    *,
    expected_manifest_digest: str,
    runners: Mapping[str, ReleaseRunner],
    dogfood_attestation_keys: Mapping[str, str | bytes] | None = None,
    dogfood_artifact_roots: Mapping[str, tuple[str | Path, ...]] | None = None,
    release_root: str | Path = ".",
) -> dict[str, Any]:
    try:
        actual_digest = release_manifest_digest(manifest)
    except (TypeError, ValueError):
        return _blocked_matrix(
            manifest_digest="",
            reasons=["release_manifest_not_json"],
        )
    expected_digest = str(expected_manifest_digest or "").strip().lower()
    if not _is_sha256(expected_digest) or not hmac.compare_digest(actual_digest, expected_digest):
        return _blocked_matrix(
            manifest_digest=actual_digest,
            reasons=["release_manifest_digest_mismatch"],
        )
    manifest_assessment = validate_release_manifest(manifest)
    if not manifest_assessment["passed"]:
        return _blocked_matrix(
            manifest_digest=actual_digest,
            reasons=list(manifest_assessment["reason_codes"]),
        )
    cases = [item for item in manifest.get("cases", []) if isinstance(item, dict)]
    missing_runners = [
        str(item.get("case_id") or "")
        for item in cases
        if not callable(runners.get(str(item.get("case_id") or "")))
    ]
    if missing_runners:
        return _blocked_matrix(
            manifest_digest=actual_digest,
            reasons=["release_case_runner_missing"],
            missing_runners=missing_runners,
        )
    trusted_release_root = Path(release_root).expanduser().resolve()
    dogfood_repo_roots: dict[str, Path] = {}
    for case in cases:
        if str(case.get("kind") or "") != "dogfood":
            continue
        case_id = str(case.get("case_id") or "")
        relative_root = _safe_relative_path(case.get("repository_root"))
        resolved_root = (trusted_release_root / relative_root).resolve() if relative_root else None
        if resolved_root is None or not _is_within(resolved_root, trusted_release_root):
            return _blocked_matrix(
                manifest_digest=actual_digest,
                reasons=["release_case_repository_root_outside_release_root"],
            )
        dogfood_repo_roots[case_id] = resolved_root

    results: list[dict[str, Any]] = []
    stopped_at = ""
    performance_summary: dict[str, Any] = {}
    performance_policy = manifest_assessment.get("performance_policy")
    performance_policy = performance_policy if isinstance(performance_policy, dict) else {}
    for index, case in enumerate(cases):
        case_id = str(case.get("case_id") or "")
        if str(case.get("kind") or "") == "dogfood" and not performance_summary:
            performance_summary = evaluate_release_performance(
                results,
                policy=performance_policy,
            )
            if not performance_summary["passed"]:
                stopped_at = "managed_performance_aggregate"
                for remaining in cases[index:]:
                    results.append(
                        {
                            "schema_version": RELEASE_GATE_SCHEMA_VERSION,
                            "case_id": str(remaining.get("case_id") or ""),
                            "kind": str(remaining.get("kind") or ""),
                            "status": "skipped",
                            "clean": False,
                            "reason_codes": ["release_matrix_stopped_after_failure"],
                        }
                    )
                break
        runner = runners[case_id]
        try:
            payload = runner()
        except Exception as exc:  # noqa: BLE001 - runner failures are release evidence
            assessment = {
                "schema_version": RELEASE_GATE_SCHEMA_VERSION,
                "case_id": case_id,
                "kind": str(case.get("kind") or ""),
                "status": "failed",
                "clean": False,
                "reason_codes": ["release_case_runner_error"],
                "error_type": type(exc).__name__,
            }
        else:
            roots_by_case = dogfood_artifact_roots if isinstance(dogfood_artifact_roots, Mapping) else {}
            assessment_case = dict(case)
            if str(case.get("kind") or "") == "dogfood":
                assessment_case["repository_root"] = str(dogfood_repo_roots[case_id])
            assessment = assess_release_case(
                assessment_case,
                payload,
                dogfood_attestation_keys=dogfood_attestation_keys,
                dogfood_artifact_roots=tuple(roots_by_case.get(case_id) or ()),
                performance_limits=(
                    performance_policy.get(str(case.get("kind") or ""))
                    if isinstance(performance_policy.get(str(case.get("kind") or "")), dict)
                    else None
                ),
            )
        results.append(assessment)
        if not assessment["clean"]:
            stopped_at = case_id
            for remaining in cases[index + 1 :]:
                results.append(
                    {
                        "schema_version": RELEASE_GATE_SCHEMA_VERSION,
                        "case_id": str(remaining.get("case_id") or ""),
                        "kind": str(remaining.get("kind") or ""),
                        "status": "skipped",
                        "clean": False,
                        "reason_codes": ["release_matrix_stopped_after_failure"],
                    }
                )
            break

    if not performance_summary:
        performance_summary = evaluate_release_performance(results, policy=performance_policy)
    dogfood_results = [item for item in results if item.get("kind") == "dogfood"]
    required_streak = int(manifest_assessment["required_clean_dogfood_streak"])
    streak = evaluate_clean_streak(dogfood_results, required=required_streak)
    if not stopped_at and not streak["passed"]:
        stopped_at = "dogfood_clean_streak"
    passed = not stopped_at and len(results) == len(cases) and all(item["clean"] for item in results)
    matrix_reasons: list[str] = []
    if not passed:
        matrix_reasons.append("release_matrix_not_clean")
        stopped = next((item for item in results if item.get("case_id") == stopped_at), None)
        if isinstance(stopped, dict):
            matrix_reasons.extend(str(item) for item in stopped.get("reason_codes") or [])
        if streak.get("duplicate_evidence"):
            matrix_reasons.append("release_dogfood_evidence_duplicate")
        if streak.get("duplicate_run_identity"):
            matrix_reasons.append("release_dogfood_run_identity_duplicate")
        if streak.get("candidate_drift"):
            matrix_reasons.append("release_dogfood_candidate_drift")
        matrix_reasons.extend(str(item) for item in performance_summary.get("reason_codes") or [])
    return {
        "schema_version": RELEASE_GATE_SCHEMA_VERSION,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "clean": passed,
        "manifest_digest": actual_digest,
        "executed_count": sum(1 for item in results if item["status"] != "skipped"),
        "total_count": len(cases),
        "stopped_at": stopped_at,
        "reason_codes": list(dict.fromkeys(matrix_reasons)),
        "dogfood_streak": streak,
        "performance": performance_summary,
        "results": results,
    }


def evaluate_clean_streak(samples: Sequence[Mapping[str, Any]], *, required: int = 3) -> dict[str, Any]:
    required_count = int(required)
    if required_count < 1:
        raise ValueError("required clean streak must be at least 1")
    current = 0
    longest = 0
    resets = 0
    duplicates = 0
    duplicate_runs = 0
    candidate_drift = 0
    seen_evidence: set[str] = set()
    seen_runs: set[str] = set()
    immutable_candidate = ""
    for sample in samples:
        clean = bool(sample.get("clean")) and str(sample.get("status") or "") == "passed"
        evidence_digest = str(sample.get("evidence_digest") or "").strip()
        generation = sample.get("generation") if isinstance(sample.get("generation"), Mapping) else {}
        candidate_digest = str(generation.get("candidate_wheel_sha256") or "").strip()
        provenance = sample.get("provenance") if isinstance(sample.get("provenance"), Mapping) else {}
        run_identity = str(
            sample.get("run_identity_digest")
            or provenance.get("run_identity_digest")
            or ""
        ).strip()
        if evidence_digest and evidence_digest in seen_evidence:
            clean = False
            duplicates += 1
        if run_identity and run_identity in seen_runs:
            clean = False
            duplicate_runs += 1
        if candidate_digest and not immutable_candidate:
            immutable_candidate = candidate_digest
        elif candidate_digest and candidate_digest != immutable_candidate:
            clean = False
            candidate_drift += 1
        if evidence_digest:
            seen_evidence.add(evidence_digest)
        if run_identity:
            seen_runs.add(run_identity)
        if clean:
            current += 1
            longest = max(longest, current)
        else:
            if current:
                resets += 1
            current = 0
    return {
        "schema_version": RELEASE_GATE_SCHEMA_VERSION,
        "passed": current >= required_count,
        "required": required_count,
        "current": current,
        "longest": longest,
        "resets": resets,
        "duplicate_evidence": duplicates,
        "duplicate_run_identity": duplicate_runs,
        "candidate_drift": candidate_drift,
        "immutable_candidate_sha256": immutable_candidate,
        "sample_count": len(samples),
    }


def evaluate_release_performance(
    samples: Sequence[Mapping[str, Any]],
    *,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    managed = [
        item
        for item in samples
        if item.get("kind") == "managed_sample" and item.get("status") != "skipped"
    ]
    aggregate = policy.get("managed_aggregate")
    aggregate = aggregate if isinstance(aggregate, Mapping) else {}
    durations = [
        float((item.get("performance") or {}).get("duration_seconds"))
        for item in managed
        if isinstance(item.get("performance"), Mapping)
        and _positive_number((item.get("performance") or {}).get("duration_seconds")) is not None
    ]
    tokens = [
        int((item.get("performance") or {}).get("total_tokens"))
        for item in managed
        if isinstance(item.get("performance"), Mapping)
        and _nonnegative_int((item.get("performance") or {}).get("total_tokens")) is not None
    ]
    reasons: list[str] = []
    if managed and (len(durations) != len(managed) or len(tokens) != len(managed)):
        reasons.append("release_performance_metrics_incomplete")
    mean_duration = sum(durations) / len(durations) if durations else None
    mean_tokens = sum(tokens) / len(tokens) if tokens else None
    p95_duration = _nearest_rank_percentile(durations, 0.95) if durations else None
    max_mean_duration = _positive_number(aggregate.get("max_mean_duration_seconds"))
    max_p95_duration = _positive_number(aggregate.get("max_p95_duration_seconds"))
    max_mean_tokens = _positive_number(aggregate.get("max_mean_total_tokens"))
    if mean_duration is not None and max_mean_duration is not None and mean_duration > max_mean_duration:
        reasons.append("release_performance_mean_duration_exceeded")
    if p95_duration is not None and max_p95_duration is not None and p95_duration > max_p95_duration:
        reasons.append("release_performance_p95_duration_exceeded")
    if mean_tokens is not None and max_mean_tokens is not None and mean_tokens > max_mean_tokens:
        reasons.append("release_performance_mean_tokens_exceeded")
    return {
        "schema_version": RELEASE_GATE_SCHEMA_VERSION,
        "passed": not reasons,
        "status": "passed" if not reasons else "failed",
        "managed_sample_count": len(managed),
        "mean_duration_seconds": round(mean_duration, 3) if mean_duration is not None else None,
        "p95_duration_seconds": round(p95_duration, 3) if p95_duration is not None else None,
        "mean_total_tokens": round(mean_tokens, 3) if mean_tokens is not None else None,
        "reason_codes": list(dict.fromkeys(reasons)),
    }


def _assess_case_performance(
    result: Mapping[str, Any],
    *,
    limits: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(limits, Mapping):
        return {"status": "not_required"}, []
    metrics = result.get("metrics") if isinstance(result.get("metrics"), Mapping) else {}
    duration = _positive_number(metrics.get("duration_seconds"))
    total_tokens = _nonnegative_int(metrics.get("total_tokens"))
    failures: list[str] = []
    if duration is None:
        failures.append("release_case_duration_missing_or_invalid")
    if total_tokens is None:
        failures.append("release_case_tokens_missing_or_invalid")
    max_duration = _positive_number(limits.get("max_duration_seconds"))
    max_tokens = _positive_number(limits.get("max_total_tokens"))
    if duration is not None and max_duration is not None and duration > max_duration:
        failures.append("release_case_duration_budget_exceeded")
    if total_tokens is not None and max_tokens is not None and total_tokens > max_tokens:
        failures.append("release_case_token_budget_exceeded")
    return {
        "status": "passed" if not failures else "failed",
        "duration_seconds": duration,
        "total_tokens": total_tokens,
        "limits": {
            "max_duration_seconds": max_duration,
            "max_total_tokens": int(max_tokens) if max_tokens is not None else None,
        },
    }, failures


def _normalize_performance_policy(value: Any) -> tuple[dict[str, Any], list[str]]:
    policy = value if isinstance(value, Mapping) else {}
    reasons: list[str] = []
    normalized: dict[str, Any] = {}
    required_fields = {
        "managed_sample": ("max_duration_seconds", "max_total_tokens"),
        "managed_aggregate": (
            "max_mean_duration_seconds",
            "max_p95_duration_seconds",
            "max_mean_total_tokens",
        ),
        "dogfood": ("max_duration_seconds", "max_total_tokens"),
    }
    for section, fields in required_fields.items():
        raw = policy.get(section) if isinstance(policy.get(section), Mapping) else {}
        clean: dict[str, float | int] = {}
        for field in fields:
            number = _positive_number(raw.get(field))
            if number is None:
                reasons.append("release_manifest_performance_policy_invalid")
                continue
            clean[field] = int(number) if number.is_integer() else number
        normalized[section] = clean
    return normalized, list(dict.fromkeys(reasons))


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nearest_rank_percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(float(item) for item in values)
    rank = max(1, math.ceil(float(percentile) * len(ordered)))
    return ordered[rank - 1]


def _blocked_matrix(
    *,
    manifest_digest: str,
    reasons: list[str],
    missing_runners: list[str] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": RELEASE_GATE_SCHEMA_VERSION,
        "status": "failed",
        "passed": False,
        "clean": False,
        "manifest_digest": manifest_digest,
        "executed_count": 0,
        "total_count": 0,
        "stopped_at": "preflight",
        "reason_codes": list(dict.fromkeys(reasons)),
        "dogfood_streak": evaluate_clean_streak([], required=3),
        "results": [],
    }
    if missing_runners:
        payload["missing_runners"] = list(missing_runners)
    return payload


def _require_zero_result(
    result: Mapping[str, Any],
    key: str,
    invalid_code: str,
    nonzero_code: str,
    failures: list[str],
) -> None:
    value = result.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        failures.append(invalid_code)
    elif value:
        failures.append(nonzero_code)


def _unique_strings(value: Any) -> list[str]:
    rows = value if isinstance(value, list) else []
    return list(dict.fromkeys(str(item).strip() for item in rows if str(item).strip()))


def _positive_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None
    return value


def _safe_relative_path(value: Any) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        return ""
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".."} for part in path.parts):
        return ""
    if path.parts and path.parts[0].endswith(":"):
        return ""
    return path.as_posix()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reject_nonfinite_json_number(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")
