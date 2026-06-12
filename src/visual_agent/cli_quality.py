from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .models import to_jsonable
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
    quality_gate_to_junit_xml,
    release_check_plan_to_markdown,
    release_trial_to_markdown,
    run_demo_workspace_check,
    run_mcp_smoke_check,
    run_quality_gate,
    run_release_trial,
    write_quality_gate_step_summary,
)
from .run_profile import SAFE_RUN_PROFILE_CHOICES


QUALITY_COMMANDS = {
    "quality-gate",
    "quality-gate-reports",
    "quality-gate-index",
    "release-check",
    "release-trial",
    "install-check",
    "mcp-client-config",
    "coding-agent-brief",
    "mcp-smoke",
    "demo-workspace-check",
}


def add_quality_parsers(subparsers: argparse._SubParsersAction[Any]) -> None:
    quality_gate = subparsers.add_parser("quality-gate", help="Show or run local/CI quality gates.")
    quality_gate.add_argument("--profile", choices=["local", "ci"], default="local", help="Quality profile. Default: local.")
    quality_gate.add_argument("--workspace-root", help="Optional workspace root for workspace regression tests.")
    quality_gate.add_argument("--run", action="store_true", help="Execute the quality gate. Default only prints the plan.")
    quality_gate.add_argument("--timeout-seconds", type=float, default=300.0, help="Timeout per step. Default: 300.")
    quality_gate.add_argument("--report-root", help="Optional report output directory.")
    quality_gate.add_argument("--ci", action="store_true", help="Emit JUnit XML for CI consumption instead of JSON.")
    quality_gate.add_argument("--junit-output", default=None, help="Optional JUnit XML output path when --ci is set.")
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

    release_trial = subparsers.add_parser("release-trial", help="Run the real trial validation bundle on a workspace.")
    release_trial.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root to initialize and validate.")
    release_trial.add_argument("--overwrite", action="store_true", help="Overwrite demo files before running.")
    release_trial.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="supervised", help="Demo/cloud run profile. Default: supervised.")
    release_trial.add_argument("--cloud-org", default="team-a", help="Org header used for local cloud execution.")
    release_trial.add_argument("--cloud-user", default="release-trial", help="User header used for local cloud execution.")
    release_trial.add_argument("--cloud-api-key", default="release-trial-key", help="Bearer token used for local cloud execution.")
    release_trial.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

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
    demo_workspace_check.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run", help="Execution profile. Use supervised for the browser demo path.")
    demo_workspace_check.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")


def handle_quality_command(args: Any, *, release_trial_runner: Any = None) -> int:
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
        if args.ci:
            junit_xml = quality_gate_to_junit_xml(result)
            if args.junit_output:
                output_path = Path(args.junit_output).expanduser().resolve()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(junit_xml, encoding="utf-8")
            write_quality_gate_step_summary(result, junit_output=args.junit_output)
            print(junit_xml)
        else:
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
            print(json.dumps(to_jsonable(reports), ensure_ascii=False, indent=2))
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
            print(json.dumps(to_jsonable(index), ensure_ascii=False, indent=2))
        return 0
    if args.command == "release-check":
        plan = build_release_check_plan(workspace_root=args.workspace_root)
        if args.format == "markdown":
            print(release_check_plan_to_markdown(plan))
        else:
            print(json.dumps(to_jsonable(plan), ensure_ascii=False, indent=2))
        return 0
    if args.command == "release-trial":
        runner = release_trial_runner or run_release_trial
        result = runner(
            workspace_root=args.workspace_root,
            overwrite=args.overwrite,
            run_profile=args.run_profile,
            cloud_org=args.cloud_org,
            cloud_user=args.cloud_user,
            cloud_api_key=args.cloud_api_key,
        )
        if args.format == "markdown":
            print(release_trial_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
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
        result = run_demo_workspace_check(root=args.root, overwrite=args.overwrite, run_profile=args.run_profile)
        if args.format == "markdown":
            print(demo_workspace_check_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
    raise ValueError(f"Unsupported quality command: {args.command}")


def parse_optional_bool(value: str | None) -> bool | None:
    if value is None or str(value).strip() == "":
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected boolean value, got: {value}")
