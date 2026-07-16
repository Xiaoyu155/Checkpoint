from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

from visual_agent.acceptance_contract import acceptance_contract_digest
from visual_agent.dogfood_evidence import (
    assess_dogfood_evidence,
    attest_dogfood_evidence,
    load_dogfood_evidence,
)


_ATTESTATION_KEY_ID = "test-release-key"
_ATTESTATION_KEY = b"dogfood-test-key-kept-outside-the-repository"


def _digest(character: str) -> str:
    return character * 64


def _artifact(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _json_artifact(path: Path, payload: dict) -> str:
    return _artifact(
        path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )


def _wheel_artifact(path: Path, *, version: str) -> str:
    buffer = io.BytesIO()
    dist_info = f"visual_agent-{version}.dist-info"
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: visual-agent\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            "[console_scripts]\npacer = visual_agent.cli:main\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
        archive.writestr("visual_agent/__init__.py", "")
    return _artifact(path, buffer.getvalue())


def _assess(evidence: dict, root: Path) -> dict:
    return assess_dogfood_evidence(
        evidence,
        repo_root=root,
        attestation_keys={_ATTESTATION_KEY_ID: _ATTESTATION_KEY},
    )


def _valid_evidence(root: Path) -> dict:
    repo = _digest("a")
    baseline = _digest("b")
    changes = _digest("c")
    artifacts = root / "artifacts"
    input_path = artifacts / "visual_agent-a.whl"
    candidate_path = artifacts / "visual_agent-b.whl"
    task_path = artifacts / "task-contract.json"
    acceptance_path = artifacts / "acceptance-contract.json"
    verification_path = artifacts / "verification-receipt.json"
    self_check_path = artifacts / "self-check-receipt.json"
    input_wheel = _wheel_artifact(input_path, version="0.1.2")
    candidate_wheel = _wheel_artifact(candidate_path, version="0.1.3")
    acceptance_payload = {
        "schema_version": 1,
        "standard_source": "repository",
        "observable_outcomes": ["Pacer manages and verifies its own source change."],
        "verification": {"required_step_classes": ["test"]},
    }
    acceptance_payload["digest"] = acceptance_contract_digest(acceptance_payload)
    acceptance_contract = _json_artifact(acceptance_path, acceptance_payload)
    task_payload = {
        "schema_version": 1,
        "goal_digest": _digest("d"),
        "requirements": [{"id": "R01-test", "text": "Improve Pacer"}],
        "acceptance_contract": acceptance_payload,
    }
    task_contract = _json_artifact(task_path, task_payload)
    verification_payload = {
        "schema_version": 1,
        "status": "passed",
        "trust": "yes",
        "launch_id": "launch-a",
        "batch_run_id": "batch-a",
        "task_contract_digest": task_contract,
        "acceptance_contract_digest": acceptance_contract,
        "change_set_digest": changes,
    }
    verification_receipt = _json_artifact(verification_path, verification_payload)
    self_check_payload = {
        "schema_version": 1,
        "status": "passed",
        "trust": "yes",
        "launch_id": "launch-b-self-check",
        "installed_wheel_sha256": candidate_wheel,
        "source_repo_identity_digest": repo,
    }
    self_check_receipt = _json_artifact(self_check_path, self_check_payload)
    evidence = {
        "schema_version": 1,
        "source_repo": {
            "product": "Pacer",
            "package_name": "visual-agent",
            "pacer_entrypoint": "visual_agent.cli:main",
            "canonical_root": str(root),
            "repo_identity_digest": repo,
            "baseline_commit": "abc123",
            "baseline_changes_digest": baseline,
            "change_set_digest": changes,
            "scan_complete": True,
            "protected_paths_unchanged": True,
            "out_of_band_changes": False,
            "source_attribution": "pacer_worker",
            "changed_files": [
                "src/visual_agent/managed_state.py",
                "tests/test_managed_state.py",
            ],
        },
        "orchestrator": {
            "input_wheel_sha256": input_wheel,
            "input_wheel_path": str(input_path),
            "input_version": "0.1.2",
            "launch_id": "launch-a",
            "mission_id": "mission-a",
            "worker_session_ids": ["session-a"],
            "repo_identity_digest": repo,
        },
        "contract": {
            "task_contract_digest": task_contract,
            "task_contract_path": str(task_path),
            "acceptance_contract_digest": acceptance_contract,
            "acceptance_contract_path": str(acceptance_path),
        },
        "verification": {
            "status": "passed",
            "trust": "yes",
            "batch_run_id": "batch-a",
            "receipt_digest": verification_receipt,
            "receipt_path": str(verification_path),
            "acceptance_contract_digest": acceptance_contract,
            "change_set_digest": changes,
            "evidence_resubmissions": 0,
            "warnings": [],
        },
        "candidate": {
            "wheel_sha256": candidate_wheel,
            "wheel_path": str(candidate_path),
            "version": "0.1.3",
            "built_from_change_set_digest": changes,
            "fresh_install": True,
            "fresh_env_id": "py311-clean-b",
            "pip_check_status": "passed",
        },
        "bootstrap": {
            "parent_wheel_sha256": input_wheel,
            "installed_wheel_sha256": candidate_wheel,
            "self_check_artifact_sha256": candidate_wheel,
            "source_repo_identity_digest": repo,
            "self_check_status": "passed",
            "self_check_receipt_digest": self_check_receipt,
            "self_check_receipt_path": str(self_check_path),
            "self_check_launch_id": "launch-b-self-check",
        },
    }
    return attest_dogfood_evidence(
        evidence,
        key_id=_ATTESTATION_KEY_ID,
        key=_ATTESTATION_KEY,
    )


def test_complete_a_to_b_generation_is_true_dogfood(tmp_path: Path) -> None:
    result = _assess(_valid_evidence(tmp_path), tmp_path)

    assert result["status"] == "passed"
    assert result["passed"] is True
    assert result["pacer_on_pacer"] is True
    assert result["self_change_attributed"] is True
    assert result["installed_artifact_verified"] is True
    assert result["artifact_files_verified"] is True
    assert len(result["evidence_digest"]) == 64


def test_generic_repo_cannot_claim_pacer_dogfood(tmp_path: Path) -> None:
    evidence = _valid_evidence(tmp_path)
    evidence["source_repo"].update(
        {
            "product": "checkout-service",
            "package_name": "checkout",
            "pacer_entrypoint": "checkout.cli:main",
        }
    )

    result = _assess(evidence, tmp_path)

    assert result["status"] == "failed"
    assert "dogfood_source_is_not_pacer" in result["reason_codes"]
    assert result["pacer_on_pacer"] is False


def test_digest_mismatch_and_evidence_resubmission_fail_closed(tmp_path: Path) -> None:
    evidence = _valid_evidence(tmp_path)
    evidence["verification"]["change_set_digest"] = _digest("4")
    evidence["verification"]["evidence_resubmissions"] = 1
    evidence["bootstrap"]["installed_wheel_sha256"] = _digest("5")

    result = _assess(evidence, tmp_path)

    assert result["status"] == "failed"
    assert {
        "dogfood_verified_changeset_mismatch",
        "dogfood_evidence_resubmitted",
        "dogfood_installed_wheel_mismatch",
    } <= set(result["reason_codes"])


def test_removing_bootstrap_from_attested_evidence_fails_closed(tmp_path: Path) -> None:
    evidence = _valid_evidence(tmp_path)
    evidence.pop("bootstrap")

    result = _assess(evidence, tmp_path)

    assert result["status"] == "failed"
    assert result["passed"] is False
    assert "dogfood_bootstrap_missing" in result["reason_codes"]
    assert "dogfood_attestation_digest_mismatch" in result["reason_codes"]


def test_unknown_attribution_or_non_product_change_is_rejected(tmp_path: Path) -> None:
    evidence = deepcopy(_valid_evidence(tmp_path))
    evidence["source_repo"]["source_attribution"] = "unknown"
    evidence["source_repo"]["changed_files"] = ["docs/readme.md"]

    result = _assess(evidence, tmp_path)

    assert result["status"] == "failed"
    assert "dogfood_source_attribution_untrusted" in result["reason_codes"]
    assert "dogfood_pacer_product_change_missing" in result["reason_codes"]


def test_tampered_candidate_wheel_fails_file_verification(tmp_path: Path) -> None:
    evidence = _valid_evidence(tmp_path)
    Path(evidence["candidate"]["wheel_path"]).write_bytes(b"tampered")

    result = _assess(evidence, tmp_path)

    assert result["passed"] is False
    assert result["artifact_files_verified"] is False
    assert "dogfood_candidate_wheel_digest_mismatch" in result["reason_codes"]


def test_canonical_loader_rejects_alternate_evidence_path(tmp_path: Path) -> None:
    evidence = _valid_evidence(tmp_path)
    canonical = tmp_path / ".pacer" / "dogfood-evidence.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(json.dumps(evidence), encoding="utf-8")

    loaded = load_dogfood_evidence(
        tmp_path,
        attestation_keys={_ATTESTATION_KEY_ID: _ATTESTATION_KEY},
    )

    assert loaded["passed"] is True
    assert loaded["evidence_path"] == ".pacer/dogfood-evidence.json"
    with pytest.raises(ValueError, match="must be .pacer/dogfood-evidence.json"):
        load_dogfood_evidence(tmp_path, evidence_path=tmp_path / "other.json")


def test_github_attestation_can_replace_static_hmac_and_reach_release_score(
    tmp_path: Path,
) -> None:
    evidence = _valid_evidence(tmp_path)
    evidence.pop("attestation")
    canonical = tmp_path / ".pacer" / "dogfood-evidence.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(json.dumps(evidence), encoding="utf-8")

    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout='[{"verified":true}]')

    loaded = load_dogfood_evidence(
        tmp_path,
        github_repository="Xiaoyu155/Checkpoint",
        github_signer_workflow=(
            "Xiaoyu155/Checkpoint/.github/workflows/pacer-dogfood.yml@refs/heads/main"
        ),
        github_run_id="100",
        github_run_attempt="1",
        require_github_provenance=True,
        github_attestation_runner=runner,
    )

    assert loaded["passed"] is True
    assert loaded["attestation"]["provider"] == "github-artifact-attestation"
    assert loaded["quality"]["score"] == 100
    assert loaded["quality"]["meets_target"] is True


