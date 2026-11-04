import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from google.genai.errors import ClientError
from visual_agent.gemini_official_pool import (
    GeminiOfficialPool,
    gemini_official_pool,
    is_gemini_official_pool_available,
)


def test_gemini_official_pool_initialization():
    pool = GeminiOfficialPool(api_keys=["key1", "key2", "key3"])
    assert pool.api_keys == ["key1", "key2", "key3"]
    assert pool.model_name == "gemini-3.8-flash"
    assert pool.current_key == "key1"


def test_gemini_official_pool_get_next_key():
    pool = GeminiOfficialPool(api_keys=["key1", "key2"])
    assert pool.get_next_key() == "key2"
    assert pool.get_next_key() == "key1"
    assert pool.current_key == "key1"


def test_gemini_official_pool_no_keys():
    pool = GeminiOfficialPool(api_keys=[])
    assert pool.api_keys == []
    assert pool.get_next_key() is None
    assert pool.current_key is None
    assert pool.check_health() is False


def test_gemini_official_pool_load_json(tmp_path: Path):
    json_path = tmp_path / "gemini_keys.json"
    json_path.write_text(
        json.dumps({
            "default_model": "gemini-1.5-pro",
            "keys": [
                {"id": "acc_01", "key": "AIzaSyTestKey1"},
                {"id": "acc_02", "key": "AIzaSyTestKey2"},
            ]
        }),
        encoding="utf-8",
    )
    with patch.dict("os.environ", {"GEMINI_API_KEYS": "", "GOOGLE_API_KEY": ""}):
        pool = GeminiOfficialPool(config_path=json_path)
        assert len(pool.api_keys) == 2
        assert pool.api_keys[0] == "AIzaSyTestKey1"
        assert pool.api_keys[1] == "AIzaSyTestKey2"
        assert pool.model_name == "gemini-1.5-pro"
        assert pool.current_key == "AIzaSyTestKey1"


@patch("visual_agent.gemini_official_pool.genai.Client")
def test_gemini_official_pool_rotation_on_429(mock_client_factory):
    mock_client_1 = MagicMock()
    mock_client_1.models.generate_content.side_effect = ClientError(429, {"status": "RESOURCE_EXHAUSTED"})
    mock_client_2 = MagicMock()
    mock_res = MagicMock()
    mock_res.text = "Success from key 2"
    mock_client_2.models.generate_content.return_value = mock_res

    mock_client_factory.side_effect = [mock_client_1, mock_client_2]

    pool = GeminiOfficialPool(api_keys=["key1", "key2"])
    res = pool.generate_content("hello")
    assert res.text == "Success from key 2"
    assert pool.current_key == "key2"
    assert [call.kwargs["api_key"] for call in mock_client_factory.call_args_list] == ["key1", "key2"]
    mock_client_1.close.assert_called_once()
    mock_client_2.close.assert_called_once()


@patch("visual_agent.gemini_official_pool.genai.Client")
def test_check_health(mock_client_factory):
    client = MagicMock()
    client.models.generate_content.return_value.text = "healthy"
    mock_client_factory.return_value = client

    pool = GeminiOfficialPool(api_keys=["fake-key"])
    assert pool.check_health(2.0) is True
    mock_client_factory.assert_called_once()
    assert mock_client_factory.call_args.kwargs["http_options"].timeout == 2000
    client.models.generate_content.assert_called_once_with(
        model="gemini-3.8-flash",
        contents="Respond with a single word: healthy",
        config={"temperature": 0, "max_output_tokens": 20},
    )


@patch("visual_agent.gemini_official_pool.genai.Client")
def test_non_quota_errors_do_not_rotate_keys(mock_client_factory):
    client = MagicMock()
    client.models.generate_content.side_effect = ClientError(400, {"status": "INVALID_ARGUMENT"})
    mock_client_factory.return_value = client
    pool = GeminiOfficialPool(api_keys=["key1", "key2"])

    with pytest.raises(ClientError):
        pool.generate_content("hello")

    assert pool.current_key == "key1"
    client.close.assert_called_once()


def test_select_best_backend_routes_to_official_pool():
    with patch("visual_agent.gemini4_vps_client.is_gemini4_vps_available", return_value=False):
        from visual_agent.subagent_orchestrator import select_best_backend
        assert select_best_backend("architect", "design system") == "local"
        assert select_best_backend("wish", "analyze user wish") == "local"
        assert select_best_backend("reviewer", "review PR diff") == "local"


