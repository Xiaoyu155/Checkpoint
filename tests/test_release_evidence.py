from __future__ import annotations

import hashlib
import json
from pathlib import Path

from visual_agent.release_evidence import (
    attest_release_evidence_bundle,
    load_release_evidence_bundle,
)
from visual_agent.release_gate import REQUIRED_RELEASE_SCENARIOS, release_manifest_digest


_KEY_ID = "release-test-key"
_KEY = b"release-test-secret"


def test_release_bundle_locks_order_files_and_external_attestation(tmp_path: Path) -> None:
    manifest = _manifest()
    bundle = _write_bundle(tmp_path, manifest)

    loaded = load_release_evidence_bundle(
        manifest=manifest,
        expected_manifest_digest=release_manifest_digest(manifest),
        evidence_root=tmp_path,
        bundle_path="release-evidence.json",
        attestation_keys={_KEY_ID: _KEY},
    )

    assert loaded["passed"] is True
    assert loaded["attestation_key_id"] == _KEY_ID
    assert [item["case_id"] for item in loaded["cases"]] == [
        item["case_id"] for item in manifest["cases"]
    ]
    assert len(loaded["bundle_digest"]) == 64
    assert bundle["attestation"]["bundle_digest"] == loaded["bundle_digest"]


def test_release_bundle_rejects_reordered_cases_even_when_resigned(tmp_path: Path) -> None:
    manifest = _manifest()
    bundle = _write_bundle(tmp_path, manifest)
    bundle["cases"][0], bundle["cases"][1] = bundle["cases"][1], bundle["cases"][0]
    bundle = attest_release_evidence_bundle(bundle, key_id=_KEY_ID, key=_KEY)
    (tmp_path / "release-evidence.json").write_text(json.dumps(bundle), encoding="utf-8")

    loaded = _load(tmp_path, manifest)

    assert loaded["passed"] is False
    assert "release_bundle_case_order_or_coverage_mismatch" in loaded["reason_codes"]


def test_release_bundle_rejects_result_tampering_and_path_escape(tmp_path: Path) -> None:
    manifest = _manifest()
    bundle = _write_bundle(tmp_path, manifest)
    first_path = tmp_path / bundle["cases"][0]["result_path"]
    first_path.write_text('{"status":"tampered"}', encoding="utf-8")

    tampered = _load(tmp_path, manifest)
    assert "release_bundle_result_digest_mismatch" in tampered["reason_codes"]

    bundle = _write_bundle(tmp_path, manifest)
    bundle["cases"][0]["result_path"] = "../outside.json"
    bundle = attest_release_evidence_bundle(bundle, key_id=_KEY_ID, key=_KEY)
    (tmp_path / "release-evidence.json").write_text(json.dumps(bundle), encoding="utf-8")
    escaped = _load(tmp_path, manifest)
    assert "release_bundle_result_path_invalid" in escaped["reason_codes"]


def test_release_bundle_requires_external_key_not_self_reported_signature(tmp_path: Path) -> None:
    manifest = _manifest()
    _write_bundle(tmp_path, manifest)

    unavailable = load_release_evidence_bundle(
        manifest=manifest,
        expected_manifest_digest=release_manifest_digest(manifest),
        evidence_root=tmp_path,
        bundle_path="release-evidence.json",
        attestation_keys={},
    )
    wrong = load_release_evidence_bundle(
        manifest=manifest,
        expected_manifest_digest=release_manifest_digest(manifest),
        evidence_root=tmp_path,
        bundle_path="release-evidence.json",
        attestation_keys={_KEY_ID: b"wrong"},
    )

    assert "release_bundle_attestation_key_unavailable" in unavailable["reason_codes"]
    assert "release_bundle_attestation_signature_mismatch" in wrong["reason_codes"]


def _load(root: Path, manifest: dict) -> dict:
    return load_release_evidence_bundle(
        manifest=manifest,
        expected_manifest_digest=release_manifest_digest(manifest),
        evidence_root=root,
        bundle_path="release-evidence.json",
        attestation_keys={_KEY_ID: _KEY},
    )


def _write_bundle(root: Path, manifest: dict) -> dict:
    entries = []
    results = root / "results"
    results.mkdir(exist_ok=True)
    for index, case in enumerate(manifest["cases"]):
        path = results / f"{index:02d}-{case['case_id']}.json"
        raw = json.dumps(
            {"schema_version": 1, "case_id": case["case_id"], "sample": index},
            sort_keys=True,
        ).encode("utf-8")
        path.write_bytes(raw)
        entries.append(
            {
                "case_id": case["case_id"],
                "result_path": path.relative_to(root).as_posix(),
                "result_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    bundle = attest_release_evidence_bundle(
        {
            "schema_version": 1,
            "manifest_digest": release_manifest_digest(manifest),
            "cases": entries,
        },
        key_id=_KEY_ID,
        key=_KEY,
    )
    (root / "release-evidence.json").write_text(json.dumps(bundle), encoding="utf-8")
    return bundle


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
            "attestation_key_id": _KEY_ID,
            "minimum_score": 100,
        }
        for index in range(1, 4)
    )
    return {
        "schema_version": 1,
        "repositories": repositories,
        "repository_roots": {item: item for item in repositories},
        "scenarios": sorted(REQUIRED_RELEASE_SCENARIOS),
        "required_clean_dogfood_streak": 3,
        "release_attestation_key_id": _KEY_ID,
        "performance_policy": {
            "managed_sample": {"max_duration_seconds": 300, "max_total_tokens": 250_000},
            "managed_aggregate": {
                "max_mean_duration_seconds": 180,
                "max_p95_duration_seconds": 300,
                "max_mean_total_tokens": 180_000,
            },
            "dogfood": {"max_duration_seconds": 900, "max_total_tokens": 600_000},
        },
        "cases": cases,
    }
