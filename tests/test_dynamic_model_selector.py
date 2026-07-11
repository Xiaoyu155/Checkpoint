from __future__ import annotations

import json

from visual_agent.dynamic_model_selector import (
    load_model_candidates,
    select_model_for_task,
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
