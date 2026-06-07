from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .auth_state import auth_state_probe_to_markdown, build_auth_state_import_plan, import_auth_state, inspect_storage_state, probe_storage_state
from .capabilities import build_atomic_capability_manifest, build_capability_manifest
from .ci_templates import ci_template_install_to_dict, install_ci_templates
from .console import build_report_detail, build_workspace_dashboard, dashboard_to_markdown, report_detail_to_markdown
from .codex_check import codex_check_to_markdown, run_codex_check
from .connect import connect_platform, connect_result_to_dict
from .dom import DomProvider
from .external_samples import (
    build_external_sample_batch_plan,
    build_external_sample_batch_failure_summary,
    build_external_sample_batch_rerun_plan,
    build_external_sample_run_summary,
    build_external_sample_rerun_plan,
    build_external_sample_run_plan,
    check_external_samples,
    external_samples_readiness,
    export_external_sample_dry_run_report,
    export_external_sample_live_placeholder,
    export_external_sample_batch_report,
    list_external_sample_batch_reports,
    load_external_sample_batch_report_index,
    list_external_samples,
    run_external_sample,
    submit_external_sample_batch,
    submit_external_sample_batch_reruns,
    submit_external_sample_reruns,
)
from .gui import (
    build_gui_action_history_index,
    build_gui_action_history_report,
    build_gui_action_history_risk_summary,
    gui_action_history_index_to_markdown,
    gui_action_history_report_to_markdown,
    gui_action_history_risk_to_markdown,
    open_workspace_window,
)
from .models import Target, to_jsonable
from .model_credentials import (
    build_model_api_probe_plan,
    inspect_model_credentials,
    model_api_probe_result_to_markdown,
    model_api_probe_plan_to_markdown,
    model_credentials_to_markdown,
    run_model_api_probe,
)
from .planner import check_planner_draft
from .planner_generate import (
    generate_planner_draft,
    planner_draft_result_to_markdown,
    preview_planner_draft_save,
    save_planner_draft_result,
)
from .preflight import run_preflight
from .product_issues import build_product_issues, product_issues_to_markdown, write_product_issues
from .quality import (
    build_coding_agent_brief,
    build_install_check_plan,
    build_mcp_client_config,
    build_release_check_plan,
    coding_agent_brief_to_markdown,
    demo_workspace_check_to_markdown,
    install_check_plan_to_markdown,
    list_quality_gate_reports,
    load_quality_gate_index,
    mcp_client_config_to_markdown,
    mcp_smoke_check_to_markdown,
    quality_gate_index_to_markdown,
    quality_gate_reports_to_markdown,
    quality_gate_to_dict,
    release_check_plan_to_markdown,
    run_demo_workspace_check,
    run_mcp_smoke_check,
    run_quality_gate,
)
from .recorder import (
    BrowserRecordingError,
    record_browser_session,
    recorded_result_ok,
    recorded_result_to_dict,
    recorded_result_to_markdown,
    recording_failure_to_markdown,
)
from .reports import (
    list_run_summaries,
    load_run_report,
    load_run_summary,
    run_report_to_dict,
    run_report_to_markdown,
    run_summary_to_dict,
)
from .scheduler import (
    cancel_queue_task,
    list_queue_tasks,
    migrate_queue_to_sqlite,
    rollback_queue_from_sqlite,
    retry_queue_task,
    run_next_queue_task,
    run_queue_worker,
    submit_queue_task,
)
from .runner import VisualAgentRunner
from .selector import SelectorResolver
from .templates import install_template, list_templates
from .uia import UIAutomationProvider
from .validation import validate_workflow_file, validate_workflow_file_strict
from .vlm import detect_cloud_vision_backend, detect_vlm_backend, public_engine_status, vlm_doctor_summary
from .workflow import WorkflowRuntime, parse_workflow_file
from .workspace import (
    discover_workflows,
    build_workspace_risk_policy_template,
    build_workspace_risk_policy_apply_plan,
    export_regression_fixture,
    find_workflow,
    init_workspace,
    list_regression_tests,
    list_workspace_reports,
    load_workspace_report_tags,
    load_workspace_report_index,
    load_workspace_gui_action_history_risk_config,
    load_workspace_inputs,
    open_workspace,
    planner_context,
    promote_regression_fixture,
    run_workspace_regression_tests,
    run_workspace_workflow,
    tag_workspace_report,
    validate_workflow_inputs,
    validate_workspace,
    validate_workspace_risk_policy,
    workspace_run_summaries,
    workspace_status,
)


