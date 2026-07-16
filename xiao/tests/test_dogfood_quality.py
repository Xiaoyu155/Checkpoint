from __future__ import annotations

from visual_agent.dogfood_quality import assess_dogfood_quality


def _assessment() -> dict:
    return {
        "passed": True,
        "pacer_on_pacer": True,
        "self_change_attributed": True,
        "installed_artifact_verified": True,
        "artifact_files_verified": True,
        "attestation": {"status": "verified"},
        "generation": {
            "parent_wheel_sha256": "a" * 64,
            "candidate_wheel_sha256": "b" * 64,
        },
    }


def test_local_hmac_lane_scores_85_without_claiming_ci_maturity() -> None:
    result = assess_dogfood_quality(_assessment())

    assert result["score"] == 85
    assert result["level"] == "local"
    assert result["meets_target"] is False


def test_github_provenance_reaches_95_and_independent_identity_reaches_100() -> None:
    provenance = {
        "verified": True,
        "provider": "github-artifact-attestation",
    }
    ci = assess_dogfood_quality(_assessment(), provenance=provenance)
    release = assess_dogfood_quality(
        _assessment(),
        provenance={**provenance, "run_identity_digest": "c" * 64},
    )

    assert ci["score"] == 95
    assert ci["meets_target"] is True
    assert release["score"] == 100
    assert release["level"] == "release"


def test_score_cannot_override_missing_critical_evidence() -> None:
    assessment = _assessment()
    assessment["artifact_files_verified"] = False
    result = assess_dogfood_quality(
        assessment,
        provenance={
            "verified": True,
            "provider": "github-artifact-attestation",
            "run_identity_digest": "c" * 64,
        },
    )

    assert result["score"] == 90
    assert result["critical_controls_passed"] is False
    assert result["meets_target"] is False
