import warnings

from visual_agent.dispatcher import ActionDispatcher
from visual_agent.models import ActionResult, ActionStatus
from visual_agent.plugins import load_action_plugins, load_provider_plugins
from visual_agent.providers import ProviderRegistry


def test_load_action_plugins_no_plugins_installed() -> None:
    dispatcher = ActionDispatcher()

    loaded = load_action_plugins(dispatcher)

    assert isinstance(loaded, dict)


def test_load_action_plugins_registers_entrypoint(monkeypatch, tmp_path) -> None:
    import visual_agent.plugins as plugins

    def handler(_resolved, _params, _context):
        return ActionResult(action="custom_action", status=ActionStatus.SUCCESS, target="custom")

    class FakeEP:
        name = "custom_action"
        value = "tests:handler"

        def load(self):
            return handler

    monkeypatch.setattr(plugins, "entry_points", lambda group: [FakeEP()] if group == "visual_agent.actions" else [])
    dispatcher = ActionDispatcher()

    loaded = load_action_plugins(dispatcher)

    assert loaded == {"custom_action": "tests:handler"}
    assert "custom_action" in dispatcher.actions_available


def test_load_action_plugins_bad_entrypoint_warns(monkeypatch) -> None:
    import visual_agent.plugins as plugins

    class FakeEP:
        name = "bad_action"
        value = "nonexistent:handler"

        def load(self):
            raise ImportError("not found")

    dispatcher = ActionDispatcher()
    monkeypatch.setattr(plugins, "entry_points", lambda group: [FakeEP()] if group == "visual_agent.actions" else [])

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        loaded = load_action_plugins(dispatcher)

    assert loaded == {}
    assert any("bad_action" in str(warning.message) for warning in captured)


def test_load_provider_plugins_registers_entrypoint(monkeypatch) -> None:
    import visual_agent.plugins as plugins

    def provider(_params, _context):
        raise RuntimeError("not called")

    class FakeEP:
        name = "observe_custom"
        value = "tests:provider"

        def load(self):
            return provider

    registry = ProviderRegistry()
    monkeypatch.setattr(plugins, "entry_points", lambda group: [FakeEP()] if group == "visual_agent.providers" else [])

    loaded = load_provider_plugins(registry)

    assert loaded == {"observe_custom": "tests:provider"}
    assert "observe_custom" in registry.actions