RUN_PROFILE_CHOICES = ["dry-run", "supervised", "semi-auto", "approved"]
SAFE_RUN_PROFILE_CHOICES = ["dry-run", "supervised", "semi-auto"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="visual-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    click = subparsers.add_parser("click", help="Locate a target on screen and click it.")
    click.add_argument("--target", required=True, help="Visible UI target name, for example 登录.")
    click.add_argument("--provider", default="mock", help="Vision provider name. Default: mock.")
    click.add_argument("--output-dir", default=".runs", help="Directory for screenshots and run artifacts.")
    click.add_argument("--dry-run", action="store_true", help="Locate target without clicking.")
    click.add_argument(
        "--synthetic-on-capture-fail",
        action="store_true",
        help="Use a generated image if desktop screenshot capture is blocked.",
    )

    inspect_dom = subparsers.add_parser("inspect-dom", help="Inspect structured DOM elements from a web page.")
    inspect_dom.add_argument("--url", required=True, help="Page URL to inspect.")
    inspect_dom.add_argument("--headed", action="store_true", help="Show the browser instead of headless mode.")
    inspect_dom.add_argument("--limit", type=int, default=30, help="Maximum elements to print.")

    resolve_dom = subparsers.add_parser("resolve-dom", help="Resolve a target from structured DOM elements.")
    resolve_dom.add_argument("--url", required=True, help="Page URL to inspect.")
    resolve_dom.add_argument("--target", required=True, help="Target text, label, or accessible name.")
    resolve_dom.add_argument("--role", help="Expected DOM role, for example button or link.")
    resolve_dom.add_argument("--headed", action="store_true", help="Show the browser instead of headless mode.")

    browser_smoke = subparsers.add_parser("browser-smoke", help="Run a real browser smoke check against a URL.")
    browser_smoke.add_argument("--url", required=True, help="Page URL to open.")
    browser_smoke.add_argument("--output-dir", default=".runs", help="Directory for smoke screenshots and artifacts.")
    browser_smoke.add_argument("--headed", action="store_true", help="Show the browser instead of headless mode.")
    browser_smoke.add_argument("--timeout-ms", type=int, default=10_000)
    browser_smoke.add_argument("--wait-until", default="domcontentloaded", choices=["domcontentloaded", "load", "networkidle"])
    browser_smoke.add_argument("--min-text-length", type=int, default=1)
    browser_smoke.add_argument("--min-interactive", type=int, default=0)
    browser_smoke.add_argument("--expect-text", action="append", default=[], help="Required visible text. Can be repeated.")
    browser_smoke.add_argument("--expect-url-contains", action="append", default=[], help="Required initial URL fragment. Can be repeated.")
    browser_smoke.add_argument("--fill", action="append", default=[], help="Fill input by semantic label, formatted as label=value. Can be repeated.")
    browser_smoke.add_argument("--fill-selector", action="append", default=[], help="Fill input by CSS selector, formatted as selector=value. Can be repeated.")
    browser_smoke.add_argument("--click-text", default=None, help="Visible button/link text to click once.")
    browser_smoke.add_argument("--click-selector", default=None, help="CSS selector to click once.")
    browser_smoke.add_argument("--require-change-after-click", action="store_true", help="Fail if click does not change URL, visible text, or interactive element count.")
    browser_smoke.add_argument("--wait-for-text-after", action="append", default=[], help="Text to wait for after click before final screenshot. Can be repeated.")
    browser_smoke.add_argument("--wait-for-url-contains-after", action="append", default=[], help="URL fragment to wait for after click before final screenshot. Can be repeated.")
    browser_smoke.add_argument("--wait-timeout-seconds", type=float, default=5.0)
    browser_smoke.add_argument("--expect-text-after", action="append", default=[], help="Required visible text after click. Can be repeated.")
    browser_smoke.add_argument("--expect-url-contains-after", action="append", default=[], help="Required URL fragment after click. Can be repeated.")
    browser_smoke.add_argument("--wait-after-seconds", type=float, default=0.5)
    browser_smoke.add_argument("--save-workflow", help="Save a reusable workflow YAML generated from this smoke configuration.")
    browser_smoke.add_argument("--overwrite-workflow", action="store_true", help="Overwrite --save-workflow if it already exists.")
    browser_smoke.add_argument("--format", choices=["json", "markdown"], default="markdown")

    browser_smoke_suite = subparsers.add_parser("browser-smoke-suite", help="Run a browser smoke suite from a JSON/YAML file.")
    browser_smoke_suite.add_argument("--file", required=True, help="Suite JSON/YAML path.")
    browser_smoke_suite.add_argument("--output-dir", default=".runs", help="Directory for suite reports and case artifacts.")
    browser_smoke_suite.add_argument("--headed", action="store_true", help="Force headed browser mode for all cases.")
    browser_smoke_suite.add_argument("--format", choices=["json", "markdown"], default="markdown")

    inspect_uia = subparsers.add_parser("inspect-uia", help="Inspect structured Windows UI Automation controls.")
    inspect_uia.add_argument("--max-depth", type=int, default=4, help="Maximum UIA tree depth to inspect.")
    inspect_uia.add_argument("--limit", type=int, default=50, help="Maximum controls to print.")

    resolve_uia = subparsers.add_parser("resolve-uia", help="Resolve a target from Windows UI Automation controls.")
    resolve_uia.add_argument("--target", required=True, help="Target text, control name, or automation id.")
    resolve_uia.add_argument("--role", help="Expected control type, for example button or edit.")
    resolve_uia.add_argument("--max-depth", type=int, default=4, help="Maximum UIA tree depth to inspect.")

    run_workflow = subparsers.add_parser("run-workflow", help="Run an audited workflow file.")
    run_workflow.add_argument("--file", required=True, help="Workflow YAML or JSON file.")
    run_workflow.add_argument("--output-dir", default=".runs", help="Directory for workflow run artifacts.")
    run_workflow.add_argument("--inputs", help="Workflow input JSON string.")
    run_workflow.add_argument("--inputs-file", help="Workflow input JSON file.")
    run_workflow.add_argument("--sensitive-fields", help="Comma-separated input paths to hash in audit logs.")
    run_workflow.add_argument("--resume-from", help="Existing run directory to resume from checkpoint.")
    run_workflow.add_argument("--allow-click", action="store_true", help="Allow real click actions. Default is dry-run.")
    run_workflow.add_argument(
        "--run-profile",
        choices=RUN_PROFILE_CHOICES,
        default="dry-run",
        help="Execution permission profile. Default is dry-run.",
    )
    run_workflow.add_argument("--skip-preflight", action="store_true", help="Skip runtime preflight checks.")
    run_workflow.add_argument("--strict-preflight", action="store_true", help="Apply strict validation during preflight.")
    run_workflow.add_argument("--allow-high-risk", action="store_true", help="Allow high-risk actions during strict preflight.")
    run_workflow.add_argument("--no-lock", action="store_true", help="Disable run lock for controlled debugging.")
    run_workflow.add_argument("--lock-ttl-seconds", type=float, default=3600.0, help="Run lock TTL. Default: 3600.")
    run_workflow.add_argument("--wait-lock", action="store_true", help="Wait for the run lock instead of failing immediately.")
    run_workflow.add_argument("--queue-when-locked", action="store_true", help="Wait for the run lock instead of failing immediately.")
    run_workflow.add_argument("--lock-wait-seconds", type=float, default=30.0, help="Maximum seconds to wait when queued. Default: 30.")
    run_workflow.add_argument("--lock-poll-seconds", type=float, default=0.5, help="Seconds between lock checks when queued. Default: 0.5.")
    run_workflow.add_argument(
        "--synthetic-on-capture-fail",
        action="store_true",
        help="Use a generated image if desktop screenshot capture is blocked.",
    )

    preflight = subparsers.add_parser("preflight-workflow", help="Run validation and capability checks without executing.")
    preflight.add_argument("--file", required=True, help="Workflow YAML or JSON file.")
    preflight.add_argument("--strict", action="store_true", help="Apply production-oriented validation rules.")
    preflight.add_argument("--allow-high-risk", action="store_true", help="Allow high-risk actions in strict preflight.")

    validate = subparsers.add_parser("validate-workflow", help="Validate a workflow file without running it.")
    validate.add_argument("--file", required=True, help="Workflow YAML or JSON file.")
    validate.add_argument("--strict", action="store_true", help="Apply production-oriented validation rules.")
    validate.add_argument("--allow-high-risk", action="store_true", help="Allow high-risk actions in strict validation.")

    list_runs = subparsers.add_parser("list-runs", help="List audited workflow runs.")
    list_runs.add_argument("--output-dir", default=".runs", help="Directory containing workflow run artifacts.")
    list_runs.add_argument("--limit", type=int, default=20, help="Maximum runs to list.")

    show_run = subparsers.add_parser("show-run", help="Show one audited workflow run summary.")
    show_run.add_argument("--run-dir", required=True, help="Run directory containing workflow_result.json.")

    report_run = subparsers.add_parser("report-run", help="Show a detailed audited run report.")
    report_run.add_argument("--run-dir", required=True, help="Run directory containing workflow_result.json.")
    report_run.add_argument("--format", choices=["json", "markdown"], default="json")

    subparsers.add_parser("capabilities", help="List framework capabilities and dependency status.")
    subparsers.add_parser("atomic-capabilities", help="List planner-visible atomic capabilities.")
    doctor = subparsers.add_parser("doctor", help="Check missing capabilities.")
    doctor.add_argument("--strict", action="store_true", help="Treat missing optional capabilities as failures.")

    quality_gate = subparsers.add_parser("quality-gate", help="Show or run local/CI quality gates.")
    quality_gate.add_argument("--profile", choices=["local", "ci"], default="local", help="Quality profile. Default: local.")
    quality_gate.add_argument("--workspace-root", help="Optional workspace root for workspace regression tests.")
    quality_gate.add_argument("--run", action="store_true", help="Execute the quality gate. Default only prints the plan.")
    quality_gate.add_argument("--timeout-seconds", type=float, default=300.0, help="Timeout per step. Default: 300.")
    quality_gate.add_argument("--report-root", help="Optional report output directory.")
    quality_gate.add_argument(
        "--fail-on-risk-policy-error",
        action="store_true",
        help="Fail executed gates when workspace risk policy validation has errors.",
    )
    quality_gate.add_argument(
        "--fail-on-secret-leak",
        action="store_true",
        help="Fail gates when reports/runs/artifacts contain possible secret leaks.",
    )

    quality_reports = subparsers.add_parser("quality-gate-reports", help="List quality gate JSON reports.")
    quality_reports.add_argument("--workspace-root", help="Optional workspace root containing reports/quality_gates.")
    quality_reports.add_argument("--report-root", help="Optional quality gate report directory.")
    quality_reports.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")
    quality_reports.add_argument("--profile", choices=["local", "ci"], help="Filter by quality gate profile.")
    quality_reports.add_argument("--status", choices=["planned", "success", "failed"], help="Filter by report status.")
    quality_reports.add_argument(
        "--strict-policy-failed",
        choices=["true", "false"],
        help="Filter by strict policy gate failure state.",
    )

    quality_index = subparsers.add_parser("quality-gate-index", help="Build or query the quality gate report index.")
    quality_index.add_argument("--workspace-root", help="Optional workspace root containing reports/quality_gates.")
    quality_index.add_argument("--report-root", help="Optional quality gate report directory.")
    quality_index.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")
    quality_index.add_argument("--rebuild", action="store_true", help="Rebuild index.json before reading.")
    quality_index.add_argument("--profile", choices=["local", "ci"], help="Filter by quality gate profile.")
    quality_index.add_argument("--status", choices=["planned", "success", "failed"], help="Filter by report status.")
    quality_index.add_argument(
        "--strict-policy-failed",
        choices=["true", "false"],
        help="Filter by strict policy gate failure state.",
    )

    release_check = subparsers.add_parser("release-check", help="Print the release readiness check plan.")
    release_check.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root to use in generated commands.")
    release_check.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    install_check = subparsers.add_parser("install-check", help="Print the local install/dependency check plan.")
    install_check.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    mcp_client_config = subparsers.add_parser("mcp-client-config", help="Generate MCP client configuration for this checkout.")
    mcp_client_config.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root passed to the MCP server.")
    mcp_client_config.add_argument("--client", choices=["cursor", "claude-desktop", "vscode"], default="cursor", help="Client config shape to generate.")
    mcp_client_config.add_argument("--python", default=".\\.venv\\Scripts\\python.exe", help="Python executable used by the MCP client.")
    mcp_client_config.add_argument("--repo-root", default=".", help="Repository root used for cwd and PYTHONPATH.")
    mcp_client_config.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    coding_agent_brief = subparsers.add_parser("coding-agent-brief", help="Generate a Codex/Claude Code/Cursor/VS Code onboarding brief.")
    coding_agent_brief.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root passed to the MCP server.")
    coding_agent_brief.add_argument("--repo-root", default=".", help="Repository root used for cwd and PYTHONPATH.")
    coding_agent_brief.add_argument("--client", choices=["codex", "claude-code", "cursor", "vscode"], default="codex", help="Coding agent target.")
    coding_agent_brief.add_argument("--python", default=".\\.venv\\Scripts\\python.exe", help="Python executable used by the MCP client.")
    coding_agent_brief.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    mcp_smoke = subparsers.add_parser("mcp-smoke", help="Run local MCP tool smoke checks through the in-process MCP adapter.")
    mcp_smoke.add_argument("--workspace-root", required=True, help="Workspace root containing demo workflows.")
    mcp_smoke.add_argument("--workflow", default="local_html_form_workflow", help="Workflow to validate and run through MCP.")
    mcp_smoke.add_argument("--inputs-file", default="demo_login.json", help="Workspace inputs file used for the dry-run.")
    mcp_smoke.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    demo_workspace_check = subparsers.add_parser("demo-workspace-check", help="Initialize and dry-run the local demo workspace.")
    demo_workspace_check.add_argument("--root", default=".agent-workspace", help="Workspace root to initialize/check.")
    demo_workspace_check.add_argument("--overwrite", action="store_true", help="Overwrite demo assets before checking.")
    demo_workspace_check.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    context_snapshot = subparsers.add_parser("context-snapshot", help="Print compact AI context for the workspace.")
    context_snapshot.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing agent_session.json.")
    context_snapshot.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    usage_status = subparsers.add_parser("usage-status", help="Show local usage counters and license feature boundaries.")
    usage_status.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing agent_session.json.")
    usage_status.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    cloud_run_plan = subparsers.add_parser("cloud-run-plan", help="Preview a cloud workflow request without sending network traffic.")
    cloud_run_plan.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root.")
    cloud_run_plan.add_argument("--workflow", required=True, help="Workflow name to run remotely in the future.")
    cloud_run_plan.add_argument("--run-profile", choices=RUN_PROFILE_CHOICES, default="dry-run")
    cloud_run_plan.add_argument("--inputs-file", default=None, help="Optional inputs file name to reference without reading its contents.")
    cloud_run_plan.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    cloud_run = subparsers.add_parser("cloud-run", help="Plan a cloud workflow run; use --execute to request execution.")
    cloud_run.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root.")
    cloud_run.add_argument("--workflow", required=True, help="Workflow name to run remotely.")
    cloud_run.add_argument("--run-profile", choices=RUN_PROFILE_CHOICES, default="dry-run")
    cloud_run.add_argument("--inputs-file", default=None, help="Optional inputs file name to reference without reading its contents.")
    cloud_run.add_argument("--execute", action="store_true", help="Explicitly request remote execution.")
    cloud_run.add_argument("--transport", choices=["none", "http"], default="none", help="Remote transport. Default: none.")
    cloud_run.add_argument("--timeout-seconds", type=float, default=30.0, help="HTTP transport timeout when --transport http is used.")
    cloud_run.add_argument("--max-retries", type=int, default=0, help="Retry count for retryable HTTP transport responses. Default: 0.")
    cloud_run.add_argument("--retry-backoff-seconds", type=float, default=0.0, help="Initial retry backoff for HTTP transport. Default: 0.")
    cloud_run.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    save_task = subparsers.add_parser("save-task-context", help="Save AI task state before switching windows.")
    save_task.add_argument("--task", required=True, help="Current task description.")
    save_task.add_argument("--files", nargs="*", default=[], help="Files already analyzed.")
    save_task.add_argument("--root-cause", default="", help="Current root-cause hypothesis.")
    save_task.add_argument("--plan", default="", help="Next-step plan.")
    save_task.add_argument("--tried", nargs="*", default=[], help="Approaches already tried.")
    save_task.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing agent_session.json.")
    save_task.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    summarize_failure = subparsers.add_parser("summarize-latest-failure", help="Print a compact latest-failure summary.")
    summarize_failure.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing agent_session.json.")
    summarize_failure.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    diagnose_failure = subparsers.add_parser("diagnose-latest-failure", help="Build an AI-readable evidence pack for the latest workflow failure.")
    diagnose_failure.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflow reports.")
    diagnose_failure.add_argument("--run-id", default=None, help="Specific run id to diagnose. Default: latest failed run.")
    diagnose_failure.add_argument("--max-chars", type=int, default=12000, help="Maximum JSON evidence budget. Default: 12000.")
    diagnose_failure.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    repair_workflow = subparsers.add_parser("repair-workflow", help="Suggest a safe workflow or app repair from failure evidence.")
    repair_workflow.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflow reports.")
    repair_workflow.add_argument("--run-id", default=None, help="Specific run id to repair. Default: latest failed run.")
    repair_workflow.add_argument("--provider", choices=["none", "anthropic", "openai"], default="none", help="Model provider. Default: none.")
    repair_workflow.add_argument("--model", default=None, help="Model name for provider-backed repair.")
    repair_workflow.add_argument("--max-chars", type=int, default=12000, help="Maximum JSON evidence budget. Default: 12000.")
    repair_workflow.add_argument("--apply", action="store_true", help="Apply a high-confidence deterministic workflow patch and create a backup.")
    repair_workflow.add_argument("--min-confidence", type=float, default=0.75, help="Minimum confidence required for --apply. Default: 0.75.")
    repair_workflow.add_argument("--verify", action="store_true", help="Rerun the repaired workflow after --apply. Default run profile: dry-run.")
    repair_workflow.add_argument("--verify-run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    repair_workflow.add_argument("--inputs-file", default=None, help="Optional workspace inputs file for verification rerun.")
    repair_workflow.add_argument("--rollback-on-fail", action="store_true", help="Restore the workflow backup when --verify fails.")
    repair_workflow.add_argument("--candidate-id", default=None, help="Repair candidate id to apply. Default: deterministic workflow patch when available.")
    repair_workflow.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    auto_repair = subparsers.add_parser("auto-repair", help="Diagnose, apply a safe deterministic repair, verify, and rollback on failure.")
    auto_repair.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflow reports.")
    auto_repair.add_argument("--run-id", default=None, help="Specific run id to repair. Default: latest failed run.")
    auto_repair.add_argument("--max-chars", type=int, default=12000, help="Maximum JSON evidence budget. Default: 12000.")
    auto_repair.add_argument("--min-confidence", type=float, default=0.75, help="Minimum confidence required for auto apply. Default: 0.75.")
    auto_repair.add_argument("--verify-run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    auto_repair.add_argument("--inputs-file", default=None, help="Optional workspace inputs file for verification rerun.")
    auto_repair.add_argument("--candidate-id", default=None, help="Repair candidate id to apply. Default: deterministic workflow patch when available.")
    auto_repair.add_argument("--dry-run", action="store_true", help="Preview the selected repair candidate without applying or verifying.")
    auto_repair.add_argument("--force", action="store_true", help="Apply even when repair health is high risk.")
    auto_repair.add_argument("--promote-regression", action="store_true", help="After verified auto repair, export and promote the failed run as a regression test.")
    auto_repair.add_argument("--overwrite-regression", action="store_true", help="Overwrite existing regression export/test when promoting.")
    auto_repair.add_argument("--run-regression", action="store_true", help="Run workspace regression tests after promotion.")
    auto_repair.add_argument("--regression-timeout-seconds", type=float, default=120.0, help="Timeout for --run-regression. Default: 120.")
    auto_repair.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    repair_history = subparsers.add_parser("repair-history", help="List recent workflow repair attempts.")
    repair_history.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing repair_history.jsonl.")
    repair_history.add_argument("--limit", type=int, default=20, help="Maximum entries to return. Default: 20.")
    repair_history.add_argument("--workflow", default=None, help="Filter by workflow name.")
    repair_history.add_argument("--status", default=None, help="Filter by repair status.")
    repair_history.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    repair_health = subparsers.add_parser("repair-health", help="Summarize repair reliability and rollback risk.")
    repair_health.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing repair_history.jsonl.")
    repair_health.add_argument("--limit", type=int, default=50, help="Maximum entries to analyze. Default: 50.")
    repair_health.add_argument("--workflow", default=None, help="Filter by workflow name.")
    repair_health.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    repair_rollback = subparsers.add_parser("repair-rollback", help="Rollback a workflow from a recorded repair backup.")
    repair_rollback.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing repair_history.jsonl.")
    repair_rollback.add_argument("--history-id", default=None, help="Specific repair history id. Default: latest repair with backup.")
    repair_rollback.add_argument("--workflow", default=None, help="Filter rollback candidate by workflow name.")
    repair_rollback.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    benchmarks = subparsers.add_parser("benchmarks", help="List public reference benchmarks for real-world Visual Agent testing.")
    benchmarks.add_argument("--category", default=None, help="Optional benchmark category filter.")
    benchmarks.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    benchmark_plan = subparsers.add_parser("benchmark-plan", help="Create an executable Visual Agent benchmark coverage plan.")
    benchmark_plan.add_argument("--category", default=None, help="Optional benchmark category filter.")
    benchmark_plan.add_argument("--benchmark-id", default=None, help="Optional benchmark id filter.")
    benchmark_plan.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    benchmark_draft = subparsers.add_parser("benchmark-draft", help="Generate a local workflow YAML draft for one benchmark scenario.")
    benchmark_draft.add_argument("--scenario-id", required=True, help="Scenario id or workflow name from benchmark-plan.")
    benchmark_draft.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root used for saving drafts.")
    benchmark_draft.add_argument("--output", default=None, help="Optional workspace-relative output path.")
    benchmark_draft.add_argument("--save", action="store_true", help="Write the workflow draft to the workspace.")
    benchmark_draft.add_argument("--overwrite", action="store_true", help="Overwrite existing workflow draft.")
    benchmark_draft.add_argument("--format", choices=["json", "markdown", "yaml"], default="markdown", help="Output format. Default: markdown.")

    verify = subparsers.add_parser("verify", help="Run verification-tagged workspace workflows.")
    verify.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflows.")
    verify.add_argument("--tags", default="verification", help="Comma-separated workflow tags to run. Default: verification.")
    verify.add_argument("--workflow", action="append", default=[], help="Workflow name or workspace-relative path to verify. Can be used multiple times.")
    verify.add_argument("--max-workflows", type=int, default=10, help="Maximum matching workflows to run. Default: 10.")
    verify.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    verify.add_argument("--wait-lock", action="store_true", help="Wait for workflow locks instead of failing immediately.")
    verify.add_argument("--lock-wait-seconds", type=float, default=30.0, help="Maximum seconds to wait when queued. Default: 30.")
    verify.add_argument("--include-slow", action="store_true", help="Include workflows tagged 'slow'. Default: skipped.")
    verify.add_argument("--for", dest="target_agent", default="codex", help="Target coding agent label.")
    verify.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    gen_workflow = subparsers.add_parser("generate-workflow", help="Generate a workflow YAML from a natural language description.")
    gen_workflow.add_argument("--description", required=True, help="Natural language description of the workflow.")
    gen_workflow.add_argument("--output", default=None, help="Output YAML file path. Default: auto-named in workflows/.")
    gen_workflow.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root used to place generated workflows.")
    gen_workflow.add_argument("--model", default="claude-haiku-4-5-20251001", help="LLM model to use for generation.")
    gen_workflow.add_argument("--dry-run", action="store_true", help="Print generated YAML without saving.")
    gen_workflow.add_argument("--format", choices=["json", "yaml"], default="json", help="Output format. Default: json.")

    gen_from_diff = subparsers.add_parser("generate-from-diff", help="Generate a verification workflow from git diff context.")
    gen_from_diff.add_argument("--task-description", required=True, help="Task or feature that the code changes implement.")
    gen_from_diff.add_argument("--base-url", required=True, help="URL or local fixture path used as workflow entry point.")
    gen_from_diff.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root used to place generated workflows.")
    gen_from_diff.add_argument("--repo-root", default=".", help="Git repository root. Default: current directory.")
    gen_from_diff.add_argument("--base", default="HEAD", help="Git base ref for diff. Default: HEAD.")
    gen_from_diff.add_argument("--framework-hint", default=None, help="Optional parser hint: html, react, vue, django, fastapi, flask.")
    gen_from_diff.add_argument("--model", default="claude-haiku-4-5-20251001", help="LLM model used when static confidence is low.")
    gen_from_diff.add_argument("--no-untracked", action="store_true", help="Do not include untracked git files.")
    gen_from_diff.add_argument("--dry-run", action="store_true", help="Print generated YAML without saving.")
    gen_from_diff.add_argument("--format", choices=["json", "markdown", "yaml"], default="json", help="Output format. Default: json.")

    verify_impl = subparsers.add_parser("verify-impl", help="Generate a workflow from git diff context and run it.")
    verify_impl.add_argument("--task-description", required=True, help="Task or feature that the code changes implement.")
    verify_impl.add_argument("--base-url", required=True, help="URL or local fixture path used as workflow entry point.")
    verify_impl.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflows.")
    verify_impl.add_argument("--repo-root", default=".", help="Git repository root. Default: current directory.")
    verify_impl.add_argument("--base", default="HEAD", help="Git base ref for diff. Default: HEAD.")
    verify_impl.add_argument("--framework-hint", default=None, help="Optional parser hint: html, react, vue, django, fastapi, flask.")
    verify_impl.add_argument("--model", default="claude-haiku-4-5-20251001", help="LLM model used when static confidence is low.")
    verify_impl.add_argument("--inputs-file", default=None, help="Workspace inputs JSON file for generated workflow values.")
    verify_impl.add_argument("--run-profile", choices=RUN_PROFILE_CHOICES, default="supervised")
    verify_impl.add_argument("--min-quality-score", type=float, default=0.6, help="Minimum generated workflow quality before running. Default: 0.6.")
    verify_impl.add_argument("--timeout-seconds", type=float, default=30.0, help="Maximum seconds to wait for the generated workflow run. Default: 30.")
    verify_impl.add_argument("--run-negative", action="store_true", help="Also run the generated negative workflow draft after the success-path workflow passes.")
    verify_impl.add_argument("--no-untracked", action="store_true", help="Do not include untracked git files.")
    verify_impl.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    agent_status = subparsers.add_parser("agent-status", help="Read .vscode-agent-status.json for AI/VS Code verification status.")
    agent_status.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing .vscode-agent-status.json.")
    agent_status.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    share_workflow = subparsers.add_parser("share-workflow", help="Share a workflow to the Visual Agent marketplace (coming soon).")
    share_workflow.add_argument("--name", required=True, help="Workflow name to share.")
    share_workflow.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflows.")
    share_workflow.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    codex_check = subparsers.add_parser("codex-check", help="Smart check for Codex/Claude Code: git-diff-aware, fast by default.")
    codex_check.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflows.")
    codex_check.add_argument("--repo-root", default=".", help="Git repository root. Default: current directory.")
    codex_check.add_argument("--base", default="HEAD", help="Git base ref for diff. Default: HEAD.")
    codex_check.add_argument("--tags", default="verification", help="Comma-separated workflow tags to run. Default: verification.")
    codex_check.add_argument("--max-workflows", type=int, default=10, help="Maximum workflows to run. Default: 10.")
    codex_check.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    codex_check.add_argument("--include-slow", action="store_true", help="Include workflows tagged 'slow'. Default: skipped.")
    codex_check.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    connect = subparsers.add_parser("connect", help="Connect Visual Agent to an AI coding platform.")
    connect.add_argument("platform", choices=["claude-code", "cursor", "codex"])
    connect.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root to initialize/connect.")
    connect.add_argument("--repo-root", default=".", help="Repository root. Default: current directory.")
    connect.add_argument("--python", default="python", help="Python executable used by MCP clients. Default: python.")
    connect.add_argument("--global", dest="global_config", action="store_true", help="Write user-level config where supported.")
    connect.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    ci_templates = subparsers.add_parser("install-ci-templates", help="Install CI/local quality gate templates.")
    ci_templates.add_argument("--root", default=".", help="Repository root to receive generated templates. Default: current directory.")
    ci_templates.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root used by generated quality gates.")
    ci_templates.add_argument("--overwrite", action="store_true", help="Overwrite existing CI template files.")

    external_samples = subparsers.add_parser("external-samples", help="List external business backend samples.")
    external_samples.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")

    external_samples_check = subparsers.add_parser("external-samples-check", help="Validate external business backend sample safety.")
    external_samples_check.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")

    external_samples_ready = subparsers.add_parser("external-samples-readiness", help="Show external sample account readiness requirements.")
    external_samples_ready.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")
    external_samples_ready.add_argument("--workspace-root", default=".", help="Workspace/project root for storage_state readiness checks.")
    external_samples_ready.add_argument("--require-live-auth", action="store_true", help="Block samples whose storage_state has no matching live session metadata.")

    external_sample_plan = subparsers.add_parser("external-sample-run-plan", help="Plan a protected external sample run.")
    external_sample_plan.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")
    external_sample_plan.add_argument("--workspace-root", default=".", help="Workspace/project root for storage_state readiness checks.")
    external_sample_plan.add_argument("--sample-id", required=True, help="External sample id from the catalog.")
    external_sample_plan.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    external_sample_plan.add_argument("--require-live-auth", action="store_true", help="Require matching non-empty storage_state metadata before planning ready.")

    external_sample_run = subparsers.add_parser("external-sample-run", help="Run a ready external sample through workspace safety gates.")
    external_sample_run.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")
    external_sample_run.add_argument("--workspace-root", required=True, help="Workspace root for the run.")
    external_sample_run.add_argument("--sample-id", required=True, help="External sample id from the catalog.")
    external_sample_run.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    external_sample_run.add_argument("--require-live-auth", action="store_true", help="Require matching non-empty storage_state metadata before running.")
    external_sample_run.add_argument("--skip-preflight", action="store_true", help="Skip runtime preflight checks after readiness gates.")

    external_batch_plan = subparsers.add_parser("external-sample-batch-plan", help="Plan protected runs for all external samples.")
    external_batch_plan.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")
    external_batch_plan.add_argument("--workspace-root", default=".", help="Workspace/project root for storage_state readiness checks.")
    external_batch_plan.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    external_batch_plan.add_argument("--ready-only", action="store_true", help="Only include ready samples in the plan.")
    external_batch_plan.add_argument("--require-live-auth", action="store_true", help="Require matching non-empty storage_state metadata for ready samples.")

    external_batch_submit = subparsers.add_parser("external-sample-batch-submit", help="Submit ready external samples to the workspace queue.")
    external_batch_submit.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")
    external_batch_submit.add_argument("--workspace-root", required=True, help="Workspace root for queue submission.")
    external_batch_submit.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    external_batch_submit.add_argument("--priority", type=int, default=0)
    external_batch_submit.add_argument("--max-retries", type=int, default=0)
    external_batch_submit.add_argument("--ready-only", action="store_true", help="Only include ready samples in the batch result.")
    external_batch_submit.add_argument("--require-live-auth", action="store_true", help="Require matching non-empty storage_state metadata before queue submission.")

    external_summary = subparsers.add_parser("external-sample-summary", help="Summarize external sample readiness, queue, and reports.")
    external_summary.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")
    external_summary.add_argument("--workspace-root", required=True, help="Workspace root for summary reads.")

    external_batch_report = subparsers.add_parser(
        "external-sample-batch-report",
        help="Export external sample readiness, queue, and report summary as JSON and Markdown.",
    )
    external_batch_report.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")
    external_batch_report.add_argument("--workspace-root", required=True, help="Workspace root for report export.")

    external_dry_run_report = subparsers.add_parser(
        "external-sample-dry-run-report",
        help="Run all ready external samples in dry-run and export readiness plus run report artifacts.",
    )
    external_dry_run_report.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")
    external_dry_run_report.add_argument("--workspace-root", required=True, help="Workspace root for report export.")
    external_dry_run_report.add_argument("--require-live-auth", action="store_true", help="Require matching non-empty storage_state metadata before running.")
    external_dry_run_report.add_argument("--skip-preflight", action="store_true", help="Skip runtime preflight checks after readiness gates.")

    external_live_placeholder = subparsers.add_parser(
        "external-sample-live-placeholder",
        help="Export a skipped live-account coordination report listing required accounts and permissions.",
    )
    external_live_placeholder.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")
    external_live_placeholder.add_argument("--workspace-root", required=True, help="Workspace root for report export.")
    external_live_placeholder.add_argument("--no-require-live-auth", action="store_true", help="Do not require live auth metadata in the placeholder readiness snapshot.")

    external_batch_reports = subparsers.add_parser("external-sample-batch-reports", help="List exported external sample batch reports.")
    external_batch_reports.add_argument("--workspace-root", required=True, help="Workspace root for report reads.")
    external_batch_reports.add_argument("--status", help="Filter by batch status.")
    external_batch_reports.add_argument("--sample-id", help="Filter reports containing a sample id.")

    external_batch_report_index = subparsers.add_parser(
        "external-sample-batch-report-index",
        help="Build or query the external sample batch report index.",
    )
    external_batch_report_index.add_argument("--workspace-root", required=True, help="Workspace root for report reads.")
    external_batch_report_index.add_argument("--rebuild", action="store_true", help="Rebuild index.json before reading.")
    external_batch_report_index.add_argument("--status", help="Filter by batch status.")
    external_batch_report_index.add_argument("--sample-id", help="Filter reports containing a sample id.")

    external_batch_failures = subparsers.add_parser(
        "external-sample-batch-failures",
        help="Summarize failed samples in one external sample batch report.",
    )
    external_batch_failures.add_argument("--workspace-root", required=True, help="Workspace root for report reads.")
    external_batch_failures.add_argument("--report-id", required=True, help="External sample batch report id.")

    external_batch_rerun_plan = subparsers.add_parser(
        "external-sample-batch-rerun-plan",
        help="Plan reruns for failed ready samples from one batch report.",
    )
    external_batch_rerun_plan.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")
    external_batch_rerun_plan.add_argument("--workspace-root", required=True, help="Workspace root for summary reads.")
    external_batch_rerun_plan.add_argument("--report-id", required=True, help="External sample batch report id.")
    external_batch_rerun_plan.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")

    external_batch_rerun_submit = subparsers.add_parser(
        "external-sample-batch-rerun-submit",
        help="Submit reruns for failed ready samples from one batch report.",
    )
    external_batch_rerun_submit.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")
    external_batch_rerun_submit.add_argument("--workspace-root", required=True, help="Workspace root for queue submission.")
    external_batch_rerun_submit.add_argument("--report-id", required=True, help="External sample batch report id.")
    external_batch_rerun_submit.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    external_batch_rerun_submit.add_argument("--priority", type=int, default=0)
    external_batch_rerun_submit.add_argument("--max-retries", type=int, default=0)

    external_rerun_plan = subparsers.add_parser("external-sample-rerun-plan", help="Plan reruns for failed ready external samples.")
    external_rerun_plan.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")
    external_rerun_plan.add_argument("--workspace-root", required=True, help="Workspace root for summary reads.")
    external_rerun_plan.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")

    external_rerun_submit = subparsers.add_parser("external-sample-rerun-submit", help="Submit failed ready external samples to the queue.")
    external_rerun_submit.add_argument("--root", default="examples/external_samples", help="External sample catalog root.")
    external_rerun_submit.add_argument("--workspace-root", required=True, help="Workspace root for queue submission.")
    external_rerun_submit.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run")
    external_rerun_submit.add_argument("--priority", type=int, default=0)
    external_rerun_submit.add_argument("--max-retries", type=int, default=0)

    auth_plan = subparsers.add_parser("auth-state-plan", help="Plan a redacted storage_state import.")
    auth_plan.add_argument("--source", required=True, help="Existing Playwright storage_state JSON.")
    auth_plan.add_argument("--name", required=True, help="Safe account alias for .agent-auth/<name>.json.")
    auth_plan.add_argument("--workspace-root", default=".", help="Project/workspace root. Default: current directory.")
    auth_plan.add_argument("--overwrite", action="store_true", help="Allow replacing an existing auth state.")

    auth_import = subparsers.add_parser("auth-state-import", help="Import Playwright storage_state JSON into .agent-auth.")
    auth_import.add_argument("--source", required=True, help="Existing Playwright storage_state JSON.")
    auth_import.add_argument("--name", required=True, help="Safe account alias for .agent-auth/<name>.json.")
    auth_import.add_argument("--workspace-root", default=".", help="Project/workspace root. Default: current directory.")
    auth_import.add_argument("--overwrite", action="store_true", help="Replace an existing auth state.")

    auth_inspect = subparsers.add_parser("auth-state-inspect", help="Inspect storage_state metadata without printing secrets.")
    auth_inspect.add_argument("--path", required=True, help="Storage state JSON path.")

    auth_probe = subparsers.add_parser("auth-state-probe", help="Load storage_state in a browser context and verify domain/session readiness without printing secrets.")
    auth_probe.add_argument("--path", required=True, help="Storage state JSON path.")
    auth_probe.add_argument("--url", required=True, help="HTTPS URL whose domain should match the storage_state.")
    auth_probe.add_argument("--allowed-domain", help="Expected allowed domain. Defaults to the URL host.")
    auth_probe.add_argument("--headed", action="store_true", help="Run browser headed.")
    auth_probe.add_argument("--timeout-ms", type=int, default=10_000)
    auth_probe.add_argument("--format", choices=["json", "markdown"], default="json")

    model_creds = subparsers.add_parser("model-credentials-inspect", help="Inspect local model API credentials without printing secrets.")
    model_creds.add_argument("--source", default=None, help="Credential file path. Defaults to VISUAL_AGENT_MODEL_CREDENTIAL_FILE or model_api_keys.txt.")
    model_creds.add_argument("--preferred", default=None, help="Preferred provider. Defaults to VISUAL_AGENT_MODEL_PROVIDER or openai.")
    model_creds.add_argument("--format", choices=["json", "markdown"], default="json")

    model_probe = subparsers.add_parser("model-api-probe-plan", help="Plan a redacted model API readiness probe without sending secrets.")
    model_probe.add_argument("--source", default=None, help="Credential file path. Defaults to VISUAL_AGENT_MODEL_CREDENTIAL_FILE or model_api_keys.txt.")
    model_probe.add_argument("--preferred", default=None, help="Preferred provider. Defaults to VISUAL_AGENT_MODEL_PROVIDER or openai.")
    model_probe.add_argument("--base-url", default=None, help="Provider base URL for a future read-only probe.")
    model_probe.add_argument("--endpoint", default=None, help="Read-only endpoint path for a future probe.")
    model_probe.add_argument("--model", default=None, help="Optional model name for the future probe.")
    model_probe.add_argument("--run", action="store_true", help="Execute one low-cost probe request. Without this flag, only a plan is generated.")
    model_probe.add_argument("--timeout-seconds", type=float, default=15.0)
    model_probe.add_argument("--max-completion-tokens", type=int, default=64)
    model_probe.add_argument("--format", choices=["json", "markdown"], default="json")

    init_ws = subparsers.add_parser("init-workspace", help="Initialize a visual-agent workspace.")
    init_ws.add_argument("--root", required=True, help="Workspace root directory.")
    init_ws.add_argument("--no-demo", action="store_true", help="Do not copy demo workflow/assets.")
    init_ws.add_argument("--overwrite", action="store_true", help="Overwrite demo files if they already exist.")
    init_ws.add_argument("--auto-detect", action="store_true", help="Scan a project root and generate matching fixture/workflow examples.")
    init_ws.add_argument("--repo-root", default=".", help="Project root to scan for --auto-detect. Default: current directory.")

    ws_status = subparsers.add_parser("workspace-status", help="Show workspace status.")
    ws_status.add_argument("--root", required=True, help="Workspace root directory.")

    subparsers.add_parser(
        "workspace-risk-policy-template",
        help="Print a copyable workspace.json quality policy template.",
    )
    ws_risk_policy_check = subparsers.add_parser(
        "workspace-risk-policy-check",
        help="Validate the workspace.json quality risk policy.",
    )
    ws_risk_policy_check.add_argument("--root", required=True, help="Workspace root directory.")
    ws_risk_policy_plan = subparsers.add_parser(
        "workspace-risk-policy-plan",
        help="Preview or apply a workspace.json quality risk policy patch.",
    )
    ws_risk_policy_plan.add_argument("--root", required=True, help="Workspace root directory.")
    ws_risk_policy_plan.add_argument("--overwrite", action="store_true", help="Let template defaults replace existing risk policy values.")
    ws_risk_policy_plan.add_argument("--apply", action="store_true", help="Write the proposed quality policy into workspace.json.")

    ws_dashboard = subparsers.add_parser("workspace-dashboard", help="Show a compact workspace console dashboard.")
    ws_dashboard.add_argument("--root", required=True, help="Workspace root directory.")
    ws_dashboard.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")
    ws_dashboard.add_argument("--limit", type=int, default=5, help="Maximum recent items per section. Default: 5.")

    ws_gui = subparsers.add_parser("workspace-gui", help="Open the read-only workspace desktop console.")
    ws_gui.add_argument("--root", required=True, help="Workspace root directory.")
    ws_gui.add_argument("--run-id", help="Optional run id to show first.")
    ws_gui.add_argument("--limit", type=int, default=10, help="Maximum recent reports to load. Default: 10.")

    ws_gui_actions = subparsers.add_parser("workspace-gui-actions", help="Export recent workspace GUI action history.")
    ws_gui_actions.add_argument("--root", required=True, help="Workspace root directory.")
    ws_gui_actions.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")
    ws_gui_actions.add_argument("--limit", type=int, default=20, help="Maximum events to load. Default: 20.")
    ws_gui_actions.add_argument("--action", help="Filter by GUI action id.")
    ws_gui_actions.add_argument("--status", choices=["success", "error"], help="Filter by action status.")

    ws_gui_action_index = subparsers.add_parser(
        "workspace-gui-action-index",
        help="Summarize recent workspace GUI action history for Planner/CI reads.",
    )
    ws_gui_action_index.add_argument("--root", required=True, help="Workspace root directory.")
    ws_gui_action_index.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")
    ws_gui_action_index.add_argument("--limit", type=int, default=100, help="Maximum recent events to summarize. Default: 100.")
    ws_gui_action_index.add_argument("--recent-error-limit", type=int, default=5, help="Maximum recent errors to include. Default: 5.")
    ws_gui_action_index.add_argument("--risk", action="store_true", help="Export risk summary with remediation checklist instead of the raw index.")

    ws_list = subparsers.add_parser("workspace-list", help="List workflows in a workspace.")
    ws_list.add_argument("--root", required=True, help="Workspace root directory.")
    ws_list.add_argument("--include-slow", action="store_true", help="Include workflows tagged 'slow'. Default: skipped.")

    ws_validate = subparsers.add_parser("workspace-validate", help="Validate all workflows in a workspace.")
    ws_validate.add_argument("--root", required=True, help="Workspace root directory.")
    ws_validate.add_argument("--strict", action="store_true", help="Apply production-oriented validation rules.")
    ws_validate.add_argument("--allow-high-risk", action="store_true", help="Allow high-risk actions in strict validation.")

    ws_run = subparsers.add_parser("workspace-run", help="Run a workflow from a workspace.")
    ws_run.add_argument("--root", required=True, help="Workspace root directory.")
    ws_run.add_argument("--workflow", required=True, help="Workflow name or relative path.")
    ws_run.add_argument("--inputs", help="Workflow input JSON string.")
    ws_run.add_argument("--inputs-file", help="Workflow input JSON file or name under inputs/.")
    ws_run.add_argument("--sensitive-fields", help="Comma-separated input paths to hash in audit logs.")
    ws_run.add_argument("--resume-from", help="Existing run directory to resume from checkpoint.")
    ws_run.add_argument("--allow-click", action="store_true", help="Allow real click/input actions. Default is dry-run.")
    ws_run.add_argument(
        "--run-profile",
        choices=RUN_PROFILE_CHOICES,
        default="dry-run",
        help="Execution permission profile. Default is dry-run.",
    )
    ws_run.add_argument("--skip-preflight", action="store_true", help="Skip runtime preflight checks.")
    ws_run.add_argument("--strict-preflight", action="store_true", help="Apply strict validation during preflight.")
    ws_run.add_argument("--allow-high-risk", action="store_true", help="Allow high-risk actions during strict preflight.")
    ws_run.add_argument("--no-lock", action="store_true", help="Disable run lock for controlled debugging.")
    ws_run.add_argument("--lock-ttl-seconds", type=float, default=3600.0, help="Run lock TTL. Default: 3600.")
    ws_run.add_argument("--wait-lock", action="store_true", help="Wait for the run lock instead of failing immediately.")
    ws_run.add_argument("--queue-when-locked", action="store_true", help="Wait for the run lock instead of failing immediately.")
    ws_run.add_argument("--lock-wait-seconds", type=float, default=30.0, help="Maximum seconds to wait when queued. Default: 30.")
    ws_run.add_argument("--lock-poll-seconds", type=float, default=0.5, help="Seconds between lock checks when queued. Default: 0.5.")
    ws_run.add_argument("--no-report-export", action="store_true", help="Do not export JSON/Markdown reports to workspace reports/.")
    ws_run.add_argument("--synthetic-on-capture-fail", action="store_true")

    ws_runs = subparsers.add_parser("workspace-runs", help="List workspace run summaries.")
    ws_runs.add_argument("--root", required=True, help="Workspace root directory.")
    ws_runs.add_argument("--limit", type=int, default=20)

    ws_reports = subparsers.add_parser("workspace-reports", help="List exported workspace reports.")
    ws_reports.add_argument("--root", required=True, help="Workspace root directory.")

    ws_report_index = subparsers.add_parser("workspace-report-index", help="Build or query the workspace report index.")
    ws_report_index.add_argument("--root", required=True, help="Workspace root directory.")
    ws_report_index.add_argument("--rebuild", action="store_true", help="Rebuild reports/index.json before reading.")
    ws_report_index.add_argument("--status", choices=["success", "failed"], help="Filter by report status.")
    ws_report_index.add_argument("--workflow", help="Filter by workflow name.")
    ws_report_index.add_argument("--failed-only", action="store_true", help="Return only failed reports.")

    ws_report_detail = subparsers.add_parser("workspace-report-detail", help="Show one workspace report detail.")
    ws_report_detail.add_argument("--root", required=True, help="Workspace root directory.")
    ws_report_detail.add_argument("--run-id", required=True, help="Run id to inspect.")
    ws_report_detail.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    ws_tag_report = subparsers.add_parser("workspace-tag-report", help="Annotate a workspace run report.")
    ws_tag_report.add_argument("--root", required=True, help="Workspace root directory.")
    ws_tag_report.add_argument("--run-id", required=True, help="Run id to annotate.")
    ws_tag_report.add_argument(
        "--review-status",
        choices=["unreviewed", "reviewed", "needs_fix", "regression_ready", "ignored"],
        help="Human review status.",
    )
    ws_tag_report.add_argument("--tag", action="append", default=[], help="Tag to attach. Can be used multiple times.")
    ws_tag_report.add_argument("--note", help="Human review note.")
    ws_tag_report.add_argument("--regression-candidate", action="store_true", help="Mark as regression test candidate.")
    ws_tag_report.add_argument("--clear-regression-candidate", action="store_true", help="Clear regression test candidate flag.")

    ws_report_tags = subparsers.add_parser("workspace-report-tags", help="Show workspace report annotations.")
    ws_report_tags.add_argument("--root", required=True, help="Workspace root directory.")

    ws_product_issues = subparsers.add_parser("workspace-product-issues", help="Summarize failed reports as product issue groups.")
    ws_product_issues.add_argument("--root", required=True, help="Workspace root directory.")
    ws_product_issues.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")
    ws_product_issues.add_argument("--write", action="store_true", help="Write reports/product_issues.json before printing.")

    ws_export_regression = subparsers.add_parser("workspace-export-regression-fixture", help="Export a failed run report into a regression fixture draft.")
    ws_export_regression.add_argument("--root", required=True, help="Workspace root directory.")
    ws_export_regression.add_argument("--run-id", required=True, help="Failed run id to export.")
    ws_export_regression.add_argument("--allow-success", action="store_true", help="Allow exporting a successful run.")
    ws_export_regression.add_argument("--overwrite", action="store_true", help="Overwrite an existing export.")

    ws_promote_regression = subparsers.add_parser("workspace-promote-regression", help="Promote a regression fixture draft into workspace regression_tests/.")
    ws_promote_regression.add_argument("--root", required=True, help="Workspace root directory.")
    ws_promote_regression.add_argument("--run-id", required=True, help="Run id to promote.")
    ws_promote_regression.add_argument("--overwrite", action="store_true", help="Overwrite an existing promoted test.")

    ws_regression_tests = subparsers.add_parser("workspace-regression-tests", help="List promoted workspace regression tests.")
    ws_regression_tests.add_argument("--root", required=True, help="Workspace root directory.")

    ws_run_regression = subparsers.add_parser("workspace-run-regression-tests", help="Run workspace regression_tests and write a report.")
    ws_run_regression.add_argument("--root", required=True, help="Workspace root directory.")
    ws_run_regression.add_argument("--timeout-seconds", type=float, default=120.0, help="Maximum pytest runtime. Default: 120.")
    ws_run_regression.add_argument("--pytest-arg", action="append", default=[], help="Extra pytest argument. Can be used multiple times.")

    ws_queue_submit = subparsers.add_parser("workspace-queue-submit", help="Submit a workflow task to the workspace queue.")
    ws_queue_submit.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_submit.add_argument("--workflow", required=True, help="Workflow name or relative path.")
    ws_queue_submit.add_argument("--inputs", help="Workflow input JSON string.")
    ws_queue_submit.add_argument("--inputs-file", help="Workflow input JSON file or name under inputs/.")
    ws_queue_submit.add_argument("--priority", type=int, default=0, help="Higher priority runs first. Default: 0.")
    ws_queue_submit.add_argument("--max-retries", type=int, default=0, help="Retry count after failures. Default: 0.")
    ws_queue_submit.add_argument(
        "--run-profile",
        choices=RUN_PROFILE_CHOICES,
        default="dry-run",
        help="Execution permission profile. Default is dry-run.",
    )
    ws_queue_submit.add_argument("--allow-click", action="store_true", help="Allow real click/input actions when the task runs.")

    ws_queue_list = subparsers.add_parser("workspace-queue-list", help="List workspace queue tasks.")
    ws_queue_list.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_list.add_argument("--status", choices=["pending", "running", "success", "failed", "canceled"], help="Filter by task status.")

    ws_queue_cancel = subparsers.add_parser("workspace-queue-cancel", help="Cancel a pending workspace queue task.")
    ws_queue_cancel.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_cancel.add_argument("--task-id", required=True, help="Task id to cancel.")
    ws_queue_cancel.add_argument("--reason", help="Optional cancel reason.")

    ws_queue_retry = subparsers.add_parser("workspace-queue-retry", help="Requeue a failed or canceled workspace queue task.")
    ws_queue_retry.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_retry.add_argument("--task-id", required=True, help="Task id to retry.")

    ws_queue_run_next = subparsers.add_parser("workspace-queue-run-next", help="Run the next pending workspace queue task.")
    ws_queue_run_next.add_argument("--root", required=True, help="Workspace root directory.")

    ws_queue_worker = subparsers.add_parser("workspace-queue-worker", help="Continuously run pending workspace queue tasks.")
    ws_queue_worker.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_worker.add_argument("--poll-seconds", type=float, default=1.0, help="Seconds to wait between idle polls. Default: 1.")
    ws_queue_worker.add_argument("--max-tasks", type=int, help="Stop after this many tasks have run.")
    ws_queue_worker.add_argument("--max-seconds", type=float, help="Stop after this many seconds.")
    ws_queue_worker.add_argument("--stop-file", help="Stop when this file exists. Default: queue/worker.stop.")
    ws_queue_worker.add_argument("--once", action="store_true", help="Run at most one pending task and exit.")

    ws_queue_migrate_sqlite = subparsers.add_parser("workspace-queue-migrate-sqlite", help="Migrate JSON workspace queue tasks into SQLite.")
    ws_queue_migrate_sqlite.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_migrate_sqlite.add_argument("--no-backup", action="store_true", help="Do not create a tasks.json backup before migration.")
    ws_queue_migrate_sqlite.add_argument("--no-set-backend", action="store_true", help="Do not switch workspace queue backend to sqlite.")

    ws_queue_rollback_json = subparsers.add_parser("workspace-queue-rollback-json", help="Export SQLite queue tasks back to JSON tasks.json.")
    ws_queue_rollback_json.add_argument("--root", required=True, help="Workspace root directory.")
    ws_queue_rollback_json.add_argument("--no-backup", action="store_true", help="Do not backup the existing tasks.json before rollback.")
    ws_queue_rollback_json.add_argument("--no-set-backend", action="store_true", help="Do not switch workspace queue backend to json.")

    ws_record_browser = subparsers.add_parser("workspace-record-browser", help="Record a headed browser session into a workflow draft.")
    ws_record_browser.add_argument("--root", required=True, help="Workspace root directory.")
    ws_record_browser.add_argument("--url", required=True, help="Initial page URL to open for recording.")
    ws_record_browser.add_argument("--save-as", required=True, help="Workflow name or relative path under workflows/.")
    ws_record_browser.add_argument("--timeout-seconds", type=float, default=0.0, help="Optional automatic stop timeout. Default: wait until browser closes.")
    ws_record_browser.add_argument("--headless", action="store_true", help="Run headless for automation tests. Default is headed.")
    ws_record_browser.add_argument("--assert-text", help="Explicit success text to append as an assert_text step.")
    ws_record_browser.add_argument("--no-auto-assert", action="store_true", help="Do not infer an assert_text step from the final page.")
    ws_record_browser.add_argument("--save-auth-state", help="Append a confirmed save_storage_state step under .agent-auth/<name>.json.")
    ws_record_browser.add_argument("--no-check", action="store_true", help="Skip validation/preflight after saving the recorded workflow.")
    ws_record_browser.add_argument("--preview-run", action="store_true", help="Run the saved workflow once in dry-run mode after recording.")
    ws_record_browser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing recorded workflow file.")
    ws_record_browser.add_argument("--queue", action="store_true", help="Submit the recorded workflow to the workspace queue in dry-run mode.")
    ws_record_browser.add_argument("--queue-priority", type=int, default=0, help="Priority for --queue. Higher runs first.")
    ws_record_browser.add_argument("--queue-max-retries", type=int, default=0, help="Max retries for --queue.")
    ws_record_browser.add_argument("--format", choices=["json", "markdown"], default="json")

    ws_planner = subparsers.add_parser("workspace-planner-context", help="Show planner-safe workspace context.")
    ws_planner.add_argument("--root", required=True, help="Workspace root directory.")
    ws_planner.add_argument("--run-limit", type=int, default=5)

    ws_check_plan = subparsers.add_parser("workspace-check-plan", help="Validate a planner draft without executing it.")
    ws_check_plan.add_argument("--root", required=True, help="Workspace root directory.")
    ws_check_plan.add_argument("--file", required=True, help="Workflow draft YAML or JSON file.")
    ws_check_plan.add_argument("--allow-high-risk", action="store_true", help="Allow high-risk capabilities in the draft.")

    ws_generate_plan = subparsers.add_parser("workspace-planner-draft", help="Generate a workflow draft with the configured model, then check it without executing.")
    ws_generate_plan.add_argument("--root", required=True, help="Workspace root directory.")
    ws_generate_plan.add_argument("--instruction", required=True, help="Natural-language automation request.")
    ws_generate_plan.add_argument("--source", default=None, help="Model credential file.")
    ws_generate_plan.add_argument("--preferred", default=None, help="Preferred model provider. Defaults to VISUAL_AGENT_MODEL_PROVIDER or openai.")
    ws_generate_plan.add_argument("--model", default=None, help="Optional model override.")
    ws_generate_plan.add_argument("--run", action="store_true", help="Call the model. Without this flag, only build the prompt and API plan.")
    ws_generate_plan.add_argument("--save-as", default=None, help="Save a valid generated draft under workspace workflows/. Requires --run.")
    ws_generate_plan.add_argument("--preview-save", action="store_true", help="Show the save target and diff for --save-as without writing the file.")
    ws_generate_plan.add_argument("--overwrite", action="store_true", help="Allow --save-as to replace an existing workflow file.")
    ws_generate_plan.add_argument("--timeout-seconds", type=float, default=30.0)
    ws_generate_plan.add_argument("--max-completion-tokens", type=int, default=1200)
    ws_generate_plan.add_argument("--format", choices=["json", "markdown"], default="json")

    subparsers.add_parser("templates", help="List available workflow templates.")

    install_template_cmd = subparsers.add_parser("install-template", help="Install a template into a workspace.")
    install_template_cmd.add_argument("--root", required=True, help="Workspace root directory.")
    install_template_cmd.add_argument("--template", required=True, help="Template id.")
    install_template_cmd.add_argument("--overwrite", action="store_true")
    return parser


def _build_perception_status(manifest: Any, vlm_summary: dict[str, Any]) -> dict[str, Any]:
    """Summarise which perception providers are actually usable right now."""
    available_names = {
        str(getattr(c, "name", ""))
        for c in manifest.capabilities
        if getattr(c, "available", False)
    }
    dom_ok = "observe_browser" in available_names or "observe_dom" in available_names
    uia_ok = "observe_uia" in available_names
    ocr_ok = "observe_ocr" in available_names and ("screen_ocr" in available_names or "pytesseract" in available_names)
    vlm_ok = bool(vlm_summary.get("ok")) or (
        vlm_summary.get("cloud", {}).get("available") is True
        or any(
            v.get("available") for v in vlm_summary.get("local", {}).values()
            if isinstance(v, dict)
        )
    )

    warnings: list[str] = []
    if not dom_ok:
        warnings.append(
            "Browser/DOM provider unavailable. Run: pip install -e .[web] && python -m playwright install chromium"
        )
    if not vlm_ok:
        warnings.append(
            "No VLM (visual fallback) is configured. "
            "Options: (A) set an API key in model_api_keys.txt for cloud VLM, "
            "(B) install torch/transformers and a local model for on-device VLM, "
            "(C) workflows that only use DOM/UIA work without VLM."
        )
    if not ocr_ok:
        warnings.append(
            "OCR provider unavailable (optional). "
            "Install screen-ocr[winrt] on Windows, or install pytesseract and the Tesseract binary."
        )

    return {
        "dom_browser": dom_ok,
        "windows_uia": uia_ok,
        "ocr": ocr_ok,
        "vlm": vlm_ok,
        "ready_for_dom_workflows": dom_ok,
        "ready_for_visual_workflows": dom_ok and vlm_ok,
        "warnings": warnings,
    }


def doctor_recommendations(missing: list[Any], *, strict: bool = False) -> list[dict[str, Any]]:
    priority_rank = {"P0": 0, "P1": 1, "P2": 2}
    recommendations = []
    for capability in missing:
        name = str(getattr(capability, "name", "") or "")
        dependency = str(getattr(capability, "dependency", "") or name)
        required = bool(getattr(capability, "required", False))
        install_hint = getattr(capability, "install_hint", None)
        priority = doctor_priority(name, dependency=dependency, required=required, strict=strict)
        recommendations.append(
            {
                "priority": priority,
                "code": f"missing_{name}",
                "name": name,
                "dependency": dependency or None,
                "required": required,
                "message": f"{priority}: {name} is unavailable.",
                "install_hint": install_hint,
            }
        )
    return sorted(recommendations, key=lambda item: (priority_rank.get(str(item.get("priority")), 99), str(item.get("name") or "")))


def doctor_priority(name: str, *, dependency: str = "", required: bool = False, strict: bool = False) -> str:
    key = (dependency or name).lower()
    if required or strict or key in {"playwright", "mcp"}:
        return "P0"
    if key in {"uiautomation", "pytesseract", "tesseract"}:
        return "P1"
    return "P2"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "click":
        result = VisualAgentRunner(output_dir=args.output_dir).click_target(
            target=args.target,
            provider=args.provider,
            dry_run=args.dry_run,
            synthetic_on_capture_fail=args.synthetic_on_capture_fail,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "inspect-dom":
        observation = DomProvider(headless=not args.headed).observe_url(args.url)
        payload = to_jsonable(observation)
        payload["elements"] = payload["elements"][: args.limit]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "resolve-dom":
        observation = DomProvider(headless=not args.headed).observe_url(args.url)
        resolved = SelectorResolver().resolve(Target(text=args.target, role=args.role), observation)
        print(json.dumps(to_jsonable(resolved), ensure_ascii=False, indent=2))
        return 0
    if args.command == "browser-smoke":
        from .browser_smoke import browser_smoke_to_markdown, run_browser_smoke

        payload = run_browser_smoke(
            url=args.url,
            output_dir=args.output_dir,
            headed=args.headed,
            timeout_ms=args.timeout_ms,
            wait_until=args.wait_until,
            min_text_length=args.min_text_length,
            min_interactive=args.min_interactive,
            expect_text=list(args.expect_text or []),
            expect_url_contains=list(args.expect_url_contains or []),
            fill=list(args.fill or []),
            fill_selector=list(args.fill_selector or []),
            click_text=args.click_text,
            click_selector=args.click_selector,
            require_change_after_click=args.require_change_after_click,
            wait_for_text_after=list(args.wait_for_text_after or []),
            wait_for_url_contains_after=list(args.wait_for_url_contains_after or []),
            wait_timeout_seconds=args.wait_timeout_seconds,
            expect_text_after=list(args.expect_text_after or []),
            expect_url_contains_after=list(args.expect_url_contains_after or []),
            wait_after_seconds=args.wait_after_seconds,
            save_workflow=args.save_workflow,
            overwrite_workflow=args.overwrite_workflow,
        )
        if args.format == "markdown":
            print(browser_smoke_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if payload.get("status") == "success" else 1
    if args.command == "browser-smoke-suite":
        from .browser_smoke_suite import browser_smoke_suite_to_markdown, run_browser_smoke_suite

        payload = run_browser_smoke_suite(
            args.file,
            output_dir=args.output_dir,
            headed=True if args.headed else None,
        )
        if args.format == "markdown":
            print(browser_smoke_suite_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if payload.get("status") == "success" else 1
    if args.command == "inspect-uia":
        observation = UIAutomationProvider(max_depth=args.max_depth).observe_desktop()
        payload = to_jsonable(observation)
        payload["elements"] = payload["elements"][: args.limit]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "resolve-uia":
        observation = UIAutomationProvider(max_depth=args.max_depth).observe_desktop()
        resolved = SelectorResolver().resolve(Target(text=args.target, role=args.role), observation)
        print(json.dumps(to_jsonable(resolved), ensure_ascii=False, indent=2))
        return 0
    if args.command == "run-workflow":
        workflow = parse_workflow_file(args.file)
        inputs = load_inputs(args.inputs, args.inputs_file)
        sensitive_fields = parse_csv_set(args.sensitive_fields)
        input_check = validate_workflow_inputs(workflow, inputs, sensitive_fields=sensitive_fields)
        if not input_check["ok"]:
            print(json.dumps(to_jsonable({"status": "blocked", "input_check": input_check}), ensure_ascii=False, indent=2))
            return 1
        if not args.skip_preflight:
            preflight_result = run_preflight(
                workflow,
                strict=args.strict_preflight,
                allow_high_risk=args.allow_high_risk,
            )
            if not preflight_result.ok:
                print(json.dumps(to_jsonable(preflight_result), ensure_ascii=False, indent=2))
                return 1
        result = WorkflowRuntime(output_dir=args.output_dir).run(
            workflow,
            dry_run=args.run_profile == "dry-run" and not args.allow_click,
            run_profile="approved" if args.allow_click else args.run_profile,
            synthetic_on_capture_fail=args.synthetic_on_capture_fail,
            inputs=inputs,
            sensitive_fields=sensitive_fields,
            resume_from=args.resume_from,
            use_lock=not args.no_lock,
            lock_ttl_seconds=args.lock_ttl_seconds,
            queue_when_locked=args.queue_when_locked or args.wait_lock,
            lock_wait_seconds=args.lock_wait_seconds,
            lock_poll_seconds=args.lock_poll_seconds,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "preflight-workflow":
        result = run_preflight(
            parse_workflow_file(args.file),
            strict=args.strict,
            allow_high_risk=args.allow_high_risk,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1
    if args.command == "validate-workflow":
        result = (
            validate_workflow_file_strict(args.file, allow_high_risk=args.allow_high_risk)
            if args.strict
            else validate_workflow_file(args.file)
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.valid else 1
    if args.command == "list-runs":
        summaries = [run_summary_to_dict(summary) for summary in list_run_summaries(args.output_dir, limit=args.limit)]
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
        return 0
    if args.command == "show-run":
        print(json.dumps(run_summary_to_dict(load_run_summary(args.run_dir)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "report-run":
        report = load_run_report(args.run_dir)
        if args.format == "markdown":
            print(run_report_to_markdown(report))
        else:
            print(json.dumps(run_report_to_dict(report), ensure_ascii=False, indent=2))
        return 0
    if args.command == "capabilities":
        print(json.dumps(to_jsonable(build_capability_manifest()), ensure_ascii=False, indent=2))
        return 0
    if args.command == "atomic-capabilities":
        print(json.dumps(to_jsonable(build_atomic_capability_manifest()), ensure_ascii=False, indent=2))
        return 0
    if args.command == "doctor":
        manifest = build_capability_manifest()
        missing = [capability for capability in manifest.capabilities if not capability.available]
        blocking = missing if args.strict else [capability for capability in missing if capability.required]
        recommendations = doctor_recommendations(missing, strict=args.strict)
        vlm_summary = vlm_doctor_summary()
        perception = _build_perception_status(manifest, vlm_summary)
        payload = {
            "ok": not blocking,
            "available_count": manifest.available_count,
            "missing_count": manifest.missing_count,
            "blocking_missing_count": len(blocking),
            "perception": perception,
            "missing": missing,
            "recommendations": recommendations,
            "vlm": {
                "local": {
                    "qwen2-vl": detect_vlm_backend("qwen2-vl"),
                    "moondream": detect_vlm_backend("moondream"),
                },
                "cloud": public_engine_status(detect_cloud_vision_backend()),
                "doctor_summary": vlm_summary,
            },
        }
        print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if not blocking else 1
    if args.command == "quality-gate":
        result = run_quality_gate(
            args.profile,
            workspace_root=args.workspace_root,
            execute=args.run,
            timeout_seconds=args.timeout_seconds,
            report_root=args.report_root,
            fail_on_risk_policy_error=args.fail_on_risk_policy_error,
            fail_on_secret_leak=args.fail_on_secret_leak,
        )
        print(json.dumps(quality_gate_to_dict(result), ensure_ascii=False, indent=2))
        return 0 if result.status in {"planned", "success"} else 1
    if args.command == "quality-gate-reports":
        reports = list_quality_gate_reports(
            report_root=args.report_root,
            workspace_root=args.workspace_root,
            profile=args.profile,
            status=args.status,
            strict_policy_failed=parse_optional_bool(args.strict_policy_failed),
        )
        if args.format == "markdown":
            print(quality_gate_reports_to_markdown(reports))
        else:
            print(
                json.dumps(
                    to_jsonable(reports),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    if args.command == "quality-gate-index":
        index = load_quality_gate_index(
            report_root=args.report_root,
            workspace_root=args.workspace_root,
            rebuild=args.rebuild,
            profile=args.profile,
            status=args.status,
            strict_policy_failed=parse_optional_bool(args.strict_policy_failed),
        )
        if args.format == "markdown":
            print(quality_gate_index_to_markdown(index))
        else:
            print(
                json.dumps(
                    to_jsonable(index),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    if args.command == "install-ci-templates":
        result = install_ci_templates(
            args.root,
            workspace_root=args.workspace_root,
            overwrite=args.overwrite,
        )
        print(json.dumps(ci_template_install_to_dict(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "external-samples":
        print(json.dumps(to_jsonable(list_external_samples(args.root)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "external-samples-check":
        result = check_external_samples(args.root)
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["invalid_samples"] == 0 else 1
    if args.command == "external-samples-readiness":
        result = external_samples_readiness(args.root, workspace_root=args.workspace_root, require_live_auth=args.require_live_auth)
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["blocked_samples"] == 0 else 1
    if args.command == "external-sample-run-plan":
        result = build_external_sample_run_plan(
            args.sample_id,
            root=args.root,
            workspace_root=args.workspace_root,
            run_profile=args.run_profile,
            require_live_auth=args.require_live_auth,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 1
    if args.command == "external-sample-run":
        result = run_external_sample(
            open_workspace(args.workspace_root),
            args.sample_id,
            root=args.root,
            run_profile=args.run_profile,
            preflight=not args.skip_preflight,
            require_live_auth=args.require_live_auth,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["status"] == "success" else 1
    if args.command == "external-sample-batch-plan":
        result = build_external_sample_batch_plan(
            root=args.root,
            workspace_root=args.workspace_root,
            run_profile=args.run_profile,
            include_blocked=not args.ready_only,
            require_live_auth=args.require_live_auth,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["ready_samples"] > 0 else 1
    if args.command == "external-sample-batch-submit":
        result = submit_external_sample_batch(
            open_workspace(args.workspace_root),
            root=args.root,
            run_profile=args.run_profile,
            priority=args.priority,
            max_retries=args.max_retries,
            include_blocked=not args.ready_only,
            require_live_auth=args.require_live_auth,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["submitted_count"] > 0 else 1
    if args.command == "external-sample-summary":
        result = build_external_sample_run_summary(open_workspace(args.workspace_root), root=args.root)
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "external-sample-batch-report":
        result = export_external_sample_batch_report(open_workspace(args.workspace_root), root=args.root)
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "external-sample-dry-run-report":
        result = export_external_sample_dry_run_report(
            open_workspace(args.workspace_root),
            root=args.root,
            require_live_auth=args.require_live_auth,
            preflight=not args.skip_preflight,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["summary"]["failed_samples"] == 0 else 1
    if args.command == "external-sample-live-placeholder":
        result = export_external_sample_live_placeholder(
            open_workspace(args.workspace_root),
            root=args.root,
            require_live_auth=not args.no_require_live_auth,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ready" else 1
    if args.command == "external-sample-batch-reports":
        result = list_external_sample_batch_reports(
            open_workspace(args.workspace_root),
            status=args.status,
            sample_id=args.sample_id,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "external-sample-batch-report-index":
        result = load_external_sample_batch_report_index(
            open_workspace(args.workspace_root),
            rebuild=args.rebuild,
            status=args.status,
            sample_id=args.sample_id,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "external-sample-batch-failures":
        result = build_external_sample_batch_failure_summary(open_workspace(args.workspace_root), args.report_id)
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "external-sample-batch-rerun-plan":
        result = build_external_sample_batch_rerun_plan(
            open_workspace(args.workspace_root),
            args.report_id,
            root=args.root,
            run_profile=args.run_profile,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["candidate_count"] > 0 else 1
    if args.command == "external-sample-batch-rerun-submit":
        result = submit_external_sample_batch_reruns(
            open_workspace(args.workspace_root),
            args.report_id,
            root=args.root,
            run_profile=args.run_profile,
            priority=args.priority,
            max_retries=args.max_retries,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["submitted_count"] > 0 else 1
    if args.command == "external-sample-rerun-plan":
        result = build_external_sample_rerun_plan(
            open_workspace(args.workspace_root),
            root=args.root,
            run_profile=args.run_profile,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["candidate_count"] > 0 else 1
    if args.command == "external-sample-rerun-submit":
        result = submit_external_sample_reruns(
            open_workspace(args.workspace_root),
            root=args.root,
            run_profile=args.run_profile,
            priority=args.priority,
            max_retries=args.max_retries,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["submitted_count"] > 0 else 1
    if args.command == "auth-state-plan":
        result = build_auth_state_import_plan(
            args.source,
            name=args.name,
            workspace_root=args.workspace_root,
            overwrite=args.overwrite,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["source_exists"] and result["safe_target"] and (args.overwrite or not result["target_exists"]) else 1
    if args.command == "auth-state-import":
        result = import_auth_state(
            args.source,
            name=args.name,
            workspace_root=args.workspace_root,
            overwrite=args.overwrite,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "auth-state-inspect":
        result = inspect_storage_state(args.path)
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "auth-state-probe":
        result = probe_storage_state(
            args.path,
            url=args.url,
            allowed_domain=args.allowed_domain,
            headed=args.headed,
            timeout_ms=args.timeout_ms,
        )
        if args.format == "markdown":
            print(auth_state_probe_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "ready" else 1
    if args.command == "model-credentials-inspect":
        result = inspect_model_credentials(source=args.source, preferred_provider=args.preferred)
        if args.format == "markdown":
            print(model_credentials_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["preferred_available"] else 1
    if args.command == "model-api-probe-plan":
        if args.run:
            result = run_model_api_probe(
                source=args.source,
                preferred_provider=args.preferred,
                base_url=args.base_url,
                endpoint=args.endpoint,
                model=args.model,
                timeout_seconds=args.timeout_seconds,
                max_completion_tokens=args.max_completion_tokens,
            )
        else:
            result = build_model_api_probe_plan(
                source=args.source,
                preferred_provider=args.preferred,
                base_url=args.base_url,
                endpoint=args.endpoint,
                model=args.model,
            )
        if args.format == "markdown":
            print(model_api_probe_result_to_markdown(result) if args.run else model_api_probe_plan_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("ready") and (not args.run or result.get("status") == "success") else 1
    if args.command == "init-workspace":
        framework_hint = detect_framework_from_dir(Path(args.repo_root).resolve()) if args.auto_detect else None
        workspace = init_workspace(args.root, with_demo=not args.no_demo, overwrite=args.overwrite, framework_hint=framework_hint)
        print(json.dumps(workspace_status(workspace), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-status":
        print(json.dumps(workspace_status(open_workspace(args.root)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-risk-policy-template":
        print(json.dumps(build_workspace_risk_policy_template(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-risk-policy-check":
        result = validate_workspace_risk_policy(open_workspace(args.root))
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    if args.command == "workspace-risk-policy-plan":
        result = build_workspace_risk_policy_apply_plan(
            open_workspace(args.root),
            overwrite=args.overwrite,
            apply=args.apply,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["validation_after"]["error_count"] == 0 else 1
    if args.command == "workspace-dashboard":
        dashboard = build_workspace_dashboard(open_workspace(args.root), limit=args.limit)
        if args.format == "markdown":
            print(dashboard_to_markdown(dashboard))
        else:
            print(json.dumps(to_jsonable(dashboard), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-gui":
        return open_workspace_window(open_workspace(args.root), selected_run_id=args.run_id, limit=args.limit)
    if args.command == "workspace-gui-actions":
        report = build_gui_action_history_report(
            open_workspace(args.root),
            limit=args.limit,
            action=args.action,
            status=args.status,
        )
        if args.format == "markdown":
            print(gui_action_history_report_to_markdown(report))
        else:
            print(json.dumps(to_jsonable(report), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-gui-action-index":
        workspace = open_workspace(args.root)
        if args.risk:
            summary = build_gui_action_history_risk_summary(
                workspace,
                limit=args.limit,
                failed_action_limit=args.recent_error_limit,
                config=load_workspace_gui_action_history_risk_config(workspace),
            )
            if args.format == "markdown":
                print(gui_action_history_risk_to_markdown(summary))
            else:
                print(json.dumps(to_jsonable(summary), ensure_ascii=False, indent=2))
            return 0
        index = build_gui_action_history_index(workspace, limit=args.limit, recent_error_limit=args.recent_error_limit)
        if args.format == "markdown":
            print(gui_action_history_index_to_markdown(index))
        else:
            print(json.dumps(to_jsonable(index), ensure_ascii=False, indent=2))
        return 0
    if args.command == "release-check":
        plan = build_release_check_plan(workspace_root=args.workspace_root)
        if args.format == "markdown":
            print(release_check_plan_to_markdown(plan))
        else:
            print(json.dumps(to_jsonable(plan), ensure_ascii=False, indent=2))
        return 0
    if args.command == "install-check":
        plan = build_install_check_plan()
        if args.format == "markdown":
            print(install_check_plan_to_markdown(plan))
        else:
            print(json.dumps(to_jsonable(plan), ensure_ascii=False, indent=2))
        return 0
    if args.command == "mcp-client-config":
        payload = build_mcp_client_config(
            workspace_root=args.workspace_root,
            client=args.client,
            python=args.python,
            repo_root=args.repo_root,
        )
        if args.format == "markdown":
            print(mcp_client_config_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "coding-agent-brief":
        payload = build_coding_agent_brief(
            workspace_root=args.workspace_root,
            repo_root=args.repo_root,
            client=args.client,
            python=args.python,
        )
        if args.format == "markdown":
            print(coding_agent_brief_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "mcp-smoke":
        result = run_mcp_smoke_check(
            workspace_root=args.workspace_root,
            workflow=args.workflow,
            inputs_file=args.inputs_file,
        )
        if args.format == "markdown":
            print(mcp_smoke_check_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
    if args.command == "demo-workspace-check":
        result = run_demo_workspace_check(root=args.root, overwrite=args.overwrite)
        if args.format == "markdown":
            print(demo_workspace_check_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
    if args.command == "context-snapshot":
        from .session import workspace_session_snapshot_text

        text = workspace_session_snapshot_text(Path(args.workspace_root).resolve())
        if args.format == "markdown":
            print(text)
        else:
            print(json.dumps({"snapshot": text, "token_estimate": len(text) // 4, "within_budget": len(text) <= 2000}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "usage-status":
        from .cloud import build_remote_workflow_request, cloud_config_status
        from .licensing import check_feature, get_license
        from .session import load_agent_session

        workspace_root = Path(args.workspace_root).resolve()
        session = load_agent_session(workspace_root)
        license_ = get_license()
        payload = {
            "schema_version": 1,
            "workspace": str(workspace_root),
            "license": {
                "tier": license_.tier,
                "seats": license_.seats,
                "expires_at": license_.expires_at,
                "source": license_.source,
                "key_present": license_.key_present,
            },
            "usage": {
                "runs_this_month": session.runs_this_month if session else 0,
                "cloud_runs_used": session.cloud_runs_used if session else 0,
                "usage_reset_date": session.usage_reset_date if session else "",
            },
            "feature_access": {
                "cloud_run": check_feature("cloud_run"),
                "team_workspace": check_feature("team_workspace"),
                "workflow_history_unlimited": check_feature("workflow_history_unlimited"),
            },
            "cloud_config": cloud_config_status(),
            "remote_request_preview": build_remote_workflow_request(
                "example_workflow",
                workspace_root,
                run_profile="dry-run",
            ),
        }
        if args.format == "markdown":
            print(usage_status_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "cloud-run-plan":
        from .cloud import build_remote_workflow_request, remote_client_from_env

        workspace_root = Path(args.workspace_root).resolve()
        request = build_remote_workflow_request(
            args.workflow,
            workspace_root,
            run_profile=args.run_profile,
            inputs=None,
            inputs_file=args.inputs_file,
        )
        diagnostic = remote_client_from_env(run_profile=args.run_profile, inputs_file=args.inputs_file)(args.workflow, workspace_root)
        payload = {
            "schema_version": 1,
            "workspace": str(workspace_root),
            "workflow_name": args.workflow,
            "request": request,
            "adapter_diagnostic": diagnostic,
        }
        if args.format == "markdown":
            print(cloud_run_plan_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "cloud-run":
        from .cloud import execute_remote_workflow_plan, http_cloud_transport_from_env

        transport = None
        if args.execute and args.transport == "http":
            transport = http_cloud_transport_from_env(
                timeout_seconds=args.timeout_seconds,
                max_retries=args.max_retries,
                retry_backoff_seconds=args.retry_backoff_seconds,
            )
        payload = execute_remote_workflow_plan(
            args.workflow,
            Path(args.workspace_root).resolve(),
            run_profile=args.run_profile,
            inputs=None,
            inputs_file=args.inputs_file,
            execute=args.execute,
            transport=transport,
        )
        payload["transport"] = args.transport
        if args.format == "markdown":
            print(cloud_run_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "save-task-context":
        from .session import save_task_context, session_to_snapshot_text

        session = save_task_context(
            Path(args.workspace_root).resolve(),
            task=args.task,
            analyzed_files=[str(item) for item in args.files],
            root_cause=args.root_cause,
            plan=args.plan,
            tried=[str(item) for item in args.tried],
        )
        snapshot = session_to_snapshot_text(session)
        if args.format == "markdown":
            print(snapshot)
        else:
            print(
                json.dumps(
                    {
                        "status": "saved",
                        "task": session.ai_task_context.task if session.ai_task_context else "",
                        "snapshot": snapshot,
                        "token_estimate": len(snapshot) // 4,
                        "within_budget": len(snapshot) <= 2000,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    if args.command == "summarize-latest-failure":
        from .failure_summary import build_failure_summary

        payload = build_failure_summary(Path(args.workspace_root).resolve())
        if args.format == "markdown":
            if payload.get("status") == "no_failure":
                print(payload["message"])
            else:
                print(payload.get("suggested_next_prompt", ""))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "diagnose-latest-failure":
        from .repair import build_failure_evidence_pack, repair_to_markdown

        payload = build_failure_evidence_pack(
            Path(args.workspace_root).resolve(),
            run_id=args.run_id,
            max_chars=args.max_chars,
        )
        if args.format == "markdown":
            print(repair_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if payload.get("status") in {"found", "no_failure"} else 1
    if args.command == "repair-workflow":
        from .repair import repair_to_markdown, suggest_workflow_repair

        payload = suggest_workflow_repair(
            Path(args.workspace_root).resolve(),
            run_id=args.run_id,
            provider=args.provider,
            model=args.model,
            max_chars=args.max_chars,
            apply=args.apply,
            min_confidence=args.min_confidence,
            verify=args.verify,
            verify_run_profile=args.verify_run_profile,
            inputs_file=args.inputs_file,
            rollback_on_fail=args.rollback_on_fail,
            candidate_id=args.candidate_id,
        )
        if args.format == "markdown":
            print(repair_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if payload.get("status") in {"suggested", "needs_model", "no_failure", "applied", "verified", "rolled_back"} else 1
    if args.command == "auto-repair":
        from .repair import auto_repair_failure, auto_repair_to_markdown

        payload = auto_repair_failure(
            Path(args.workspace_root).resolve(),
            run_id=args.run_id,
            max_chars=args.max_chars,
            min_confidence=args.min_confidence,
            verify_run_profile=args.verify_run_profile,
            inputs_file=args.inputs_file,
            candidate_id=args.candidate_id,
            dry_run=args.dry_run,
            force=args.force,
            promote_regression=args.promote_regression,
            overwrite_regression=args.overwrite_regression,
            run_regression=args.run_regression,
            regression_timeout_seconds=args.regression_timeout_seconds,
        )
        if args.format == "markdown":
            print(auto_repair_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if payload.get("status") in {"suggested", "verified", "rolled_back", "no_failure"} else 1
    if args.command == "repair-history":
        from .repair_history import list_repair_history, repair_history_to_markdown

        payload = list_repair_history(
            Path(args.workspace_root).resolve(),
            limit=args.limit,
            workflow=args.workflow,
            status=args.status,
        )
        if args.format == "markdown":
            print(repair_history_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "repair-health":
        from .repair_history import build_repair_health, repair_health_to_markdown

        payload = build_repair_health(
            Path(args.workspace_root).resolve(),
            limit=args.limit,
            workflow=args.workflow,
        )
        if args.format == "markdown":
            print(repair_health_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "repair-rollback":
        from .repair_history import repair_rollback_to_markdown, rollback_repair_history_entry

        payload = rollback_repair_history_entry(
            Path(args.workspace_root).resolve(),
            history_id=args.history_id,
            workflow=args.workflow,
        )
        if args.format == "markdown":
            print(repair_rollback_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if payload.get("status") == "manual_rolled_back" else 1
    if args.command == "benchmarks":
        from .benchmarks import list_public_benchmarks

        payload = list_public_benchmarks(category=args.category)
        if args.format == "markdown":
            lines = ["# Visual Agent Public Benchmarks", ""]
            for item in payload["benchmarks"]:
                lines.append(f"- `{item['id']}`: {item['source']}")
            print("\n".join(lines))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "benchmark-plan":
        from .benchmarks import benchmark_plan_to_markdown, build_benchmark_plan

        payload = build_benchmark_plan(category=args.category, benchmark_id=args.benchmark_id)
        if args.format == "markdown":
            print(benchmark_plan_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if payload.get("status") == "ready" else 1
    if args.command == "benchmark-draft":
        from .benchmarks import benchmark_draft_to_markdown, build_benchmark_workflow_draft

        workspace_root = Path(args.workspace_root).resolve()
        output_path = (workspace_root / args.output).resolve() if args.output else None
        payload = build_benchmark_workflow_draft(
            scenario_id=args.scenario_id,
            workspace_root=workspace_root,
            output_path=output_path,
            dry_run=not args.save,
            overwrite=args.overwrite,
        )
        if args.format == "yaml" and payload.get("yaml"):
            print(payload["yaml"])
        elif args.format == "markdown":
            print(benchmark_draft_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if payload.get("status") == "success" else 1
    if args.command == "verify":
        from .verify import run_verify, verify_to_markdown

        workspace = open_workspace(args.workspace_root)
        tags = tuple(item.strip() for item in str(args.tags).split(",") if item.strip())
        report = run_verify(
            workspace,
            tags=tags or ("verification",),
            workflow_names=tuple(args.workflow or ()),
            max_workflows=args.max_workflows,
            run_profile=args.run_profile,
            wait_lock=args.wait_lock,
            lock_wait_seconds=args.lock_wait_seconds,
            include_slow=args.include_slow,
        )
        if args.format == "markdown":
            print(verify_to_markdown(report))
        else:
            print(json.dumps(to_jsonable(report), ensure_ascii=False, indent=2))
        return 0 if report.failed == 0 else 1
    if args.command == "generate-workflow":
        from .workflow_generator import generate_workflow_yaml

        result = generate_workflow_yaml(
            description=args.description,
            workspace_root=Path(args.workspace_root).resolve(),
            output_path=Path(args.output).resolve() if args.output else None,
            model=args.model,
            dry_run=args.dry_run,
        )
        if args.format == "yaml" and result.get("yaml"):
            print(result["yaml"])
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
    if args.command == "generate-from-diff":
        from .context_ingestion import GenerationContext
        from .git_diff import collect_code_changes
        from .workflow_synthesis import generate_workflow_from_context

        workspace = open_workspace(args.workspace_root)
        repo_root = Path(args.repo_root).resolve()
        changes = collect_code_changes(
            base=args.base,
            cwd=repo_root,
            include_untracked=not args.no_untracked,
        )
        if not changes:
            payload = {"status": "error", "message": "No code changes found in git diff.", "changed_files": []}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
        ctx = GenerationContext(
            task_description=args.task_description,
            code_changes=changes,
            base_url=args.base_url,
            project_root=str(workspace.root),
            framework_hint=args.framework_hint,
        )
        result = generate_workflow_from_context(ctx=ctx, dry_run=args.dry_run, model_id=args.model)
        if args.format == "yaml" and result.workflow_yaml:
            print(result.workflow_yaml)
        elif args.format == "markdown":
            print(generate_from_diff_cli_markdown(workflow_generation_cli_payload(result, changes)))
        else:
            print(json.dumps(to_jsonable(workflow_generation_cli_payload(result, changes)), ensure_ascii=False, indent=2))
        return 0 if result.status == "success" else 1
    if args.command == "verify-impl":
        from .mcp_server import verify_implementation_payload

        workspace = open_workspace(args.workspace_root)
        verify_args = {
            "workspace_root": str(workspace.root),
            "task_description": args.task_description,
            "base_url": args.base_url,
            "repo_root": str(Path(args.repo_root).resolve()),
            "base": args.base,
            "include_untracked": not args.no_untracked,
            "framework_hint": args.framework_hint,
            "model": args.model,
            "run_profile": args.run_profile,
            "min_quality_score": args.min_quality_score,
            "timeout_seconds": args.timeout_seconds,
            "run_negative": args.run_negative,
        }
        if args.inputs_file:
            verify_args["inputs"] = load_workspace_inputs(workspace, None, args.inputs_file)
        payload = verify_implementation_payload(verify_args)
        if args.format == "markdown":
            print(verify_impl_cli_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if payload.get("result") == "pass" else 1
    if args.command == "agent-status":
        from .verification_status import read_verification_status, verification_status_to_markdown

        status = read_verification_status(Path(args.workspace_root).resolve())
        if status is None:
            payload = {
                "status": "missing",
                "message": "No .vscode-agent-status.json found for this workspace.",
                "workspace_root": str(Path(args.workspace_root).resolve()),
            }
            if args.format == "markdown":
                print("No AI verification status yet.")
            else:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 1
        if args.format == "markdown":
            print(verification_status_to_markdown(status))
        else:
            print(json.dumps(to_jsonable(status), ensure_ascii=False, indent=2))
        return 0 if status.result == "pass" else 1
    if args.command == "share-workflow":
        from .workflow_index import mark_workflow_public

        workspace = open_workspace(args.workspace_root)
        ref = find_workflow(workspace, args.name)
        index_path = mark_workflow_public(workspace.root, ref)
        payload = {
            "status": "coming_soon",
            "workflow": ref.name,
            "visibility": "public",
            "index_path": str(index_path),
            "message": (
                f"Sharing workflows to the marketplace is coming soon. "
                f"Workflow '{ref.name}' has been marked as public in your local index. "
                "Sign up at https://visualagent.dev to publish when the marketplace launches."
            ),
        }
        if args.format == "markdown":
            print(payload["message"])
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "codex-check":
        workspace = open_workspace(args.workspace_root)
        tags = tuple(item.strip() for item in str(args.tags).split(",") if item.strip())
        result = run_codex_check(
            workspace,
            base=args.base,
            repo_root=args.repo_root,
            include_slow=args.include_slow,
            tags=tags or ("verification",),
            max_workflows=args.max_workflows,
            run_profile=args.run_profile,
        )
        if args.format == "markdown":
            print(codex_check_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.failed == 0 else 1
    if args.command == "connect":
        result = connect_platform(
            args.platform,
            workspace_root=args.workspace_root,
            repo_root=args.repo_root,
            python=args.python,
            global_config=args.global_config,
        )
        payload = connect_result_to_dict(result)
        if args.format == "markdown":
            print(f"Connected {payload['platform']} to Visual Agent.")
            print(f"Workspace: {payload['workspace_root']}")
            print(f"Config: {payload['config_path']}")
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-list":
        refs = discover_workflows(open_workspace(args.root), include_slow=args.include_slow)
        print(json.dumps(to_jsonable(refs), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-validate":
        results = validate_workspace(open_workspace(args.root), strict=args.strict, allow_high_risk=args.allow_high_risk)
        print(json.dumps(to_jsonable(results), ensure_ascii=False, indent=2))
        return 0 if all(result.valid for result in results) else 1
    if args.command == "workspace-run":
        workspace = open_workspace(args.root)
        workflow_ref = find_workflow(workspace, args.workflow)
        workflow = parse_workflow_file(workflow_ref.path)
        inputs = load_workspace_inputs(workspace, args.inputs, args.inputs_file)
        sensitive_fields = parse_csv_set(args.sensitive_fields)
        input_check = validate_workflow_inputs(workflow, inputs, sensitive_fields=sensitive_fields)
        if not input_check["ok"]:
            print(json.dumps(to_jsonable({"status": "blocked", "input_check": input_check}), ensure_ascii=False, indent=2))
            return 1
        if not args.skip_preflight:
            preflight_result = run_preflight(
                workflow,
                strict=args.strict_preflight,
                allow_high_risk=args.allow_high_risk,
            )
            if not preflight_result.ok:
                print(json.dumps(to_jsonable(preflight_result), ensure_ascii=False, indent=2))
                return 1
        result = run_workspace_workflow(
            workspace,
            args.workflow,
            inputs=inputs,
            dry_run=args.run_profile == "dry-run" and not args.allow_click,
            run_profile="approved" if args.allow_click else args.run_profile,
            preflight=False,
            strict_preflight=args.strict_preflight,
            allow_high_risk=args.allow_high_risk,
            synthetic_on_capture_fail=args.synthetic_on_capture_fail,
            sensitive_fields=sensitive_fields,
            resume_from=args.resume_from,
            use_lock=not args.no_lock,
            lock_ttl_seconds=args.lock_ttl_seconds,
            queue_when_locked=args.queue_when_locked,
            lock_wait_seconds=args.lock_wait_seconds,
            lock_poll_seconds=args.lock_poll_seconds,
            export_report=not args.no_report_export,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-runs":
        summaries = workspace_run_summaries(open_workspace(args.root), limit=args.limit)
        print(json.dumps(to_jsonable(summaries), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-reports":
        print(json.dumps(to_jsonable(list_workspace_reports(open_workspace(args.root))), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-report-index":
        print(
            json.dumps(
                to_jsonable(
                    load_workspace_report_index(
                        open_workspace(args.root),
                        rebuild=args.rebuild,
                        status=args.status,
                        workflow=args.workflow,
                        failed_only=args.failed_only,
                    )
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "workspace-report-detail":
        detail = build_report_detail(open_workspace(args.root), args.run_id)
        if args.format == "markdown":
            print(report_detail_to_markdown(detail))
        else:
            print(json.dumps(to_jsonable(detail), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-tag-report":
        regression_candidate = None
        if args.regression_candidate:
            regression_candidate = True
        if args.clear_regression_candidate:
            regression_candidate = False
        annotation = tag_workspace_report(
            open_workspace(args.root),
            args.run_id,
            review_status=args.review_status,
            tags=tuple(args.tag),
            note=args.note,
            regression_candidate=regression_candidate,
        )
        print(json.dumps(to_jsonable(annotation), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-report-tags":
        print(json.dumps(to_jsonable(load_workspace_report_tags(open_workspace(args.root))), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-product-issues":
        workspace = open_workspace(args.root)
        if args.write:
            write_product_issues(workspace)
        payload = build_product_issues(workspace)
        if args.format == "markdown":
            print(product_issues_to_markdown(to_jsonable(payload)))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-export-regression-fixture":
        result = export_regression_fixture(
            open_workspace(args.root),
            args.run_id,
            allow_success=args.allow_success,
            overwrite=args.overwrite,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-promote-regression":
        result = promote_regression_fixture(
            open_workspace(args.root),
            args.run_id,
            overwrite=args.overwrite,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-regression-tests":
        print(json.dumps(to_jsonable(list_regression_tests(open_workspace(args.root))), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-run-regression-tests":
        result = run_workspace_regression_tests(
            open_workspace(args.root),
            pytest_args=tuple(args.pytest_arg),
            timeout_seconds=args.timeout_seconds,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.exit_code == 0 else 1
    if args.command == "workspace-queue-submit":
        task = submit_queue_task(
            open_workspace(args.root),
            args.workflow,
            inputs=load_inputs(args.inputs, None) if args.inputs else None,
            inputs_file=args.inputs_file,
            priority=args.priority,
            max_retries=args.max_retries,
            run_profile="approved" if args.allow_click else args.run_profile,
            dry_run=args.run_profile == "dry-run" and not args.allow_click,
        )
        print(json.dumps(to_jsonable(task), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-queue-list":
        print(json.dumps(to_jsonable(list_queue_tasks(open_workspace(args.root), status=args.status)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-queue-cancel":
        task = cancel_queue_task(open_workspace(args.root), args.task_id, reason=args.reason)
        print(json.dumps(to_jsonable(task), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-queue-retry":
        task = retry_queue_task(open_workspace(args.root), args.task_id)
        print(json.dumps(to_jsonable(task), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-queue-run-next":
        result = run_next_queue_task(open_workspace(args.root))
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if not result["ran"] or result["task"]["status"] in {"success", "pending"} else 1
    if args.command == "workspace-queue-worker":
        result = run_queue_worker(
            open_workspace(args.root),
            poll_seconds=args.poll_seconds,
            max_tasks=args.max_tasks,
            max_seconds=args.max_seconds,
            stop_file=args.stop_file,
            once=args.once,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        failed_runs = [
            run for run in result["runs"] if run.get("task") and run["task"].get("status") not in {"success", "pending"}
        ]
        return 1 if failed_runs else 0
    if args.command == "workspace-queue-migrate-sqlite":
        result = migrate_queue_to_sqlite(
            open_workspace(args.root),
            set_backend=not args.no_set_backend,
            backup_json=not args.no_backup,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-queue-rollback-json":
        result = rollback_queue_from_sqlite(
            open_workspace(args.root),
            set_backend=not args.no_set_backend,
            backup_json=not args.no_backup,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-record-browser":
        try:
            result = record_browser_session(
                open_workspace(args.root),
                url=args.url,
                save_as=args.save_as,
                timeout_seconds=args.timeout_seconds,
                headed=not args.headless,
                assert_text=args.assert_text,
                auto_assert=not args.no_auto_assert,
                save_auth_state=args.save_auth_state,
                check=not args.no_check,
                preview_run=args.preview_run,
                overwrite=args.overwrite,
                queue_run=args.queue,
                queue_priority=args.queue_priority,
                queue_max_retries=args.queue_max_retries,
            )
        except BrowserRecordingError as exc:
            if args.format == "markdown":
                print(recording_failure_to_markdown(to_jsonable(exc.failure_report)))
            else:
                print(json.dumps(to_jsonable(exc.failure_report), ensure_ascii=False, indent=2))
            return 1
        payload = recorded_result_to_dict(result)
        if args.format == "markdown":
            print(recorded_result_to_markdown(to_jsonable(payload)))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if recorded_result_ok(payload) else 1
    if args.command == "workspace-planner-context":
        print(json.dumps(to_jsonable(planner_context(open_workspace(args.root), run_limit=args.run_limit)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-check-plan":
        workspace = open_workspace(args.root)
        workflow_path = Path(args.file)
        if not workflow_path.is_absolute():
            workflow_path = workspace.root / workflow_path
            if not workflow_path.exists():
                workflow_path = workspace.workflows_dir / args.file
        result = check_planner_draft(
            parse_workflow_file(workflow_path),
            workspace=workspace,
            allow_high_risk=args.allow_high_risk,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.valid else 1
    if args.command == "workspace-planner-draft":
        result = generate_planner_draft(
            open_workspace(args.root),
            args.instruction,
            source=args.source,
            preferred_provider=args.preferred,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            max_completion_tokens=args.max_completion_tokens,
            execute=args.run,
        )
        if args.preview_save and not args.save_as:
            result = {**result, "save": {"requested": True, "status": "blocked", "path": None, "reason": "missing_save_as"}}
        elif args.preview_save:
            result = preview_planner_draft_save(open_workspace(args.root), result, args.save_as)
        elif args.save_as:
            result = save_planner_draft_result(open_workspace(args.root), result, args.save_as, overwrite=args.overwrite)
        if args.format == "markdown":
            print(planner_draft_result_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        if args.save_as or args.preview_save:
            save = result.get("save") if isinstance(result.get("save"), dict) else {}
            return 0 if save.get("status") in {"saved", "previewed"} else 1
        return 0 if result.get("status") in {"planned", "valid"} else 1
    if args.command == "templates":
        print(json.dumps(to_jsonable(list_templates()), ensure_ascii=False, indent=2))
        return 0
    if args.command == "install-template":
        result = install_template(open_workspace(args.root), args.template, overwrite=args.overwrite)
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    return 2


def workflow_generation_cli_payload(result: Any, changes: tuple[Any, ...]) -> dict[str, Any]:
    from .context_ingestion import summarize_data_displays

    quality = result.quality_score
    model = result.semantic_model
    display_summary = summarize_data_displays(model)
    return {
        "status": result.status,
        "workflow_name": result.workflow_name,
        "workflow_path": result.workflow_path,
        "inputs_path": result.inputs_path,
        "negative_workflow_path": result.negative_workflow_path,
        "negative_workflow_ready": result.negative_workflow_ready,
        "negative_workflow_reason": result.negative_workflow_reason,
        "negative_workflow_reset_strategy": result.negative_workflow_reset_strategy,
        "negative_oracles": list(result.negative_oracles),
        "generation_method": result.generation_method,
        "changed_files": [change.file_path for change in changes],
        "quality": {
            "score": quality.total_score,
            "covers_success_path": quality.covers_success_path,
            "covers_error_path": quality.covers_error_path,
            "business_assertions": quality.business_assertion_count,
            "data_display_assertions": quality.data_display_assertion_count,
            "forbidden_error_assertions": quality.forbidden_error_assertion_count,
            "text_from_input_references": quality.text_from_input_reference_count,
            "invalid_text_from_references": list(quality.invalid_text_from_references),
            "gaps": list(quality.gaps),
            "recommendation": quality.recommendation,
        },
        "framework_detected": model.framework,
        "confidence": model.confidence,
        "fields": [field.name for field in model.form_fields],
        "success_states": [state.value for state in model.success_states],
        "semantic_summary": {
            "framework": model.framework,
            "confidence": model.confidence,
            "generation_method": result.generation_method,
            "field_count": len(model.form_fields),
            "required_field_count": sum(1 for field in model.form_fields if field.required),
            "sensitive_field_count": sum(1 for field in model.form_fields if field.is_sensitive),
            "validation_rule_count": sum(len(field.validation_rules) for field in model.form_fields),
            "submit_action_count": len(model.submit_actions),
            "success_state_count": len(model.success_states),
            "error_state_count": len(model.error_states),
            "data_display_count": len(model.data_displays),
            "matched_data_displays": list(display_summary.matched),
            "unmatched_data_displays": list(display_summary.unmatched),
            "negative_input_case_count": len(result.negative_input_cases),
            "fields": [field.name for field in model.form_fields],
            "success_states": [state.value for state in model.success_states],
            "data_displays": list(model.data_displays),
            "warnings": list(result.warnings),
        },
        "negative_input_cases": list(result.negative_input_cases),
        "negative_workflow_yaml": result.negative_workflow_yaml if result.workflow_path is None else None,
        "generation_trace": list(result.generation_trace[:10]),
        "warnings": list(result.warnings),
        "message": result.message,
        "yaml": result.workflow_yaml if result.workflow_path is None else None,
    }


def detect_framework_from_dir(root: Path) -> str | None:
    package_json = root / "package.json"
    if package_json.exists():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            package = {}
        deps: dict[str, Any] = {}
        if isinstance(package, dict):
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                value = package.get(key)
                if isinstance(value, dict):
                    deps.update(value)
        dep_names = set(deps)
        if "next" in dep_names:
            return "nextjs"
        if any(name.startswith("@remix-run/") for name in dep_names):
            return "remix"
        if "vue" in dep_names:
            return "vue"
        if "react" in dep_names or "react-dom" in dep_names:
            return "react"
    if (root / "manage.py").exists():
        return "django"
    requirements = root / "requirements.txt"
    if requirements.exists():
        text = requirements.read_text(encoding="utf-8", errors="ignore").lower()
        if "django" in text:
            return "django"
        if "fastapi" in text:
            return "fastapi"
        if "flask" in text:
            return "flask"
    if any(root.rglob("*.vue")):
        return "vue"
    if any(root.rglob("*.tsx")) or any(root.rglob("*.jsx")):
        return "react"
    if any(root.rglob("*.html")):
        return "html"
    return None


def generate_from_diff_cli_markdown(payload: dict[str, Any]) -> str:
    semantic = payload.get("semantic_summary") if isinstance(payload.get("semantic_summary"), dict) else {}
    quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
    lines = [
        f"[generate-from-diff] Status: {payload.get('status')}",
        (
            "[generate-from-diff] Framework: "
            f"{semantic.get('framework') or payload.get('framework_detected')}  "
            f"Confidence: {semantic.get('confidence') or payload.get('confidence')}  "
            f"Method: {payload.get('generation_method')}"
        ),
        (
            "[generate-from-diff] Fields: "
            f"{semantic.get('field_count', 0)} (required: {semantic.get('required_field_count', 0)})  "
            f"Success states: {semantic.get('success_state_count', 0)}  "
            f"Data displays: {semantic.get('data_display_count', 0)}"
        ),
    ]
    if payload.get("workflow_path"):
        lines.append(f"[generate-from-diff] Workflow: {payload['workflow_path']}")
    if payload.get("inputs_path"):
        lines.append(f"[generate-from-diff] Inputs: {payload['inputs_path']}")
    if quality:
        lines.append(f"[generate-from-diff] Quality: {quality.get('score')}")
    warnings = semantic.get("warnings") if isinstance(semantic.get("warnings"), list) else payload.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.append("")
        lines.append("Parse warnings (" + str(len(warnings)) + "):")
        for warning in warnings:
            lines.append(f"  - {warning}")
    return "\n".join(lines)


def verify_impl_cli_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"[verify-impl] Result: {payload.get('result')}",
        f"[verify-impl] Workflow: {payload.get('workflow_name')}",
        f"[verify-impl] Quality: {payload.get('quality_score')}",
    ]
    if payload.get("run_id"):
        lines.append(f"[verify-impl] Run: {payload['run_id']}")
    if payload.get("report_path"):
        lines.append(f"[verify-impl] Report: {payload['report_path']}")
    if payload.get("inputs_path"):
        lines.append(f"[verify-impl] Inputs: {payload['inputs_path']}")
    if payload.get("inputs_source"):
        lines.append(f"[verify-impl] Inputs source: {payload['inputs_source']}")
    trace = payload.get("generation_trace") if isinstance(payload.get("generation_trace"), list) else []
    if trace:
        lines.append("[verify-impl] Generation trace: " + "; ".join(str(item) for item in trace[:5]))
    semantic = payload.get("semantic_summary") if isinstance(payload.get("semantic_summary"), dict) else {}
    if semantic:
        lines.append(
            "[verify-impl] Semantics: "
            f"{semantic.get('framework')} confidence={semantic.get('confidence')} "
            f"fields={semantic.get('field_count')} required={semantic.get('required_field_count')} "
            f"success_states={semantic.get('success_state_count')} data_displays={semantic.get('data_display_count')} "
            f"negative_cases={semantic.get('negative_input_case_count')}"
        )
        warnings = semantic.get("warnings") if isinstance(semantic.get("warnings"), list) else []
        if warnings:
            lines.append("[verify-impl] Parse warnings:")
            for warning in warnings:
                lines.append(f"  - {warning}")
    quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
    gaps = quality.get("gaps") if isinstance(quality.get("gaps"), list) else []
    if gaps:
        lines.append("[verify-impl] Quality gaps: " + "; ".join(str(item) for item in gaps))
    if quality.get("recommendation"):
        lines.append(f"[verify-impl] Recommendation: {quality['recommendation']}")
    negative = payload.get("negative_verification") if isinstance(payload.get("negative_verification"), dict) else {}
    if negative:
        lines.append(
            "[verify-impl] Negative: "
            f"{negative.get('status')} workflow={negative.get('workflow_name') or ''} "
            f"run={negative.get('run_id') or ''}"
        )
        if negative.get("reason"):
            lines.append(f"[verify-impl] Negative reason: {negative['reason']}")
        if negative.get("reset_strategy"):
            lines.append(f"[verify-impl] Negative reset: {negative['reset_strategy']}")
        oracles = negative.get("oracles") if isinstance(negative.get("oracles"), list) else []
        if oracles:
            lines.append(f"[verify-impl] Negative oracles: {len(oracles)}")
        if negative.get("report_hint"):
            lines.append(f"[verify-impl] Negative report: {negative['report_hint']}")
        if negative.get("next_action"):
            lines.append(f"[verify-impl] Negative next: {negative['next_action']}")
    failed_step = payload.get("failed_step") if isinstance(payload.get("failed_step"), dict) else None
    if failed_step:
        lines.append(f"[verify-impl] Failed at {failed_step.get('id')} ({failed_step.get('action')})")
        if failed_step.get("actual"):
            lines.append(f"  Actual: {failed_step['actual']}")
        if failed_step.get("fix_hint"):
            lines.append(f"  Fix: {failed_step['fix_hint']}")
    elif payload.get("message"):
        lines.append(str(payload["message"]))
    if payload.get("next_action"):
        lines.append(f"[verify-impl] Next: {payload['next_action']}")
    return "\n".join(lines)


def usage_status_to_markdown(payload: dict[str, Any]) -> str:
    license_ = payload.get("license") if isinstance(payload.get("license"), dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    feature_access = payload.get("feature_access") if isinstance(payload.get("feature_access"), dict) else {}
    cloud_config = payload.get("cloud_config") if isinstance(payload.get("cloud_config"), dict) else {}
    lines = [
        "# Visual Agent Usage",
        "",
        f"- Workspace: `{payload.get('workspace')}`",
        f"- License tier: `{license_.get('tier') or 'free'}`",
        f"- License source: `{license_.get('source') or 'default'}`",
        f"- License key present: `{bool(license_.get('key_present', False))}`",
        f"- Local runs this month: `{usage.get('runs_this_month', 0)}`",
        f"- Cloud runs used: `{usage.get('cloud_runs_used', 0)}`",
        f"- Reset month: `{usage.get('usage_reset_date') or ''}`",
        "",
        "## Cloud Config",
        f"- Ready: `{bool(cloud_config.get('available', False))}`",
        f"- Endpoint: `{cloud_config.get('endpoint') or ''}`",
        f"- API key present: `{bool(cloud_config.get('api_key_present', False))}`",
        f"- Org: `{cloud_config.get('org') or ''}`",
        f"- Network probe: `{cloud_config.get('network_probe') or 'not_run'}`",
    ]
    blockers = cloud_config.get("blockers") if isinstance(cloud_config.get("blockers"), list) else []
    if blockers:
        lines.append(f"- Blockers: {', '.join(str(item) for item in blockers)}")
    lines.extend(
        [
            "",
        "## Feature Access",
        ]
    )
    for name in ("cloud_run", "team_workspace", "workflow_history_unlimited"):
        lines.append(f"- {name}: `{bool(feature_access.get(name, False))}`")
    return "\n".join(lines)


def cloud_run_plan_to_markdown(payload: dict[str, Any]) -> str:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    config = request.get("cloud_config") if isinstance(request.get("cloud_config"), dict) else {}
    diagnostic = payload.get("adapter_diagnostic") if isinstance(payload.get("adapter_diagnostic"), dict) else {}
    lines = [
        "# Cloud Run Plan",
        "",
        f"- Workspace: `{payload.get('workspace')}`",
        f"- Workflow: `{payload.get('workflow_name')}`",
        f"- Request status: `{request.get('status') or 'blocked'}`",
        f"- Run profile: `{request.get('run_profile') or ''}`",
        f"- Inputs file: `{request.get('inputs_file') or ''}`",
        f"- Network probe: `{request.get('network_probe') or 'not_run'}`",
        "",
        "## Cloud Config",
        f"- Ready: `{bool(config.get('available', False))}`",
        f"- Endpoint: `{config.get('endpoint') or ''}`",
        f"- API key present: `{bool(config.get('api_key_present', False))}`",
        f"- Org: `{config.get('org') or ''}`",
    ]
    blockers = config.get("blockers") if isinstance(config.get("blockers"), list) else []
    if blockers:
        lines.append(f"- Blockers: {', '.join(str(item) for item in blockers)}")
    lines.extend(
        [
            "",
            "## Adapter Diagnostic",
            f"- Status: `{diagnostic.get('status') or 'blocked'}`",
            f"- Message: {diagnostic.get('message') or ''}",
        ]
    )
    return "\n".join(lines)


def cloud_run_to_markdown(payload: dict[str, Any]) -> str:
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    config = request.get("cloud_config") if isinstance(request.get("cloud_config"), dict) else {}
    diagnostic = payload.get("adapter_diagnostic") if isinstance(payload.get("adapter_diagnostic"), dict) else {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    lines = [
        "# Cloud Run",
        "",
        f"- Workspace: `{payload.get('workspace')}`",
        f"- Workflow: `{payload.get('workflow_name')}`",
        f"- Execution requested: `{bool(payload.get('execution_requested', False))}`",
        f"- Transport: `{payload.get('transport') or 'none'}`",
        f"- Network sent: `{bool(payload.get('network_sent', False))}`",
        f"- Request status: `{request.get('status') or 'blocked'}`",
        f"- Run profile: `{request.get('run_profile') or ''}`",
        f"- Inputs file: `{request.get('inputs_file') or ''}`",
        "",
        "## Cloud Config",
        f"- Ready: `{bool(config.get('available', False))}`",
        f"- Endpoint: `{config.get('endpoint') or ''}`",
        f"- API key present: `{bool(config.get('api_key_present', False))}`",
        f"- Org: `{config.get('org') or ''}`",
    ]
    blockers = config.get("blockers") if isinstance(config.get("blockers"), list) else []
    if blockers:
        lines.append(f"- Blockers: {', '.join(str(item) for item in blockers)}")
    if diagnostic:
        lines.extend(
            [
                "",
                "## Adapter Diagnostic",
                f"- Status: `{diagnostic.get('status') or 'blocked'}`",
                f"- Message: {diagnostic.get('message') or ''}",
            ]
        )
    if result:
        lines.extend(
            [
                "",
                "## Execution Result",
                f"- Status: `{result.get('status') or 'blocked'}`",
                f"- Run id: `{result.get('run_id') or ''}`",
                f"- Report URL: `{result.get('report_url') or ''}`",
                f"- Usage recorded: `{bool(result.get('usage_recorded', False))}`",
                f"- Message: {result.get('message') or ''}",
            ]
        )
    return "\n".join(lines)


def load_inputs(raw_inputs: str | None, inputs_file: str | None) -> dict:
    if raw_inputs and inputs_file:
        raise ValueError("Use either --inputs or --inputs-file, not both.")
    if inputs_file:
        return json.loads(Path(inputs_file).read_text(encoding="utf-8-sig"))
    if raw_inputs:
        return json.loads(raw_inputs)
    return {}


def parse_csv_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def parse_optional_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.lower() == "true"


if __name__ == "__main__":
    raise SystemExit(main())
