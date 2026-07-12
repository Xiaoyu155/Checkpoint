from visual_agent.models import Observation, ProviderKind, Target
from visual_agent.selector import SelectorResolver
from visual_agent.uia import UIAutomationProvider, element_accessible_name, element_bounds, find_uia_element_bounds, normalize_control_type
from visual_agent.fixtures import load_observation_fixture


def test_normalize_control_type_removes_control_suffix() -> None:
    assert normalize_control_type("ButtonControl") == "button"
    assert normalize_control_type("Edit Control") == "edit"


def test_uia_accessible_name_uses_name_and_automation_id() -> None:
    element = {
        "name": "确定",
        "automation_id": "okButton",
        "class_name": "Button",
    }

    assert element_accessible_name(element) == "确定 okbutton button"


def test_uia_element_bounds_rejects_zero_size() -> None:
    assert element_bounds({"bounds": {"left": 0, "top": 0, "width": 0, "height": 30}}) is None


def test_uia_selector_strategy_resolves_matching_control() -> None:
    observation = Observation(
        provider=ProviderKind.UIA,
        source="windows-desktop",
        elements=(
            {
                "name": "取消",
                "automation_id": "cancelButton",
                "control_type": "button",
                "bounds": {"left": 10, "top": 20, "width": 100, "height": 40},
            },
            {
                "name": "确定",
                "automation_id": "okButton",
                "control_type": "button",
                "bounds": {"left": 300, "top": 200, "width": 120, "height": 48},
            },
        ),
    )

    resolved = SelectorResolver().resolve(Target(text="确定", role="button"), observation)

    assert resolved.evidence.provider == ProviderKind.UIA
    assert resolved.evidence.handle == "okButton"
    assert resolved.click_point.x == 360
    assert resolved.click_point.y == 224


def test_uia_selector_falls_back_to_mock_when_no_control_matches() -> None:
    observation = Observation(
        provider=ProviderKind.UIA,
        source="windows-desktop",
        width=1280,
        height=720,
        elements=(
            {
                "name": "取消",
                "automation_id": "cancelButton",
                "control_type": "button",
                "bounds": {"left": 10, "top": 20, "width": 100, "height": 40},
            },
        ),
    )

    resolved = SelectorResolver().resolve(Target.from_text("确定"), observation)

    assert resolved.evidence.provider == ProviderKind.MOCK
    assert resolved.click_point.x == 640


def test_uia_fixture_resolves_notepad_demo_controls() -> None:
    observation = load_observation_fixture("examples/fixtures/windows_notepad_observation.json")

    subject = SelectorResolver().resolve(
        Target(text="主题输入框", role="edit", preferred=(ProviderKind.UIA,)),
        observation,
    )
    save = SelectorResolver().resolve(
        Target(text="保存", role="button", preferred=(ProviderKind.UIA,)),
        observation,
    )

    assert observation.provider == ProviderKind.UIA
    assert subject.evidence.handle == "subjectEdit"
    assert save.evidence.handle == "saveButton"


def test_find_uia_element_bounds_matches_window_title(monkeypatch) -> None:
    observation = Observation(
        provider=ProviderKind.UIA,
        source="windows-desktop",
        elements=(
            {
                "name": "微信开发者工具 - miniprogram",
                "automation_id": "",
                "control_type": "window",
                "class_name": "WeChatDevTools",
                "bounds": {"left": 120, "top": 100, "width": 300, "height": 200},
            },
            {
                "name": "微信开发者工具 - miniprogram",
                "automation_id": "",
                "control_type": "window",
                "class_name": "WeChatDevTools",
                "bounds": {"left": 100, "top": 80, "width": 1200, "height": 900},
            },
        ),
    )
    monkeypatch.setattr(UIAutomationProvider, "observe_desktop", lambda _self: observation)

    bounds = find_uia_element_bounds({"title_contains": "微信开发者工具", "control_type": "window"})

    assert bounds.left == 100
    assert bounds.top == 80
    assert bounds.width == 1200
    assert bounds.height == 900
