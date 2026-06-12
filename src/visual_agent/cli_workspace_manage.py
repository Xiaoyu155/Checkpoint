from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import to_jsonable
from .planner import check_planner_draft
from .planner_generate import (
    generate_planner_draft,
    planner_draft_result_to_markdown,
    preview_planner_draft_save,
    save_planner_draft_result,
)
from .product_issues import build_product_issues, product_issues_to_markdown, write_product_issues
from .templates import install_template, list_templates
from .workflow import parse_workflow_file
from .workspace import (
    export_regression_fixture,
    list_regression_tests,
    open_workspace,
    planner_context,
    promote_regression_fixture,
    run_workspace_regression_tests,
    tag_workspace_report,
)


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


def handle_workspace_manage_command(args: Any) -> int:
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
    raise ValueError(f"Unsupported workspace manage command: {args.command}")
