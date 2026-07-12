"""Workspace CLI command handlers — consolidated from 5 thin modules."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .models import to_jsonable


# ── command sets (used by cli.py for routing) ────────────────────────────────

WORKSPACE_QUEUE_COMMANDS = {
    "workspace-queue-submit",
    "workspace-queue-list",
    "workspace-queue-cancel",
    "workspace-queue-retry",
    "workspace-queue-run-next",
    "workspace-queue-worker",
    "workspace-queue-migrate-sqlite",
    "workspace-queue-rollback-json",
}

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

WORKSPACE_RECORD_COMMANDS = {"workspace-record-browser"}

WORKSPACE_RUN_COMMANDS = {"workspace-run"}

WORKSPACE_MANAGE_COMMANDS = {
    "workspace-tag-report",
    "workspace-product-issues",
    "workspace-export-regression-fixture",
    "workspace-promote-regression",
    "workspace-regression-tests",
    "workspace-run-regression-tests",
    "workspace-planner-context",
    "workspace-check-plan",
    "workspace-planner-draft",
    "templates",
    "install-template",
}


# ── queue ─────────────────────────────────────────────────────────────────────

def handle_workspace_queue_command(args: Any) -> int:
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
    from .workspace import open_workspace

    if args.command == "workspace-queue-submit":
        task = submit_queue_task(
            open_workspace(args.root),
            args.workflow,
            inputs=_load_inline_inputs(args.inputs) if args.inputs else None,
            inputs_file=args.inputs_file,
            priority=args.priority,
            max_retries=args.max_retries,
            run_profile="approved" if args.allow_click else args.run_profile,
            dry_run=args.run_profile == "dry-run" and not args.allow_click,
        )
        _print_json(task)
        return 0
    if args.command == "workspace-queue-list":
        _print_json(list_queue_tasks(open_workspace(args.root), status=args.status))
        return 0
    if args.command == "workspace-queue-cancel":
        task = cancel_queue_task(open_workspace(args.root), args.task_id, reason=args.reason)
        _print_json(task)
        return 0
    if args.command == "workspace-queue-retry":
        task = retry_queue_task(open_workspace(args.root), args.task_id)
        _print_json(task)
        return 0
    if args.command == "workspace-queue-run-next":
        result = run_next_queue_task(open_workspace(args.root))
        _print_json(result)
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
        _print_json(result)
        failed_runs = [
            run for run in result["runs"]
            if run.get("task") and run["task"].get("status") not in {"success", "pending"}
        ]
        return 1 if failed_runs else 0
    if args.command == "workspace-queue-migrate-sqlite":
        result = migrate_queue_to_sqlite(
            open_workspace(args.root),
            set_backend=not args.no_set_backend,
            backup_json=not args.no_backup,
        )
        _print_json(result)
        return 0
    if args.command == "workspace-queue-rollback-json":
        result = rollback_queue_from_sqlite(
            open_workspace(args.root),
            set_backend=not args.no_set_backend,
            backup_json=not args.no_backup,
        )
        _print_json(result)
        return 0
    raise ValueError(f"Unsupported workspace queue command: {args.command}")


# ── read ──────────────────────────────────────────────────────────────────────

def handle_workspace_read_command(args: Any) -> int:
    from .console import build_report_detail, build_workspace_dashboard, dashboard_to_markdown, report_detail_to_markdown
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

    if args.command == "workspace-status":
        print(json.dumps(workspace_status(open_workspace(args.root)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-dashboard":
        dashboard = build_workspace_dashboard(open_workspace(args.root), limit=args.limit)
        _print_payload(dashboard, args.format, markdown=dashboard_to_markdown)
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
        from .workspace import list_workspace_reports
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
        _print_payload(detail, args.format, markdown=report_detail_to_markdown)
        return 0
    if args.command == "workspace-report-tags":
        print(json.dumps(to_jsonable(load_workspace_report_tags(open_workspace(args.root))), ensure_ascii=False, indent=2))
        return 0
    raise ValueError(f"Unsupported workspace read command: {args.command}")


# ── record ────────────────────────────────────────────────────────────────────

def handle_workspace_record_command(args: Any, *, recorder: Callable[..., Any] | None = None) -> int:
    from .recorder import (
        BrowserRecordingError,
        record_browser_session,
        recorded_result_ok,
        recorded_result_to_dict,
        recorded_result_to_markdown,
        recording_failure_to_markdown,
    )

    from .workspace import open_workspace
    if recorder is None:
        recorder = record_browser_session
    try:
        result = recorder(
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


# ── run ───────────────────────────────────────────────────────────────────────

def handle_workspace_run_command(args: Any) -> int:
    from .preflight import run_preflight
    from .reports import load_run_report, run_report_to_markdown
    from .workflow import parse_workflow_file
    from .workspace import (
        find_workflow,
        load_workspace_inputs,
        open_workspace,
        run_workspace_workflow,
        validate_workflow_inputs,
    )

    workspace = open_workspace(args.root)
    workflow_ref = find_workflow(workspace, args.workflow)
    workflow = parse_workflow_file(workflow_ref.path)
    inputs = load_workspace_inputs(workspace, args.inputs, args.inputs_file)
    sensitive_fields = _parse_csv_set(args.sensitive_fields)
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
        from_step=args.from_step,
        use_lock=not args.no_lock,
        lock_ttl_seconds=args.lock_ttl_seconds,
        queue_when_locked=args.queue_when_locked,
        lock_wait_seconds=args.lock_wait_seconds,
        lock_poll_seconds=args.lock_poll_seconds,
        export_report=not args.no_report_export,
    )
    if args.format == "markdown":
        print(run_report_to_markdown(load_run_report(result.run_dir)))
    else:
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
    return 0


# ── manage ────────────────────────────────────────────────────────────────────

def handle_workspace_manage_command(args: Any) -> int:
    from .workspace import open_workspace

    if args.command == "workspace-tag-report":
        from .workspace import tag_workspace_report
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
    if args.command == "workspace-product-issues":
        from .product_issues import build_product_issues, product_issues_to_markdown, write_product_issues
        workspace = open_workspace(args.root)
        if args.write:
            write_product_issues(workspace)
        payload = build_product_issues(workspace)
        _print_payload(payload, args.format, markdown=product_issues_to_markdown)
        return 0
    if args.command == "workspace-export-regression-fixture":
        from .workspace import export_regression_fixture
        result = export_regression_fixture(
            open_workspace(args.root), args.run_id,
            allow_success=args.allow_success, overwrite=args.overwrite,
        )
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-promote-regression":
        from .workspace import promote_regression_fixture
        result = promote_regression_fixture(open_workspace(args.root), args.run_id, overwrite=args.overwrite)
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-regression-tests":
        from .workspace import list_regression_tests
        print(json.dumps(to_jsonable(list_regression_tests(open_workspace(args.root))), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-run-regression-tests":
        from .workspace import run_workspace_regression_tests
        result = run_workspace_regression_tests(open_workspace(args.root), pytest_args=tuple(args.pytest_arg), timeout_seconds=args.timeout_seconds)
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.exit_code == 0 else 1
    if args.command == "workspace-planner-context":
        from .workspace import planner_context
        print(json.dumps(to_jsonable(planner_context(open_workspace(args.root), run_limit=args.run_limit)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workspace-check-plan":
        from .planner import check_planner_draft
        from .workflow import parse_workflow_file
        workspace = open_workspace(args.root)
        workflow_path = Path(args.file)
        if not workflow_path.is_absolute():
            workflow_path = workspace.root / workflow_path
            if not workflow_path.exists():
                workflow_path = workspace.workflows_dir / args.file
        result = check_planner_draft(parse_workflow_file(workflow_path), workspace=workspace, allow_high_risk=args.allow_high_risk)
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.valid else 1
    if args.command == "workspace-planner-draft":
        from .planner_generate import generate_planner_draft, planner_draft_result_to_markdown, preview_planner_draft_save, save_planner_draft_result
        result = generate_planner_draft(
            open_workspace(args.root), args.instruction,
            source=args.source, preferred_provider=args.preferred, model=args.model,
            timeout_seconds=args.timeout_seconds, max_completion_tokens=args.max_completion_tokens,
            execute=args.run,
        )
        if args.preview_save and not args.save_as:
            result = {**result, "save": {"requested": True, "status": "blocked", "path": None, "reason": "missing_save_as"}}
        elif args.preview_save:
            result = preview_planner_draft_save(open_workspace(args.root), result, args.save_as)
        elif args.save_as:
            result = save_planner_draft_result(open_workspace(args.root), result, args.save_as, overwrite=args.overwrite)
        _print_payload(result, args.format, markdown=planner_draft_result_to_markdown)
        if args.save_as or args.preview_save:
            save = result.get("save") if isinstance(result.get("save"), dict) else {}
            return 0 if save.get("status") in {"saved", "previewed"} else 1
        return 0 if result.get("status") in {"planned", "valid"} else 1
    if args.command == "templates":
        from .templates import list_templates
        print(json.dumps(to_jsonable(list_templates()), ensure_ascii=False, indent=2))
        return 0
    if args.command == "install-template":
        from .templates import install_template
        result = install_template(open_workspace(args.root), args.template, overwrite=args.overwrite)
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    raise ValueError(f"Unsupported workspace manage command: {args.command}")


# ── shared helpers ────────────────────────────────────────────────────────────

def _print_json(payload: Any) -> None:
    print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))


def _print_payload(payload: Any, fmt: str, *, markdown: Any) -> None:
    if fmt == "markdown":
        print(markdown(payload))
    else:
        print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))


def _load_inline_inputs(raw_inputs: str) -> dict[str, Any]:
    return json.loads(raw_inputs)


def _parse_csv_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}
