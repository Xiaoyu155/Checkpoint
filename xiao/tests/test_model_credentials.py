from __future__ import annotations

import json

from visual_agent.cli import main
from visual_agent.model_credentials import (
    build_auth_headers,
    build_model_api_probe_plan,
    extract_secret_from_line,
    inspect_model_credentials,
    model_api_probe_plan_to_markdown,
    model_credentials_to_markdown,
    resolve_model_provider_config,
    run_model_api_probe,
)


def test_inspect_model_credentials_prefers_xiaomimimo_when_available(tmp_path) -> None:
    path = tmp_path / "keys.txt"
    path.write_text(
        "\n".join(
            [
                "qwen sk-qwen-secret-value-123456",
                "xiaomimimo api key: sk-xiaomi-secret-value-abcdef",
                "登录密码: plain-password",
            ]
        ),
        encoding="utf-8",
    )

    result = inspect_model_credentials(source=path, preferred_provider="xiaomimimo")
    text = json.dumps(result, ensure_ascii=False)

    assert result["preferred_available"] is True
    assert result["selected_provider"] == "xiaomimimo"
    assert {entry["provider"] for entry in result["providers"]} == {"qwen", "xiaomimimo"}
    assert "sk-xiaomi-secret-value-abcdef" not in text
    assert "plain-password" not in text


def test_inspect_model_credentials_reports_missing_preferred_without_fallback(tmp_path) -> None:
    path = tmp_path / "keys.txt"
    path.write_text("千问 sk-qwen-secret-value-123456\n", encoding="utf-8")

    result = inspect_model_credentials(source=path, preferred_provider="xiaomimimo")
    markdown = model_credentials_to_markdown(result)

    assert result["preferred_available"] is False
    assert result["selected_provider"] is None
    assert result["providers"][0]["provider"] == "qwen"
    assert "sk-qwen-secret-value-123456" not in markdown
    assert "Preferred provider: `xiaomimimo`" in markdown
    assert "Preferred available: `False`" in markdown


