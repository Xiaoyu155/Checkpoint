from __future__ import annotations

import tomllib
from pathlib import Path


def _project() -> dict:
    root = Path(__file__).resolve().parents[1]
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_release_metadata_pins_the_current_version() -> None:
    assert _project()["version"] == "0.1.2"


def test_core_install_stays_thin() -> None:
    """Pacer launches other people's agents; it must not drag a stack behind it.

    The visual-automation runtime that needed playwright/pyautogui/mss/mcp is
    gone. If one of those reappears here, something deleted has crept back.
    """
    project = _project()
    core = " ".join(project["dependencies"]).lower()

    for banned in ("mcp", "playwright", "pyautogui", "mss", "pillow", "fastapi", "uvicorn", "boto3", "celery"):
        assert banned not in core, f"{banned} is back in the core install"
    assert len(project["dependencies"]) <= 4


def test_console_scripts_all_point_at_the_cli() -> None:
    scripts = _project()["scripts"]

    assert set(scripts) == {"pacer", "checkpoint", "visual-agent"}
    assert set(scripts.values()) == {"visual_agent.cli:main"}
