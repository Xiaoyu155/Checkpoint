import json
import os
from pathlib import Path

import pytest

from visual_agent import provider_runtime as runtime


def config(data):
    path = runtime.providers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_template_is_exclusively_created_without_overwriting_user_data(tmp_path):
    path = runtime.ensure_template(tmp_path)
    assert json.loads(path.read_text())["tier3_premium_escalation"][0]["provider"] == "anthropic"
    path.write_text('{"private_user_config": true}')
    runtime.ensure_template(tmp_path)
    assert path.read_text() == '{"private_user_config": true}'


def test_disabled_node_stays_disabled_even_if_provider_key_exists(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    config({"tier2_domestic": [{"enabled": False, "provider": "deepseek", "model": "test"}]})
    assert not runtime.api_nodes("domestic_api")[0].is_available()


def test_disabled_architect_stays_disabled_even_if_environment_key_exists(monkeypatch):
    monkeypatch.setenv("PACER_GEMINI4_API_KEY", "test-only")
    config({"tier4_architect_reviewer": {"gemini4": {"enabled": False, "provider": "gemini", "model": "test"}}})
    assert not runtime.architect_node().is_available()


def test_status_never_claims_remote_key_is_online_or_prints_it():
    config({"tier1_free": {"antigravity_gemini38": {"enabled": False}, "workbuddy": {"enabled": False}},
            "tier3_premium_escalation": [{"enabled": True, "provider": "anthropic", "model": "test", "api_key": "secret-for-test", "base_url": "https://no-network.invalid/v1"}]})
    text = runtime.format_status_table()
    assert "configured_unverified" in text
    assert "secret-for-test" not in text


@pytest.mark.parametrize("url", ["https://remote.invalid", "http://127.0.0.1:8091", "http://[::1]:8091", "http://127.0.0.1:18199"])
def test_antigravity_rejects_remote_and_self_endpoints(url, monkeypatch):
    config({})
    gateway = Path(os.environ["PACER_GATEWAY_CONFIG"])
    gateway.write_text(json.dumps({"port": 18199}))
    monkeypatch.setenv("PACER_ANTIGRAVITY_URL", url)
    node = runtime.antigravity_node()
    assert node.blockers
    assert not node.is_available()


def test_antigravity_cli_fallback_can_be_disabled_by_provider_config(monkeypatch):
    config({"tier1_free": {"antigravity_gemini38": {"enabled": True, "cli_fallback": False}}})
    monkeypatch.setattr(runtime, "tcp_reachable", lambda _url: False)
    monkeypatch.setattr("visual_agent.gemini_bridge.find_gemini_executable", lambda: "gemini")

    node = runtime.antigravity_node()

    assert node.cli_available
    assert not node.cli_fallback
    assert not node.is_available()


def test_reading_missing_config_does_not_write_a_template():
    path = runtime.providers_path()
    assert not path.exists()
    runtime.load_providers()
    assert not path.exists()


def test_invalid_configuration_fails_closed_without_echoing_secrets():
    path = config({"tier2_domestic": "secret-do-not-echo"})
    with pytest.raises(ValueError) as exc:
        runtime.load_providers()
    assert "secret-do-not-echo" not in str(exc.value)
    path.write_text('{"api_key": "secret-do-not-echo"')
    with pytest.raises(ValueError) as exc:
        runtime.load_providers()
    assert "secret-do-not-echo" not in str(exc.value)


# --- Gemini 4 account-pool gate (Phase 8: zero-second fallback) -------------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def getcode(self):
        return 200

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture(autouse=True)
def _clear_pool_cache():
    from visual_agent import gemini4_vps_client
    gemini4_vps_client._reset_pool_state_cache()
    yield
    gemini4_vps_client._reset_pool_state_cache()


def _fake_health(monkeypatch, payload):
    from visual_agent import gemini4_vps_client
    real_urlopen = gemini4_vps_client.urllib.request.urlopen

    def _fake(request, *args, **kwargs):
        url = getattr(request, "full_url", "") or ""
        if not url.endswith("/gemini4/health"):
            return real_urlopen(request, *args, **kwargs)
        return _FakeResponse(payload)

    monkeypatch.setattr(gemini4_vps_client.urllib.request, "urlopen", _fake)


def test_empty_account_pool_makes_architect_unavailable_without_a_request(monkeypatch):
    from visual_agent import gemini4_vps_client
    _fake_health(monkeypatch, {"available_accounts": 0, "pool_size": 8})
    monkeypatch.setenv("PACER_GEMINI4_API_KEY", "test-only")
    monkeypatch.setenv("PACER_GEMINI4_BASE_URL", "https://direct.aiboxz.com/gemini4")
    config({"tier4_architect_reviewer": {"gemini4": {"enabled": True, "provider": "gemini",
                                                     "model": "gemini-4"}}})
    node = runtime.architect_node()
    assert "empty_account_pool" in node.blockers
    assert not node.is_available()


def test_populated_account_pool_keeps_architect_available(monkeypatch):
    _fake_health(monkeypatch, {"available_accounts": 5, "pool_size": 8})
    monkeypatch.setenv("PACER_GEMINI4_API_KEY", "test-only")
    monkeypatch.setenv("PACER_GEMINI4_BASE_URL", "https://direct.aiboxz.com/gemini4")
    config({"tier4_architect_reviewer": {"gemini4": {"enabled": True, "provider": "gemini",
                                                     "model": "gemini-4"}}})
    node = runtime.architect_node()
    assert "empty_account_pool" not in node.blockers
    assert node.is_available()


def test_unreachable_gateway_makes_architect_unavailable(monkeypatch):
    from visual_agent import gemini4_vps_client

    def _boom(*_args, **_kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(gemini4_vps_client.urllib.request, "urlopen", _boom)
    monkeypatch.setenv("PACER_GEMINI4_API_KEY", "test-only")
    monkeypatch.setenv("PACER_GEMINI4_BASE_URL", "https://direct.aiboxz.com/gemini4")
    config({"tier4_architect_reviewer": {"gemini4": {"enabled": True, "provider": "gemini",
                                                     "model": "gemini-4"}}})
    node = runtime.architect_node()
    assert "empty_account_pool" in node.blockers
    assert not node.is_available()


def test_pool_gate_does_not_apply_to_a_non_gateway_base_url(monkeypatch):
    """A direct Google API key is not the account pool — do not gate it."""
    _fake_health(monkeypatch, {"available_accounts": 0, "pool_size": 8})
    monkeypatch.setenv("PACER_GEMINI4_API_KEY", "test-only")
    monkeypatch.setenv("PACER_GEMINI4_BASE_URL", "https://generativelanguage.googleapis.com")
    config({"tier4_architect_reviewer": {"gemini4": {"enabled": True, "provider": "gemini",
                                                     "model": "gemini-4"}}})
    node = runtime.architect_node()
    assert "empty_account_pool" not in node.blockers
    assert node.is_available()


def test_disabled_architect_never_probes_the_pool(monkeypatch):
    """A disabled slot must not trigger a network probe at all."""
    def _boom(*_args, **_kwargs):
        raise AssertionError("pool probe must not run for a disabled slot")

    from visual_agent import gemini4_vps_client
    monkeypatch.setattr(gemini4_vps_client.urllib.request, "urlopen", _boom)
    monkeypatch.setenv("PACER_GEMINI4_API_KEY", "test-only")
    monkeypatch.setenv("PACER_GEMINI4_BASE_URL", "https://direct.aiboxz.com/gemini4")
    config({"tier4_architect_reviewer": {"gemini4": {"enabled": False, "provider": "gemini",
                                                     "model": "gemini-4"}}})
    node = runtime.architect_node()
    assert not node.is_available()


def test_pool_state_reports_reachable_and_accounts(monkeypatch):
    from visual_agent import gemini4_vps_client
    _fake_health(monkeypatch, {"available_accounts": 3, "pool_size": 8})
    state = gemini4_vps_client.gemini4_pool_state()
    assert state["reachable"] is True
    assert state["available_accounts"] == 3
    assert state["pool_size"] == 8
    assert gemini4_vps_client.is_gemini4_vps_pool_healthy() is True


def test_empty_account_pool_falls_back_to_a_local_plan_without_calling_gemini4(tmp_path, monkeypatch):
    """Phase 8: an empty pool must trigger the zero-second local fallback."""
    from visual_agent import gemini4_vps_client, provider_runtime
    from visual_agent.model_workflow import plan_with_gemini4

    _fake_health(monkeypatch, {"available_accounts": 0, "pool_size": 8})
    monkeypatch.setenv("PACER_GEMINI4_API_KEY", "test-only")
    monkeypatch.setenv("PACER_GEMINI4_BASE_URL", "https://direct.aiboxz.com/gemini4")
    config({"tier4_architect_reviewer": {"gemini4": {"enabled": True, "provider": "gemini",
                                                     "model": "gemini-4"}}})

    def _must_not_call(*_args, **_kwargs):
        raise AssertionError("Gemini 4 must not be called while the account pool is empty")

    monkeypatch.setattr(gemini4_vps_client, "invoke_gemini4_vps", _must_not_call)
    monkeypatch.setattr(provider_runtime, "invoke_architect", _must_not_call)

    plan = plan_with_gemini4("重构支付子系统的架构", tmp_path)
    assert plan.planner_backend == "local"


def test_planner_checks_direct_vps_pool_when_provider_slot_is_disabled(tmp_path, monkeypatch):
    from visual_agent import gemini4_vps_client, model_workflow, provider_runtime
    from visual_agent.model_workflow import plan_with_gemini4

    config({"tier4_architect_reviewer": {"gemini4": {"enabled": False, "provider": "gemini", "model": "gemini-4"}}})
    settings = {"enabled": True, "base_url": "https://direct.aiboxz.com/gemini4", "model": "gemini-4", "api_key": "test-only"}
    monkeypatch.setattr(gemini4_vps_client, "gemini4_settings", lambda: settings)
    monkeypatch.setattr(gemini4_vps_client, "is_gemini4_vps_available", lambda: True)
    monkeypatch.setattr(provider_runtime, "_is_vps_gateway", lambda _url: True)
    monkeypatch.setattr(gemini4_vps_client, "is_gemini4_vps_pool_healthy", lambda: False)
    sentinel = object()
    monkeypatch.setattr(model_workflow, "_local_rule_plan", lambda _goal, _root: sentinel)
    monkeypatch.setattr(gemini4_vps_client, "invoke_gemini4_vps", lambda *_a, **_k: pytest.fail("Empty VPS pool must not receive a generation request"))

    assert plan_with_gemini4("Design a hard system", tmp_path) is sentinel


@pytest.mark.parametrize("pool_healthy", [False, True])
def test_reviewer_candidates_discover_direct_gemini4_without_pool_probe(tmp_path, monkeypatch, pool_healthy):
    from types import SimpleNamespace
    from visual_agent import gemini4_vps_client, grok_bridge, model_workflow, provider_runtime, workbuddy_bridge

    config({"tier4_architect_reviewer": {"gemini4": {"enabled": False, "provider": "gemini", "model": "gemini-4"}}})
    settings = {"enabled": True, "base_url": "https://direct.aiboxz.com/gemini4", "model": "gemini-4", "api_key": "test-only"}
    unavailable = SimpleNamespace(is_available=lambda: False)
    monkeypatch.setattr(provider_runtime, "antigravity_node", lambda: unavailable)
    monkeypatch.setattr(provider_runtime, "api_nodes", lambda _slot: [])
    monkeypatch.setattr(provider_runtime, "architect_node", lambda: unavailable)
    monkeypatch.setattr(workbuddy_bridge, "get_gateway_info", lambda: None)
    monkeypatch.setattr(grok_bridge, "is_grok_available", lambda: False)
    monkeypatch.setattr(gemini4_vps_client, "gemini4_settings", lambda: settings)
    monkeypatch.setattr(gemini4_vps_client, "is_gemini4_vps_available", lambda: True)
    monkeypatch.setattr(provider_runtime, "_is_vps_gateway", lambda _url: True)
    monkeypatch.setattr(gemini4_vps_client, "is_gemini4_vps_pool_healthy", lambda: pytest.fail("Discovery must not probe the network"))

    candidates = model_workflow._adversarial_reviewer_candidates()
    gemini4 = [row for row in candidates if row["backend"] == "gemini4_vps"]

    assert len(gemini4) == 1
    assert gemini4[0]["model"] == "gemini-4"
    assert gemini4[0].get("node") is None


def test_reviewer_discovery_failures_are_logged_and_leave_no_candidates(monkeypatch, caplog):
    from visual_agent import gemini4_vps_client, grok_bridge, model_workflow, provider_runtime, workbuddy_bridge
    from visual_agent import subprocess_window

    def fail(*_args, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(subprocess_window, "resolve_codex_executable", lambda: None)
    monkeypatch.setattr(provider_runtime, "antigravity_node", fail)
    monkeypatch.setattr(provider_runtime, "api_nodes", fail)
    monkeypatch.setattr(provider_runtime, "architect_node", fail)
    monkeypatch.setattr(workbuddy_bridge, "get_gateway_info", fail)
    monkeypatch.setattr(grok_bridge, "is_grok_available", fail)
    monkeypatch.setattr(gemini4_vps_client, "gemini4_settings", fail)

    with caplog.at_level("WARNING", logger="visual_agent.model_workflow"):
        candidates = model_workflow._adversarial_reviewer_candidates()

    assert candidates == []
    failures = [record for record in caplog.records if record.message == "reviewer unavailable" and record.skipped_reason.startswith("discovery_error:")]
    assert {record.backend for record in failures} >= {
        "gemini38", "workbuddy", "grok", "domestic_api", "premium_api", "gemini4_vps",
        "gemini4_vps_explicit",
    }
    assert all(record.skipped_reason == "discovery_error:RuntimeError" for record in failures)
