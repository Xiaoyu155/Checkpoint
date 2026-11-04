from __future__ import annotations

import re
from typing import Any, Mapping


DOGFOOD_TARGET_SCORE = 95
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")

_CONTROL_WEIGHTS = (
    ("immutable_generation_chain", 15),
    ("pacer_self_change", 20),
    ("trusted_acceptance", 15),
    ("fresh_candidate_install", 15),
    ("artifact_integrity", 10),
    ("external_evidence_attestation", 10),
    ("standard_build_provenance", 10),
    ("independent_run_identity", 5),
)


def assess_dogfood_quality(
    assessment: Mapping[str, Any],
    *,
    provenance: Mapping[str, Any] | None = None,
    target_score: int = DOGFOOD_TARGET_SCORE,
) -> dict[str, Any]:
    """Score mechanical controls without turning a score into evidence."""

    generation = (
        assessment.get("generation")
        if isinstance(assessment.get("generation"), Mapping)
        else {}
    )
    attestation = (
        assessment.get("attestation")
        if isinstance(assessment.get("attestation"), Mapping)
        else {}
    )
    standard = provenance if isinstance(provenance, Mapping) else {}
    parent = str(generation.get("parent_wheel_sha256") or "")
    candidate = str(generation.get("candidate_wheel_sha256") or "")
    run_identity = str(standard.get("run_identity_digest") or "")
    values = {
        "immutable_generation_chain": bool(
            _SHA256.fullmatch(parent)
            and _SHA256.fullmatch(candidate)
            and parent != candidate
        ),
        "pacer_self_change": assessment.get("self_change_attributed") is True,
        "trusted_acceptance": bool(
            assessment.get("passed") is True
            and assessment.get("pacer_on_pacer") is True
        ),
        "fresh_candidate_install": assessment.get("installed_artifact_verified") is True,
        "artifact_integrity": assessment.get("artifact_files_verified") is True,
        "external_evidence_attestation": str(attestation.get("status") or "") == "verified",
        "standard_build_provenance": bool(
            standard.get("verified") is True
            and str(standard.get("provider") or "") == "github-artifact-attestation"
        ),
        "independent_run_identity": bool(_SHA256.fullmatch(run_identity)),
    }
    controls = [
        {
            "id": control_id,
            "weight": weight,
            "passed": bool(values[control_id]),
        }
        for control_id, weight in _CONTROL_WEIGHTS
    ]
    score = sum(item["weight"] for item in controls if item["passed"])
    bounded_target = max(1, min(100, int(target_score)))
    critical = {
        "immutable_generation_chain",
        "pacer_self_change",
        "trusted_acceptance",
        "fresh_candidate_install",
        "artifact_integrity",
        "external_evidence_attestation",
    }
    critical_passed = all(values[item] for item in critical)
    return {
        "schema_version": 1,
        "score": score,
        "target_score": bounded_target,
        "meets_target": score >= bounded_target and critical_passed,
        "level": "release" if score == 100 else "ci" if score >= 95 else "local" if score >= 85 else "insufficient",
        "critical_controls_passed": critical_passed,
        "controls": controls,
    }
