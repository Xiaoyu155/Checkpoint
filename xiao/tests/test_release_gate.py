from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

import pytest
import visual_agent.release_gate as release_gate

from visual_agent.managed_state import new_managed_run, transition_managed_run
from visual_agent.release_gate import (
    REQUIRED_RELEASE_SCENARIOS,
    assess_release_manifest_file,
    assess_release_case,
    evaluate_clean_streak,
    release_manifest_digest,
    run_release_matrix,
    validate_release_manifest,
)


def _manifest() -> dict:
    repositories = ["repo-a", "repo-b", "repo-c"]
    cases = [{"case_id": "deterministic", "kind": "deterministic"}]
    for repository in repositories:
        for scenario in sorted(REQUIRED_RELEASE_SCENARIOS):
            cases.append(
                {
                    "case_id": f"{repository}-{scenario}",
                    "kind": "managed_sample",
                    "repository": repository,
                    "scenario": scenario,
                }
            )
    cases.extend(
        {
            "case_id": f"dogfood-{index}",
            "kind": "dogfood",
            "repository_root": ".",
            "attestation_key_id": "test-release-key",
            "minimum_score": 100,
        }
        for index in range(1, 4)
    )
    return {
        "schema_version": 1,
        "repositories": repositories,
        "repository_roots": {repository: repository for repository in repositories},
        "scenarios": sorted(REQUIRED_RELEASE_SCENARIOS),
        "required_clean_dogfood_streak": 3,
        "release_attestation_key_id": "test-release-key",
        "performance_policy": {
            "managed_sample": {
                "max_duration_seconds": 300,
                "max_total_tokens": 250_000,
            },
            "managed_aggregate": {
                "max_mean_duration_seconds": 180,
                "max_p95_duration_seconds": 300,
                "max_mean_total_tokens": 180_000,
            },
            "dogfood": {
                "max_duration_seconds": 900,
                "max_total_tokens": 600_000,
            },
        },
        "cases": cases,
    }


def _passed_payload(kind: str, *, case_id: str = "") -> dict:
    common = {"status": "passed", "warnings": [], "retry_count": 0}
    if kind == "deterministic":
        return {**common, "exit_code": 0}
    if kind == "managed_sample":
        pending = new_managed_run(
            run_id=f"run-{case_id or 'managed'}",
            idempotency_key=f"managed:{case_id or 'sample'}",
            now="t0",
        )
        running = transition_managed_run(
            pending,
            expected_revision=0,
            next_state="RUNNING",
            event="worker_started",
            attempt_id="attempt-1",
            now="t1",
        )
        verifying = transition_managed_run(
            running,
            expected_revision=1,
            next_state="VERIFYING",
            event="verification_started",
            attempt_id="attempt-1",
            now="t2",
        )
        succeeded = transition_managed_run(
            verifying,
            expected_revision=2,
            next_state="SUCCEEDED",
            event="verification_passed",
            attempt_id="attempt-1",
            now="t3",
        )
        return {
            **common,
            "metrics": {"duration_seconds": 100, "total_tokens": 100_000},
            "trust": "yes",
            "scan_complete": True,
            "evidence_rejections": 0,
            "evidence_resubmissions": 0,
            "managed_state": succeeded.to_dict(),
            "managed_runtime": {
                "transition_valid": True,
                "idempotency_key": succeeded.idempotency_key,
                "budget_status": "within_budget",
            },
        }
    return {
        **common,
        "metrics": {"duration_seconds": 400, "total_tokens": 300_000},
        "repo_root": ".",
        "dogfood_evidence": {"test_case_id": case_id},
    }


def _runners(manifest: dict, calls: dict[str, int] | None = None) -> dict[str, Callable[[], dict]]:
    runners = {}
    for case in manifest["cases"]:
        case_id = case["case_id"]
        kind = case["kind"]

        def run(case_id: str = case_id, kind: str = kind) -> dict:
            if calls is not None:
                calls[case_id] = calls.get(case_id, 0) + 1
            return _passed_payload(kind, case_id=case_id)

        runners[case_id] = run
    return runners


def test_release_manifest_requires_complete_three_by_five_matrix() -> None:
    manifest = _manifest()
    assert validate_release_manifest(manifest)["passed"] is True

    manifest["cases"] = manifest["cases"][:-4]
    assessment = validate_release_manifest(manifest)
    assert assessment["passed"] is False
    assert "release_manifest_managed_matrix_incomplete" in assessment["reason_codes"]
    assert "release_manifest_dogfood_cases_incomplete" in assessment["reason_codes"]


def test_release_manifest_rejects_repository_root_escape_and_unattested_dogfood() -> None:
    manifest = _manifest()
    manifest["repository_roots"]["repo-a"] = "../outside"
    manifest["cases"][-1].pop("attestation_key_id")

    assessment = validate_release_manifest(manifest)

    assert assessment["passed"] is False
    assert {
        "release_manifest_repository_root_invalid",
        "release_manifest_dogfood_attestation_key_missing",
    } <= set(assessment["reason_codes"])


