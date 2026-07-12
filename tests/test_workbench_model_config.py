from __future__ import annotations

from visual_agent.agent_backends import resolve_backend_by_name
from visual_agent.workbench_model_config import (
    DEFAULT_SUB2API_BASE_URL,
    WorkbenchModelConfig,
    load_workbench_model_config,
    redacted_config_summary,
    save_workbench_model_config,
)


def test_workbench_model_config_round_trips_to_bugteam_backend(tmp_path) -> None:
    source = tmp_path / "model_api_keys.txt"
    config = WorkbenchModelConfig(
        base_url="http://174.138.75.136:8080/v1",
        api_key="sk-sub2apiabcdefghijklmnopqrstuvwxyz",
        model="gpt-4o-mini",
        reasoning_effort="xhigh",
        monthly_budget_usd=20.0,
        per_mission_budget_usd=1.5,
        auto_switch_quota_percent=76.0,
    )

    save_workbench_model_config(config, source)

    loaded = load_workbench_model_config(source)
    backend = resolve_backend_by_name("bugteam", source=source)
    assert loaded == config
    assert backend is not None
    assert backend["env"]["ANTHROPIC_BASE_URL"] == "http://174.138.75.136:8080/v1"
    assert backend["env"]["ANTHROPIC_API_KEY"] == "sk-sub2apiabcdefghijklmnopqrstuvwxyz"
    assert backend["model"] == "gpt-4o-mini"
    assert backend["reasoning_effort"] == "xhigh"
    assert loaded.monthly_budget_usd == 20.0
    assert loaded.per_mission_budget_usd == 1.5
    assert loaded.auto_switch_quota_percent == 76.0


def test_workbench_model_config_replaces_existing_bugteam_line(tmp_path) -> None:
    source = tmp_path / "model_api_keys.txt"
    source.write_text(
        "deepseek sk-existing-token-abcdefghijklmnopqrstuvwxyz\n"
        "bugteam api_key=sk-oldabcdefghijklmnopqrstuvwxyz base_url=http://old/v1 model=old-model\n",
        encoding="utf-8",
    )

    save_workbench_model_config(
        WorkbenchModelConfig(
            base_url="http://174.138.75.136:8080/v1",
            api_key="sk-newabcdefghijklmnopqrstuvwxyz",
            model="deepseek-chat",
        ),
        source,
    )

    text = source.read_text(encoding="utf-8")
    assert text.count("bugteam ") == 1
    assert "sk-old" not in text
    assert "deepseek sk-existing-token" in text
    assert "model=deepseek-chat" in text


def test_workbench_model_config_defaults_and_redaction(tmp_path) -> None:
    loaded = load_workbench_model_config(tmp_path / "missing.txt")

    assert loaded.base_url == DEFAULT_SUB2API_BASE_URL
    assert loaded.api_key == ""
    assert "未配置" in redacted_config_summary(loaded)
    assert loaded.auto_switch_quota_percent == 80.0
