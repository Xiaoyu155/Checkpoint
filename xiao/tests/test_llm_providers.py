from __future__ import annotations

import pytest

from visual_agent.llm_providers import llm_backend_manifest, resolve_llm_backend, run_llm_completion


def test_resolve_llm_backend_defaults_to_anthropic() -> None:
    backend = resolve_llm_backend(None)

    assert backend.provider == "anthropic"
    assert backend.model_id == "claude-haiku-4-5-20251001"


def test_resolve_llm_backend_supports_provider_prefix() -> None:
    backend = resolve_llm_backend("openai:gpt-4.1")

    assert backend.provider == "openai"
    assert backend.model_id == "gpt-4.1"
    assert backend.supported is True


def test_llm_backend_manifest_lists_future_targets() -> None:
    manifest = llm_backend_manifest()

    providers = {item["provider"] for item in manifest}
    assert {"anthropic", "openai", "gemini"}.issubset(providers)
    assert {"xiaomimimo", "qwen", "kimi", "deepseek", "volcengine"}.issubset(providers)


def test_run_llm_completion_supports_openai_compatible_backend(monkeypatch) -> None:
    backend = resolve_llm_backend("openai:gpt-4.1")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"OK"}}]}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data.decode("utf-8")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("visual_agent.llm_providers.urllib.request.urlopen", fake_urlopen)

    text = run_llm_completion(
        backend=backend,
        system_prompt="system prompt",
        prompt="user prompt",
        max_tokens=16,
        api_key="sk-test",
        base_url="https://api.openai.com/v1",
        endpoint="/chat/completions",
    )

    assert text == "OK"
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert '"system prompt"' in captured["body"]
    assert '"user prompt"' in captured["body"]


def test_run_llm_completion_supports_gemini_backend(monkeypatch) -> None:
    backend = resolve_llm_backend("gemini:gemini-2.0-flash")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"candidates":[{"content":{"parts":[{"text":"OK"}]}}]}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data.decode("utf-8")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("visual_agent.llm_providers.urllib.request.urlopen", fake_urlopen)

    text = run_llm_completion(
        backend=backend,
        system_prompt="system prompt",
        prompt="user prompt",
        max_tokens=16,
        api_key="gemini-key",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        endpoint="/models/gemini-2.0-flash:generateContent",
    )

    assert text == "OK"
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    assert captured["headers"]["Api-key"] == "gemini-key"
    assert '"system_instruction"' in captured["body"]


def test_run_llm_completion_rejects_unknown_backend() -> None:
    backend = resolve_llm_backend("future:model")

    with pytest.raises(NotImplementedError):
        run_llm_completion(backend=backend, system_prompt="system", prompt="prompt", max_tokens=16)
