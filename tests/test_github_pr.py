from __future__ import annotations

import json
from pathlib import Path

from visual_agent.github_pr import (
    build_pr_failure_comment,
    github_event_pr_number,
    post_pr_comment,
)
from visual_agent.reports import RunReport, StepReport
from visual_agent.cli import main


def test_build_pr_failure_comment_includes_failure_details_and_screenshots() -> None:
    report = RunReport(
        schema_version=1,
        run_id="run-123",
        run_dir=Path("runs/run-123"),
        workflow_name="checkout",
        workflow_schema_version=1,
        runtime_version="0.1.0",
        run_profile="dry-run",
        status="failed",
        total_steps=2,
        succeeded_steps=1,
        failed_step="assert_total",
        dry_run_actions=0,
        elapsed_seconds=1.2,
        artifacts={},
        downloads=(),
        steps=(
            StepReport(
                id="observe",
                action="observe_html",
                status="success",
                message="ok",
                elapsed_seconds=0.2,
                attempts=1,
                provider="dom",
                target="Checkout",
                selector_resolution=None,
                observation_summary={"screenshot_path": "runs/run-123/observe.png"},
                artifact_paths=("runs/run-123/observe.png",),
                failure_artifacts=None,
                failure_diagnosis=None,
            ),
            StepReport(
                id="assert_total",
                action="assert_text",
                status="failed",
                message="Text not found",
                elapsed_seconds=1.0,
                attempts=1,
                provider="dom",
                target="Total",
                selector_resolution=None,
                observation_summary={"screenshot_path": "runs/run-123/failure.png"},
                artifact_paths=("runs/run-123/failure.png",),
                failure_artifacts={"screenshot": "runs/run-123/failure.png"},
                failure_diagnosis={
                    "root_cause": "assertion_wrong",
                    "confidence": 0.92,
                    "expected": "expected text: 128",
                    "actual": "visible text: 0",
                    "suggested_fix": "Update the assertion to match the rendered total.",
                    "artifacts": {"screenshot": "runs/run-123/failure.png"},
                },
            ),
        ),
    )

    payload = build_pr_failure_comment(
        report,
        quality_gate_entry={"profile": "ci", "status": "failed", "json_report": "quality_gates/run-123.json"},
        artifact_url="https://github.com/acme/repo/actions/runs/7/artifacts/9",
        run_url="https://github.com/acme/repo/actions/runs/7",
    )

    body = payload["body"]
    assert payload["status"] == "ready"
    assert "Checkpoint found a regression" in body
    assert "workflow_name" not in body
    assert "checkout" in body
    assert "assert_total" in body
    assert "assertion_wrong" in body
    assert "expected text: 128" in body
    assert "visible text: 0" in body
    assert "https://github.com/acme/repo/actions/runs/7/artifacts/9" in body
    assert "runs/run-123/failure.png" in body


def test_github_event_pr_number_reads_pull_request_payload(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {"number": 42}}), encoding="utf-8")

    assert github_event_pr_number(event_path) == 42


def test_post_pr_comment_formats_github_api_request() -> None:
    seen: dict[str, object] = {}

    class FakeResponse:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return b'{"id": 1, "body": "ok"}'

    def fake_opener(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["authorization"] = request.headers.get("Authorization")
        seen["body"] = request.data.decode("utf-8")
        return FakeResponse()

    result = post_pr_comment(
        repository="acme/repo",
        number=7,
        token="gh_secret",
        body="hello",
        opener=fake_opener,
    )

    assert result["status"] == "success"
    assert seen["url"] == "https://api.github.com/repos/acme/repo/issues/7/comments"
    assert seen["authorization"] == "Bearer gh_secret"
    assert json.loads(seen["body"]) == {"body": "hello"}


def test_github_pr_comment_cli_dry_run_uses_latest_failed_run(tmp_path, monkeypatch, capsys) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {"number": 17}}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/repo")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_RUN_ID", "9")
    monkeypatch.setattr(
        "visual_agent.cli.pr_failure_comment_result",
        lambda **_kwargs: {
            "schema_version": 1,
            "status": "ready",
            "body": "## Checkpoint found a regression\n- Workflow: `checkout`\n",
        },
    )

    code = main([
        "github-pr-comment",
        "--dry-run",
        "--format",
        "json",
    ])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["pull_request_number"] == 17
    assert payload["repository"] == "acme/repo"
    assert "Checkpoint found a regression" in payload["body"]

