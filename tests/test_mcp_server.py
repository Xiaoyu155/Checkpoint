from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from visual_agent.mcp_server import (
    call_tool,
    list_run_artifacts_payload,
    list_workflows_payload,
    mcp_tools,
    mcp_workspace_root_allowed,
    require_workspace,
    get_latest_failure_payload,
    get_workspace_dashboard_payload,
    generate_workflow_from_context_payload,
    run_workflow_payload,
    validate_workflow_payload,
    verify_implementation_payload,
)
from visual_agent.workspace import init_workspace


ROOT = Path(__file__).resolve().parent.parent


def content_payload(result):
    return json.loads(result[0].text)


def test_mcp_tools_include_expected_names() -> None:
    names = {tool.name for tool in mcp_tools()}

    assert names == {
        "list_workflows",
        "validate_workflow",
        "run_workflow",
        "get_run_report",
        "list_run_artifacts",
        "get_workspace_dashboard",
        "get_latest_failure",
        "summarize_latest_failure",
        "diagnose_failure",
        "repair_workflow",
        "auto_repair_failure",
        "list_repair_history",
        "rollback_repair",
        "get_repair_health",
        "list_benchmarks",
        "build_benchmark_plan",
        "build_benchmark_draft",
        "run_browser_smoke",
        "run_browser_smoke_suite",
        "get_session_context",
        "save_task_context",
        "run_verification",
        "generate_workflow_from_context",
        "verify_implementation",
        "generate_workflow",
    }


def test_mcp_generate_workflow_from_context_returns_quality_summary(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    html = (
        "<form action='/dashboard'>"
        "<label for='email'>Email</label><input id='email' name='email' type='email' required minlength='6'>"
        "<button type='submit'>Sign in</button>"
        "</form><p>Welcome to Dashboard</p>"
    )

    result = generate_workflow_from_context_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify login redirects to dashboard",
            "base_url": "fixtures/login.html",
            "dry_run": True,
            "code_changes": [{"file_path": "login.html", "before": None, "after": html, "change_type": "added"}],
        }
    )

    assert result["status"] == "success"
    assert result["workflow_path"] is None
    assert result["quality"]["score"] >= 0.6
    assert result["quality"]["forbidden_error_assertions"] == 0
    assert result["quality"]["text_from_input_references"] == 0
    assert result["quality"]["invalid_text_from_references"] == []
    assert result["framework_detected"] == "html"
    assert result["fields"] == ["email"]
    assert result["semantic_summary"]["framework"] == "html"
    assert result["semantic_summary"]["field_count"] == 1
    assert result["semantic_summary"]["required_field_count"] == 1
    assert result["semantic_summary"]["validation_rule_count"] == 3
    assert result["semantic_summary"]["data_display_count"] == 0
    assert result["semantic_summary"]["data_displays"] == []
    assert result["semantic_summary"]["matched_data_displays"] == []
    assert result["semantic_summary"]["unmatched_data_displays"] == []
    assert result["semantic_summary"]["negative_input_case_count"] == 3
    assert len(result["negative_input_cases"]) == 3
    assert result["negative_input_cases"][0]["mode"] == "draft_only"
    assert "generation_trace" in result
    assert len(result["generation_trace"]) <= 10
    assert "field email -> paste input.email" in result["generation_trace"]
    assert "success url /dashboard -> wait_for url" in result["generation_trace"]
    assert result["semantic_summary"]["success_state_count"] >= 1
    assert "yaml" in result


def test_mcp_generate_workflow_from_context_returns_data_display_match_summary(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    jsx = """
    function Profile() {
      return (
        <form>
          <input name="displayName" placeholder="Display name" />
          <button type="submit">Save</button>
          <p>Profile saved successfully</p>
          <p>{profile.displayName}</p>
          <p>{profile.timezone}</p>
        </form>
      );
    }
    """

    result = generate_workflow_from_context_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify profile saves",
            "base_url": "fixtures/profile.html",
            "dry_run": True,
            "code_changes": [{"file_path": "Profile.jsx", "before": None, "after": jsx, "change_type": "added"}],
        }
    )

    assert result["semantic_summary"]["data_displays"] == ["profile.displayName", "profile.timezone"]
    assert result["semantic_summary"]["matched_data_displays"] == ["profile.displayName"]
    assert result["semantic_summary"]["unmatched_data_displays"] == ["profile.timezone"]
    assert result["quality"]["data_display_assertions"] == 1
    assert result["quality"]["text_from_input_references"] == 1
    assert "display displayName -> assert_text text_from input.displayName" in result["generation_trace"]
    assert "display profile.timezone -> semantic_summary only" in result["generation_trace"]
    assert "text_from: input.displayName" in result["yaml"]
    assert "input.timezone" not in result["yaml"]


def test_mcp_generate_workflow_from_context_can_read_git_diff(tmp_path) -> None:
    init_git_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    page = workspace.fixtures_dir / "login.html"
    page.write_text("<form><input name='email'></form>\n", encoding="utf-8")
    git(tmp_path, "add", ".agent-workspace/fixtures/login.html")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    page.write_text(
        "<form action='/dashboard'><label for='email'>Email</label><input id='email' name='email'>"
        "<button type='submit'>Sign in</button></form><p>Welcome Dashboard</p>\n",
        encoding="utf-8",
    )

    result = generate_workflow_from_context_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify login redirects to dashboard",
            "base_url": "fixtures/login.html",
            "repo_root": str(tmp_path),
            "include_untracked": False,
            "dry_run": True,
        }
    )

    assert result["status"] == "success"
    assert result["fields"] == ["email"]
    assert result["quality"]["score"] >= 0.6
    assert result["semantic_summary"]["fields"] == ["email"]
    assert "url_contains: /dashboard" in result["yaml"]


