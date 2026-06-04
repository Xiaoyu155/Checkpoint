from __future__ import annotations

import json

from visual_agent.cli import main
from visual_agent.codex_check import CodexCheckResult, CodexWorkflowCheck


def test_codex_check_cli_returns_zero_when_all_selected_workflows_pass(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def fake_run_codex_check(*_args, **_kwargs):
        return CodexCheckResult(
            changed_files=["src/payment/checkout.py"],
            selected_workflows=["checkout"],
            skipped_slow_workflows=[],
            results=[CodexWorkflowCheck(name="checkout", status="passed", step_count=1, elapsed_seconds=0.01)],
        )

    monkeypatch.setattr("visual_agent.cli.run_codex_check", fake_run_codex_check)

    code = main(["codex-check", "--workspace-root", str(workspace), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["selected_workflows"] == ["checkout"]
    assert payload["results"][0]["status"] == "passed"


def test_codex_check_cli_returns_one_when_any_workflow_fails(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def fake_run_codex_check(*_args, **_kwargs):
        return CodexCheckResult(
            changed_files=["src/payment/checkout.py"],
            selected_workflows=["checkout"],
            skipped_slow_workflows=["visual_checkout"],
            results=[
                CodexWorkflowCheck(
                    name="checkout",
                    status="failed",
                    step_count=2,
                    elapsed_seconds=0.01,
                    failed_step="assert_total",
                    message="Text not found",
                )
            ],
        )

    monkeypatch.setattr("visual_agent.cli.run_codex_check", fake_run_codex_check)

    code = main(["codex-check", "--workspace-root", str(workspace), "--format", "markdown"])
    output = capsys.readouterr().out

    assert code == 1
    assert "FAILED at 'assert_total'" in output
    assert "Skipping slow workflows: visual_checkout" in output
