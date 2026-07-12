from __future__ import annotations

import json
from pathlib import Path

from .helpers import ROOT, json_output, run_cli


WORKFLOW = ROOT / "examples" / "local_html_form_workflow.yaml"
INPUTS = ROOT / "examples" / "inputs" / "demo_login.json"


def test_local_html_form_dry_run_resolves_real_dom_targets_without_clicking(tmp_path: Path) -> None:
    result = run_cli(
        "run-workflow",
        "--file",
        str(WORKFLOW),
        "--inputs-file",
        str(INPUTS),
        "--run-profile",
        "dry-run",
        "--output-dir",
        str(tmp_path / "runs"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json_output(result)

    assert payload["workflow_name"] == "local_html_form_workflow"
    assert payload["run_profile"] == "dry-run"
    assert [step["id"] for step in payload["steps"]] == [
        "observe_html",
        "wait_page",
        "fill_username",
        "fill_password",
        "wait_login_button",
        "click_login",
    ]
    assert all(step["status"] in {"success", "dry_run"} for step in payload["steps"])

    fill_username = next(step for step in payload["steps"] if step["id"] == "fill_username")
    click_login = next(step for step in payload["steps"] if step["id"] == "click_login")
    assert fill_username["resolved_target"]["evidence"]["provider"] == "dom"
    assert fill_username["resolved_target"]["evidence"]["handle"] == "#username"
    assert click_login["status"] == "dry_run"
    assert click_login["message"] == "click skipped by dry-run"


def test_local_html_form_audit_redacts_sensitive_password(tmp_path: Path) -> None:
    password = json.loads(INPUTS.read_text(encoding="utf-8"))["password"]
    result = run_cli(
        "run-workflow",
        "--file",
        str(WORKFLOW),
        "--inputs-file",
        str(INPUTS),
        "--run-profile",
        "dry-run",
        "--output-dir",
        str(tmp_path / "runs"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json_output(result)
    combined = json.dumps(payload, ensure_ascii=False)

    assert password not in combined
    password_step = next(step for step in payload["steps"] if step["id"] == "fill_password")
    metadata = password_step["action_result"]["metadata"]
    assert metadata["sensitive"] is True
    assert "sha256" in metadata
    assert "text_preview" not in metadata


def test_local_html_form_report_command_is_user_readable(tmp_path: Path) -> None:
    output_dir = tmp_path / "runs"
    result = run_cli(
        "run-workflow",
        "--file",
        str(WORKFLOW),
        "--inputs-file",
        str(INPUTS),
        "--run-profile",
        "dry-run",
        "--output-dir",
        str(output_dir),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json_output(result)

    report = run_cli("report-run", "--run-dir", str(output_dir / payload["run_id"]), "--format", "markdown")
    assert report.returncode == 0, report.stdout + report.stderr
    assert "Run Report: local_html_form_workflow" in report.stdout
    assert "fill_password" in report.stdout
    assert "demo_password" not in report.stdout
