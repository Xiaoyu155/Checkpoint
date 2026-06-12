from __future__ import annotations

import json
from typing import Any

from .models import to_jsonable
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


WORKSPACE_RUN_COMMANDS = {"workspace-run"}


def handle_workspace_run_command(args: Any) -> int:
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


def parse_csv_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}