def test_mcp_verify_implementation_dry_run_writes_status(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.fixtures_dir / "simple_form.html").write_text(
        "<form><label for='email'>Email</label><input id='email' name='email'>"
        "<button type='submit'>Save</button></form>",
        encoding="utf-8",
    )

    result = verify_implementation_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify simple form can submit",
            "base_url": "fixtures/simple_form.html",
            "run_profile": "dry-run",
            "min_quality_score": 0.0,
            "inputs": {"email": "demo@example.com"},
            "code_changes": [
                {
                    "file_path": "simple_form.html",
                    "before": None,
                    "after": (workspace.fixtures_dir / "simple_form.html").read_text(encoding="utf-8"),
                    "change_type": "added",
                }
            ],
        }
    )

    status_path = workspace.root / ".vscode-agent-status.json"

    assert result["result"] == "pass"
    assert result["workflow_path"]
    assert result["run_id"]
    assert result["inputs_path"]
    assert result["inputs_source"] == "explicit"
    assert result["report_path"].endswith(f"{result['run_id']}.json")
    assert result["report_markdown_path"].endswith(f"{result['run_id']}.md")
    assert "get_run_report" in result["report_hint"]
    assert result["next_action"].startswith("Implementation verified")
    assert result["semantic_summary"]["framework"] == "html"
    assert result["semantic_summary"]["field_count"] == 1
    assert result["semantic_summary"]["required_field_count"] == 0
    assert status_path.exists()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["result"] == "pass"
    assert status["report_path"] == result["report_path"]
    assert status["semantic_summary"]["field_count"] == 1


def test_mcp_verify_implementation_uses_generated_inputs_when_not_supplied(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.fixtures_dir / "simple_form.html").write_text(
        "<form><label for='email'>Email</label><input id='email' name='email' type='email' required>"
        "<button type='submit'>Save</button></form><p>Saved successfully</p>",
        encoding="utf-8",
    )

    result = verify_implementation_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify simple form can submit",
            "base_url": "fixtures/simple_form.html",
            "run_profile": "dry-run",
            "min_quality_score": 0.0,
            "code_changes": [
                {
                    "file_path": "simple_form.html",
                    "before": None,
                    "after": (workspace.fixtures_dir / "simple_form.html").read_text(encoding="utf-8"),
                    "change_type": "added",
                }
            ],
        }
    )

    assert result["result"] == "pass"
    assert result["inputs_path"]
    assert result["inputs_source"] == "generated_template"
    assert Path(result["inputs_path"]).exists()
    assert "negative_verification" not in result


def test_mcp_verify_implementation_can_opt_into_negative_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    html = (
        "<form><label for='email'>Email</label><input id='email' name='email' type='email' required>"
        "<button type='submit'>Save</button></form><p>Saved successfully</p>"
    )
    (workspace.fixtures_dir / "simple_form.html").write_text(html, encoding="utf-8")

    result = verify_implementation_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify simple form can submit",
            "base_url": "fixtures/simple_form.html",
            "run_profile": "dry-run",
            "min_quality_score": 0.0,
            "run_negative": True,
            "code_changes": [
                {
                    "file_path": "simple_form.html",
                    "before": None,
                    "after": html,
                    "change_type": "added",
                }
            ],
        }
    )

    assert result["result"] == "pass"
    assert result["negative_verification"]["requested"] is True
    assert result["negative_verification"]["status"] == "skipped"
    assert result["negative_verification"]["reason"] == "no_negative_oracle"
    assert result["negative_verification"]["workflow_name"].endswith("_negative_draft")
    assert result["negative_verification"]["workflow_path"].endswith("_negative_draft.yaml")
    assert result["negative_verification"]["reset_strategy"] == "fresh_observe_per_case"
    assert "validation error text" in result["negative_verification"]["next_action"]


