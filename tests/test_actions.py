from visual_agent import actions
from visual_agent.actions import DesktopActions
from visual_agent.security import text_metadata
from visual_agent.models import ActionStatus, Point, ProviderKind, Target


def test_desktop_actions_constructor_keeps_gui_backend_lazy(monkeypatch) -> None:
    monkeypatch.setattr(actions.pyautogui, "_module", None)

    DesktopActions()

    assert actions.pyautogui._module is None


def test_text_metadata_masks_preview() -> None:
    assert text_metadata("abcdef") == {"sensitive": False, "text_length": 6, "text_preview": "abc***"}
    assert text_metadata("ab") == {"sensitive": False, "text_length": 2, "text_preview": "***"}


def test_type_text_dry_run_records_metadata() -> None:
    result = DesktopActions().type_text(
        "demo_user",
        Target.from_text("用户名"),
        point=Point(10, 20),
        provider=ProviderKind.DOM,
        dry_run=True,
    )

    assert result.status == ActionStatus.DRY_RUN
    assert result.action == "type"
    assert result.metadata["text_length"] == 9


def test_paste_text_dry_run_records_metadata() -> None:
    result = DesktopActions().paste_text(
        "secret",
        Target.from_text("密码"),
        point=Point(10, 20),
        provider=ProviderKind.DOM,
        dry_run=True,
    )

    assert result.status == ActionStatus.DRY_RUN
    assert result.action == "paste"
    assert result.metadata["text_preview"] == "sec***"
    assert result.metadata["clipboard"] is False


def test_paste_text_sensitive_dry_run_hashes_metadata() -> None:
    result = DesktopActions().paste_text(
        "secret",
        Target.from_text("密码"),
        point=Point(10, 20),
        provider=ProviderKind.DOM,
        dry_run=True,
        sensitive=True,
    )

    assert result.metadata["sensitive"] is True
    assert "sha256" in result.metadata
    assert "text_length" not in result.metadata


def test_click_releases_buttons_and_uses_explicit_left_button(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr("visual_agent.actions.pyautogui.mouseUp", lambda **kwargs: calls.append(("mouseUp", kwargs)))
    monkeypatch.setattr("visual_agent.actions.pyautogui.click", lambda **kwargs: calls.append(("click", kwargs)))

    result = DesktopActions().click(
        Point(10, 20),
        Target.from_text("提交"),
        provider=ProviderKind.OCR,
        dry_run=False,
    )

    assert result.status == ActionStatus.SUCCESS
    assert calls[:3] == [
        ("mouseUp", {"button": "left"}),
        ("mouseUp", {"button": "right"}),
        ("mouseUp", {"button": "middle"}),
    ]
    assert calls[3] == ("click", {"x": 10, "y": 20, "button": "left"})


def test_paste_text_defaults_to_keyboard_write_without_clipboard(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr("visual_agent.actions.pyautogui.mouseUp", lambda **kwargs: calls.append(("mouseUp", kwargs)))
    monkeypatch.setattr("visual_agent.actions.pyautogui.click", lambda **kwargs: calls.append(("click", kwargs)))
    monkeypatch.setattr("visual_agent.actions.pyautogui.keyUp", lambda key: calls.append(("keyUp", key)))
    monkeypatch.setattr("visual_agent.actions.pyautogui.write", lambda text, interval: calls.append(("write", text, interval)))
    monkeypatch.setattr("visual_agent.actions.pyautogui.hotkey", lambda *args: calls.append(("hotkey", args)))
    monkeypatch.setattr("visual_agent.actions.pyperclip.copy", lambda value: calls.append(("copy", value)))

    result = DesktopActions().paste_text(
        "new value",
        Target.from_text("输入框"),
        point=Point(1, 2),
        provider=ProviderKind.OCR,
        dry_run=False,
    )

    assert result.status == ActionStatus.SUCCESS
    assert result.metadata["clipboard"] is False
    assert ("click", {"x": 1, "y": 2, "button": "left"}) in calls
    assert ("write", "new value", 0.01) in calls
    assert not any(call[0] in {"hotkey", "copy"} for call in calls)


def test_paste_text_restores_clipboard_after_hotkey_when_enabled(monkeypatch) -> None:
    calls = []
    clipboard = {"value": "existing"}

    monkeypatch.setattr("visual_agent.actions.pyautogui.mouseUp", lambda **kwargs: calls.append(("mouseUp", kwargs)))
    monkeypatch.setattr("visual_agent.actions.pyautogui.click", lambda **kwargs: calls.append(("click", kwargs)))
    monkeypatch.setattr("visual_agent.actions.pyautogui.hotkey", lambda *args: calls.append(("hotkey", args)))
    monkeypatch.setattr("visual_agent.actions.pyperclip.paste", lambda: clipboard["value"])

    def copy(value):
        calls.append(("copy", value))
        clipboard["value"] = value

    monkeypatch.setattr("visual_agent.actions.pyperclip.copy", copy)

    result = DesktopActions().paste_text(
        "new value",
        Target.from_text("输入框"),
        point=Point(1, 2),
        provider=ProviderKind.OCR,
        dry_run=False,
        use_clipboard=True,
    )

    assert result.status == ActionStatus.SUCCESS
    assert result.metadata["clipboard"] is True
    assert ("click", {"x": 1, "y": 2, "button": "left"}) in calls
    assert ("copy", "new value") in calls
    assert ("hotkey", ("ctrl", "v")) in calls
    assert calls[-1] == ("copy", "existing")
    assert clipboard["value"] == "existing"
