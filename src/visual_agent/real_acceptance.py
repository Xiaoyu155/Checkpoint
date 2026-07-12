from __future__ import annotations

from typing import Any

from .capabilities import build_capability_manifest
from .models import to_jsonable
from .ocr import detect_screen_ocr, detect_tesseract


def build_real_acceptance_readiness(*, workspace_root: str = ".agent-workspace") -> dict[str, Any]:
    manifest = build_capability_manifest()
    available = {str(item.name) for item in manifest.capabilities if item.available}
    by_name = {str(item.name): item for item in manifest.capabilities}
    screen_ocr = detect_screen_ocr()
    tesseract = detect_tesseract()

    browser_blockers = []
    if "playwright" not in available:
        browser_blockers.append("missing_playwright")
    if "observe_browser" not in available:
        browser_blockers.append("missing_observe_browser")
    if "click" not in available:
        browser_blockers.append("missing_click_action")

    real_ocr_available = bool(screen_ocr.get("available") or tesseract.get("available"))
    desktop_blockers = []
    for dependency in ("mss", "pyautogui"):
        if dependency not in available:
            desktop_blockers.append(f"missing_{dependency}")
    if "observe_ocr" not in available:
        desktop_blockers.append("missing_observe_ocr")
    if "click_text" not in available:
        desktop_blockers.append("missing_click_text")
    if not real_ocr_available:
        desktop_blockers.append("missing_real_ocr_engine")

    uia_blockers = []
    if "uiautomation" not in available:
        uia_blockers.append("missing_uiautomation")
    if "observe_uia" not in available:
        uia_blockers.append("missing_observe_uia")

    browser_ready = not browser_blockers
    desktop_ready = not desktop_blockers
    uia_ready = not uia_blockers
    ready_lanes = [
        name
        for name, ready in (
            ("browser", browser_ready),
            ("desktop_ocr", desktop_ready),
            ("windows_uia", uia_ready),
        )
        if ready
    ]
    blockers = sorted(set(browser_blockers + desktop_blockers + uia_blockers))

    return {
        "schema_version": 1,
        "workspace_root": workspace_root,
        "ready": bool(ready_lanes),
        "ready_lanes": ready_lanes,
        "browser": {
            "ready": browser_ready,
            "blockers": browser_blockers,
            "next_command": f"python -m visual_agent.cli verify-now --workspace-root {workspace_root} --live --format markdown"
            if browser_ready
            else "pip install -e .[web] && python -m playwright install chromium",
        },
        "desktop_ocr": {
            "ready": desktop_ready,
            "blockers": desktop_blockers,
            "screen_ocr": public_engine_status(screen_ocr),
            "tesseract": public_engine_status(tesseract),
            "next_command": f"python -m visual_agent.cli install-template --root {workspace_root} --template desktop_ocr_real_acceptance --overwrite"
            if desktop_ready
            else "Install screen-ocr[winrt] on Windows, or install pytesseract plus the Tesseract binary.",
        },
        "windows_uia": {
            "ready": uia_ready,
            "blockers": uia_blockers,
            "next_command": "python -m visual_agent.cli run-workflow --workflow <uia_workflow.yaml> --run-profile supervised"
            if uia_ready
            else "pip install -e .[desktop]",
        },
        "blockers": blockers,
        "install_hints": install_hints(blockers, by_name),
    }


def public_engine_status(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "engine": status.get("engine"),
        "available": bool(status.get("available")),
        "module_available": bool(status.get("module_available")),
        "binary_path": status.get("binary_path"),
        "version": status.get("version"),
        "install_hint": status.get("install_hint"),
        "error": status.get("error"),
    }


def install_hints(blockers: list[str], by_name: dict[str, Any]) -> list[dict[str, Any]]:
    hints = []
    for blocker in blockers:
        name = blocker.removeprefix("missing_")
        capability = by_name.get(name)
        hint = getattr(capability, "install_hint", None) if capability is not None else None
        if blocker == "missing_real_ocr_engine":
            hint = "Install screen-ocr[winrt] on Windows, or install pytesseract plus the Tesseract binary."
        hints.append({"blocker": blocker, "install_hint": hint})
    return hints


def real_acceptance_readiness_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Real Acceptance Readiness",
        "",
        f"- Ready: `{bool(payload.get('ready'))}`",
        f"- Ready lanes: `{', '.join(payload.get('ready_lanes') or []) or 'none'}`",
        f"- Workspace: `{payload.get('workspace_root')}`",
    ]
    for lane in ("browser", "desktop_ocr", "windows_uia"):
        data = payload.get(lane) if isinstance(payload.get(lane), dict) else {}
        blockers = list(data.get("blockers") or [])
        lines.extend(
            [
                "",
                f"## {lane}",
                f"- Ready: `{bool(data.get('ready'))}`",
                f"- Blockers: `{', '.join(blockers) or 'none'}`",
                f"- Next command: `{data.get('next_command') or ''}`",
            ]
        )
    hints = payload.get("install_hints") if isinstance(payload.get("install_hints"), list) else []
    if hints:
        lines.extend(["", "## Fix First"])
        for item in hints:
            if isinstance(item, dict):
                lines.append(f"- `{item.get('blocker')}`: {item.get('install_hint') or 'no install hint'}")
    return "\n".join(lines).rstrip() + "\n"


def real_acceptance_readiness_to_jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    return to_jsonable(payload)