def test_negative_workflow_report_passes_with_error_oracle(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from visual_agent.mcp_server import run_negative_workflow_verification
    from visual_agent.models import ActionStatus
    from visual_agent.workflow import WorkflowRunResult, WorkflowStepResult

    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    generation = SimpleNamespace(
        workflow_name="simple_form_verification",
        negative_workflow_path=str(workspace.workflows_dir / "simple_form_verification_negative_draft.yaml"),
        negative_input_cases=({"expected_error_texts": ["Invalid input"]},),
        negative_workflow_ready=True,
        negative_workflow_reason="ready",
        negative_workflow_reset_strategy="fresh_observe_per_case",
        negative_oracles=({"text": "Invalid input", "source": "html:text"},),
    )

    def fake_run(_workspace, workflow_name, *, inputs, dry_run, run_profile, timeout_seconds):
        assert workflow_name == "simple_form_verification_negative_draft"
        assert inputs == {}
        assert dry_run is True
        return WorkflowRunResult(
            run_id="run-negative",
            run_dir=workspace.runs_dir / "run-negative",
            workflow_name=workflow_name,
            steps=(WorkflowStepResult(id="assert_error", action="assert_text_contract", status=ActionStatus.DRY_RUN),),
            run_profile=run_profile,
        )

    monkeypatch.setattr("visual_agent.mcp_server.run_workspace_workflow_with_timeout", fake_run)

    report = run_negative_workflow_verification(workspace, generation, run_profile="dry-run", timeout_seconds=30)

    assert report["requested"] is True
    assert report["status"] == "pass"
    assert report["run_id"] == "run-negative"
    assert report["reset_strategy"] == "fresh_observe_per_case"
    assert report["oracles"] == [{"text": "Invalid input", "source": "html:text"}]
    assert report["report_path"].endswith("run-negative.json")
    assert report["report_markdown_path"].endswith("run-negative.md")
    assert "get_run_report" in report["report_hint"]
    assert report["next_action"].startswith("Negative validation passed")
    assert report["steps_passed"] == 1
    assert report["steps_total"] == 1


def test_negative_workflow_report_failure_has_next_action_and_artifacts(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from visual_agent.mcp_server import run_negative_workflow_verification
    from visual_agent.models import ActionStatus
    from visual_agent.workflow import WorkflowRunResult, WorkflowStepResult

    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    generation = SimpleNamespace(
        workflow_name="simple_form_verification",
        negative_workflow_path=str(workspace.workflows_dir / "simple_form_verification_negative_draft.yaml"),
        negative_workflow_ready=True,
        negative_workflow_reason="ready",
        negative_workflow_reset_strategy="fresh_observe_per_case",
        negative_oracles=({"text": "Invalid input", "source": "html:text"},),
    )

    def fake_run(_workspace, workflow_name, *, inputs, dry_run, run_profile, timeout_seconds):
        return WorkflowRunResult(
            run_id="run-negative-fail",
            run_dir=workspace.runs_dir / "run-negative-fail",
            workflow_name=workflow_name,
            steps=(WorkflowStepResult(id="assert_error", action="assert_text_contract", status=ActionStatus.FAILED, message="missing error"),),
            run_profile=run_profile,
        )

    monkeypatch.setattr("visual_agent.mcp_server.run_workspace_workflow_with_timeout", fake_run)

    report = run_negative_workflow_verification(workspace, generation, run_profile="dry-run", timeout_seconds=30)

    assert report["status"] == "fail"
    assert report["failed_step"]["id"] == "assert_error"
    assert report["report_path"].endswith("run-negative-fail.json")
    assert "negative verification report" in report["next_action"]


def test_negative_workflow_report_redacts_oracle_secrets(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from visual_agent.mcp_server import run_negative_workflow_verification
    from visual_agent.models import ActionStatus
    from visual_agent.workflow import WorkflowRunResult, WorkflowStepResult

    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    generation = SimpleNamespace(
        workflow_name="simple_form_verification",
        negative_workflow_path=str(workspace.workflows_dir / "simple_form_verification_negative_draft.yaml"),
        negative_workflow_ready=True,
        negative_workflow_reason="ready",
        negative_workflow_reset_strategy="fresh_observe_per_case",
        negative_oracles=({"text": "Invalid api_key=sk-secret-value", "source": "html:text"},),
    )

    def fake_run(_workspace, workflow_name, *, inputs, dry_run, run_profile, timeout_seconds):
        return WorkflowRunResult(
            run_id="run-negative-redacted",
            run_dir=workspace.runs_dir / "run-negative-redacted",
            workflow_name=workflow_name,
            steps=(WorkflowStepResult(id="assert_error", action="assert_text_contract", status=ActionStatus.DRY_RUN),),
            run_profile=run_profile,
        )

    monkeypatch.setattr("visual_agent.mcp_server.run_workspace_workflow_with_timeout", fake_run)

    report = run_negative_workflow_verification(workspace, generation, run_profile="dry-run", timeout_seconds=30)

    raw = str(report)
    assert report["oracles"][0]["text"] == "Invalid api_key=[REDACTED]"
    assert "sk-secret-value" not in raw


def test_mcp_verify_implementation_blocks_low_quality_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.fixtures_dir / "simple_form.html").write_text(
        "<form><label for='email'>Email</label><input id='email' name='email'>"
        "<button type='submit'>Save</button></form>",
        encoding="utf-8",
    )

    result = verify_implementation_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify simple form can submit",
            "base_url": "fixtures/simple_form.html",
            "run_profile": "dry-run",
            "inputs": {"email": "demo@example.com"},
            "code_changes": [
                {
                    "file_path": "simple_form.html",
                    "before": None,
                    "after": (workspace.fixtures_dir / "simple_form.html").read_text(encoding="utf-8"),
                    "change_type": "added",
                }
            ],
        }
    )

    status = json.loads((workspace.root / ".vscode-agent-status.json").read_text(encoding="utf-8"))

    assert result["result"] == "needs_workflow_improvement"
    assert result["quality_score"] < result["min_quality_score"]
    assert result["run_id"] is None
    assert result["quality"]["gaps"]
    assert result["semantic_summary"]["confidence"] >= 0.5
    assert result["next_action"]
    assert status["result"] == "needs_workflow_improvement"
    assert status["quality"]["gaps"]
    assert status["semantic_summary"]["framework"] == "html"
    assert status["next_action"] == result["next_action"]


def test_mcp_verify_implementation_can_lower_quality_threshold(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.fixtures_dir / "simple_form.html").write_text(
        "<form><label for='email'>Email</label><input id='email' name='email'>"
        "<button type='submit'>Save</button></form>",
        encoding="utf-8",
    )

    result = verify_implementation_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify simple form can submit",
            "base_url": "fixtures/simple_form.html",
            "run_profile": "dry-run",
            "min_quality_score": 0.0,
            "inputs": {"email": "demo@example.com"},
            "code_changes": [
                {
                    "file_path": "simple_form.html",
                    "before": None,
                    "after": (workspace.fixtures_dir / "simple_form.html").read_text(encoding="utf-8"),
                    "change_type": "added",
                }
            ],
        }
    )

    assert result["result"] == "pass"
    assert result["run_id"]


def test_mcp_verify_implementation_timeout_before_run_writes_status(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.fixtures_dir / "login.html").write_text(
        "<form action='/dashboard'><label for='email'>Email</label><input id='email' name='email'>"
        "<button type='submit'>Sign in</button></form><p>Welcome Dashboard</p>",
        encoding="utf-8",
    )

    result = verify_implementation_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify login redirects",
            "base_url": "fixtures/login.html",
            "run_profile": "dry-run",
            "timeout_seconds": 0,
            "inputs": {"email": "demo@example.com"},
            "code_changes": [
                {
                    "file_path": "login.html",
                    "before": None,
                    "after": (workspace.fixtures_dir / "login.html").read_text(encoding="utf-8"),
                    "change_type": "added",
                }
            ],
        }
    )

    status = json.loads((workspace.root / ".vscode-agent-status.json").read_text(encoding="utf-8"))

    assert result["result"] == "timeout"
    assert result["workflow_path"]
    assert result["run_id"] is None
    assert result["timeout_seconds"] == 0
    assert result["next_action"].startswith("Increase timeout_seconds")
    assert result["semantic_summary"]["success_state_count"] >= 1
    assert status["result"] == "timeout"
    assert status["next_action"] == result["next_action"]


def init_git_repo(path: Path) -> None:
    try:
        git(path, "init")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git is required for this test")


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)


