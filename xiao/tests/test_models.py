from pathlib import Path

from visual_agent.models import Bounds, ProviderKind, RunResult, Target, to_jsonable
from visual_agent.selector import MockSelectorStrategy, SelectorResolver


def test_bounds_center_uses_integer_midpoint() -> None:
    assert Bounds(left=10, top=20, width=101, height=51).center.x == 60
    assert Bounds(left=10, top=20, width=101, height=51).center.y == 45


def test_target_from_text_has_default_provider_order() -> None:
    target = Target.from_text("登录")

    assert target.display_name == "登录"
    assert target.preferred[-1] == ProviderKind.MOCK


def test_to_jsonable_converts_paths_and_enums() -> None:
    payload = to_jsonable({"path": Path(".runs/demo"), "provider": ProviderKind.MOCK})

    assert payload == {"path": ".runs\\demo", "provider": "mock"}


def test_run_result_is_importable_public_model() -> None:
    assert RunResult.__name__ == "RunResult"
    assert MockSelectorStrategy().provider == ProviderKind.MOCK
    assert isinstance(SelectorResolver(), SelectorResolver)

