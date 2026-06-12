from __future__ import annotations

import json
from typing import Any

from .console import build_report_detail, build_workspace_dashboard, dashboard_to_markdown, report_detail_to_markdown
from .models import to_jsonable
from .workspace import (
    discover_workflows,
    list_workspace_reports,
    load_workspace_report_index,
    load_workspace_report_tags,
    open_workspace,
    validate_workspace,
    workspace_run_summaries,
    workspace_status,
)


WORKSPACE_READ_COMMANDS = {
    "workspace-status",
    "workspace-dashboard",
    "workspace-list",
    "workspace-validate",
    "workspace-runs",
    "workspace-reports",
    "workspace-report-index",
    "workspace-report-detail",
    "workspace-report-tags",
}


def handle_workspace_read_command(args: Any) -> int:
    if args.command == "workspace-status":
        print(json.dumps(workspace_status(open_workspace(args.root)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-dashboard":
        dashboard = build_workspace_dashboard(open_workspace(args.root), limit=args.limit)
        print_payload(dashboard, args.format, markdown=dashboard_to_markdown)
        return 0
    if args.command == "workspace-list":
        refs = discover_workflows(open_workspace(args.root), include_slow=args.include_slow)
        print(json.dumps(to_jsonable(refs), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-validate":
        results = validate_workspace(open_workspace(args.root), strict=args.strict, allow_high_risk=args.allow_high_risk)
        print(json.dumps(to_jsonable(results), ensure_ascii=False, indent=2))
        return 0 if all(result.valid for result in results) else 1
    if args.command == "workspace-runs":
        summaries = workspace_run_summaries(open_workspace(args.root), limit=args.limit)
        print(json.dumps(to_jsonable(summaries), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-reports":
        print(json.dumps(to_jsonable(list_workspace_reports(open_workspace(args.root))), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-report-index":
        index = load_workspace_report_index(
            open_workspace(args.root),
            rebuild=args.rebuild,
            status=args.status,
            workflow=args.workflow,
            failed_only=args.failed_only,
        )
        print(json.dumps(to_jsonable(index), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-report-detail":
        detail = build_report_detail(open_workspace(args.root), args.run_id)
        print_payload(detail, args.format, markdown=report_detail_to_markdown)
        return 0
    if args.command == "workspace-report-tags":
        print(json.dumps(to_jsonable(load_workspace_report_tags(open_workspace(args.root))), ensure_ascii=False, indent=2))
        return 0
    raise ValueError(f"Unsupported workspace read command: {args.command}")


def print_payload(payload: dict[str, Any], fmt: str, *, markdown: Any) -> None:
    if fmt == "markdown":
        print(markdown(payload))
    else:
        print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
