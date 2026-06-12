from __future__ import annotations

import json
import re
import sys
import argparse
from pathlib import Path
from typing import Any, Callable

from .models import to_jsonable
from .run_profile import RUN_PROFILE_CHOICES
from .validation import validate_workflow_file
from .workflow import parse_workflow_file
from .workspace import discover_workflows, find_workflow, load_workspace_inputs, open_workspace


WORKFLOW_COMMANDS = {
    "generate-workflow",
    "workflow-lint",
    "workflow-add-step",
    "generate-from-diff",
    "verify-impl",
    "agent-status",
    "list-workflows",
    "search-workflows",
    "share-workflow",
    "withdraw-workflow",
    "publish-workflow",
}


FormatError = Callable[[Exception | str], str]
SuggestError = Callable[[str], str]


def add_workflow_parsers(subparsers: argparse._SubParsersAction[Any]) -> None:
    gen_workflow = subparsers.add_parser("generate-workflow", help="Generate a workflow YAML from a natural language description.")
    gen_workflow.add_argument("--description", help="Natural language description of the workflow.")
    gen_workflow.add_argument("--output", default=None, help="Output YAML file path. Default: auto-named in workflows/.")
    gen_workflow.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root used to place generated workflows.")
    gen_workflow.add_argument("--model", default="claude-haiku-4-5-20251001", help="LLM model to use for generation.")
    gen_workflow.add_argument("--page-type", choices=["auth", "form", "list", "detail", "ecommerce"], default=None, help="Optional page type hint for few-shot selection.")
    gen_workflow.add_argument("--url", default=None, help="Entry URL to use for the first observe_browser step.")
    gen_workflow.add_argument("--from-existing", default=None, help="Existing workflow name or path used to generate a variant.")
    gen_workflow.add_argument("--variant", choices=["mobile"], default=None, help="Variant to generate with --from-existing.")
    gen_workflow.add_argument("--from-sitemap", default=None, help="Sitemap XML path used to batch-generate smoke workflows.")
    gen_workflow.add_argument("--limit", type=int, default=50, help="Maximum sitemap URLs to generate. Default: 50.")
    gen_workflow.add_argument("--dry-run", action="store_true", help="Print generated YAML without saving.")
    gen_workflow.add_argument("--format", choices=["json", "yaml"], default="json", help="Output format. Default: json.")

    workflow_lint = subparsers.add_parser("workflow-lint", help="Lint workflow structure and verification quality.")
    workflow_lint.add_argument("workflow", nargs="?", help="Workflow YAML/JSON path.")
    workflow_lint.add_argument("--file", dest="workflow_file", default=None, help="Workflow YAML/JSON path.")
    workflow_lint.add_argument("--min-quality-score", type=float, default=0.6, help="Minimum acceptable quality score. Default: 0.6.")
    workflow_lint.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    workflow_add_step = subparsers.add_parser("workflow-add-step", help="Insert a workflow step after an existing step id.")
    workflow_add_step.add_argument("--workflow", required=True, help="Workflow YAML path to modify.")
    workflow_add_step.add_argument("--after", required=True, help="Existing step id after which the new step is inserted.")
    workflow_add_step.add_argument("--action", required=True, help="Action for the new step, for example wait_for_text or assert_text.")
    workflow_add_step.add_argument("--id", dest="step_id", default=None, help="Optional id for the new step. Default is generated from action.")
    workflow_add_step.add_argument("--text", default=None, help="Text parameter for wait/assert actions.")
    workflow_add_step.add_argument("--url-contains", default=None, help="URL fragment for wait_for.")
    workflow_add_step.add_argument("--timeout-ms", type=int, default=None, help="timeout_ms parameter for wait actions.")
    workflow_add_step.add_argument("--observation", default=None, help="Observation id to attach to the new step.")
    workflow_add_step.add_argument("--dry-run", action="store_true", help="Preview the insertion without writing the workflow.")
    workflow_add_step.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

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
    gen_from_diff.add_argument("--audit-log", default=None, help="Append a JSONL parser audit entry for this generation run.")
    gen_from_diff.add_argument("--format", choices=["json", "markdown", "yaml"], default="json", help="Output format. Default: json.")

    verify_impl = subparsers.add_parser(
        "verify-impl",
        help="Generate a workflow from git diff context and run it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  checkpoint verify-impl --task-description \"Verify login redirects\" --base-url http://127.0.0.1:5173 --run-profile dry-run --no-untracked\n"
            "  checkpoint verify-impl --task-description \"Verify profile form\" --base-url fixtures/profile.html --run-profile dry-run --no-untracked\n"
            "  checkpoint workspace-run --root .agent-workspace --workflow checkout_verification --run-profile dry-run --format markdown\n"
            "\n"
            "Use verify-impl to draft or explore a workflow from git diff context. For stable contract regression, run a hand-written workflow with workspace-run or verify --workflow.\n"
        ),
    )
    verify_impl.add_argument("--task-description", required=True, help="Task or feature that the code changes implement.")
    verify_impl.add_argument("--base-url", default=None, help="URL or local fixture path used as workflow entry point. If omitted, inferred from project config or workspace fixtures.")
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

    list_workflows_cmd = subparsers.add_parser("list-workflows", help="List indexed workflows.")
    list_workflows_cmd.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflow_index.json.")
    list_workflows_cmd.add_argument("--visibility", choices=["public", "private"], default=None, help="Filter by workflow visibility.")
    list_workflows_cmd.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    search_workflows_cmd = subparsers.add_parser("search-workflows", help="Search indexed workflows by name, description, or tags.")
    search_workflows_cmd.add_argument("query", nargs="?", default="", help="Search query.")
    search_workflows_cmd.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflow_index.json.")
    search_workflows_cmd.add_argument("--visibility", choices=["public", "private"], default=None, help="Filter by workflow visibility.")
    search_workflows_cmd.add_argument("--limit", type=int, default=20, help="Maximum results. Default: 20.")
    search_workflows_cmd.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    share_workflow = subparsers.add_parser("share-workflow", help="Mark a workflow public in the local workflow library.")
    share_workflow.add_argument("--name", required=True, help="Workflow name to share.")
    share_workflow.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflows.")
    share_workflow.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    withdraw_workflow = subparsers.add_parser("withdraw-workflow", help="Mark a workflow private in the local workflow library.")
    withdraw_workflow.add_argument("--name", required=True, help="Workflow name to withdraw.")
    withdraw_workflow.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflows.")
    withdraw_workflow.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    publish_workflow = subparsers.add_parser("publish-workflow", help="Validate and publish a workflow into the local public catalog.")
    publish_workflow.add_argument("--name", required=True, help="Workflow name to publish.")
    publish_workflow.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root containing workflows.")
    publish_workflow.add_argument("--min-quality-score", type=float, default=0.6, help="Minimum quality score required for publishing. Default: 0.6.")
    publish_workflow.add_argument(
        "--catalog-url-base",
        default="https://visualagent.local/workflows",
        help="Base URL used to generate the published workflow URL.",
    )
    publish_workflow.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")


