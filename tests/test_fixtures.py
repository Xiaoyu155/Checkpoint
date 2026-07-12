from pathlib import Path

from visual_agent.fixtures import load_observation_fixture, observation_from_dict
from visual_agent.models import ProviderKind


def test_observation_from_dict_defaults_source() -> None:
    observation = observation_from_dict({"provider": "dom"}, source_fallback="fixture://demo")

    assert observation.provider == ProviderKind.DOM
    assert observation.source == "fixture://demo"
    assert observation.elements == ()


def test_load_observation_fixture_reads_dom_elements() -> None:
    path = Path("examples/fixtures/login_page_observation.json")

    observation = load_observation_fixture(path)

    assert observation.provider == ProviderKind.DOM
    assert observation.elements[-1]["text"] == "登录"

