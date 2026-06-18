from __future__ import annotations

from visual_agent.codex_check import codex_check_to_markdown, run_codex_check
from visual_agent.models import ActionStatus
from visual_agent.workflow import WorkflowStepResult
from visual_agent.workspace import init_workspace


def write_check_workflow(workspace, name: str, *, affects: str, slow: bool = False) -> None:
    tags = "  - verification\n" + ("  - slow\n" if slow else "")
    workspace.workflows_dir.joinpath(f"{name}.yaml").write_text(
        "schema_version: 1\n"
        f"name: {name}\n"
        "version: 1\n"
        f"affects:\n  - {affects}\n"
        "tags:\n"
        f"{tags}"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_ocr\n"
        "    mock_text: ready\n",
        encoding="utf-8",
    )


def test_run_codex_check_runs_only_affected_non_slow_workflows(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_check_workflow(workspace, "checkout", affects="src/payment/")
    write_check_workflow(workspace, "profile", affects="src/profile/")
    write_check_workflow(workspace, "visual_checkout", affects="src/payment/", slow=True)
    calls = []

    def fake_run_workspace_workflow(_workspace, name, **_kwargs):
        calls.append(name)

        class Result:
            run_id = f"run-{name}"
            steps = (WorkflowStepResult(id="observe", action="observe_ocr", status=ActionStatus.SUCCESS),)

        return Result()

    monkeypatch.setattr("visual_agent.codex_check.run_workspace_workflow", fake_run_workspace_workflow)

    result = run_codex_check(workspace, changed=["src/payment/checkout.py"])

    assert calls == ["checkout"]
    assert result.selected_workflows == ["checkout"]
    assert result.skipped_slow_workflows == ["visual_checkout"]
    # observe-only workflow: it must surface as inspection-only, never as a real pass
    assert result.passed == 0
    assert result.inspection_only == 1
    assert result.failed == 0
    assert result.verdict == "inspection_only"


def test_run_codex_check_passes_only_with_real_interaction(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_check_workflow(workspace, "checkout", affects="src/payment/")

    def fake_run_workspace_workflow(_workspace, name, **_kwargs):
        class Result:
            run_id = f"run-{name}"
            steps = (
                WorkflowStepResult(id="open", action="observe_ocr", status=ActionStatus.SUCCESS),
                WorkflowStepResult(
                    id="submit",
                    action="click_text",
                    status=ActionStatus.SUCCESS,
                    metadata={
                        "operation_receipt": {
                            "engine": "playwright",
                            "live": True,
                            "observed_after_action": True,
                            "actionability": {"checked": True, "count": 1, "visible": True, "enabled": True},
                        }
                    },
                ),
                WorkflowStepResult(id="confirm", action="assert_text", status=ActionStatus.SUCCESS),
            )
            acceptance = {"label": "L3", "name": "data_round_trip", "is_product_acceptance": True}

        return Result()

    monkeypatch.setattr("visual_agent.codex_check.run_workspace_workflow", fake_run_workspace_workflow)

    result = run_codex_check(workspace, changed=["src/payment/checkout.py"], run_profile="supervised")

    assert result.passed == 1
    assert result.inspection_only == 0
    assert result.verdict == "pass"
    assert result.results[0].real_interaction_count == 1
    assert result.results[0].invalid_interaction_count == 0
    assert result.results[0].acceptance_level == "L3"
    assert result.results[0].is_product_acceptance is True

    text = codex_check_to_markdown(result)
    assert "[L3 data_round_trip]" in text
    assert "Strict product acceptance (L3+ without blockers): 1/1 workflow(s)." in text


def test_run_codex_check_dry_run_interactions_are_inspection_only(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_check_workflow(workspace, "checkout", affects="src/payment/")

    def fake_run_workspace_workflow(_workspace, name, **_kwargs):
        class Result:
            run_id = f"run-{name}"
            steps = (
                WorkflowStepResult(id="open", action="observe_ocr", status=ActionStatus.SUCCESS),
                WorkflowStepResult(id="submit", action="click_text", status=ActionStatus.DRY_RUN),
            )

        return Result()

    monkeypatch.setattr("visual_agent.codex_check.run_workspace_workflow", fake_run_workspace_workflow)

    result = run_codex_check(workspace, changed=["src/payment/checkout.py"])

    assert result.passed == 0
    assert result.inspection_only == 1
    assert result.verdict == "inspection_only"
    assert result.results[0].skipped_interaction_count == 1

    text = codex_check_to_markdown(result)
    assert "INSPECT checkout" in text
    assert "NOT" in text
    assert "Verdict: INSPECTION ONLY" in text


def test_codex_check_to_markdown_reports_failure_details() -> None:
    from visual_agent.codex_check import CodexCheckResult, CodexWorkflowCheck

    report = CodexCheckResult(
        changed_files=["src/payment/checkout.py"],
        selected_workflows=["checkout"],
        skipped_slow_workflows=[],
        results=[
            CodexWorkflowCheck(
                name="checkout",
                status="failed",
                step_count=2,
                elapsed_seconds=0.1,
                failed_step="assert_total",
                message="Text not found",
                expected="expected text: 128",
                actual="visible text: 0",
                hint="Fix total calculation.",
            )
        ],
    )

    text = codex_check_to_markdown(report)

    assert "Changed files: src/payment/checkout.py" in text
    assert "FAILED at 'assert_total'" in text
    assert "Expected: expected text: 128" in text
    assert "Fix total calculation" in text
