from __future__ import annotations

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


def test_browser_recording_generates_valid_replayable_workflow(e2e_workspace: Path) -> None:
    save_as = "recorded/e2e_login"
    url = (ROOT / "examples" / "web" / "login_demo.html").as_uri()

    recording = run_cli(
        "workspace-record-browser",
        "--root",
        str(e2e_workspace),
        "--url",
        url,
        "--save-as",
        save_as,
        "--timeout-seconds",
        "2",
        "--headless",
        "--assert-text",
        "客户管理系统",
        "--overwrite",
        timeout=120.0,
    )
    assert recording.returncode == 0, recording.stdout + recording.stderr
    payload = json_output(recording)

    workflow_path = Path(str(payload["workflow_path"]))
    assert workflow_path.exists()
    assert payload["validation"]["valid"] is True
    assert payload["preflight"]["ok"] is True
    assert payload["event_count"] >= 1

    replay = run_cli(
        "workspace-run",
        "--root",
        str(e2e_workspace),
        "--workflow",
        save_as,
        "--run-profile",
        "dry-run",
        timeout=120.0,
    )
    assert replay.returncode == 0, replay.stdout + replay.stderr
    replay_payload = json_output(replay)
    assert replay_payload["workflow_name"] == "recorded_e2e_login"
    assert not [step for step in replay_payload["steps"] if step["status"] == "failed"]
