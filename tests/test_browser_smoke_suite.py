from __future__ import annotations

import json
from pathlib import Path

from visual_agent.browser_smoke_suite import browser_smoke_suite_to_markdown, run_browser_smoke_suite
from visual_agent.cli import main


def fake_run_browser_smoke(**kwargs):
    status = "failed" if "fail" in str(kwargs["url"]) else "success"
    return {
        "status": status,
        "url": kwargs["url"],
        "run_dir": str(kwargs["output_dir"]),
        "initial": {"screenshot_path": str(Path(kwargs["output_dir"]) / "initial.png")},
        "after_click": None,
        "fills": [],
        "click": None,
        "waits": [],
        "change": None,
        "issues": [] if status == "success" else [{"type": "missing_text", "message": "Text not found: Ready"}],
    }


def test_browser_smoke_suite_runs_cases_and_writes_reports(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke_suite.run_browser_smoke", fake_run_browser_smoke)
    suite = tmp_path / "suite.json"
    suite.write_text(
        json.dumps(
            {
                "name": "local suite",
                "defaults": {"min_text_length": 1},
                "cases": [
                    {"id": "home", "url": "https://example.test/home", "expect_text": "Ready"},
                    {"id": "broken", "url": "https://example.test/fail", "expect_text": "Ready"},
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = run_browser_smoke_suite(suite, output_dir=tmp_path / "runs")
    markdown = browser_smoke_suite_to_markdown(payload)

    assert payload["status"] == "failed"
    assert payload["case_count"] == 2
    assert payload["passed_count"] == 1
    assert payload["failed_count"] == 1
    assert payload["results"][0]["case_id"] == "home"
    assert (Path(payload["run_dir"]) / "suite-result.json").exists()
    assert (Path(payload["run_dir"]) / "suite-result.md").exists()
    assert "broken" in markdown


def test_browser_smoke_suite_cli_outputs_json(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke_suite.run_browser_smoke", fake_run_browser_smoke)
    suite = tmp_path / "suite.json"
    suite.write_text(
        json.dumps({"cases": [{"id": "home", "url": "https://example.test/home"}]}),
        encoding="utf-8",
    )

    code = main(["browser-smoke-suite", "--file", str(suite), "--output-dir", str(tmp_path / "runs"), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "success"
    assert payload["case_count"] == 1
