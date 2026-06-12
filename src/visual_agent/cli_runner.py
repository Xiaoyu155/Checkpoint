from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .dom import DomProvider
from .models import Target, to_jsonable
from .preflight import run_preflight
from .reports import (
    list_run_summaries,
    load_run_report,
    load_run_summary,
    run_report_to_dict,
    run_report_to_markdown,
    run_summary_to_dict,
)
from .runner import VisualAgentRunner
from .selector import SelectorResolver
from .uia import UIAutomationProvider
from .workflow import WorkflowRuntime, parse_workflow_file
from .workspace import validate_workflow_inputs


RUNNER_COMMANDS = {
    "click",
    "inspect-dom",
    "resolve-dom",
    "browser-smoke",
    "browser-smoke-suite",
    "inspect-uia",
    "resolve-uia",
    "run-workflow",
    "preflight-workflow",
    "validate-workflow",
    "list-runs",
    "show-run",
    "report-run",
}


def handle_runner_command(args: Any, *, load_inputs_func: Any, format_error: Any, parse_csv_set_func: Any, run_progress_func: Any) -> int:
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

        payload = run_browser_smoke_suite(args.file, output_dir=args.output_dir, headed=True if args.headed else None)
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
        workflow_arg = args.workflow or args.file
        if not workflow_arg:
            print(format_error(ValueError("run-workflow requires --workflow."), command="run-workflow"), file=sys.stderr)
            return 1
        workflow_path = Path(workflow_arg).resolve()
        workflow = parse_workflow_file(workflow_path)
        try:
            inputs = load_inputs_func(args.inputs, args.inputs_file)
        except Exception as exc:
            print(format_error(exc, command="run-workflow"), file=sys.stderr)
            return 1
        sensitive_fields = parse_csv_set_func(args.sensitive_fields)
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
        result = run_progress_func(
            WorkflowRuntime(output_dir=args.output_dir),
            workflow,
            dry_run=args.run_profile == "dry-run" and not args.allow_click,
            run_profile="approved" if args.allow_click else args.run_profile,
            synthetic_on_capture_fail=args.synthetic_on_capture_fail,
            inputs=inputs,
            sensitive_fields=sensitive_fields,
            workspace_root=workflow_path.parent.parent if workflow_path.parent.name == "workflows" else workflow_path.parent,
            resume_from=args.resume_from,
            from_step=args.from_step,
            use_lock=not args.no_lock,
            lock_ttl_seconds=args.lock_ttl_seconds,
            queue_when_locked=args.queue_when_locked or args.wait_lock,
            lock_wait_seconds=args.lock_wait_seconds,
            lock_poll_seconds=args.lock_poll_seconds,
        )
        try:
            from .telemetry import record_run
            from .visual_status import append_run_history, write_status_file

            workspace_root = Path(".agent-workspace").resolve()
            workspace_root.mkdir(parents=True, exist_ok=True)
            append_run_history(workspace_root, workflow, result)
            write_status_file(Path.cwd(), result)
            record_run(workspace_root, workflow, result)
        except Exception:
            pass
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "preflight-workflow":
        workflow_path = Path(args.file).resolve()
        result = run_preflight(
            parse_workflow_file(workflow_path),
            strict=args.strict,
            allow_high_risk=args.allow_high_risk,
            workspace_root=args.workspace_root,
        )
        if result.environment:
            from .visual_status import write_environment_status_file

            project_root = Path(args.workspace_root).resolve() if args.workspace_root else workflow_path.parent
            write_environment_status_file(project_root, result.environment)
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1
    if args.command == "validate-workflow":
        from .validation import validate_workflow_file, validate_workflow_file_strict

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
    raise ValueError(f"Unsupported runner command: {args.command}")
