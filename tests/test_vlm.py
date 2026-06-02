import json

from PIL import Image

from visual_agent.models import ProviderKind
from visual_agent.vlm import cloud_vision_query, detect_cloud_vision_backend, detect_vlm_backend, observe_vision, vlm_doctor_summary


def test_detect_vlm_backend_reports_missing_modules(monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.vlm.module_available", lambda module: False)

    status = detect_vlm_backend("qwen2-vl")

    assert status["engine"] == "qwen2-vl"
    assert status["available"] is False
    assert status["module_available"] is False
    assert "torch" in status["missing_modules"]


def test_detect_vlm_backend_reports_missing_model_path(monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.vlm.module_available", lambda module: True)

    status = detect_vlm_backend("moondream")

    assert status["available"] is False
    assert status["module_available"] is True
    assert status["error"] == "Missing local model_path."


def test_vision_mock_keeps_engine_status_available(tmp_path) -> None:
    observation = observe_vision(
        {"mock_description": "页面显示已登录状态", "mock_status": "success"},
        tmp_path,
        synthetic_on_capture_fail=True,
    )

    assert observation.provider == ProviderKind.VISION
    assert observation.metadata["engine"] == "mock"
    assert observation.metadata["engine_available"] is True
    assert observation.metadata["engine_status"]["available"] is True


def test_vision_mock_structures_candidate_targets_from_description(tmp_path) -> None:
    observation = observe_vision(
        {"mock_description": "页面上有“登录”按钮和“注册”链接。", "mock_status": "success"},
        tmp_path,
        synthetic_on_capture_fail=True,
    )

    candidates = [element for element in observation.elements if element["role"] == "vision_candidate"]

    assert observation.elements[0]["role"] == "vision_description"
    assert observation.metadata["structured_target_count"] == 2
    assert [candidate["label"] for candidate in candidates] == ["登录", "注册"]
    assert candidates[0]["target_role"] == "button"
    assert candidates[1]["target_role"] == "link"


def test_vision_candidate_labels_seed_structured_targets(tmp_path) -> None:
    observation = observe_vision(
        {
            "mock_description": "The checkout screen shows a Submit button.",
            "candidate_labels": ["Submit", "Cancel"],
        },
        tmp_path,
        synthetic_on_capture_fail=True,
    )

    candidates = [element for element in observation.elements if element["role"] == "vision_candidate"]

    assert [candidate["label"] for candidate in candidates] == ["Submit"]
    assert candidates[0]["source"] == "candidate_labels"
    assert candidates[0]["target_role"] == "button"


def test_vision_target_parsing_can_be_disabled(tmp_path) -> None:
    observation = observe_vision(
        {"mock_description": "页面上有“登录”按钮。", "parse_targets": False},
        tmp_path,
        synthetic_on_capture_fail=True,
    )

    assert len(observation.elements) == 1
    assert observation.metadata["structured_target_count"] == 0


def test_vision_local_backend_unavailable_returns_diagnostic(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (120, 80), "white").save(image_path)
    monkeypatch.setattr("visual_agent.vlm.module_available", lambda module: False)

    observation = observe_vision({"path": str(image_path), "engine": "qwen2-vl"}, tmp_path)

    assert observation.elements == ()
    assert observation.metadata["engine"] == "qwen2-vl"
    assert observation.metadata["engine_available"] is False
    assert "Missing Python modules" in observation.metadata["engine_status"]["error"]


def test_vision_local_backend_configured_diagnostic_adapter(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "screen.png"
    model_path = tmp_path / "model"
    model_path.mkdir()
    Image.new("RGB", (120, 80), "white").save(image_path)
    monkeypatch.setattr("visual_agent.vlm.module_available", lambda module: True)

    observation = observe_vision(
        {
            "path": str(image_path),
            "engine": "moondream",
            "model_path": str(model_path),
            "prompt": "描述页面",
        },
        tmp_path,
    )

    assert observation.metadata["engine_available"] is True
    assert observation.metadata["status"] == "configured"
    assert observation.elements[0]["engine"] == "moondream"
    assert "local moondream backend configured" in observation.elements[0]["text"]


def test_cloud_vision_query_uses_openai_compatible_payload_and_auth(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (10, 8), "white").save(image_path)
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "login button visible"}}]}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("visual_agent.vlm.urllib.request.urlopen", fake_urlopen)

    result = cloud_vision_query(
        str(image_path),
        "Describe UI",
        provider="openai",
        api_key="sk-test-secret-value-123456",
        base_url="https://api.openai.test/v1",
        model="gpt-4o",
        timeout_seconds=7,
    )

    assert result == "login button visible"
    assert captured["url"] == "https://api.openai.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test-secret-value-123456"
    assert captured["body"]["model"] == "gpt-4o"
    assert captured["body"]["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert captured["timeout"] == 7


def test_observe_vision_cloud_uses_env_without_leaking_key(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (10, 8), "white").save(image_path)
    monkeypatch.setenv("VISUAL_AGENT_VLM_PROVIDER", "openai")
    monkeypatch.setenv("VISUAL_AGENT_VLM_API_KEY", "sk-test-secret-value-123456")
    monkeypatch.setenv("VISUAL_AGENT_VLM_BASE_URL", "https://api.openai.test/v1")
    monkeypatch.setenv("VISUAL_AGENT_VLM_MODEL", "gpt-4o")
    monkeypatch.setattr("visual_agent.vlm.cloud_vision_query", lambda *_args, **_kwargs: "cloud description")

    observation = observe_vision({"path": str(image_path), "engine": "auto"}, tmp_path)
    text = json.dumps(observation.metadata, ensure_ascii=False)

    assert observation.metadata["engine"] == "cloud"
    assert observation.metadata["engine_available"] is True
    assert observation.metadata["status"] == "success"
    assert observation.elements[0]["text"] == "cloud description"
    assert "sk-test-secret-value-123456" not in text


def test_observe_vision_cloud_error_falls_back_to_mock(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (10, 8), "white").save(image_path)
    monkeypatch.setenv("VISUAL_AGENT_VLM_PROVIDER", "openai")
    monkeypatch.setenv("VISUAL_AGENT_VLM_API_KEY", "sk-test-secret-value-123456")

    def failing_cloud(*_args, **_kwargs):
        raise RuntimeError("cloud outage")

    monkeypatch.setattr("visual_agent.vlm.cloud_vision_query", failing_cloud)

    observation = observe_vision(
        {
            "path": str(image_path),
            "engine": "cloud",
            "fallback_mock_description": "fallback page description",
        },
        tmp_path,
    )
    text = json.dumps(observation.metadata, ensure_ascii=False)

    assert observation.metadata["engine"] == "mock"
    assert observation.metadata["status"] == "fallback"
    assert observation.metadata["description"] == "fallback page description"
    assert observation.metadata["engine_available"] is True
    assert observation.metadata["fallback_chain"][0]["from"] == "cloud"
    assert observation.metadata["fallback_chain"][-1]["to"] == "mock"
    assert "sk-test-secret-value-123456" not in text


def test_observe_vision_cloud_unavailable_without_fallback_is_explicit(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (10, 8), "white").save(image_path)
    monkeypatch.delenv("VISUAL_AGENT_VLM_API_KEY", raising=False)
    monkeypatch.delenv("VISUAL_AGENT_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    observation = observe_vision(
        {
            "path": str(image_path),
            "engine": "cloud",
            "provider": "openai",
            "credential_source": "__missing_keys__.txt",
            "fallback_mock": False,
        },
        tmp_path,
    )

    assert observation.elements == ()
    assert observation.metadata["engine"] == "cloud"
    assert observation.metadata["engine_available"] is False
    assert observation.metadata["status"] == "error"
    assert observation.metadata["fallback_chain"][0]["status"] == "unavailable"
    assert observation.metadata["fallback_chain"][-1]["to"] == "none"
    assert "missing_api_key" in observation.metadata["engine_status"]["blockers"]


def test_detect_cloud_vision_backend_uses_model_credentials_file(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.txt"
    key_file.write_text("openai api key: sk-openai-secret-value-123456\n", encoding="utf-8")
    monkeypatch.delenv("VISUAL_AGENT_VLM_API_KEY", raising=False)
    monkeypatch.delenv("VISUAL_AGENT_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    status = detect_cloud_vision_backend({"provider": "openai", "credential_source": str(key_file)})
    text = json.dumps(status, ensure_ascii=False)

    assert status["available"] is True
    assert status["provider"] == "openai"
    assert status["credential_source"] == str(key_file)
    assert status["base_url"] == "https://api.openai.com/v1"
    assert status["model"] == "gpt-4o"
    assert "sk-openai-secret-value-123456" in status["_api_key"]
    assert "sk-openai-secret-value-123456" not in json.dumps({k: v for k, v in status.items() if k != "_api_key"}, ensure_ascii=False)


def test_cloud_vision_query_uses_xiaomimimo_auth_style(tmp_path, monkeypatch) -> None:
    image_path = tmp_path / "screen.png"
    Image.new("RGB", (10, 8), "white").save(image_path)
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        return FakeResponse()

    monkeypatch.setattr("visual_agent.vlm.urllib.request.urlopen", fake_urlopen)

    cloud_vision_query(
        str(image_path),
        "Describe UI",
        provider="xiaomimimo",
        api_key="sk-xiaomi-secret-value-abcdef",
        base_url="https://api.xiaomimimo.test/v1",
        model="mimo-v2.5",
    )

    assert captured["headers"]["Api-key"] == "sk-xiaomi-secret-value-abcdef"


def test_detect_cloud_vision_backend_reports_missing_key(monkeypatch) -> None:
    monkeypatch.delenv("VISUAL_AGENT_VLM_API_KEY", raising=False)
    monkeypatch.delenv("VISUAL_AGENT_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    status = detect_cloud_vision_backend({"provider": "openai", "credential_source": "__missing_keys__.txt"})

    assert status["available"] is False
    assert "missing_api_key" in status["blockers"]
    assert "_api_key" in status


def test_vlm_doctor_summary_reports_cloud_config_without_secret(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "keys.txt"
    key_file.write_text("openai api key: sk-openai-secret-value-123456\n", encoding="utf-8")
    monkeypatch.setattr("visual_agent.vlm.module_available", lambda module: False)
    monkeypatch.delenv("VISUAL_AGENT_VLM_API_KEY", raising=False)
    monkeypatch.delenv("VISUAL_AGENT_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    summary = vlm_doctor_summary({"provider": "openai", "credential_source": str(key_file)})
    text = json.dumps(summary, ensure_ascii=False)

    assert summary["ok"] is True
    assert summary["recommended_engine"] == "cloud"
    assert summary["cloud"]["available"] is True
    assert summary["cloud"]["provider"] == "openai"
    assert summary["cloud"]["api_key_configured"] is True
    assert summary["cloud"]["auth_headers_configured"] is True
    assert summary["cloud"]["base_url"] == "https://api.openai.com/v1"
    assert summary["cloud"]["model"] == "gpt-4o"
    assert "sk-openai-secret-value-123456" not in text


def test_vlm_doctor_summary_reports_blockers_without_secret(monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.vlm.module_available", lambda module: False)
    monkeypatch.delenv("VISUAL_AGENT_VLM_API_KEY", raising=False)
    monkeypatch.delenv("VISUAL_AGENT_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    summary = vlm_doctor_summary({"provider": "openai", "credential_source": "__missing_keys__.txt"})

    assert summary["ok"] is False
    assert summary["recommended_engine"] == "mock"
    assert "missing_api_key" in summary["cloud"]["blockers"]
    assert summary["local"]["qwen2-vl"]["module_available"] is False
