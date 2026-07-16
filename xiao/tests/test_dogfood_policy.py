from __future__ import annotations

import json
from pathlib import Path

from visual_agent.dogfood_policy import load_dogfood_policy, validate_dogfood_policy


def test_repository_dogfood_policy_is_pinned_and_targets_95() -> None:
    repo = Path(__file__).resolve().parents[1]

    result = load_dogfood_policy(repo)

    assert result["passed"] is True
    assert result["target_score"] == 95
    assert result["release_score"] == 100
    assert result["required_independent_runs"] == 3
    assert len(result["policy_digest"]) == 64


def test_policy_rejects_unpinned_reference_and_mutable_candidate_runs() -> None:
    repo = Path(__file__).resolve().parents[1]
    policy = json.loads((repo / ".pacer" / "dogfood.json").read_text(encoding="utf-8"))
    policy["same_candidate_required"] = False
    policy["github_references"][0]["commit"] = "main"

    result = validate_dogfood_policy(policy)

    assert result["passed"] is False
    assert {
        "dogfood_policy_candidate_not_immutable",
        "dogfood_policy_reference_not_pinned",
    } <= set(result["reason_codes"])