def handle_workflow_command(
    args: Any,
    *,
    format_error: Callable[..., str],
    cli_error_suggestion: Callable[..., str],
) -> int:
    if args.command == "generate-workflow":
        from .workflow_generator import generate_workflow_yaml, generate_workflow_variant, generate_workflows_from_sitemap

        workspace_root = Path(args.workspace_root).resolve()
        try:
            if args.from_existing:
                result = generate_workflow_variant(
                    workspace_root=workspace_root,
                    existing=args.from_existing,
                    variant=args.variant or "mobile",
                    output_path=Path(args.output).resolve() if args.output else None,
                    dry_run=args.dry_run,
                )
            elif args.from_sitemap:
                result = generate_workflows_from_sitemap(
                    sitemap_path=Path(args.from_sitemap).resolve(),
                    workspace_root=workspace_root,
                    output_dir=Path(args.output).resolve() if args.output else None,
                    dry_run=args.dry_run,
                    limit=args.limit,
                )
            else:
                if not args.description:
                    raise ValueError("generate-workflow requires --description, --from-existing, or --from-sitemap.")
                result = generate_workflow_yaml(
                    description=args.description,
                    workspace_root=workspace_root,
                    output_path=Path(args.output).resolve() if args.output else None,
                    model=args.model,
                    dry_run=args.dry_run,
                    page_type=args.page_type,
                    url=args.url,
                )
        except (ValueError, FileNotFoundError, RuntimeError, OSError) as exc:
            print(format_error(exc, command="generate-workflow"), file=sys.stderr)
            return 1
        if args.format == "yaml" and result.get("yaml"):
            print(result["yaml"])
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1

    if args.command == "workflow-lint":
        workflow_path = args.workflow_file or args.workflow
        if not workflow_path:
            print(format_error(ValueError("workflow-lint requires a workflow path or --file."), command="workflow-lint"), file=sys.stderr)
            return 1
        result = workflow_lint_payload(Path(workflow_path), min_quality_score=args.min_quality_score)
        if args.format == "markdown":
            print(workflow_lint_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    if args.command == "workflow-add-step":
        result = workflow_add_step_payload(
            Path(args.workflow),
            after_step_id=args.after,
            action=args.action,
            step_id=args.step_id,
            text=args.text,
            url_contains=args.url_contains,
            timeout_ms=args.timeout_ms,
            observation=args.observation,
            dry_run=args.dry_run,
        )
        if args.format == "markdown":
            print(workflow_add_step_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"updated", "preview"} else 1

    if args.command == "generate-from-diff":
        from .context_audit import append_context_parse_audit
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
        payload = workflow_generation_cli_payload(result, changes)
        if args.audit_log:
            append_context_parse_audit(
                args.audit_log,
                task_description=args.task_description,
                generation=result,
                changed_files=payload["changed_files"],
            )
        if args.format == "yaml" and result.workflow_yaml:
            print(result.workflow_yaml)
        elif args.format == "markdown":
            print(generate_from_diff_cli_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if result.status == "success" else 1

    if args.command == "verify-impl":
        from .mcp_server import verify_implementation_payload

        try:
            workspace = open_workspace(args.workspace_root)
            base_url = args.base_url or infer_verify_impl_base_url(Path(args.repo_root).resolve(), workspace.root)
            verify_args = {
                "workspace_root": str(workspace.root),
                "task_description": args.task_description,
                "base_url": base_url,
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
            payload["base_url"] = base_url
        except Exception as exc:
            suggestion = cli_error_suggestion(str(exc), command="verify-impl")
            if args.format == "markdown":
                print(format_error(exc, command="verify-impl"), file=sys.stderr)
            else:
                print(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "result": "error",
                            "message": str(exc),
                            "suggestion": suggestion,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            return 1
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

    if args.command == "list-workflows":
        from .workflow_index import list_workflows

        workspace = open_workspace(args.workspace_root)
        ensure_workflow_index(workspace)
        items = list_workflows(workspace.root, visibility=args.visibility)
        payload = {"schema_version": 1, "workspace": str(workspace.root), "workflow_count": len(items), "workflows": items}
        if args.format == "markdown":
            print(workflow_list_to_markdown(payload))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "search-workflows":
        from .workflow_index import search_workflows

        workspace = open_workspace(args.workspace_root)
        ensure_workflow_index(workspace)
        items = search_workflows(workspace.root, args.query, visibility=args.visibility, limit=args.limit)
        payload = {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "query": args.query,
            "workflow_count": len(items),
            "workflows": items,
        }
        if args.format == "markdown":
            print(workflow_list_to_markdown(payload))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "share-workflow":
        from .workflow_index import mark_workflow_public

        workspace = open_workspace(args.workspace_root)
        ref = find_workflow(workspace, args.name)
        if not ref.license:
            ref = find_workflow(workspace, args.name)
        index_path = mark_workflow_public(workspace.root, ref)
        payload = {
            "status": "success",
            "workflow": ref.name,
            "visibility": "public",
            "license": "cc-by-4.0",
            "index_path": str(index_path),
            "message": f"Workflow '{ref.name}' is now public in the local workflow library.",
        }
        if args.format == "markdown":
            print(payload["message"])
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "withdraw-workflow":
        from .workflow_index import withdraw_workflow

        workspace = open_workspace(args.workspace_root)
        ref = find_workflow(workspace, args.name)
        result = withdraw_workflow(workspace.root, ref)
        payload = {
            "status": result.get("status"),
            "workflow": result.get("workflow"),
            "visibility": result.get("visibility"),
            "index_path": result.get("index_path"),
            "message": f"Workflow '{ref.name}' is now private in the local workflow library.",
        }
        if args.format == "markdown":
            print(payload["message"])
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "publish-workflow":
        from .workflow_index import publish_workflow

        workspace = open_workspace(args.workspace_root)
        ref = find_workflow(workspace, args.name)
        result = publish_workflow(
            workspace.root,
            ref,
            min_quality_score=float(args.min_quality_score),
            catalog_url_base=str(args.catalog_url_base),
        )
        if result.get("status") == "published":
            payload = {
                "status": "published",
                "id": result.get("id"),
                "name": result.get("name"),
                "version": result.get("version"),
                "quality_score": result.get("quality_score"),
                "url": result.get("url"),
                "index_path": result.get("index_path"),
            }
        else:
            payload = {
                "status": str(result.get("status") or "blocked"),
                "reason": result.get("reason"),
                "workflow": result.get("workflow"),
                "quality_score": result.get("quality_score"),
                "min_quality_score": result.get("min_quality_score"),
                "issues": result.get("issues"),
            }
        if args.format == "markdown":
            print(publish_workflow_to_markdown(payload))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["status"] == "published" else 1

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


def workflow_lint_payload(path: Path, *, min_quality_score: float = 0.6) -> dict[str, Any]:
    from .workflow_quality import score_workflow_quality

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "status": "error",
            "ok": False,
            "workflow_path": str(path),
            "message": str(exc),
            "validation": {"valid": False, "issues": [{"level": "error", "step_id": "", "message": str(exc)}]},
            "quality": None,
            "suggestions": ["Check that the workflow path exists and is readable."],
        }
    try:
        workflow = parse_workflow_file(path)
        validation = validate_workflow_file(path)
        workflow_name = workflow.name
        step_count = len(workflow.steps)
    except Exception as exc:
        quality = score_workflow_quality(text)
        return {
            "status": "error",
            "ok": False,
            "workflow_path": str(path),
            "workflow_name": path.stem,
            "step_count": 0,
            "message": str(exc),
            "validation": {"valid": False, "issues": [{"level": "error", "step_id": "", "message": str(exc)}]},
            "quality": workflow_quality_payload(quality),
            "suggestions": ["Fix workflow syntax/schema errors before evaluating runtime behavior."],
        }
    quality = score_workflow_quality(text)
    validation_issues = [
        {"level": issue.level, "step_id": issue.step_id, "message": issue.message}
        for issue in validation.issues
    ]
    suggestions = workflow_lint_suggestions(quality.gaps, validation_issues)
    ok = validation.valid and quality.total_score >= min_quality_score
    return {
        "status": "ok" if ok else "needs_work",
        "ok": ok,
        "workflow_path": str(path),
        "workflow_name": workflow_name,
        "step_count": step_count,
        "min_quality_score": min_quality_score,
        "validation": {"valid": validation.valid, "issues": validation_issues},
        "quality": workflow_quality_payload(quality),
        "suggestions": suggestions,
    }


def workflow_quality_payload(quality: Any) -> dict[str, Any]:
    return {
        "score": quality.total_score,
        "assertion_density": quality.assertion_density,
        "business_assertions": quality.business_assertion_count,
        "structural_assertions": quality.structural_assertion_count,
        "data_display_assertions": quality.data_display_assertion_count,
        "forbidden_error_assertions": quality.forbidden_error_assertion_count,
        "text_from_input_references": quality.text_from_input_reference_count,
        "invalid_text_from_references": list(quality.invalid_text_from_references),
        "visual_action_count": quality.visual_action_count,
        "visual_assertion_count": quality.visual_assertion_count,
        "covers_success_path": quality.covers_success_path,
        "covers_error_path": quality.covers_error_path,
        "covers_data_display": quality.covers_data_display,
        "gaps": list(quality.gaps),
        "recommendation": quality.recommendation,
    }


def workflow_lint_suggestions(gaps: tuple[str, ...], validation_issues: list[dict[str, Any]]) -> list[str]:
    suggestions: list[str] = []
    for issue in validation_issues:
        suggestions.append(f"Fix validation issue at step '{issue['step_id'] or '<workflow>'}': {issue['message']}")
    for gap in gaps:
        lower = gap.lower()
        if "success state" in lower:
            suggestions.append("Add wait_for_text, wait_for url_contains, or assert_text after the submit/action step.")
        elif "assertion density" in lower:
            suggestions.append("Add at least one assertion after the main action so the workflow verifies behavior, not just execution.")
        elif "business assertions" in lower:
            suggestions.append("Add a business-facing assert_text or wait_for_text for the user-visible outcome.")
        elif "error path" in lower:
            suggestions.append("Add assert_text_contract forbidden_any or assert_no_error to catch visible failure states.")
        elif "visual workflow has no visual assertion" in lower:
            suggestions.append("Add assert_visual_text after click_visual, or pair the visual interaction with a semantic assert_text.")
        elif "text_from references" in lower:
            suggestions.append("Fix text_from input references or add the missing fields to the inputs template.")
        else:
            suggestions.append(f"Address quality gap: {gap}")
    if not suggestions:
        suggestions.append("Workflow quality is good.")
    return list(dict.fromkeys(suggestions))


def workflow_lint_to_markdown(result: dict[str, Any]) -> str:
    quality = result.get("quality") if isinstance(result.get("quality"), dict) else {}
    validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
    issues = validation.get("issues") if isinstance(validation.get("issues"), list) else []
    gaps = quality.get("gaps") if isinstance(quality.get("gaps"), list) else []
    lines = [
        f"Workflow: {result.get('workflow_name') or Path(str(result.get('workflow_path') or '')).stem} ({result.get('step_count', 0)} steps)",
        f"Status: {result.get('status')}",
    ]
    if quality:
        lines.append(f"Quality score: {quality.get('score')} (threshold: {result.get('min_quality_score')})")
        lines.append(f"Assertion density: {quality.get('assertion_density')}")
    if issues:
        lines.extend(["", "Issues:"])
        for issue in issues:
            location = f" step {issue.get('step_id')}" if issue.get("step_id") else ""
            lines.append(f"  - [{issue.get('level')}] {location} {issue.get('message')}".rstrip())
    if gaps:
        if not issues:
            lines.extend(["", "Issues:"])
        for gap in gaps:
            lines.append(f"  - [quality] {gap}")
    suggestions = result.get("suggestions") if isinstance(result.get("suggestions"), list) else []
    if suggestions:
        lines.extend(["", "Suggestions:"])
        lines.extend(f"  - {suggestion}" for suggestion in suggestions)
    return "\n".join(lines)


def workflow_add_step_payload(
    path: Path,
    *,
    after_step_id: str,
    action: str,
    step_id: str | None = None,
    text: str | None = None,
    url_contains: str | None = None,
    timeout_ms: int | None = None,
    observation: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    try:
        import yaml

        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "error", "workflow_path": str(path), "message": str(exc)}
    if not isinstance(doc, dict):
        return {"status": "error", "workflow_path": str(path), "message": "Workflow YAML root must be an object."}
    steps = doc.get("steps")
    if not isinstance(steps, list):
        return {"status": "error", "workflow_path": str(path), "message": "Workflow YAML must contain a steps list."}
    insert_index = next((index for index, step in enumerate(steps) if isinstance(step, dict) and step.get("id") == after_step_id), None)
    if insert_index is None:
        return {"status": "error", "workflow_path": str(path), "message": f"Step id not found: {after_step_id}"}
    existing_ids = {str(step.get("id")) for step in steps if isinstance(step, dict) and step.get("id")}
    new_step = build_workflow_step_for_cli(
        action=action,
        step_id=step_id or unique_step_id(action, existing_ids),
        text=text,
        url_contains=url_contains,
        timeout_ms=timeout_ms,
        observation=observation,
    )
    if new_step["id"] in existing_ids:
        return {"status": "error", "workflow_path": str(path), "message": f"Step id already exists: {new_step['id']}"}
    updated_steps = [*steps[: insert_index + 1], new_step, *steps[insert_index + 1 :]]
    updated_doc = {**doc, "steps": updated_steps}
    yaml_text = yaml.safe_dump(updated_doc, allow_unicode=True, sort_keys=False).rstrip() + "\n"
    if not dry_run:
        path.write_text(yaml_text, encoding="utf-8")
    validation = None
    if not dry_run:
        try:
            validation_result = validate_workflow_file(path)
            validation = {
                "valid": validation_result.valid,
                "issues": [
                    {"level": issue.level, "step_id": issue.step_id, "message": issue.message}
                    for issue in validation_result.issues
                ],
            }
        except Exception as exc:
            validation = {"valid": False, "issues": [{"level": "error", "step_id": "", "message": str(exc)}]}
    return {
        "status": "preview" if dry_run else "updated",
        "workflow_path": str(path),
        "after": after_step_id,
        "inserted_index": insert_index + 1,
        "step": new_step,
        "validation": validation,
        "yaml": yaml_text if dry_run else None,
    }


def build_workflow_step_for_cli(
    *,
    action: str,
    step_id: str,
    text: str | None,
    url_contains: str | None,
    timeout_ms: int | None,
    observation: str | None,
) -> dict[str, Any]:
    step: dict[str, Any] = {"id": step_id, "action": action}
    if text is not None:
        if action == "wait_for":
            step["condition"] = "text"
        step["text"] = text
    if url_contains is not None:
        step["condition"] = "url"
        step["url_contains"] = url_contains
    if timeout_ms is not None:
        step["timeout_ms"] = timeout_ms
    if observation is not None:
        step["observation"] = observation
    return step


def unique_step_id(action: str, existing_ids: set[str]) -> str:
    base = re.sub(r"[^0-9a-zA-Z_]+", "_", action).strip("_").lower() or "step"
    candidate = base
    counter = 2
    while candidate in existing_ids:
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def workflow_add_step_to_markdown(result: dict[str, Any]) -> str:
    lines = [
        f"Status: {result.get('status')}",
        f"Workflow: {result.get('workflow_path')}",
    ]
    if result.get("message"):
        lines.append(f"Message: {result['message']}")
    step = result.get("step") if isinstance(result.get("step"), dict) else None
    if step:
        lines.extend(["", "Inserted step:", f"- id: {step.get('id')}", f"- action: {step.get('action')}"])
        if step.get("text"):
            lines.append(f"- text: {step['text']}")
        if step.get("url_contains"):
            lines.append(f"- url_contains: {step['url_contains']}")
    validation = result.get("validation") if isinstance(result.get("validation"), dict) else None
    if validation is not None:
        lines.append(f"Validation: {'valid' if validation.get('valid') else 'invalid'}")
    return "\n".join(lines)


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


def infer_verify_impl_base_url(repo_root: Path, workspace_root: Path) -> str:
    from .preflight import detect_project_type, recommended_project_port

    project_type = detect_project_type(repo_root)
    port = infer_dev_server_port(repo_root, project_type) or recommended_project_port(project_type)
    if port is not None:
        return f"http://127.0.0.1:{port}"
    fixture = first_workspace_fixture(workspace_root)
    if fixture is not None:
        return fixture
    raise ValueError(
        "verify-impl could not infer --base-url from package.json, vite.config.*, next.config.*, manifest.json, or workspace fixtures."
    )


def infer_dev_server_port(repo_root: Path, project_type: str | None) -> int | None:
    package_json = repo_root / "package.json"
    if package_json.exists():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            package = {}
        scripts = package.get("scripts") if isinstance(package, dict) and isinstance(package.get("scripts"), dict) else {}
        for value in scripts.values():
            port = parse_port_hint(str(value))
            if port is not None:
                return port
    for name in ("vite.config.ts", "vite.config.js", "vite.config.mts", "vite.config.mjs", "next.config.js", "next.config.mjs", "next.config.ts"):
        path = repo_root / name
        if path.exists():
            port = parse_port_hint(path.read_text(encoding="utf-8", errors="ignore"))
            if port is not None:
                return port
    manifest = repo_root / "manifest.json"
    if manifest.exists() and project_type == "uni-app":
        return 8080
    return None


def parse_port_hint(text: str) -> int | None:
    patterns = [
        r"--port(?:=|\s+)(\d{2,5})",
        r"\bport\s*[:=]\s*(\d{2,5})",
        r"localhost:(\d{2,5})",
        r"127\.0\.0\.1:(\d{2,5})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        port = int(match.group(1))
        if 1 <= port <= 65535:
            return port
    return None


def first_workspace_fixture(workspace_root: Path) -> str | None:
    fixtures_dir = workspace_root / "fixtures"
    if not fixtures_dir.exists():
        return None
    for path in sorted(fixtures_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".html", ".htm"}:
            return (Path("fixtures") / path.relative_to(fixtures_dir)).as_posix()
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


def workflow_list_to_markdown(payload: dict[str, Any]) -> str:
    workflows = payload.get("workflows") if isinstance(payload.get("workflows"), list) else []
    lines = [
        "# Workflows",
        "",
        f"- Workspace: `{payload.get('workspace')}`",
        f"- Count: `{payload.get('workflow_count', len(workflows))}`",
    ]
    if payload.get("query") is not None:
        lines.append(f"- Query: `{payload.get('query')}`")
    lines.extend(["", "| name | visibility | tags | path |", "| --- | --- | --- | --- |"])
    for item in workflows:
        if not isinstance(item, dict):
            continue
        tags = ", ".join(str(tag) for tag in item.get("tags", []) if str(tag))
        lines.append(
            "| "
            + " | ".join(
                markdown_cell(value)
                for value in (
                    item.get("name"),
                    item.get("visibility"),
                    tags,
                    item.get("path"),
                )
            )
            + " |"
        )
    return "\n".join(lines)


def publish_workflow_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Publish Workflow",
        "",
        f"- Status: `{payload.get('status')}`",
    ]
    if payload.get("workflow"):
        lines.append(f"- Workflow: `{payload.get('workflow')}`")
    if payload.get("quality_score") is not None:
        lines.append(f"- Quality score: `{payload.get('quality_score')}`")
    if payload.get("min_quality_score") is not None:
        lines.append(f"- Minimum quality score: `{payload.get('min_quality_score')}`")
    if payload.get("url"):
        lines.append(f"- URL: `{payload.get('url')}`")
    if payload.get("index_path"):
        lines.append(f"- Index path: `{payload.get('index_path')}`")
    if payload.get("reason"):
        lines.append(f"- Reason: `{payload.get('reason')}`")
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    if issues:
        lines.extend(["", "## Issues"])
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            location = f" step {issue.get('step_id')}" if issue.get("step_id") else ""
            lines.append(f"- [{issue.get('level')}] {location} {issue.get('message')}".rstrip())
    return "\n".join(lines)


def ensure_workflow_index(workspace: Any) -> None:
    from .workflow_index import update_workflow_index

    for ref in discover_workflows(workspace, include_slow=True):
        update_workflow_index(workspace.root, ref)


def markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")