def test_default_openai_can_auto_select_available_file_provider(tmp_path, monkeypatch) -> None:
    path = tmp_path / "keys.txt"
    path.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")
    monkeypatch.delenv("VISUAL_AGENT_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("VISUAL_AGENT_OPENAI_API_KEY", raising=False)

    result = inspect_model_credentials(source=path)
    plan = build_model_api_probe_plan(source=path)

    assert result["preferred_provider"] == "openai"
    assert result["preferred_available"] is False
    assert result["selected_provider"] == "xiaomimimo"
    assert result["auto_selected"] is True
    assert plan["ready"] is True
    assert plan["selected_provider"] == "xiaomimimo"
    assert plan["probe"]["base_url"] == "https://api.xiaomimimo.com/v1"


def test_model_credentials_inspect_cli_is_redacted(tmp_path, capsys) -> None:
    path = tmp_path / "keys.txt"
    path.write_text("deepseek密钥 sk-deepseek-secret-value-123456\n", encoding="utf-8")

    code = main(["model-credentials-inspect", "--source", str(path), "--preferred", "deepseek", "--format", "markdown"])
    output = capsys.readouterr().out

    assert code == 0
    assert "deepseek" in output
    assert "sk-deepseek-secret-value-123456" not in output


def test_model_api_probe_plan_uses_xiaomimimo_defaults_without_sending_secret(tmp_path) -> None:
    path = tmp_path / "keys.txt"
    path.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")

    plan = build_model_api_probe_plan(source=path, preferred_provider="xiaomimimo")
    markdown = model_api_probe_plan_to_markdown(plan)

    assert plan["selected_provider"] == "xiaomimimo"
    assert plan["probe"]["sends_secret"] is False
    assert plan["probe"]["mode"] == "plan-only"
    assert plan["blockers"] == []
    assert plan["ready"] is True
    assert plan["probe"]["base_url"] == "https://api.xiaomimimo.com/v1"
    assert plan["probe"]["endpoint"] == "/chat/completions"
    assert "sk-xiaomi-secret-value-abcdef" not in json.dumps(plan, ensure_ascii=False)
    assert "sk-xiaomi-secret-value-abcdef" not in markdown


def test_model_api_probe_plan_cli_can_be_ready_with_base_url_and_endpoint(tmp_path, capsys) -> None:
    path = tmp_path / "keys.txt"
    path.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")

    code = main(
        [
            "model-api-probe-plan",
            "--source",
            str(path),
            "--preferred",
            "xiaomimimo",
            "--base-url",
            "https://api.example.test",
            "--endpoint",
            "/v1/models",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "Ready: `True`" in output
    assert "Sends secret: `False`" in output
    assert "sk-xiaomi-secret-value-abcdef" not in output


def test_run_model_api_probe_uses_redacted_compact_response(tmp_path, monkeypatch) -> None:
    path = tmp_path / "keys.txt"
    path.write_text("xiaomimimo api key: sk-xiaomi-secret-value-abcdef\n", encoding="utf-8")
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "model": "mimo-v2.5",
                    "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 3},
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data.decode("utf-8")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("visual_agent.model_credentials.urllib.request.urlopen", fake_urlopen)

    result = run_model_api_probe(source=path, preferred_provider="xiaomimimo", timeout_seconds=3, max_completion_tokens=2)
    text = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "success"
    assert result["response"]["content_preview"] == "OK"
    assert result["probe"]["sends_secret"] is True
    assert result["probe"]["mode"] == "execute"
    assert captured["headers"]["Api-key"] == "sk-xiaomi-secret-value-abcdef"
    assert '"max_completion_tokens": 2' in captured["body"]
    assert "sk-xiaomi-secret-value-abcdef" not in text


def test_build_auth_headers_uses_provider_auth_style() -> None:
    assert build_auth_headers("xiaomimimo", "sk-test") == {"api-key": "sk-test"}
    assert build_auth_headers("qwen", "sk-test") == {"Authorization": "Bearer sk-test"}
    assert build_auth_headers("openai", "sk-test") == {"Authorization": "Bearer sk-test"}
    assert build_auth_headers("kimi", "sk-test") == {"Authorization": "Bearer sk-test"}


def test_resolve_model_provider_config_reads_secret_and_defaults(tmp_path) -> None:
    path = tmp_path / "keys.txt"
    path.write_text("qwen sk-qwen-secret-value-123456\n", encoding="utf-8")

    config = resolve_model_provider_config(source=path, preferred_provider="qwen")
    text = json.dumps({key: value for key, value in config.items() if not key.startswith("_")}, ensure_ascii=False)

    assert config["available"] is True
    assert config["provider"] == "qwen"
    assert config["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert config["endpoint"] == "/chat/completions"
    assert config["model"] == "qwen-max"
    assert config["_auth_headers"] == {"Authorization": "Bearer sk-qwen-secret-value-123456"}
    assert config["auth_headers_configured"] is True
    assert "sk-qwen-secret-value-123456" not in text


def test_openai_probe_plan_can_use_environment_key_without_file(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "missing-keys.txt"
    monkeypatch.setenv("VISUAL_AGENT_OPENAI_API_KEY", "sk-openai-secret-value-123456")

    plan = build_model_api_probe_plan(source=missing, preferred_provider="openai")
    text = json.dumps(plan, ensure_ascii=False)

    assert plan["ready"] is True
    assert plan["selected_provider"] == "openai"
    assert plan["probe"]["base_url"] == "https://api.openai.com/v1"
    assert plan["probe"]["endpoint"] == "/chat/completions"
    assert plan["probe"]["model"] == "gpt-4o"
    assert "credential_source_missing" not in plan["blockers"]
    assert "sk-openai-secret-value-123456" not in text


def test_openai_probe_plan_prefers_checkpoint_environment_key(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "missing-keys.txt"
    monkeypatch.setenv("VISUAL_AGENT_OPENAI_API_KEY", "sk-legacy-secret-value-123456")
    monkeypatch.setenv("CHECKPOINT_OPENAI_API_KEY", "sk-checkpoint-secret-value-123456")

    plan = build_model_api_probe_plan(source=missing, preferred_provider="openai")
    config = resolve_model_provider_config(source=missing, preferred_provider="openai")
    text = json.dumps(plan, ensure_ascii=False) + json.dumps({key: value for key, value in config.items() if not key.startswith("_")}, ensure_ascii=False)

    assert plan["ready"] is True
    assert config["_api_key"] == "sk-checkpoint-secret-value-123456"
    assert "credential_source_missing" not in plan["blockers"]
    assert "sk-checkpoint-secret-value-123456" not in text
    assert "sk-legacy-secret-value-123456" not in text


def test_run_model_api_probe_openai_uses_bearer_env_key(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "missing-keys.txt"
    monkeypatch.setenv("VISUAL_AGENT_OPENAI_API_KEY", "sk-openai-secret-value-123456")
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        return FakeResponse()

    monkeypatch.setattr("visual_agent.model_credentials.urllib.request.urlopen", fake_urlopen)

    result = run_model_api_probe(source=missing, preferred_provider="openai")
    text = json.dumps(result, ensure_ascii=False)

    assert result["status"] == "success"
    assert captured["headers"]["Authorization"] == "Bearer sk-openai-secret-value-123456"
    assert "sk-openai-secret-value-123456" not in text


def test_extract_secret_from_line_ignores_comments_and_urls() -> None:
    assert extract_secret_from_line("# xiaomimimo: see docs at https://example.com") is None
    assert extract_secret_from_line("xiaomimimo: https://api.example.com/v1") is None
    assert extract_secret_from_line("# Token based authentication") is None
    assert extract_secret_from_line("api_key: sk-abcdefghijklmnopqrstuv1234567890") == "sk-abcdefghijklmnopqrstuv1234567890"
    assert extract_secret_from_line("SECRET=abcdefghijklmnopqrstuvwxyz123456") == "abcdefghijklmnopqrstuvwxyz123456"
