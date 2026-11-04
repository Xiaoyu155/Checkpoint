from visual_agent import cheap_backends, gemini4_vps_client, grok_bridge, model_catalog


def test_unconfigured_endpoints_and_ssh_are_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("PACER_MODEL_PROFILES", str(tmp_path / "profiles.json"))
    monkeypatch.delenv("PACER_GEMINI4_BASE_URL", raising=False)
    monkeypatch.delenv("PACER_GEMINI4_API_KEY", raising=False)
    row = model_catalog.load_profiles()["providers"]["gemini4"]
    assert row["base_url"] == ""
    assert row["allow_ssh_fallback"] is False
    monkeypatch.setattr(gemini4_vps_client, "live_http_call_blocked", lambda: False)
    result = gemini4_vps_client.invoke_gemini4_vps("test")
    assert not result.success and "PACER_GEMINI4_BASE_URL" in result.error


def test_cheap_backends_ignore_plaintext_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for spec in cheap_backends._INTAKE_BACKENDS:
        for name in spec["env_names"]:
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "model_api_keys.txt").write_text("deepseek sk-" + "x" * 24)
    assert cheap_backends.resolve_cheap_backends() == []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + "x" * 24)
    assert len(cheap_backends.resolve_cheap_backends()) == 1


def test_grok_missing_base_is_explicit(monkeypatch):
    monkeypatch.setattr(grok_bridge, "_read_pacer_credentials", lambda: {})
    monkeypatch.delenv("PACER_GROK_API_BASE", raising=False)
    monkeypatch.delenv("SUB2API_API_BASE", raising=False)
    monkeypatch.setenv("PACER_GROK_API_KEY", "test-only")
    assert "PACER_GROK_API_BASE" in grok_bridge.invoke_grok("test").error