def test_mcp_list_workflows_returns_workspace_workflows(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = list_workflows_payload({"workspace_root": str(workspace.root)})

    assert result["workflow_count"] >= 1
    assert any(item["name"] == "local_html_form_workflow" for item in result["workflows"])
    assert next(item for item in result["workflows"] if item["name"] == "local_html_form_workflow")["visibility"] == "private"


def test_mcp_list_workflows_truncates_large_response(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    for index in range(250):
        (workspace.workflows_dir / f"workflow_{index:03d}_with_long_name_for_budget.yaml").write_text(
            "schema_version: 1\n"
            "min_runtime_version: '0.1.0'\n"
            f"name: workflow_{index:03d}_with_long_name_for_budget\n"
            "version: 1\n"
            "steps:\n"
            "  - id: observe\n"
            "    action: observe_screen\n",
            encoding="utf-8",
        )

    result = asyncio.run(call_tool("list_workflows", {"workspace_root": str(workspace.root)}))
    payload = content_payload(result)

    assert len(result[0].text) <= 8000
    assert payload["workflow_count"] == 250
    assert payload["truncated"] is True
    assert payload["omitted_count"] > 0


def test_mcp_validate_workflow_returns_validation_and_preflight(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = validate_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow"})

    assert result["valid"] is True
    assert result["preflight"]["ok"] is True


def test_mcp_validate_missing_observation_workflow_returns_not_valid(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.workflows_dir / "bad.yaml").write_text(
        "schema_version: 1\nmin_runtime_version: '0.1.0'\nname: bad\nversion: 1\nsteps:\n  - id: click\n    action: click\n",
        encoding="utf-8",
    )

    result = validate_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "bad"})

    assert result["valid"] is False


def test_mcp_run_workflow_defaults_to_dry_run_and_audits(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    args = {"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"}
    result = run_workflow_payload(args)
    audit = workspace.root / "gui" / "actions.jsonl"

    assert result["status"] == "success"
    assert result["run_profile"] == "dry-run"
    assert result["run_id"]
    assert not audit.exists()

    async_result = asyncio.run(call_tool("run_workflow", args))
    payload = content_payload(async_result)

    assert payload["status"] == "success"
    assert audit.exists()
    assert "mcp:run_workflow" in audit.read_text(encoding="utf-8")


def test_mcp_run_workflow_defaults_to_compact_report_and_supports_verbose(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    args = {"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"}

    compact = run_workflow_payload(args)
    verbose = run_workflow_payload({**args, "verbose": True})

    assert compact["status"] == "success"
    assert compact["workflow"] == "local_html_form_workflow"
    assert "steps" in compact
    assert "failed_steps" not in compact
    assert verbose["status"] == "success"
    assert "failed_steps" in verbose


def test_mcp_run_workflow_rejects_approved_outside_whitelist(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    with pytest.raises(ValueError):
        run_workflow_payload(
            {
                "workspace_root": str(workspace.root),
                "workflow_name": "local_html_form_workflow",
                "run_profile": "approved",
                "inputs_file": "demo_login.json",
            }
        )


def test_mcp_run_workflow_rejects_approved_when_whitelist_empty_and_when_outside(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    manifest = workspace.root / "workspace.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["mcp"]["max_run_profile"] = "approved"
    data["mcp"]["approved_workflows"] = ["other_workflow"]
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(ValueError):
        run_workflow_payload(
            {
                "workspace_root": str(workspace.root),
                "workflow_name": "local_html_form_workflow",
                "run_profile": "approved",
                "inputs_file": "demo_login.json",
            }
        )


def test_mcp_run_workflow_allows_approved_when_whitelisted_and_max_profile_allows(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    manifest = workspace.root / "workspace.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["mcp"]["max_run_profile"] = "approved"
    data["mcp"]["approved_workflows"] = ["local_html_form_workflow"]
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_workflow_payload(
        {
            "workspace_root": str(workspace.root),
            "workflow_name": "local_html_form_workflow",
            "run_profile": "approved",
            "inputs_file": "demo_login.json",
        }
    )

    assert result["run_profile"] == "approved"


def test_mcp_run_workflow_downgrades_approved_to_max_profile_when_whitelisted(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    manifest = workspace.root / "workspace.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["mcp"]["max_run_profile"] = "supervised"
    data["mcp"]["approved_workflows"] = ["local_html_form_workflow"]
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_workflow_payload(
        {
            "workspace_root": str(workspace.root),
            "workflow_name": "local_html_form_workflow",
            "run_profile": "approved",
            "inputs_file": "demo_login.json",
        }
    )

    assert result["requested_run_profile"] == "approved"
    assert result["run_profile"] == "supervised"


def test_mcp_get_run_report_markdown_is_redacted(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})

    result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "markdown"}))
    payload = content_payload(result)
    text = payload["content"]

    assert "Report Detail" in text
    assert "secret" not in text.lower()
    assert "cookie" not in text.lower()


def test_mcp_get_run_report_scrubs_sensitive_field_names_and_values(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})
    report_path = workspace.reports_dir / f"{run['run_id']}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["artifacts"] = {
        "password": "plain-password",
        "token": "plain-token",
        "cookie": "session-cookie",
        "api_key": "sk-testsecret123456",
        "authorization": "Bearer abcdefghijklmnop",
        "bearer": "abcdefghijklmnop",
        "message": "token=abc12345",
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    json_result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "json"}))
    json_payload = content_payload(json_result)
    markdown_result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "markdown"}))
    markdown_payload = content_payload(markdown_result)
    combined = json.dumps(json_payload, ensure_ascii=False) + markdown_payload["content"]

    assert "plain-password" not in combined
    assert "plain-token" not in combined
    assert "session-cookie" not in combined
    assert "sk-testsecret123456" not in combined
    assert "abcdefghijklmnop" not in combined
    assert "[REDACTED]" in combined or '"redacted": true' in combined.lower()