def test_self_consistent_fake_wheel_cannot_pass(tmp_path: Path) -> None:
    evidence = _valid_evidence(tmp_path)
    candidate_path = Path(evidence["candidate"]["wheel_path"])
    fake_digest = _artifact(candidate_path, b"not-a-wheel")
    evidence["candidate"]["wheel_sha256"] = fake_digest
    evidence["bootstrap"]["installed_wheel_sha256"] = fake_digest
    evidence["bootstrap"]["self_check_artifact_sha256"] = fake_digest
    self_check_path = Path(evidence["bootstrap"]["self_check_receipt_path"])
    self_check = json.loads(self_check_path.read_text(encoding="utf-8"))
    self_check["installed_wheel_sha256"] = fake_digest
    evidence["bootstrap"]["self_check_receipt_digest"] = _json_artifact(
        self_check_path,
        self_check,
    )
    evidence = attest_dogfood_evidence(
        evidence,
        key_id=_ATTESTATION_KEY_ID,
        key=_ATTESTATION_KEY,
    )

    result = _assess(evidence, tmp_path)

    assert result["passed"] is False
    assert "dogfood_candidate_wheel_not_wheel" in result["reason_codes"]


def test_missing_or_wrong_external_attestation_key_fails_closed(tmp_path: Path) -> None:
    evidence = _valid_evidence(tmp_path)

    unavailable = assess_dogfood_evidence(evidence, repo_root=tmp_path)
    wrong = assess_dogfood_evidence(
        evidence,
        repo_root=tmp_path,
        attestation_keys={_ATTESTATION_KEY_ID: b"wrong-key"},
    )

    assert unavailable["passed"] is False
    assert "dogfood_attestation_key_unavailable" in unavailable["reason_codes"]
    assert wrong["passed"] is False
    assert "dogfood_attestation_signature_mismatch" in wrong["reason_codes"]


def test_canonical_loader_rejects_symlinked_pacer_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    evidence = _valid_evidence(tmp_path)
    (outside / "dogfood-evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    try:
        os.symlink(outside, tmp_path / ".pacer", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable in this Windows environment")

    with pytest.raises(ValueError, match="cannot use symbolic links"):
        load_dogfood_evidence(
            tmp_path,
            attestation_keys={_ATTESTATION_KEY_ID: _ATTESTATION_KEY},
        )
