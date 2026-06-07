from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from visual_agent.cli import generate_from_diff_cli_markdown, load_inputs, main, verify_impl_cli_markdown
from visual_agent.codex_check import CodexCheckResult, CodexWorkflowCheck
from visual_agent.session import load_agent_session, record_cloud_run_usage, update_agent_session
from visual_agent.verification_status import enrich_verification_payload, write_verification_status
from visual_agent.workflow import WorkflowRunResult, WorkflowStepResult
from visual_agent.models import ActionStatus
from visual_agent.workspace import init_workspace


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


def test_load_inputs_file_accepts_utf8_bom(tmp_path) -> None:
    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_text('\ufeff{"username": "demo_user"}', encoding="utf-8")

    assert load_inputs(None, str(inputs_file)) == {"username": "demo_user"}


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


def test_share_workflow_cli_marks_local_index_public(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    code = main(["share-workflow", "--workspace-root", str(workspace.root), "--name", "local_html_form_workflow"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "coming_soon"
    assert payload["workflow"] == "local_html_form_workflow"
    assert payload["visibility"] == "public"
    assert "marketplace is coming soon" in payload["message"]


def test_generate_from_diff_cli_dry_run_outputs_context_workflow(tmp_path: Path, capsys) -> None:
    init_git_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    page = workspace.fixtures_dir / "login.html"
    page.write_text("<form><input name='email'></form>\n", encoding="utf-8")
    git(tmp_path, "add", ".agent-workspace/fixtures/login.html")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    page.write_text(
        "<form action='/dashboard'><label for='email'>Email</label><input id='email' name='email' type='email' required>"
        "<button type='submit'>Sign in</button></form><p>Welcome Dashboard</p>\n",
        encoding="utf-8",
    )

    code = main(
        [
            "generate-from-diff",
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--task-description",
            "Verify login redirects",
            "--base-url",
            "fixtures/login.html",
            "--dry-run",
            "--no-untracked",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "success"
    assert payload["changed_files"] == [".agent-workspace/fixtures/login.html"]
    assert payload["quality"]["score"] >= 0.6
    assert payload["quality"]["data_display_assertions"] == 0
    assert payload["quality"]["forbidden_error_assertions"] == 0
    assert payload["quality"]["invalid_text_from_references"] == []
    assert payload["semantic_summary"]["framework"] == "html"
    assert payload["semantic_summary"]["field_count"] == 1
    assert payload["semantic_summary"]["required_field_count"] == 1
    assert payload["semantic_summary"]["validation_rule_count"] == 2
    assert payload["semantic_summary"]["data_display_count"] == 0
    assert payload["semantic_summary"]["data_displays"] == []
    assert payload["semantic_summary"]["matched_data_displays"] == []
    assert payload["semantic_summary"]["unmatched_data_displays"] == []
    assert payload["semantic_summary"]["negative_input_case_count"] == 2
    assert len(payload["negative_input_cases"]) == 2
    assert payload["negative_workflow_ready"] is False
    assert payload["negative_workflow_reason"] == "no_negative_oracle"
    assert payload["negative_workflow_reset_strategy"] == "fresh_observe_per_case"
    assert payload["negative_oracles"] == []
    assert len(payload["generation_trace"]) <= 10
    assert "field email -> paste input.email" in payload["generation_trace"]
    assert payload["semantic_summary"]["success_state_count"] >= 1
    assert "url_contains: /dashboard" in payload["yaml"]


def test_generate_from_diff_cli_appends_audit_log(tmp_path: Path, capsys) -> None:
    init_git_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    page = workspace.fixtures_dir / "login.html"
    page.write_text("<form><input name='email'></form>\n", encoding="utf-8")
    git(tmp_path, "add", ".agent-workspace/fixtures/login.html")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    page.write_text(
        "<form action='/dashboard'><label for='email'>Email</label><input id='email' name='email' type='email' required>"
        "<button type='submit'>Sign in</button></form><p>Welcome Dashboard</p>\n",
        encoding="utf-8",
    )
    audit_log = workspace.root / "audit" / "context_parse.jsonl"

    for _ in range(2):
        code = main(
            [
                "generate-from-diff",
                "--workspace-root",
                str(workspace.root),
                "--repo-root",
                str(tmp_path),
                "--task-description",
                "Verify login redirects",
                "--base-url",
                "fixtures/login.html",
                "--dry-run",
                "--no-untracked",
                "--audit-log",
                str(audit_log),
            ]
        )
        json.loads(capsys.readouterr().out)
        assert code == 0

    entries = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]

    assert len(entries) == 2
    assert entries[0]["task"] == "Verify login redirects"
    assert entries[0]["framework"] == "html"
    assert entries[0]["confidence"] >= 0.5
    assert entries[0]["method"]
    assert entries[0]["fields"] == ["email"]
    assert entries[0]["submit_actions"] == ["Sign in"]
    assert entries[0]["success_states"]
    assert entries[0]["unmatched_data_displays"] == []
    assert isinstance(entries[0]["warnings"], list)
    assert entries[0]["quality_score"] >= 0.6


def test_init_workspace_auto_detect_nextjs(tmp_path: Path, capsys) -> None:
    (tmp_path / "package.json").write_text('{"dependencies":{"next":"13.0.0","react":"18.0.0"}}', encoding="utf-8")
    workspace_root = tmp_path / ".agent-workspace"

    code = main(
        [
            "init-workspace",
            "--root",
            str(workspace_root),
            "--auto-detect",
            "--repo-root",
            str(tmp_path),
            "--no-demo",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["framework_hint"] == "nextjs"
    assert (workspace_root / "fixtures" / "nextjs_demo.html").exists()
    assert (workspace_root / "workflows" / "nextjs_verification.yaml").exists()


def test_generate_from_diff_markdown_prints_warnings() -> None:
    output = generate_from_diff_cli_markdown(
        {
            "status": "success",
            "workflow_path": ".agent-workspace/workflows/verify_profile.yaml",
            "generation_method": "static",
            "quality": {"score": 0.71},
            "semantic_summary": {
                "framework": "nextjs",
                "confidence": 0.82,
                "field_count": 2,
                "required_field_count": 1,
                "success_state_count": 1,
                "data_display_count": 1,
                "warnings": ["Unrecognized field: <DatePicker name=\"birthdate\">"],
            },
        }
    )

    assert "[generate-from-diff] Framework: nextjs" in output
    assert "Parse warnings (1):" in output
    assert "DatePicker" in output


def test_verify_impl_cli_dry_run_writes_status(tmp_path: Path, capsys) -> None:
    init_git_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    page = workspace.fixtures_dir / "simple_form.html"
    page.write_text("<form><input name='email'></form>\n", encoding="utf-8")
    git(tmp_path, "add", ".agent-workspace/fixtures/simple_form.html")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    page.write_text(
        "<form><label for='email'>Email</label><input id='email' name='email'>"
        "<button type='submit'>Save</button></form><p>Saved successfully</p>\n",
        encoding="utf-8",
    )
    code = main(
        [
            "verify-impl",
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--task-description",
            "Verify simple form submits",
            "--base-url",
            "fixtures/simple_form.html",
            "--run-profile",
            "dry-run",
            "--min-quality-score",
            "0",
            "--no-untracked",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["result"] == "pass"
    assert payload["inputs_source"] == "generated_template"
    assert payload["inputs_path"]
    assert payload["semantic_summary"]["framework"] == "html"
    assert payload["semantic_summary"]["field_count"] == 1
    assert (workspace.root / ".vscode-agent-status.json").exists()


def test_verify_impl_cli_can_run_negative_workflow_when_requested(tmp_path: Path, capsys) -> None:
    init_git_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    page = workspace.fixtures_dir / "simple_form.html"
    page.write_text("<form><input name='email'></form>\n", encoding="utf-8")
    git(tmp_path, "add", ".agent-workspace/fixtures/simple_form.html")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    page.write_text(
        "<form><label for='email'>Email</label><input id='email' name='email' type='email' required>"
        "<button type='submit'>Save</button></form><p>Saved successfully</p>\n",
        encoding="utf-8",
    )

    code = main(
        [
            "verify-impl",
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--task-description",
            "Verify simple form submits",
            "--base-url",
            "fixtures/simple_form.html",
            "--run-profile",
            "dry-run",
            "--min-quality-score",
            "0",
            "--run-negative",
            "--no-untracked",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["result"] == "pass"
    assert payload["negative_verification"]["requested"] is True
    assert payload["negative_verification"]["status"] == "skipped"
    assert payload["negative_verification"]["reason"] == "no_negative_oracle"
    assert payload["negative_verification"]["workflow_path"].endswith("_negative_draft.yaml")


def test_verify_impl_cli_markdown_includes_inputs_source() -> None:
    output = verify_impl_cli_markdown(
        {
            "result": "pass",
            "workflow_name": "login_verification",
            "quality_score": 0.9,
            "inputs_path": "inputs/login_verification_inputs.json",
            "inputs_source": "generated_template",
            "generation_trace": ["field email -> paste input.email"],
            "negative_verification": {
                "status": "skipped",
                "reason": "no_negative_oracle",
                "workflow_name": "login_verification_negative_draft",
                "reset_strategy": "fresh_observe_per_case",
                "oracles": [{"text": "Invalid input", "source": "html:text"}],
                "next_action": "Add or expose parsed validation error text before treating negative verification as executable.",
            },
            "message": "All steps passed.",
        }
    )

    assert "[verify-impl] Inputs: inputs/login_verification_inputs.json" in output
    assert "[verify-impl] Inputs source: generated_template" in output
    assert "[verify-impl] Generation trace: field email -> paste input.email" in output
    assert "[verify-impl] Negative: skipped workflow=login_verification_negative_draft" in output
    assert "[verify-impl] Negative reason: no_negative_oracle" in output
    assert "[verify-impl] Negative reset: fresh_observe_per_case" in output
    assert "[verify-impl] Negative oracles: 1" in output
    assert "[verify-impl] Negative next: Add or expose parsed validation error text" in output


def test_agent_status_cli_reads_status_file_as_markdown_and_json(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    payload = enrich_verification_payload(
        {
            "result": "pass",
            "workflow_name": "profile_verification",
            "workflow_path": str(workspace / "workflows" / "profile.yaml"),
            "run_id": "run-123",
            "quality_score": 0.91,
            "quality": {
                "score": 0.91,
                "data_display_assertions": 1,
                "forbidden_error_assertions": 1,
                "text_from_input_references": 1,
                "invalid_text_from_references": [],
                "gaps": [],
                "recommendation": "Workflow quality is good.",
            },
            "semantic_summary": {
                "framework": "nextjs",
                "confidence": 0.82,
                "generation_method": "static",
                "field_count": 1,
                "required_field_count": 1,
                "validation_rule_count": 2,
                "success_state_count": 1,
                "data_display_count": 1,
                "negative_input_case_count": 2,
                "data_displays": ["profile.displayName"],
                "matched_data_displays": ["profile.displayName"],
                "unmatched_data_displays": [],
                "warnings": [],
            },
            "inputs_path": str(workspace / "inputs" / "profile_inputs.json"),
            "inputs_source": "generated_template",
            "generation_trace": ["display displayName -> assert_text text_from input.displayName"],
            "negative_verification": {
                "requested": True,
                "status": "skipped",
                "reason": "no_negative_oracle",
                "reset_strategy": "fresh_observe_per_case",
                "oracles": [],
                "next_action": "Add parsed validation error text before enabling negative verification.",
            },
            "message": "ok",
        },
        workspace_root=workspace,
    )
    write_verification_status(workspace, payload)

    markdown_code = main(["agent-status", "--workspace-root", str(workspace), "--format", "markdown"])
    markdown = capsys.readouterr().out
    json_code = main(["agent-status", "--workspace-root", str(workspace), "--format", "json"])
    json_payload = json.loads(capsys.readouterr().out)

    assert markdown_code == 0
    assert "Result: pass" in markdown
    assert "Report Hint: Use get_run_report with run_id='run-123'" in markdown
    assert "Negative Verification:" in markdown
    assert "- status: skipped" in markdown
    assert "- matched display: profile.displayName" in markdown
    assert "display displayName -> assert_text text_from input.displayName" in markdown
    assert json_code == 0
    assert json_payload["result"] == "pass"
    assert json_payload["report_hint"].startswith("Use get_run_report")
    assert json_payload["negative_verification"]["status"] == "skipped"


def test_agent_status_cli_reports_missing_status(tmp_path: Path, capsys) -> None:
    code = main(["agent-status", "--workspace-root", str(tmp_path), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["status"] == "missing"


def test_usage_status_cli_reports_usage_and_license_without_secret(tmp_path: Path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    update_agent_session(workspace, cli_run_result("checkout"))
    record_cloud_run_usage(workspace, count=2)
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_TIER", "pro")
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_KEY", "va_secret_key_value")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret_key")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ORG", "team-a")

    code = main(["usage-status", "--workspace-root", str(workspace), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["license"]["tier"] == "pro"
    assert payload["license"]["key_present"] is True
    assert payload["usage"]["runs_this_month"] == 1
    assert payload["usage"]["cloud_runs_used"] == 2
    assert payload["feature_access"]["cloud_run"] is True
    assert payload["cloud_config"]["available"] is True
    assert payload["cloud_config"]["api_key_present"] is True
    assert payload["cloud_config"]["endpoint"] == "https://cloud.visualagent.test"
    assert payload["remote_request_preview"]["status"] == "ready"
    assert payload["remote_request_preview"]["workflow_name"] == "example_workflow"
    assert payload["remote_request_preview"]["inputs"]["provided"] is False
    assert payload["remote_request_preview"]["network_probe"] == "not_run"
    assert "va_secret_key_value" not in json.dumps(payload)
    assert "va_cloud_secret_key" not in json.dumps(payload)


def test_usage_status_cli_outputs_markdown(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    update_agent_session(workspace, cli_run_result("checkout"))

    code = main(["usage-status", "--workspace-root", str(workspace), "--format", "markdown"])
    output = capsys.readouterr().out

    assert code == 0
    assert "# Visual Agent Usage" in output
    assert "Local runs this month: `1`" in output
    assert "## Cloud Config" in output
    assert "Blockers: missing_endpoint, missing_api_key" in output
    assert "cloud_run" in output


def test_cloud_run_plan_cli_outputs_blocked_request_without_reading_inputs(tmp_path: Path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    inputs_dir = workspace / "inputs"
    inputs_dir.mkdir(parents=True)
    (inputs_dir / "checkout.json").write_text('{"password": "demo_password"}', encoding="utf-8")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret_key")

    code = main(
        [
            "cloud-run-plan",
            "--workspace-root",
            str(workspace),
            "--workflow",
            "checkout",
            "--run-profile",
            "approved",
            "--inputs-file",
            "checkout.json",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    raw = json.dumps(payload)

    assert code == 0
    assert payload["workflow_name"] == "checkout"
    assert payload["request"]["status"] == "blocked"
    assert payload["request"]["run_profile"] == "approved"
    assert payload["request"]["inputs_file"] == "checkout.json"
    assert payload["request"]["inputs"]["provided"] is False
    assert payload["adapter_diagnostic"]["status"] == "blocked"
    assert "demo_password" not in raw
    assert "va_cloud_secret_key" not in raw


def test_cloud_run_plan_cli_outputs_markdown_ready_without_network(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret_key")

    code = main(
        [
            "cloud-run-plan",
            "--workspace-root",
            str(tmp_path),
            "--workflow",
            "checkout",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "# Cloud Run Plan" in output
    assert "Request status: `ready`" in output
    assert "Adapter Diagnostic" in output
    assert "transport is not enabled" in output
    assert "va_cloud_secret_key" not in output


def test_cloud_run_cli_defaults_to_plan_without_network(tmp_path: Path, capsys, monkeypatch) -> None:
    inputs_file = tmp_path / "checkout.json"
    inputs_file.write_text('{"password": "demo_password"}', encoding="utf-8")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret_key")

    code = main(
        [
            "cloud-run",
            "--workspace-root",
            str(tmp_path),
            "--workflow",
            "checkout",
            "--inputs-file",
            str(inputs_file),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    raw = json.dumps(payload)

    assert code == 0
    assert payload["execution_requested"] is False
    assert payload["network_sent"] is False
    assert payload["request"]["status"] == "ready"
    assert payload["request"]["inputs_file"] == str(inputs_file)
    assert payload["request"]["inputs"]["provided"] is False
    assert payload["adapter_diagnostic"]["status"] == "blocked"
    assert "transport is not enabled" in payload["adapter_diagnostic"]["message"]
    assert load_agent_session(tmp_path) is None
    assert "demo_password" not in raw
    assert "va_cloud_secret_key" not in raw


def test_cloud_run_cli_execute_without_transport_blocks_without_usage(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret_key")

    code = main(
        [
            "cloud-run",
            "--workspace-root",
            str(tmp_path),
            "--workflow",
            "checkout",
            "--execute",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "# Cloud Run" in output
    assert "Execution requested: `True`" in output
    assert "Network sent: `False`" in output
    assert "Status: `blocked`" in output
    assert "transport is not enabled" in output
    assert "va_cloud_secret_key" not in output
    assert load_agent_session(tmp_path) is None


def test_cloud_run_cli_execute_http_without_config_blocks_without_network(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("VISUAL_AGENT_CLOUD_ENDPOINT", raising=False)
    monkeypatch.delenv("VISUAL_AGENT_CLOUD_API_KEY", raising=False)

    code = main(
        [
            "cloud-run",
            "--workspace-root",
            str(tmp_path),
            "--workflow",
            "checkout",
            "--execute",
            "--transport",
            "http",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["transport"] == "http"
    assert payload["execution_requested"] is True
    assert payload["network_sent"] is False
    assert payload["request"]["status"] == "blocked"
    assert payload["request"]["cloud_config"]["blockers"] == ["missing_endpoint", "missing_api_key"]
    assert payload["result"]["status"] == "blocked"
    assert payload["result"]["usage_recorded"] is False
    assert load_agent_session(tmp_path) is None


def init_git_repo(path: Path) -> None:
    try:
        git(path, "init")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git is required for this test")


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)


def cli_run_result(workflow: str) -> WorkflowRunResult:
    return WorkflowRunResult(
        run_id="run-usage",
        run_dir=Path("runs") / "run-usage",
        workflow_name=workflow,
        steps=(WorkflowStepResult(id="observe", action="observe_fixture", status=ActionStatus.SUCCESS, message="ok"),),
        run_profile="dry-run",
    )
