from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)


def run_cli(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [str(PYTHON), "-m", "visual_agent.cli", *args],
        cwd=str(cwd or ROOT),
        env=merged_env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def json_output(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    text = (result.stdout or "").lstrip("\ufeff").strip()
    assert text, f"command produced no stdout\nstderr:\n{result.stderr}"
    return json.loads(text)


def playwright_available() -> bool:
    result = subprocess.run(
        [str(PYTHON), "-c", "from playwright.sync_api import sync_playwright"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


@lru_cache(maxsize=1)
def playwright_chromium_available() -> bool:
    browsers = ROOT / ".pw-browsers"
    merged_env = os.environ.copy()
    if browsers.exists():
        merged_env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    script = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        "    browser = p.chromium.launch(headless=True)\n"
        "    browser.close()\n"
    )
    result = subprocess.run(
        [str(PYTHON), "-c", script],
        cwd=str(ROOT),
        env=merged_env,
        text=True,
        capture_output=True,
        timeout=30.0,
        check=False,
    )
    return result.returncode == 0
