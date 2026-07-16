from __future__ import annotations

import json
import subprocess

from visual_agent.github_attestation import verify_github_artifact_attestations


def test_github_attestation_delegates_each_subject_to_gh(tmp_path) -> None:
    first = tmp_path / "candidate.whl"
    second = tmp_path / "dogfood-evidence.json"
    first.write_bytes(b"wheel")
    second.write_text("{}", encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps([{"verified": True}]))

    result = verify_github_artifact_attestations(
        [first, second],
        repository="Xiaoyu155/Checkpoint",
        signer_workflow=(
            "Xiaoyu155/Checkpoint/.github/workflows/pacer-dogfood.yml@refs/heads/main"
        ),
        run_id="100",
        run_attempt="2",
        runner=runner,
    )

    assert result["verified"] is True
    assert result["key_id"] == "github:Xiaoyu155/Checkpoint"
    assert len(result["subjects"]) == 2
    assert len(result["run_identity_digest"]) == 64
    assert all(call[:3] == ["gh", "attestation", "verify"] for call in calls)
    assert all("--signer-workflow" in call for call in calls)


def test_github_attestation_fails_closed_on_bad_identity_or_verifier_failure(tmp_path) -> None:
    subject = tmp_path / "candidate.whl"
    subject.write_bytes(b"wheel")
    calls = 0

    def runner(argv, **_kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 1, stdout="{}")

    invalid = verify_github_artifact_attestations(
        [subject],
        repository="not-a-repository",
        runner=runner,
    )
    failed = verify_github_artifact_attestations(
        [subject],
        repository="Xiaoyu155/Checkpoint",
        runner=runner,
    )

    assert invalid["reason_codes"] == ["github_attestation_repository_invalid"]
    assert failed["reason_codes"] == ["github_attestation_verification_failed"]
    assert calls == 1


def test_github_attestation_needs_run_and_attempt_for_independent_identity(tmp_path) -> None:
    subject = tmp_path / "candidate.whl"
    subject.write_bytes(b"wheel")

    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout='[{"verified":true}]')

    result = verify_github_artifact_attestations(
        [subject],
        repository="Xiaoyu155/Checkpoint",
        signer_workflow=(
            "Xiaoyu155/Checkpoint/.github/workflows/pacer-dogfood.yml@refs/heads/main"
        ),
        runner=runner,
    )

    assert result["verified"] is True
    assert result["run_identity_digest"] == ""