def test_mcp_get_run_report_is_budgeted_when_report_is_large(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})
    report_path = workspace.reports_dir / f"{run['run_id']}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["steps"] = [
        {
            "id": f"step_{index}",
            "action": "assert_text",
            "status": "success",
            "message": "large report line " + ("x" * 500),
            "elapsed_seconds": 0.01,
        }
        for index in range(80)
    ]
    report["total_steps"] = 80
    report["succeeded_steps"] = 80
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    markdown_result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "markdown"}))
    markdown_payload = content_payload(markdown_result)
    json_result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "json"}))
    json_payload = content_payload(json_result)

    assert markdown_payload["truncated"] is True
    assert markdown_payload["within_budget"] is True
    assert len(markdown_result[0].text) <= 8000
    assert json_payload["truncated"] is True
    assert json_payload["within_budget"] is True
    assert len(json_result[0].text) <= 8000


def test_mcp_list_run_artifacts_paths_stay_inside_workspace(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})

    result = list_run_artifacts_payload({"workspace_root": str(workspace.root), "run_id": run["run_id"]})

    assert result["artifact_count"] > 0
    for artifact in result["artifacts"]:
        assert str(artifact["path"]).startswith(str(workspace.root))
        assert ".." not in artifact["relative_path"]


def test_mcp_list_run_artifacts_truncates_large_response(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})
    artifact_dir = workspace.runs_dir / run["run_id"] / "many"
    artifact_dir.mkdir(parents=True)
    for index in range(300):
        (artifact_dir / f"artifact_{index:03d}_with_long_name_for_budget.txt").write_text("x", encoding="utf-8")

    result = asyncio.run(call_tool("list_run_artifacts", {"workspace_root": str(workspace.root), "run_id": run["run_id"]}))
    payload = content_payload(result)

    assert len(result[0].text) <= 8000
    assert payload["artifact_count"] >= 300
    assert payload["truncated"] is True
    assert payload["omitted_count"] > 0


def test_mcp_list_run_artifacts_skips_symlink_that_escapes_workspace(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside", encoding="utf-8")
    link = workspace.runs_dir / run["run_id"] / "outside-link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this Windows environment.")

    result = list_run_artifacts_payload({"workspace_root": str(workspace.root), "run_id": run["run_id"]})

    assert all("outside-link.txt" not in artifact["relative_path"] for artifact in result["artifacts"])
    assert all(str(outside.resolve()) != artifact["path"] for artifact in result["artifacts"])


def test_mcp_workspace_dashboard_returns_agent_readable_health(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})

    result = get_workspace_dashboard_payload({"workspace_root": str(workspace.root), "format": "markdown"})

    assert result["format"] == "markdown"
    assert "Workspace Dashboard" in result["content"]
    assert "Workflows" in result["content"]


def test_mcp_latest_failure_returns_none_when_clean(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = get_latest_failure_payload({"workspace_root": str(workspace.root), "format": "json"})

    assert result["status"] == "none"
    assert result["report"] is None


def test_mcp_latest_failure_returns_failed_report_with_diagnosis(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    failure_workflow = workspace.workflows_dir / "failure.yaml"
    failure_workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )
    run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "failure"})

    result = get_latest_failure_payload({"workspace_root": str(workspace.root), "format": "json"})

    assert result["status"] == "found"
    assert result["report"]["status"] == "failed"
    assert result["report"]["failure"]["diagnosis"]["expected"]


def test_mcp_session_context_and_failure_summary_stay_within_budget(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    failure_workflow = workspace.workflows_dir / "failure.yaml"
    failure_workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )
    run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "failure"})

    context = content_payload(asyncio.run(call_tool("get_session_context", {"workspace_root": str(workspace.root)})))
    summary = content_payload(asyncio.run(call_tool("summarize_latest_failure", {"workspace_root": str(workspace.root)})))
    combined = json.dumps(context, ensure_ascii=False) + json.dumps(summary, ensure_ascii=False)

    assert context["within_budget"] is True
    assert len(context["snapshot"]) <= 2000
    assert summary["status"] == "found"
    assert len(json.dumps(summary, ensure_ascii=False)) <= 2000
    for keyword in ("password", "cookie", "Bearer ", "demo123"):
        assert keyword not in combined


def test_mcp_diagnose_failure_and_repair_workflow_return_ai_ready_payloads(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    failure_workflow = workspace.workflows_dir / "failure.yaml"
    failure_workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "failure"})

    diagnosis = content_payload(
        asyncio.run(call_tool("diagnose_failure", {"workspace_root": str(workspace.root), "run_id": run["run_id"]}))
    )
    repair = content_payload(
        asyncio.run(call_tool("repair_workflow", {"workspace_root": str(workspace.root), "run_id": run["run_id"]}))
    )

    assert diagnosis["status"] == "found"
    assert diagnosis["failed_step"]["id"] == "assert_missing"
    assert "repair_prompt" in diagnosis
    assert repair["status"] == "suggested"
    assert repair["repair"]["classification"] == "app_bug"
    assert repair["source"] == "deterministic"
    assert repair["repair"]["candidates"][0]["id"] == "manual_investigation"
    assert repair["repair"]["candidates"][0]["apply_supported"] is False


def test_mcp_list_repair_history_returns_recorded_attempts(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    failure_workflow = workspace.workflows_dir / "failure.yaml"
    failure_workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "failure"})
    content_payload(asyncio.run(call_tool("repair_workflow", {"workspace_root": str(workspace.root), "run_id": run["run_id"]})))

    history = content_payload(asyncio.run(call_tool("list_repair_history", {"workspace_root": str(workspace.root)})))

    assert history["total_entries"] == 1
    assert history["entries"][0]["workflow"] == "failure"
    assert history["entries"][0]["status"] == "suggested"


def test_mcp_rollback_repair_restores_recorded_backup(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: typo_failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_title\n"
        "    action: assert_text\n"
        "    text: 客户管理系統\n",
        encoding="utf-8",
    )
    original = workflow_path.read_text(encoding="utf-8")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})
    repair = content_payload(
        asyncio.run(
            call_tool(
                "repair_workflow",
                {"workspace_root": str(workspace.root), "run_id": run["run_id"], "apply": True},
            )
        )
    )

    payload = content_payload(
        asyncio.run(
            call_tool(
                "rollback_repair",
                {"workspace_root": str(workspace.root), "history_id": repair["history"]["history_id"]},
            )
        )
    )

    assert payload["status"] == "manual_rolled_back"
    assert workflow_path.read_text(encoding="utf-8") == original


