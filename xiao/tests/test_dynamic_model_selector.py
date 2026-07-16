from __future__ import annotations

import json

from visual_agent.dynamic_model_selector import (
    load_model_candidates,
    routing_request_evidence,
    select_model_for_task,
    selection_to_dict,
    selection_to_markdown,
)


def test_load_model_candidates_from_config(tmp_path) -> None:
    cfg = tmp_path / "model_pool.json"
    cfg.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "cheap-fast",
                        "provider": "local",
                        "model": "mini",
                        "capability": 0.35,
                        "cost": 0.1,
                        "modes": ["cheap", "standard"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    models = load_model_candidates(config_path=cfg)

    assert models[0].id == "cheap-fast"
    assert models[0].modes == ("cheap", "standard")


def test_selects_low_cost_model_for_mechanical_task_under_quota_pressure(tmp_path) -> None:
    cfg = tmp_path / "model_pool.json"
    cfg.write_text(
        json.dumps(
            {
                "models": [
                    {"id": "expensive", "provider": "x", "model": "max", "capability": 0.95, "cost": 0.95, "modes": ["cheap", "standard", "strong"]},
                    {"id": "cheap", "provider": "x", "model": "mini", "capability": 0.40, "cost": 0.08, "modes": ["cheap", "standard"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    selection = select_model_for_task(
        objective="fix typo in button label",
        changed_files=["src/button.tsx"],
        quota_snapshot={"rate_limits": {"five_hour": {"used_percentage": 92}}},
        config_path=cfg,
    )

    assert selection.status == "selected"
    assert selection.selected is not None
    assert selection.selected.id == "cheap"


def test_selects_low_cost_model_from_provider_quota_snapshot(tmp_path) -> None:
    cfg = tmp_path / "model_pool.json"
    cfg.write_text(
        json.dumps(
            {
                "models": [
                    {"id": "expensive", "provider": "x", "model": "max", "capability": 0.95, "cost": 0.95, "modes": ["cheap", "standard", "strong"]},
                    {"id": "cheap", "provider": "x", "model": "mini", "capability": 0.40, "cost": 0.08, "modes": ["cheap", "standard"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    selection = select_model_for_task(
        objective="fix typo in button label",
        changed_files=["src/button.tsx"],
        quota_snapshot={"providers": {"codex": {"rate_limits": {"five_hour": {"used_percentage": 92}}}}},
        config_path=cfg,
    )

    assert selection.status == "selected"
    assert selection.selected is not None
    assert selection.selected.id == "cheap"


def test_strong_task_requires_capability_even_when_quota_is_hot(tmp_path) -> None:
    cfg = tmp_path / "model_pool.json"
    cfg.write_text(
        json.dumps(
            {
                "models": [
                    {"id": "cheap", "provider": "x", "model": "mini", "capability": 0.40, "cost": 0.05, "modes": ["cheap", "standard"]},
                    {"id": "strong", "provider": "x", "model": "max", "capability": 0.86, "cost": 0.7, "modes": ["strong"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    selection = select_model_for_task(
        objective="refactor authentication architecture for security",
        changed_files=["src/auth.py"],
        quota_snapshot={"rate_limits": {"five_hour": {"used_percentage": 90}}},
        config_path=cfg,
    )

    assert selection.required_tier == "strong"
    assert selection.selected is not None
    assert selection.selected.id == "strong"


def test_selection_markdown_explains_choice(tmp_path) -> None:
    cfg = tmp_path / "model_pool.json"
    cfg.write_text(
        json.dumps({"models": [{"id": "balanced", "provider": "x", "model": "b", "capability": 0.65, "cost": 0.4}]}),
        encoding="utf-8",
    )

    selection = select_model_for_task(objective="Add checkout field", changed_files=["src/checkout.py"], config_path=cfg)
    text = selection_to_markdown(selection)

    assert "Dynamic Model Selection" in text
    assert "balanced" in text


def test_unknown_candidate_quality_or_cost_is_not_viable(tmp_path) -> None:
    cfg = tmp_path / "model_pool.json"
    cfg.write_text(
        json.dumps({"models": [{"id": "unknown", "provider": "x", "model": "m"}]}),
        encoding="utf-8",
    )

    selection = select_model_for_task(objective="fix typo", config_path=cfg)

    assert selection.status == "blocked"
    assert selection.candidates[0]["viable"] is False


def test_incompatible_agent_backend_is_not_selected(tmp_path) -> None:
    cfg = tmp_path / "model_pool.json"
    cfg.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "other-agent",
                        "provider": "x",
                        "model": "m",
                        "capability": 0.9,
                        "cost": 0.1,
                        "agent_backend": "other",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    selection = select_model_for_task(objective="refactor architecture", config_path=cfg)

    assert selection.status == "blocked"
    assert selection.candidates[0]["backend_compatible"] is False


def test_routing_decision_matches_actual_request(tmp_path) -> None:
    cfg = tmp_path / "model_pool.json"
    cfg.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "chosen",
                        "provider": "custom",
                        "model": "gpt-x",
                        "capability": 0.8,
                        "cost": 0.4,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    selection = selection_to_dict(
        select_model_for_task(objective="Add checkout field", config_path=cfg)
    )

    matched = routing_request_evidence(
        selection,
        requested_provider="custom",
        requested_model="gpt-x",
    )
    mismatched = routing_request_evidence(
        selection,
        requested_provider="custom",
        requested_model="other",
    )

    assert selection["policy_version"] == 2
    assert selection["decision_id"]
    assert matched["policy_match"] is True
    assert matched["verdict"] == "matched"
    assert mismatched["policy_match"] is False
