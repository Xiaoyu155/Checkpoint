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

WORKFLOW = ROOT / "examples" / "browser_business_backend_workflow.yaml"


def test_business_backend_handles_exception_response_pagination_and_download(tmp_path: Path) -> None:
    result = run_cli(
        "run-workflow",
        "--file",
        str(WORKFLOW),
        "--run-profile",
        "supervised",
        "--output-dir",
        str(tmp_path / "runs"),
        timeout=120.0,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json_output(result)

    assert payload["workflow_name"] == "browser_business_backend_workflow"
    assert not [step for step in payload["steps"] if step["status"] == "failed"]

    response = next(step for step in payload["steps"] if step["id"] == "assert_process_response")
    assert response["status"] == "success"
    assert "response" in response["message"] or "network" in response["message"]

    dialog = next(step for step in payload["steps"] if step["id"] == "assert_exception_dialog")
    assert dialog["status"] == "success"

    page = next(step for step in payload["steps"] if step["id"] == "assert_page_changed")
    assert page["status"] == "success"

    download = next(step for step in payload["steps"] if step["id"] == "download_exception_order")
    download_path = Path(download["action_result"]["metadata"]["path"])
    if not download_path.is_absolute():
        download_path = ROOT / download_path
    assert download_path.exists()
    assert download_path.read_text(encoding="utf-8").splitlines() == [
        "order_id,customer,status",
        "A2002,Globex,exception",
    ]


def test_business_backend_report_exposes_business_result(tmp_path: Path) -> None:
    output_dir = tmp_path / "runs"
    result = run_cli(
        "run-workflow",
        "--file",
        str(WORKFLOW),
        "--run-profile",
        "supervised",
        "--output-dir",
        str(output_dir),
        timeout=120.0,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json_output(result)

    report = run_cli("report-run", "--run-dir", str(output_dir / payload["run_id"]), "--format", "markdown")
    assert report.returncode == 0, report.stdout + report.stderr
    assert "browser_business_backend_workflow" in report.stdout
    assert "assert_process_response" in report.stdout
    assert "download_exception_order" in report.stdout
    assert "A2002.csv" in report.stdout
