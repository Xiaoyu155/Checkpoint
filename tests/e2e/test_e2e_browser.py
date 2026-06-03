from __future__ import annotations

import json
from pathlib import Path

import pytest

from .helpers import ROOT, json_output, playwright_chromium_available, run_cli


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.browser,
    pytest.mark.skipif(
        not playwright_chromium_available(),
        reason="Playwright Chromium is unavailable. Run: python -m playwright install chromium",
    ),
]

WORKFLOW = ROOT / "examples" / "browser_form_workflow.yaml"
INPUTS = ROOT / "examples" / "inputs" / "demo_login.json"


def run_browser_form(tmp_path: Path, run_profile: str) -> dict:
    result = run_cli(
        "run-workflow",
        "--file",
        str(WORKFLOW),
        "--inputs-file",
        str(INPUTS),
        "--run-profile",
        run_profile,
        "--output-dir",
        str(tmp_path / "runs"),
        timeout=120.0,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json_output(result)


def test_browser_form_supervised_fills_real_chromium_page(tmp_path: Path) -> None:
    payload = run_browser_form(tmp_path, "supervised")

    assert payload["workflow_name"] == "browser_form_workflow"
    assert payload["run_profile"] == "supervised"
    assert not [step for step in payload["steps"] if step["status"] == "failed"]
    assert next(step for step in payload["steps"] if step["id"] == "fill_username")["status"] == "success"
    assert next(step for step in payload["steps"] if step["id"] == "click_login")["message"] == "playwright clicked"


def test_browser_form_dry_run_does_not_perform_clicks(tmp_path: Path) -> None:
    payload = run_browser_form(tmp_path, "dry-run")

    click = next(step for step in payload["steps"] if step["id"] == "click_login")
    assert click["status"] == "dry_run"
    assert "dry-run" in click["message"]


def test_browser_form_password_is_redacted_from_browser_audit(tmp_path: Path) -> None:
    password = json.loads(INPUTS.read_text(encoding="utf-8"))["password"]
    payload = run_browser_form(tmp_path, "supervised")
    combined = json.dumps(payload, ensure_ascii=False)

    assert password not in combined
    password_step = next(step for step in payload["steps"] if step["id"] == "fill_password")
    metadata = password_step["action_result"]["metadata"]
    assert metadata["sensitive"] is True
    assert "sha256" in metadata
    assert "text_preview" not in metadata