def test_clean_release_runs_locked_manifest_in_order(monkeypatch) -> None:
    manifest = _manifest()
    calls: dict[str, int] = {}

    def assess(evidence, **_kwargs) -> dict:
        case_id = str((evidence or {}).get("test_case_id") or "")
        return {
            "status": "passed",
            "passed": True,
            "pacer_on_pacer": True,
            "evidence_digest": (case_id.encode().hex() + ("0" * 64))[:64],
            "generation": {
                "candidate_wheel_sha256": hashlib.sha256(b"immutable-candidate").hexdigest()
            },
            "provenance": {
                "run_identity_digest": hashlib.sha256(case_id.encode()).hexdigest()
            },
            "quality": {"score": 100, "target_score": 100, "meets_target": True},
            "attestation": {
                "status": "verified",
                "key_id": "test-release-key",
            },
            "reason_codes": [],
        }

    monkeypatch.setattr(release_gate, "assess_dogfood_evidence", assess)
    result = run_release_matrix(
        manifest,
        expected_manifest_digest=release_manifest_digest(manifest),
        runners=_runners(manifest, calls),
    )

    assert result["status"] == "passed"
    assert result["executed_count"] == len(manifest["cases"])
    assert result["dogfood_streak"]["current"] == 3
    assert all(value == 1 for value in calls.values())


def test_first_failure_stops_remaining_runners() -> None:
    manifest = _manifest()
    calls: dict[str, int] = {}
    runners = _runners(manifest, calls)
    failed_id = manifest["cases"][2]["case_id"]

    def fail() -> dict:
        calls[failed_id] = calls.get(failed_id, 0) + 1
        return {
            **_passed_payload("managed_sample"),
            "status": "timeout",
            "timeout": True,
        }

    runners[failed_id] = fail
    result = run_release_matrix(
        manifest,
        expected_manifest_digest=release_manifest_digest(manifest),
        runners=runners,
    )

    assert result["status"] == "failed"
    assert result["stopped_at"] == failed_id
    assert result["executed_count"] == 3
    assert result["results"][3]["status"] == "skipped"
    assert manifest["cases"][3]["case_id"] not in calls


def test_digest_mismatch_and_missing_runner_block_before_execution() -> None:
    manifest = _manifest()
    calls: dict[str, int] = {}
    runners = _runners(manifest, calls)
    mismatch = run_release_matrix(
        manifest,
        expected_manifest_digest="0" * 64,
        runners=runners,
    )
    assert mismatch["stopped_at"] == "preflight"
    assert mismatch["executed_count"] == 0
    assert not calls

    runners.pop(manifest["cases"][-1]["case_id"])
    missing = run_release_matrix(
        manifest,
        expected_manifest_digest=release_manifest_digest(manifest),
        runners=runners,
    )
    assert "release_case_runner_missing" in missing["reason_codes"]
    assert not calls


def test_release_manifest_file_requires_matching_digest(tmp_path) -> None:
    manifest = _manifest()
    path = tmp_path / "release.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    locked = assess_release_manifest_file(
        path,
        expected_manifest_digest=release_manifest_digest(manifest),
    )
    mismatched = assess_release_manifest_file(
        path,
        expected_manifest_digest="0" * 64,
    )

    assert locked["passed"] is True
    assert locked["digest_locked"] is True
    assert mismatched["passed"] is False
    assert "release_manifest_digest_mismatch" in mismatched["reason_codes"]

@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"http_status": 503}, "release_case_http_5xx"),
        ({"trust": "no"}, "release_case_trust_failed"),
        ({"scan_complete": False}, "release_case_scan_incomplete"),
        ({"evidence_rejections": 1}, "release_case_evidence_rejected"),
        ({"evidence_resubmissions": 1}, "release_case_evidence_resubmitted"),
    ],
)
def test_managed_release_case_rejects_untrusted_evidence(change: dict, reason: str) -> None:
    case = {"case_id": "managed", "kind": "managed_sample"}
    payload = {**_passed_payload("managed_sample"), **change}

    assessment = assess_release_case(case, payload)

    assert assessment["clean"] is False
    assert reason in assessment["reason_codes"]


def test_release_case_enforces_observed_duration_and_token_budgets() -> None:
    limits = {"max_duration_seconds": 300, "max_total_tokens": 250_000}
    missing = assess_release_case(
        {"case_id": "managed", "kind": "managed_sample"},
        _passed_payload("managed_sample") | {"metrics": {}},
        performance_limits=limits,
    )
    exceeded = assess_release_case(
        {"case_id": "managed", "kind": "managed_sample"},
        _passed_payload("managed_sample")
        | {"metrics": {"duration_seconds": 301, "total_tokens": 250_001}},
        performance_limits=limits,
    )

    assert {
        "release_case_duration_missing_or_invalid",
        "release_case_tokens_missing_or_invalid",
    } <= set(missing["reason_codes"])
    assert {
        "release_case_duration_budget_exceeded",
        "release_case_token_budget_exceeded",
    } <= set(exceeded["reason_codes"])


