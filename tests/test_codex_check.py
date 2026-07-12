from __future__ import annotations

from visual_agent.codex_check import CodexCheckResult, codex_check_to_markdown, filter_runtime_changed_files, run_codex_check
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


def write_fallback_workflow(workspace, name: str) -> None:
    workspace.workflows_dir.joinpath(f"{name}.yaml").write_text(
        "schema_version: 1\n"
        f"name: {name}\n"
        "version: 1\n"
        "tags:\n"
        "  - verification\n"
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
    assert result.coverage["status"] == "covered"
    assert result.coverage["precise_covered_files"] == ["src/payment/checkout.py"]
    # observe-only workflow: it must surface as inspection-only, never as a real pass
    assert result.passed == 0
    assert result.inspection_only == 1
    assert result.failed == 0
    assert result.verdict == "inspection_only"


def test_run_codex_check_reports_fallback_only_coverage_gap(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_fallback_workflow(workspace, "broad_smoke")
    calls = []

    def fake_run_workspace_workflow(_workspace, name, **_kwargs):
        calls.append(name)

        class Result:
            run_id = f"run-{name}"
            steps = (WorkflowStepResult(id="observe", action="observe_ocr", status=ActionStatus.SUCCESS),)

        return Result()

    monkeypatch.setattr("visual_agent.codex_check.run_workspace_workflow", fake_run_workspace_workflow)

    result = run_codex_check(workspace, changed=["src/payment/checkout.py"])
    text = codex_check_to_markdown(result)

    assert calls == ["broad_smoke"]
    assert result.coverage["status"] == "fallback_only"
    assert result.coverage["fallback_only_files"] == ["src/payment/checkout.py"]
    assert result.coverage["suggested_affects"] == [
        {
            "workflow": "broad_smoke",
            "path": "workflows/broad_smoke.yaml",
            "add_affects": ["src/payment/"],
            "reason": "fallback workflow has no affects paths but was selected for this diff",
        }
    ]
    assert result.verdict == "coverage_gap"
    assert "Verdict: COVERAGE GAP" in text
    assert "Suggested affects: broad_smoke -> src/payment/" in text


def test_run_codex_check_prefers_precise_workflow_over_fallback(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_fallback_workflow(workspace, "broad_smoke")
    write_check_workflow(workspace, "checkout", affects="src/payment/")
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
    assert result.coverage["status"] == "covered"


def test_run_codex_check_reports_uncovered_changed_files(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_check_workflow(workspace, "profile", affects="src/profile/")
    calls = []

    def fake_run_workspace_workflow(_workspace, name, **_kwargs):
        calls.append(name)
        raise AssertionError("should not run")

    monkeypatch.setattr("visual_agent.codex_check.run_workspace_workflow", fake_run_workspace_workflow)

    result = run_codex_check(workspace, changed=["src/payment/checkout.py"])
    text = codex_check_to_markdown(result)

    assert calls == []
    assert result.selected_workflows == []
    assert result.coverage["status"] == "uncovered"
    assert result.coverage["uncovered_files"] == ["src/payment/checkout.py"]
    assert result.coverage["suggested_new_workflows"] == [
        {
            "changed_file": "src/payment/checkout.py",
            "suggested_name": "src_payment_verification",
            "affects": ["src/payment/"],
            "reason": "no workflow precisely covers this changed file",
        }
    ]
    assert result.verdict == "coverage_gap"
    assert "Uncovered files: src/payment/checkout.py" in text
    assert "Suggested new workflow: src_payment_verification affects src/payment/" in text


def test_codex_check_command_mode_does_not_report_coverage_gap() -> None:
    result = CodexCheckResult(
        changed_files=["src/payment/checkout.py"],
        selected_workflows=[],
        skipped_slow_workflows=[],
        coverage={
            "status": "uncovered",
            "verification_mode": "command",
            "uncovered_files": ["src/payment/checkout.py"],
            "next_action": "Add precise workflow coverage for changed files.",
            "suggested_new_workflows": [
                {
                    "changed_file": "src/payment/checkout.py",
                    "suggested_name": "src_payment_verification",
                    "affects": ["src/payment/"],
                }
            ],
        },
    )

    text = codex_check_to_markdown(result)

    assert result.verdict != "coverage_gap"
    assert "Verification mode: command" in text
    assert "workflow coverage 由显式测试命令接管" in text
    assert "Uncovered files" not in text
    assert "Coverage next action" not in text
    assert "Suggested new workflow" not in text
    assert "COVERAGE GAP" not in text


def test_run_codex_check_ignores_runtime_artifacts_for_coverage(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_check_workflow(workspace, "pacer", affects="src/visual_agent/")
    calls = []

    def fake_run_workspace_workflow(_workspace, name, **_kwargs):
        calls.append(name)

        class Result:
            run_id = f"run-{name}"
            steps = (WorkflowStepResult(id="observe", action="observe_ocr", status=ActionStatus.SUCCESS),)

        return Result()

    monkeypatch.setattr("visual_agent.codex_check.run_workspace_workflow", fake_run_workspace_workflow)

    result = run_codex_check(
        workspace,
        changed=[
            "artifacts/pacer-dogfood/followup-workspace/missions/m1/state.json",
            "artifacts/dashboard-commercial-live/readiness.png",
            "artifacts/random-run/latest/report.json",
            "src/visual_agent/chief_run.py",
        ],
    )
    text = codex_check_to_markdown(result)

    assert calls == ["pacer"]
    assert result.changed_files == ["src/visual_agent/chief_run.py"]
    assert result.coverage["status"] == "covered"
    assert result.coverage["ignored_runtime_file_count"] == 3
    assert "Ignored generated runtime files" in text


def test_runtime_filter_handles_nested_repo_prefix_without_hiding_source_artifacts(tmp_path) -> None:
    repo_root = tmp_path / "xiao"
    kept, ignored = filter_runtime_changed_files(
        [
            "xiao/artifacts/pacer-dogfood/latest/report.json",
            "xiao/.agent-workspace/runs/latest/report.json",
            "xiao/src/artifacts/schema.json",
        ],
        repo_root=repo_root,
    )

    assert kept == ["xiao/src/artifacts/schema.json"]
    assert ignored == [
        "xiao/.agent-workspace/runs/latest/report.json",
        "xiao/artifacts/pacer-dogfood/latest/report.json",
    ]


def test_run_codex_check_runtime_only_changes_do_not_run_all_workflows(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_check_workflow(workspace, "pacer", affects="src/visual_agent/")
    calls = []

    def fake_run_workspace_workflow(_workspace, name, **_kwargs):
        calls.append(name)
        raise AssertionError("runtime-only changes should not run workflows")

    monkeypatch.setattr("visual_agent.codex_check.run_workspace_workflow", fake_run_workspace_workflow)

    result = run_codex_check(
        workspace,
        changed=[
            ".agent-workspace/missions/m1/state.json",
            "artifacts/any-local-run/followup-workspace/repo_map.json",
        ],
    )
    text = codex_check_to_markdown(result)

    assert calls == []
    assert result.changed_files == []
    assert result.selected_workflows == []
    assert result.coverage["status"] == "runtime_only"
    assert result.verdict == "no_changed_files"
    assert "NO PRODUCT CHANGES" in text


def test_default_pacer_workflow_covers_visual_agent_changes(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    calls = []

    def fake_run_workspace_workflow(_workspace, name, **_kwargs):
        calls.append(name)

        class Result:
            run_id = f"run-{name}"
            steps = (WorkflowStepResult(id="static", action="run_command", status=ActionStatus.DRY_RUN),)

        return Result()

    monkeypatch.setattr("visual_agent.codex_check.run_workspace_workflow", fake_run_workspace_workflow)

    result = run_codex_check(
        workspace,
        changed=[
            ".gitignore",
            "xiao/src/visual_agent/dashboard/static/app.js",
            "xiao/examples/workflows/index.json",
            "examples/workflows/pacer/pacer_workbench_static_acceptance.yaml",
        ],
    )

    assert "pacer_workbench_static_acceptance" in result.selected_workflows
    assert "pacer_workbench_static_acceptance" in result.coverage["primary_workflows"]
    assert result.coverage["status"] == "covered"
    assert result.coverage["uncovered_files"] == []


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


def test_run_codex_check_dry_run_failure_after_skipped_interaction_is_inconclusive(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_check_workflow(workspace, "checkout", affects="src/payment/")

    def fake_run_workspace_workflow(_workspace, name, **_kwargs):
        class Result:
            run_id = f"run-{name}"
            steps = (
                WorkflowStepResult(id="open", action="observe_ocr", status=ActionStatus.SUCCESS),
                WorkflowStepResult(id="place_order", action="click_text", status=ActionStatus.DRY_RUN),
                WorkflowStepResult(id="assert_confirmed", action="assert_text_contract", status=ActionStatus.FAILED),
            )

        return Result()

    monkeypatch.setattr("visual_agent.codex_check.run_workspace_workflow", fake_run_workspace_workflow)

    result = run_codex_check(workspace, changed=["src/payment/checkout.py"])

    # The assertion failed only because dry-run skipped the interaction it depends on,
    # so it is inconclusive rather than a real regression.
    assert result.failed == 0
    assert result.inspection_only == 1
    assert result.results[0].failed_step == "assert_confirmed"
    assert "supervised" in (result.results[0].hint or "")


def test_run_codex_check_dry_run_failure_before_interaction_stays_failed(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_check_workflow(workspace, "checkout", affects="src/payment/")

    def fake_run_workspace_workflow(_workspace, name, **_kwargs):
        class Result:
            run_id = f"run-{name}"
            steps = (
                WorkflowStepResult(id="open", action="observe_ocr", status=ActionStatus.SUCCESS),
                # Static regression before any interaction: a real failure, not a dry-run artifact.
                WorkflowStepResult(id="assert_price", action="assert_text", status=ActionStatus.FAILED),
                WorkflowStepResult(id="place_order", action="click_text", status=ActionStatus.DRY_RUN),
            )

        return Result()

    monkeypatch.setattr("visual_agent.codex_check.run_workspace_workflow", fake_run_workspace_workflow)

    result = run_codex_check(workspace, changed=["src/payment/checkout.py"])

    assert result.failed == 1
    assert result.verdict == "fail"
    assert result.results[0].failed_step == "assert_price"


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
