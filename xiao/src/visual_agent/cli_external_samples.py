from __future__ import annotations

import argparse
import json
from typing import Any

from .external_samples import (
    build_external_sample_batch_failure_summary,
    build_external_sample_batch_plan,
    build_external_sample_batch_rerun_plan,
    build_external_sample_rerun_plan,
    build_external_sample_run_plan,
    build_external_sample_run_summary,
    check_external_samples,
    external_samples_readiness,
    export_external_sample_batch_report,
    export_external_sample_dry_run_report,
    export_external_sample_live_placeholder,
    list_external_sample_batch_reports,
    list_external_samples,
    load_external_sample_batch_report_index,
    run_external_sample,
    submit_external_sample_batch,
    submit_external_sample_batch_reruns,
    submit_external_sample_reruns,
)
from .models import to_jsonable
from .run_profile import SAFE_RUN_PROFILE_CHOICES
from .workspace import open_workspace


EXTERNAL_SAMPLE_COMMANDS = {
    "external-samples",
    "external-samples-check",
    "external-samples-readiness",
    "external-sample-run-plan",
    "external-sample-run",
    "external-sample-batch-plan",
    "external-sample-batch-submit",
    "external-sample-summary",
    "external-sample-batch-report",
    "external-sample-dry-run-report",
    "external-sample-live-placeholder",
    "external-sample-batch-reports",
    "external-sample-batch-report-index",
    "external-sample-batch-failures",
    "external-sample-batch-rerun-plan",
    "external-sample-batch-rerun-submit",
    "external-sample-rerun-plan",
    "external-sample-rerun-submit",
}


def add_external_sample_parsers(subparsers: argparse._SubParsersAction[Any]) -> None:
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


def handle_external_sample_command(args: Any) -> int:
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
    raise ValueError(f"Unsupported external sample command: {args.command}")