def test_managed_aggregate_budget_stops_before_dogfood_execution() -> None:
    manifest = _manifest()
    calls: dict[str, int] = {}
    runners = _runners(manifest, calls)
    for case in manifest["cases"]:
        if case["kind"] != "managed_sample":
            continue
        case_id = case["case_id"]

        def slow(case_id: str = case_id) -> dict:
            calls[case_id] = calls.get(case_id, 0) + 1
            return _passed_payload("managed_sample") | {
                "metrics": {"duration_seconds": 200, "total_tokens": 100_000}
            }

        runners[case_id] = slow

    result = run_release_matrix(
        manifest,
        expected_manifest_digest=release_manifest_digest(manifest),
        runners=runners,
    )

    assert result["passed"] is False
    assert result["stopped_at"] == "managed_performance_aggregate"
    assert "release_performance_mean_duration_exceeded" in result["reason_codes"]
    assert result["performance"]["mean_duration_seconds"] == 200
    assert not any(case_id.startswith("dogfood-") for case_id in calls)


def test_managed_release_case_rejects_forged_state_history() -> None:
    payload = _passed_payload("managed_sample", case_id="forged")
    payload["managed_state"]["history"][2]["from_state"] = "PENDING"

    assessment = assess_release_case(
        {"case_id": "managed", "kind": "managed_sample"},
        payload,
    )

    assert assessment["clean"] is False
    assert "release_case_managed_state_invalid" in assessment["reason_codes"]


def test_retried_pass_is_unstable_and_resets_clean_streak() -> None:
    case = {"case_id": "managed", "kind": "managed_sample"}
    assessment = assess_release_case(
        case,
        {**_passed_payload("managed_sample"), "retry_count": 1},
    )
    assert assessment["status"] == "unstable"
    assert assessment["clean"] is False

    streak = evaluate_clean_streak(
        [
            {"status": "passed", "clean": True},
            {"status": "failed", "clean": False},
            {"status": "passed", "clean": True},
            {"status": "passed", "clean": True},
            {"status": "passed", "clean": True},
        ],
        required=3,
    )
    assert streak["passed"] is True
    assert streak["current"] == 3
    assert streak["resets"] == 1


def test_dogfood_case_rejects_self_report_without_raw_evidence() -> None:
    assessment = assess_release_case(
        {"case_id": "dogfood", "kind": "dogfood"},
        {
            "status": "passed",
            "warnings": [],
            "retry_count": 0,
            "passed": True,
            "pacer_on_pacer": True,
        },
    )

    assert assessment["clean"] is False
    assert "release_case_dogfood_unverified" in assessment["reason_codes"]


def test_clean_streak_rejects_duplicate_dogfood_evidence() -> None:
    streak = evaluate_clean_streak(
        [
            {"status": "passed", "clean": True, "evidence_digest": "a" * 64},
            {"status": "passed", "clean": True, "evidence_digest": "a" * 64},
            {"status": "passed", "clean": True, "evidence_digest": "b" * 64},
            {"status": "passed", "clean": True, "evidence_digest": "c" * 64},
        ],
        required=3,
    )

    assert streak["passed"] is False
    assert streak["duplicate_evidence"] == 1


def test_clean_streak_accepts_independent_runs_for_same_candidate_wheel() -> None:
    candidate = "f" * 64
    samples = [
        {
            "status": "passed",
            "clean": True,
            "evidence_digest": character * 64,
            "generation": {"candidate_wheel_sha256": candidate},
            "provenance": {"run_identity_digest": str(index) * 64},
        }
        for index, character in enumerate(("a", "b", "c"), start=1)
    ]

    streak = evaluate_clean_streak(samples, required=3)

    assert streak["passed"] is True
    assert streak["duplicate_evidence"] == 0
    assert streak["duplicate_run_identity"] == 0
    assert streak["candidate_drift"] == 0
    assert streak["immutable_candidate_sha256"] == candidate


def test_clean_streak_rejects_candidate_drift_and_duplicate_run_identity() -> None:
    duplicate_run = "d" * 64
    streak = evaluate_clean_streak(
        [
            {
                "status": "passed",
                "clean": True,
                "evidence_digest": "a" * 64,
                "generation": {"candidate_wheel_sha256": "f" * 64},
                "provenance": {"run_identity_digest": duplicate_run},
            },
            {
                "status": "passed",
                "clean": True,
                "evidence_digest": "b" * 64,
                "generation": {"candidate_wheel_sha256": "e" * 64},
                "provenance": {"run_identity_digest": duplicate_run},
            },
        ],
        required=2,
    )

    assert streak["passed"] is False
    assert streak["candidate_drift"] == 1
    assert streak["duplicate_run_identity"] == 1
