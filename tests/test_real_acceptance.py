from types import SimpleNamespace

from visual_agent.real_acceptance import build_real_acceptance_readiness, real_acceptance_readiness_to_markdown


def capability(name: str, available: bool = True, install_hint: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, available=available, install_hint=install_hint)


def test_real_acceptance_readiness_reports_browser_lane_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        "visual_agent.real_acceptance.build_capability_manifest",
        lambda: SimpleNamespace(
            capabilities=(
                capability("playwright"),
                capability("observe_browser"),
                capability("click"),
                capability("mss"),
                capability("pyautogui"),
                capability("observe_ocr"),
                capability("click_text"),
                capability("uiautomation", False, "pip install -e .[desktop]"),
                capability("observe_uia", False),
            )
        ),
    )
    monkeypatch.setattr("visual_agent.real_acceptance.detect_screen_ocr", lambda: {"engine": "screen-ocr", "available": False, "install_hint": "install screen-ocr"})
    monkeypatch.setattr("visual_agent.real_acceptance.detect_tesseract", lambda: {"engine": "tesseract", "available": False, "install_hint": "install tesseract"})

    payload = build_real_acceptance_readiness(workspace_root=".agent-workspace")

    assert payload["ready"] is True
    assert payload["ready_lanes"] == ["browser"]
    assert payload["browser"]["ready"] is True
    assert payload["desktop_ocr"]["ready"] is False
    assert "missing_real_ocr_engine" in payload["desktop_ocr"]["blockers"]


def test_real_acceptance_readiness_markdown_is_actionable(monkeypatch) -> None:
    monkeypatch.setattr(
        "visual_agent.real_acceptance.build_capability_manifest",
        lambda: SimpleNamespace(
            capabilities=(
                capability("playwright", False, "pip install -e .[web]"),
                capability("observe_browser", False),
                capability("click"),
                capability("mss"),
                capability("pyautogui"),
                capability("observe_ocr"),
                capability("click_text"),
                capability("uiautomation", False, "pip install -e .[desktop]"),
                capability("observe_uia", False),
            )
        ),
    )
    monkeypatch.setattr("visual_agent.real_acceptance.detect_screen_ocr", lambda: {"engine": "screen-ocr", "available": False, "install_hint": "install screen-ocr"})
    monkeypatch.setattr("visual_agent.real_acceptance.detect_tesseract", lambda: {"engine": "tesseract", "available": False, "install_hint": "install tesseract"})

    payload = build_real_acceptance_readiness()
    markdown = real_acceptance_readiness_to_markdown(payload)

    assert payload["ready"] is False
    assert "missing_playwright" in payload["browser"]["blockers"]
    assert "Next command" in markdown
    assert "missing_real_ocr_engine" in markdown
