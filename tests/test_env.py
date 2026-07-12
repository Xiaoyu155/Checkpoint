from __future__ import annotations

from visual_agent.env import checkpoint_env_name, env_get, env_present, provider_api_key_env_names


def test_env_get_prefers_checkpoint_name() -> None:
    environ = {
        "CHECKPOINT_CLOUD_ENDPOINT": "https://checkpoint.example/api",
        "VISUAL_AGENT_CLOUD_ENDPOINT": "https://legacy.example/api",
    }

    assert env_get("VISUAL_AGENT_CLOUD_ENDPOINT", environ=environ) == "https://checkpoint.example/api"


def test_env_get_falls_back_to_legacy_visual_agent_name() -> None:
    environ = {"VISUAL_AGENT_CLOUD_ENDPOINT": "https://legacy.example/api"}

    assert env_get("VISUAL_AGENT_CLOUD_ENDPOINT", environ=environ) == "https://legacy.example/api"


def test_env_get_leaves_non_visual_agent_names_exact() -> None:
    environ = {"OPENAI_API_KEY": "sk-test"}

    assert env_get("OPENAI_API_KEY", environ=environ) == "sk-test"
    assert checkpoint_env_name("OPENAI_API_KEY") == "OPENAI_API_KEY"


def test_env_present_uses_checkpoint_alias() -> None:
    assert env_present("VISUAL_AGENT_LICENSE_KEY", environ={"CHECKPOINT_LICENSE_KEY": "license"}) is True
    assert env_present("VISUAL_AGENT_LICENSE_KEY", environ={"CHECKPOINT_LICENSE_KEY": ""}) is False


def test_provider_api_key_env_names() -> None:
    assert provider_api_key_env_names("openai") == ("CHECKPOINT_OPENAI_API_KEY", "VISUAL_AGENT_OPENAI_API_KEY")