def test_mcp_get_repair_health_summarizes_history(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: typo_failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_title\n"
        "    action: assert_text\n"
        "    text: 客户管理系統\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})
    content_payload(
        asyncio.run(
            call_tool(
                "repair_workflow",
                {"workspace_root": str(workspace.root), "run_id": run["run_id"], "apply": True, "verify": True},
            )
        )
    )

    health = content_payload(asyncio.run(call_tool("get_repair_health", {"workspace_root": str(workspace.root)})))

    assert health["applied_count"] == 1
    assert health["verified_count"] == 1
    assert health["risk_level"] == "low"
    assert health["status_counts"]["verified"] == 1


def test_mcp_auto_repair_failure_applies_verifies_and_returns_health(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: typo_failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_title\n"
        "    action: assert_text\n"
        "    text: 客户管理系統\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})

    payload = content_payload(
        asyncio.run(
            call_tool(
                "auto_repair_failure",
                {"workspace_root": str(workspace.root), "run_id": run["run_id"]},
            )
        )
    )

    assert payload["status"] == "verified"
    assert payload["repair_result"]["workflow_repair_plan"]["verification"]["status"] == "passed"
    assert payload["repair_health"]["risk_level"] == "low"
    assert "客户管理系统" in workflow_path.read_text(encoding="utf-8")


def test_mcp_auto_repair_failure_dry_run_does_not_modify_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: typo_failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_title\n"
        "    action: assert_text\n"
        "    text: 客户管理系統\n",
        encoding="utf-8",
    )
    original = workflow_path.read_text(encoding="utf-8")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})

    payload = content_payload(
        asyncio.run(
            call_tool(
                "auto_repair_failure",
                {"workspace_root": str(workspace.root), "run_id": run["run_id"], "dry_run": True},
            )
        )
    )

    assert payload["status"] == "suggested"
    assert payload["auto_repair"]["dry_run"] is True
    assert payload["repair_result"]["workflow_repair_plan"]["applied"] is False
    assert workflow_path.read_text(encoding="utf-8") == original


def test_mcp_auto_repair_failure_blocks_high_risk_health(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: typo_failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_title\n"
        "    action: assert_text\n"
        "    text: 客户管理系統\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})
    verified = content_payload(
        asyncio.run(call_tool("auto_repair_failure", {"workspace_root": str(workspace.root), "run_id": run["run_id"]}))
    )
    history = content_payload(asyncio.run(call_tool("list_repair_history", {"workspace_root": str(workspace.root)})))
    content_payload(
        asyncio.run(
            call_tool(
                "rollback_repair",
                {"workspace_root": str(workspace.root), "history_id": history["entries"][0]["history_id"]},
            )
        )
    )
    failed_again = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})

    payload = content_payload(
        asyncio.run(call_tool("auto_repair_failure", {"workspace_root": str(workspace.root), "run_id": failed_again["run_id"]}))
    )

    assert payload["status"] == "blocked"
    assert payload["auto_repair"]["blocked"] is True
    assert payload["auto_repair"]["apply"] is False
    assert payload["preflight_repair_health"]["risk_level"] == "high"


def test_mcp_auto_repair_failure_respects_workspace_policy(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["auto_repair"] = {"min_confidence": 0.99}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: typo_failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_title\n"
        "    action: assert_text\n"
        "    text: 客户管理系統\n",
        encoding="utf-8",
    )
    original = workflow_path.read_text(encoding="utf-8")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})

    payload = content_payload(
        asyncio.run(call_tool("auto_repair_failure", {"workspace_root": str(workspace.root), "run_id": run["run_id"]}))
    )

    assert payload["status"] == "suggested"
    assert payload["auto_repair"]["policy"]["min_confidence"] == 0.99
    assert payload["repair_result"]["workflow_repair_plan"]["status"] == "not_applied"
    assert workflow_path.read_text(encoding="utf-8") == original


