from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any


PLAYWRIGHT_INSTALL_HINT = (
    "pip install -e .[web] && python -m playwright install chromium"
)


def playwright_module_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def ensure_playwright_browsers_path(*anchors: str | Path | None) -> Path | None:
    """Use the repo-local Playwright browser cache when bootstrap installed it.

    Playwright only reads PLAYWRIGHT_BROWSERS_PATH at runtime. The bootstrap
    script installs browsers into `.pw-browsers`, so direct CLI invocations from
    an editable checkout should find that cache without requiring users to set
    an environment variable every time.
    """
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return Path(configured).expanduser()
    local_path = find_local_playwright_browsers_path(*anchors)
    if local_path is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_path)
    return local_path


def find_local_playwright_browsers_path(*anchors: str | Path | None) -> Path | None:
    seen: set[Path] = set()
    candidates: list[Path] = []

    def add_candidate(path: Path) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            candidates.append(resolved)

    for anchor in anchors:
        if anchor is None:
            continue
        base = Path(anchor)
        if base.name == ".agent-workspace":
            add_candidate(base.parent / ".pw-browsers")
        add_candidate(base / ".pw-browsers")
        add_candidate(base.parent / ".pw-browsers")

    add_candidate(Path.cwd() / ".pw-browsers")
    try:
        add_candidate(Path(__file__).resolve().parents[2] / ".pw-browsers")
    except IndexError:
        pass

    for candidate in candidates:
        if is_playwright_browsers_dir(candidate):
            return candidate
    return None


def is_playwright_browsers_dir(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    if (path / ".links").exists():
        return True
    return any(path.glob("chromium*")) or any(path.glob("ffmpeg-*"))


def playwright_runtime_status(*anchors: str | Path | None) -> dict[str, Any]:
    path = ensure_playwright_browsers_path(*anchors)
    status: dict[str, Any] = {
        "module_available": playwright_module_available(),
        "browsers_path": str(path) if path is not None else os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
        "browsers_path_configured": bool(os.environ.get("PLAYWRIGHT_BROWSERS_PATH")),
        "executable_path": "",
        "executable_exists": False,
        "ready": False,
        "install_hint": PLAYWRIGHT_INSTALL_HINT,
    }
    if not status["module_available"]:
        status["error"] = "playwright Python package is not installed"
        return status

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        status["error"] = str(exc)
        return status

    playwright = None
    try:
        playwright = sync_playwright().start()
        executable = Path(str(playwright.chromium.executable_path))
        status["executable_path"] = str(executable)
        status["executable_exists"] = executable.exists()
        status["ready"] = bool(status["executable_exists"])
        if not status["ready"]:
            status["error"] = "Playwright Chromium executable is missing"
    except Exception as exc:
        status["error"] = str(exc)
    finally:
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
    return status


def playwright_runtime_ready(*anchors: str | Path | None) -> bool:
    return bool(playwright_runtime_status(*anchors).get("ready"))