def test_mcp_auto_repair_failure_can_promote_regression(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: typo_failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_title\n"
        "    action: assert_text\n"
        "    text: 客户管理系統\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})

    payload = content_payload(
        asyncio.run(
            call_tool(
                "auto_repair_failure",
                {"workspace_root": str(workspace.root), "run_id": run["run_id"], "promote_regression": True},
            )
        )
    )

    assert payload["status"] == "verified"
    assert payload["regression"]["status"] == "promoted"
    assert Path(payload["regression"]["test_path"]).exists()


def test_mcp_auto_repair_failure_can_promote_and_run_regression(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: typo_failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_title\n"
        "    action: assert_text\n"
        "    text: 客户管理系統\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})

    payload = content_payload(
        asyncio.run(
            call_tool(
                "auto_repair_failure",
                {
                    "workspace_root": str(workspace.root),
                    "run_id": run["run_id"],
                    "promote_regression": True,
                    "run_regression": True,
                    "regression_timeout_seconds": 30,
                },
            )
        )
    )

    assert payload["status"] == "verified"
    assert payload["regression"]["test_run"]["status"] == "success"
    assert payload["regression"]["test_run"]["passed_tests"] == 1


def test_mcp_session_context_includes_latest_repair_summary(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    failure_workflow = workspace.workflows_dir / "failure.yaml"
    failure_workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "failure"})
    content_payload(asyncio.run(call_tool("repair_workflow", {"workspace_root": str(workspace.root), "run_id": run["run_id"]})))

    context = content_payload(asyncio.run(call_tool("get_session_context", {"workspace_root": str(workspace.root)})))

    assert "Latest Repair" in context["snapshot"]
    assert "Workflow: failure" in context["snapshot"]


def test_mcp_list_benchmarks_returns_public_references(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    payload = content_payload(asyncio.run(call_tool("list_benchmarks", {"workspace_root": str(workspace.root)})))

    assert payload["status"] == "ready"
    assert payload["benchmark_count"] >= 4
    assert any(item["id"] == "stagehand_act_extract" for item in payload["benchmarks"])


def test_mcp_build_benchmark_plan_returns_scenarios(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    payload = content_payload(
        asyncio.run(
            call_tool(
                "build_benchmark_plan",
                {"workspace_root": str(workspace.root), "benchmark_id": "healenium_locator_repair"},
            )
        )
    )

    assert payload["status"] == "ready"
    assert payload["benchmark_count"] == 1
    assert payload["scenario_count"] >= 1
    assert payload["scenarios"][0]["benchmark_id"] == "healenium_locator_repair"


def test_mcp_build_benchmark_draft_can_save_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)

    payload = content_payload(
        asyncio.run(
            call_tool(
                "build_benchmark_draft",
                {
                    "workspace_root": str(workspace.root),
                    "scenario_id": "healenium_locator_repair_1",
                    "save": True,
                },
            )
        )
    )

    assert payload["status"] == "success"
    assert Path(payload["saved_to"]).exists()
    assert payload["workflow_name"].startswith("benchmark_healenium_locator_repair")


def test_mcp_run_browser_smoke_returns_diagnostics(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)

    def fake_run_browser_smoke(**kwargs):
        return {
            "status": "success",
            "url": kwargs["url"],
            "run_dir": str(workspace.root / "browser-smoke-runs" / "fake"),
            "initial": {"visible_text_length": 5, "interactive_count": 1, "screenshot_path": "fake.png"},
            "after_click": None,
            "click": None,
            "issues": [],
        }

    monkeypatch.setattr("visual_agent.browser_smoke.run_browser_smoke", fake_run_browser_smoke)
    payload = content_payload(
        asyncio.run(
            call_tool(
                "run_browser_smoke",
                {"workspace_root": str(workspace.root), "url": "https://example.test/login", "expect_text": ["Login"]},
            )
        )
    )

    assert payload["status"] == "success"
    assert payload["workspace"] == str(workspace.root)
    assert payload["url"] == "https://example.test/login"


def test_mcp_run_browser_smoke_suite_returns_summary(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    suite = workspace.root / "suite.json"
    suite.write_text('{"cases":[{"id":"home","url":"https://example.test/home"}]}', encoding="utf-8")

    def fake_run_browser_smoke_suite(*_args, **_kwargs):
        return {
            "status": "success",
            "suite_name": "suite",
            "run_dir": str(workspace.root / "browser-smoke-suite-runs" / "fake"),
            "case_count": 1,
            "passed_count": 1,
            "failed_count": 0,
            "results": [{"case_id": "home", "status": "success"}],
        }

    monkeypatch.setattr("visual_agent.browser_smoke_suite.run_browser_smoke_suite", fake_run_browser_smoke_suite)
    payload = content_payload(
        asyncio.run(
            call_tool(
                "run_browser_smoke_suite",
                {"workspace_root": str(workspace.root), "suite_file": "suite.json"},
            )
        )
    )

    assert payload["status"] == "success"
    assert payload["workspace"] == str(workspace.root)
    assert payload["case_count"] == 1


def test_mcp_run_verification_returns_ai_ready_report(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow = workspace.workflows_dir / "verification.yaml"
    workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: verification\n"
        "version: 1\n"
        "tags:\n"
        "  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n",
        encoding="utf-8",
    )

    payload = content_payload(asyncio.run(call_tool("run_verification", {"workspace_root": str(workspace.root)})))

    assert payload["total"] == 1
    assert payload["passed"] == 1
    assert payload["failed"] == 0
    assert payload["within_budget"] is True
    assert "Verification Report" in payload["content"]


def test_mcp_run_verification_can_target_one_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    for name in ("slow_visual_contract", "fast_smoke_contract"):
        (workspace.workflows_dir / f"{name}.yaml").write_text(
            "schema_version: 1\n"
            "min_runtime_version: '0.1.0'\n"
            f"name: {name}\n"
            "version: 1\n"
            "tags:\n"
            "  - verification\n"
            "steps:\n"
            "  - id: observe\n"
            "    action: observe_fixture\n"
            f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n",
            encoding="utf-8",
        )

    payload = content_payload(
        asyncio.run(
            call_tool(
                "run_verification",
                {
                    "workspace_root": str(workspace.root),
                    "workflow": ["fast_smoke_contract"],
                    "max_workflows": 1,
                },
            )
        )
    )

    assert payload["total"] == 1
    assert "fast_smoke_contract" in payload["content"]
    assert "slow_visual_contract" not in payload["content"]


def test_mcp_run_verification_skips_slow_by_default_and_includes_when_requested(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    for name, extra_tags in (("slow_visual_contract", "  - slow\n"), ("fast_smoke_contract", "")):
        (workspace.workflows_dir / f"{name}.yaml").write_text(
            "schema_version: 1\n"
            "min_runtime_version: '0.1.0'\n"
            f"name: {name}\n"
            "version: 1\n"
            "tags:\n"
            "  - verification\n"
            f"{extra_tags}"
            "steps:\n"
            "  - id: observe\n"
            "    action: observe_fixture\n"
            f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n",
            encoding="utf-8",
        )

    default_payload = content_payload(asyncio.run(call_tool("run_verification", {"workspace_root": str(workspace.root)})))
    included_payload = content_payload(
        asyncio.run(call_tool("run_verification", {"workspace_root": str(workspace.root), "include_slow": True}))
    )

    assert default_payload["total"] == 1
    assert "fast_smoke_contract" in default_payload["content"]
    assert "slow_visual_contract" not in default_payload["content"]
    assert included_payload["total"] == 2
    assert "slow_visual_contract" in included_payload["content"]


def test_mcp_list_workflows_skips_slow_by_default_and_includes_when_requested(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.workflows_dir / "fast.yaml").write_text(
        "schema_version: 1\nname: fast\nversion: 1\ntags:\n  - verification\nsteps:\n  - id: observe\n    action: observe_ocr\n    mock_text: ready\n",
        encoding="utf-8",
    )
    (workspace.workflows_dir / "slow.yaml").write_text(
        "schema_version: 1\nname: slow\nversion: 1\ntags:\n  - verification\n  - slow\nsteps:\n  - id: observe\n    action: observe_ocr\n    mock_text: ready\n",
        encoding="utf-8",
    )

    default_payload = list_workflows_payload({"workspace_root": str(workspace.root)})
    included_payload = list_workflows_payload({"workspace_root": str(workspace.root), "include_slow": True})

    assert [item["name"] for item in default_payload["workflows"]] == ["fast"]
    assert {item["name"] for item in included_payload["workflows"]} == {"fast", "slow"}
    assert next(item for item in included_payload["workflows"] if item["name"] == "slow")["tags"] == ["verification", "slow"]


def test_mcp_workspace_root_rejects_path_traversal(tmp_path) -> None:
    with pytest.raises(ValueError):
        require_workspace({"workspace_root": str(tmp_path / ".." / "workspace")})


def test_mcp_workspace_root_rejects_system_path_with_structured_error() -> None:
    system_path = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"
    if not system_path.exists():
        pytest.skip("System32 path is unavailable on this environment.")

    assert mcp_workspace_root_allowed(system_path) is False
    result = asyncio.run(call_tool("list_workflows", {"workspace_root": str(system_path)}))
    payload = content_payload(result)

    assert "error" in payload
    assert "outside allowed MCP roots" in payload["error"]


def test_mcp_unknown_workflow_returns_structured_error(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = asyncio.run(call_tool("validate_workflow", {"workspace_root": str(workspace.root), "workflow_name": "missing"}))
    payload = content_payload(result)

    assert "error" in payload
    assert "Workflow not found" in payload["error"]


def test_mcp_call_audit_writes_entry_and_exit_events(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    asyncio.run(call_tool("list_workflows", {"workspace_root": str(workspace.root)}))
    audit_path = workspace.root / "gui" / "actions.jsonl"
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

    assert len(events) >= 2
    assert events[-2]["action"] == "mcp:list_workflows"
    assert events[-2]["status"] == "started"
    assert events[-1]["action"] == "mcp:list_workflows"
    assert events[-1]["status"] == "success"


def assert_mcp_tool_audited(workspace, tool_name: str, args: dict[str, object]) -> None:
    asyncio.run(call_tool(tool_name, {"workspace_root": str(workspace.root), **args}))
    audit_path = workspace.root / "gui" / "actions.jsonl"
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

    assert events[-2]["action"] == f"mcp:{tool_name}"
    assert events[-2]["status"] == "started"
    assert events[-1]["action"] == f"mcp:{tool_name}"
    assert events[-1]["status"] in {
        "success",
        "none",
        "found",
        "no_failure",
        "saved",
        "suggested",
        "needs_model",
        "ready",
        "applied",
        "verified",
        "applied_unverified",
        "rolled_back",
        "rollback_failed",
        "manual_rolled_back",
        "not_found",
        "blocked",
    }


def test_mcp_get_session_context_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "get_session_context", {})


def test_mcp_summarize_latest_failure_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "summarize_latest_failure", {})


def test_mcp_diagnose_failure_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "diagnose_failure", {})


def test_mcp_repair_workflow_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "repair_workflow", {})


def test_mcp_auto_repair_failure_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "auto_repair_failure", {})


def test_mcp_list_repair_history_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "list_repair_history", {})


def test_mcp_rollback_repair_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "rollback_repair", {})


def test_mcp_get_repair_health_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "get_repair_health", {})


def test_mcp_list_benchmarks_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "list_benchmarks", {})


def test_mcp_build_benchmark_plan_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "build_benchmark_plan", {})


def test_mcp_build_benchmark_draft_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "build_benchmark_draft", {"scenario_id": "stagehand_act_extract_1"})


def test_mcp_run_browser_smoke_writes_audit_entry(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    monkeypatch.setattr(
        "visual_agent.browser_smoke.run_browser_smoke",
        lambda **_kwargs: {"status": "success", "url": "https://example.test", "run_dir": "fake", "issues": []},
    )
    assert_mcp_tool_audited(workspace, "run_browser_smoke", {"url": "https://example.test"})


def test_mcp_run_browser_smoke_suite_writes_audit_entry(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    (workspace.root / "suite.json").write_text('{"cases":[{"id":"home","url":"https://example.test/home"}]}', encoding="utf-8")

    monkeypatch.setattr(
        "visual_agent.browser_smoke_suite.run_browser_smoke_suite",
        lambda *_args, **_kwargs: {"status": "success", "suite_name": "suite", "case_count": 1, "passed_count": 1, "failed_count": 0, "results": []},
    )
    assert_mcp_tool_audited(workspace, "run_browser_smoke_suite", {"suite_file": "suite.json"})


def test_mcp_save_task_context_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "save_task_context", {"task": "Fix checkout"})


def test_mcp_run_verification_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "run_verification", {})


def test_mcp_generate_workflow_dry_run_returns_valid_yaml(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)

    payload = content_payload(
        asyncio.run(
            call_tool(
                "generate_workflow",
                {
                    "workspace_root": str(workspace.root),
                    "description": "Verify the user can log in and see the dashboard",
                    "dry_run": True,
                },
            )
        )
    )

    assert payload["status"] == "success"
    assert payload["saved_to"] is None
    assert "observe_browser" in payload["yaml"]
    assert "visibility: private" in payload["yaml"]


def test_mcp_save_task_context_updates_session_context(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    saved = content_payload(
        asyncio.run(
            call_tool(
                "save_task_context",
                {
                    "workspace_root": str(workspace.root),
                    "task": "Fix checkout export",
                    "analyzed_files": ["src/checkout.py", "tests/test_checkout.py"],
                    "root_cause": "button handler missing",
                    "plan": "patch handler and rerun verification",
                    "tried": ["ran unit tests"],
                },
            )
        )
    )
    context = content_payload(asyncio.run(call_tool("get_session_context", {"workspace_root": str(workspace.root)})))

    assert saved["status"] == "saved"
    assert "Fix checkout export" in context["snapshot"]
    assert "src/checkout.py" in context["snapshot"]
    assert context["within_budget"] is True
