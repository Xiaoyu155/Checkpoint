from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .acceptance import STRICT_OUTCOME_ASSERTION_ACTIONS
from .console import build_report_detail, build_workspace_dashboard, dashboard_to_markdown, find_report_json_path, report_detail_to_markdown
from .mcp_common import (
    MCP_DETAIL_CONTENT_MAX_CHARS,
    MCP_DETAIL_RESPONSE_MAX_CHARS,
    budget_list_payload,
    budget_mcp_report_dict,
    budget_mcp_text,
    preflight_summary,
    require_str,
    require_workspace,
    safe_artifact,
    safe_workspace_child,
)
from .models import to_jsonable
from .preflight import run_preflight
from .reports import list_run_summaries
from .run_profile import MUTATING_ACTIONS
from .git_diff import affected_workflows, changed_files as git_changed_files, workflow_affects_changed_path
from .security import scrub_secrets
from .validation import validate_workflow_file
from .workflow import parse_workflow_file
from .workflow_diff import workflow_text_diff
from .workflow_quality import WorkflowQualityScore, score_workflow_quality
from .workspace import discover_workflows, find_workflow, workspace_report_access_payload


def list_workflows_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    include_slow = bool(args.get("include_slow", False))
    diff_context = workflow_diff_context(args)
    latest_by_workflow = {}
    for summary in list_run_summaries(workspace.runs_dir, limit=50):
        latest_by_workflow.setdefault(summary.workflow_name, summary)
    workflows = []
    refs = list(discover_workflows(workspace, include_slow=include_slow))
    recommended_refs = affected_workflows(refs, changed=diff_context["changed_files"]) if diff_context["enabled"] else []
    recommended_names = {ref.name for ref in recommended_refs}
    for ref in refs:
        latest = latest_by_workflow.get(ref.name)
        quality = workflow_quality_payload(ref.path)
        diff_match = workflow_diff_match(ref, diff_context["changed_files"]) if diff_context["enabled"] else None
        workflows.append(
            {
                "name": ref.name,
                "path": ref.relative_path,
                "tags": list(ref.tags),
                "affects": list(ref.affects),
                "visibility": ref.visibility,
                "author": ref.author,
                "description": ref.description,
                "license": ref.license,
                "last_run_status": latest.status if latest else None,
                "last_run_id": latest.run_id if latest else None,
                "quality": quality,
                "agent_readiness": workflow_agent_readiness(quality),
                "diff_recommendation": workflow_diff_recommendation(ref, diff_match, ref.name in recommended_names),
            }
        )
    payload = {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "workflow_count": len(workflows),
        "recommendations": workflow_recommendations_payload(diff_context, workflows),
        "workflows": workflows,
    }
    return budget_list_payload(payload, list_key="workflows", count_key="workflow_count")


def plan_coverage_repair_payload(args: dict[str, Any]) -> dict[str, Any]:
    payload = list_workflows_payload(args)
    recommendations = payload.get("recommendations") if isinstance(payload.get("recommendations"), dict) else {}
    coverage = recommendations.get("coverage") if isinstance(recommendations.get("coverage"), dict) else {}
    plan = {
        "schema_version": 1,
        "workspace": payload.get("workspace"),
        "status": coverage.get("status") or "not_evaluated",
        "changed_files": recommendations.get("changed_files") if isinstance(recommendations.get("changed_files"), list) else [],
        "primary_recommended_workflows": recommendations.get("primary_recommended_workflows")
        if isinstance(recommendations.get("primary_recommended_workflows"), list)
        else [],
        "fallback_no_affects_workflows": recommendations.get("fallback_no_affects_workflows")
        if isinstance(recommendations.get("fallback_no_affects_workflows"), list)
        else [],
        "acceptance_candidate_workflows": recommendations.get("acceptance_candidate_workflows")
        if isinstance(recommendations.get("acceptance_candidate_workflows"), list)
        else [],
        "precise_covered_files": coverage.get("precise_covered_files") if isinstance(coverage.get("precise_covered_files"), list) else [],
        "fallback_only_files": coverage.get("fallback_only_files") if isinstance(coverage.get("fallback_only_files"), list) else [],
        "uncovered_files": coverage.get("uncovered_files") if isinstance(coverage.get("uncovered_files"), list) else [],
        "suggested_affects": coverage.get("suggested_affects") if isinstance(coverage.get("suggested_affects"), list) else [],
        "suggested_new_workflows": coverage.get("suggested_new_workflows")
        if isinstance(coverage.get("suggested_new_workflows"), list)
        else [],
        "next_action": coverage.get("next_action") or recommendations.get("next_action"),
    }
    plan["ready_to_verify"] = plan["status"] == "covered" and bool(plan["acceptance_candidate_workflows"])
    plan["agent_instruction"] = coverage_agent_instruction(plan)
    return plan


def draft_coverage_repair_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    plan = plan_coverage_repair_payload(args)
    max_items = max(1, min(int(args.get("max_items") or 5), 20))
    patches: list[dict[str, Any]] = []
    for suggestion in plan.get("suggested_affects", []) if isinstance(plan.get("suggested_affects"), list) else []:
        if len(patches) >= max_items:
            break
        if not isinstance(suggestion, dict):
            continue
        patch = draft_affects_patch(workspace, suggestion)
        if patch:
            patches.append(patch)
    for suggestion in plan.get("suggested_new_workflows", []) if isinstance(plan.get("suggested_new_workflows"), list) else []:
        if len(patches) >= max_items:
            break
        if not isinstance(suggestion, dict):
            continue
        patches.append(draft_new_workflow_patch(workspace, suggestion))
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "status": plan.get("status"),
        "ready_to_verify": plan.get("ready_to_verify"),
        "patch_count": len(patches),
        "patches": patches,
        "agent_instruction": draft_coverage_agent_instruction(plan, patches),
    }


def apply_coverage_repair_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    apply_changes = bool(args.get("apply", False))
    overwrite = bool(args.get("overwrite", False))
    plan = plan_coverage_repair_payload(args)
    max_items = max(1, min(int(args.get("max_items") or 5), 20))
    if not apply_changes:
        draft = draft_coverage_repair_payload(args)
        return {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "status": "dry_run",
            "apply": False,
            "message": "No files were changed. Rerun with apply=true after reviewing patches.",
            "draft": draft,
        }
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for suggestion in plan.get("suggested_affects", []) if isinstance(plan.get("suggested_affects"), list) else []:
        if len(applied) >= max_items:
            break
        if not isinstance(suggestion, dict):
            continue
        result = apply_affects_repair(workspace, suggestion)
        (applied if result.get("status") == "applied" else skipped).append(result)
    for suggestion in plan.get("suggested_new_workflows", []) if isinstance(plan.get("suggested_new_workflows"), list) else []:
        if len(applied) >= max_items:
            break
        if not isinstance(suggestion, dict):
            continue
        result = apply_new_workflow_repair(workspace, suggestion, overwrite=overwrite)
        (applied if result.get("status") == "applied" else skipped).append(result)
    post_apply_plan = plan_coverage_repair_payload(args) if applied else plan
    coverage_fixed = bool(post_apply_plan.get("status") == "covered" and post_apply_plan.get("primary_recommended_workflows"))
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "status": "applied" if applied else "no_changes",
        "apply": True,
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped,
        "coverage_fixed": coverage_fixed,
        "post_apply_plan": post_apply_plan,
        "next_action": apply_coverage_next_action(coverage_fixed, post_apply_plan),
    }


def apply_coverage_next_action(coverage_fixed: bool, post_apply_plan: dict[str, Any]) -> str:
    if coverage_fixed:
        return "Coverage is now precise. Run codex-check, then read get_run_report and require agent_verdict before claiming completion."
    status = str(post_apply_plan.get("status") or "")
    if status == "fallback_only":
        return "Coverage is still fallback-only. Review remaining suggested_affects and apply the relevant workflow-specific patch."
    if status == "uncovered":
        return "Coverage is still uncovered. Create the suggested_new_workflows or record a targeted workflow."
    return "Rerun plan_coverage_repair and codex-check to verify coverage after applying repairs."


def draft_affects_patch(workspace: Any, suggestion: dict[str, Any]) -> dict[str, Any] | None:
    relative = str(suggestion.get("path") or "").strip()
    if not relative:
        return None
    path = safe_workspace_child(workspace, workspace.root / relative)
    if not path.exists():
        return None
    current = path.read_text(encoding="utf-8")
    affects = [str(item) for item in suggestion.get("add_affects", []) if str(item).strip()]
    proposed = workflow_text_with_affects(current, affects)
    return {
        "kind": "add_affects",
        "workflow": suggestion.get("workflow"),
        "path": relative,
        "add_affects": affects,
        "diff": workflow_text_diff(path, proposed, relative_path=relative),
        "applied": False,
    }


def apply_affects_repair(workspace: Any, suggestion: dict[str, Any]) -> dict[str, Any]:
    relative = str(suggestion.get("path") or "").strip()
    if not relative:
        return {"status": "skipped", "reason": "missing_path", "workflow": suggestion.get("workflow")}
    path = safe_workspace_child(workspace, workspace.root / relative)
    if not path.exists():
        return {"status": "skipped", "reason": "workflow_not_found", "path": relative, "workflow": suggestion.get("workflow")}
    current = path.read_text(encoding="utf-8")
    affects = [str(item) for item in suggestion.get("add_affects", []) if str(item).strip()]
    proposed = workflow_text_with_affects(current, affects)
    if proposed == current:
        return {"status": "skipped", "reason": "already_up_to_date", "path": relative, "workflow": suggestion.get("workflow")}
    path.write_text(proposed, encoding="utf-8")
    return {
        "status": "applied",
        "kind": "add_affects",
        "workflow": suggestion.get("workflow"),
        "path": relative,
        "add_affects": affects,
    }


def draft_new_workflow_patch(workspace: Any, suggestion: dict[str, Any]) -> dict[str, Any]:
    name = str(suggestion.get("suggested_name") or "coverage_verification")
    relative = f"workflows/{name}.yaml"
    path = safe_workspace_child(workspace, workspace.root / relative)
    affects = [str(item) for item in suggestion.get("affects", []) if str(item).strip()]
    proposed = new_workflow_yaml(name, affects)
    return {
        "kind": "new_workflow",
        "workflow": name,
        "path": relative,
        "affects": affects,
        "diff": workflow_text_diff(path, proposed, relative_path=relative),
        "applied": False,
    }


def apply_new_workflow_repair(workspace: Any, suggestion: dict[str, Any], *, overwrite: bool = False) -> dict[str, Any]:
    name = str(suggestion.get("suggested_name") or "coverage_verification")
    relative = f"workflows/{name}.yaml"
    path = safe_workspace_child(workspace, workspace.root / relative)
    existed = path.exists()
    if existed and not overwrite:
        return {"status": "skipped", "reason": "target_exists", "path": relative, "workflow": name}
    affects = [str(item) for item in suggestion.get("affects", []) if str(item).strip()]
    proposed = new_workflow_yaml(name, affects)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(proposed, encoding="utf-8")
    return {
        "status": "applied",
        "kind": "new_workflow",
        "workflow": name,
        "path": relative,
        "affects": affects,
        "overwrote": existed and overwrite,
    }


def workflow_text_with_affects(text: str, affects: list[str]) -> str:
    try:
        import yaml

        doc = yaml.safe_load(text)
        if not isinstance(doc, dict):
            raise ValueError("workflow root is not an object")
        existing = doc.get("affects") if isinstance(doc.get("affects"), list) else []
        merged = list(dict.fromkeys([*(str(item) for item in existing if str(item).strip()), *affects]))
        doc["affects"] = merged
        return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    except Exception:
        lines = text.splitlines()
        insert_at = 1
        for index, line in enumerate(lines):
            if line.startswith("version:"):
                insert_at = index + 1
                break
        block = ["affects:", *[f"  - {item}" for item in affects]]
        return "\n".join([*lines[:insert_at], *block, *lines[insert_at:]]) + "\n"


def new_workflow_yaml(name: str, affects: list[str]) -> str:
    try:
        import yaml

        payload = {
            "schema_version": 1,
            "name": name,
            "version": 1,
            "tags": ["verification"],
            "affects": affects,
            "steps": [
                {
                    "id": "observe",
                    "action": "observe_browser",
                    "url": "http://localhost:3000",
                },
                {
                    "id": "assert_ready",
                    "action": "assert_text_contract",
                    "required_any": ["Ready"],
                    "forbidden_any": ["Error", "Exception", "Traceback"],
                },
            ],
        }
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    except Exception:
        affects_lines = "\n".join(f"  - {item}" for item in affects)
        return (
            f"schema_version: 1\nname: {name}\nversion: 1\ntags:\n  - verification\naffects:\n"
            f"{affects_lines}\nsteps:\n  - id: observe\n    action: observe_browser\n    url: http://localhost:3000\n"
            "  - id: assert_ready\n    action: assert_text_contract\n    required_any:\n      - Ready\n"
            "    forbidden_any:\n      - Error\n      - Exception\n      - Traceback\n"
        )


def draft_coverage_agent_instruction(plan: dict[str, Any], patches: list[dict[str, Any]]) -> str:
    if not patches:
        return str(plan.get("agent_instruction") or "No coverage repair patches were drafted.")
    return "Review the patch diffs, apply only the relevant coverage repair, then rerun plan_coverage_repair and codex-check."


def coverage_agent_instruction(plan: dict[str, Any]) -> str:
    status = str(plan.get("status") or "")
    if status == "covered":
        if plan.get("acceptance_candidate_workflows"):
            return "Run primary_recommended_workflows, then read get_run_report and require agent_verdict.can_claim_product_done before claiming completion."
        return "Primary coverage exists, but no recommended workflow is an acceptance candidate; improve assertions before relying on it."
    if status == "fallback_only":
        return "Apply suggested_affects to the relevant fallback workflow or generate a targeted workflow, then rerun list_workflows/codex-check."
    if status == "uncovered":
        return "Create suggested_new_workflows for uncovered_files before claiming verification."
    if status == "not_evaluated":
        return "Pass changed_files or repo_root/base so Checkpoint can evaluate coverage."
    return "Review coverage status and run the smallest relevant workflow set."


def workflow_diff_context(args: dict[str, Any]) -> dict[str, Any]:
    explicit = args.get("changed_files")
    if isinstance(explicit, list):
        changed = [str(item).replace("\\", "/").strip() for item in explicit if str(item).strip()]
        return {
            "enabled": True,
            "source": "changed_files",
            "base": str(args.get("base") or ""),
            "repo_root": str(args.get("repo_root") or ""),
            "changed_files": sorted(dict.fromkeys(changed)),
        }
    if args.get("repo_root") or args.get("base"):
        repo_root = Path(str(args.get("repo_root") or ".")).resolve()
        base = str(args.get("base") or "HEAD")
        return {
            "enabled": True,
            "source": "git_diff",
            "base": base,
            "repo_root": str(repo_root),
            "changed_files": git_changed_files(base=base, cwd=repo_root),
        }
    return {"enabled": False, "source": "none", "base": "", "repo_root": "", "changed_files": []}


def workflow_diff_match(ref: Any, changed: list[str]) -> dict[str, Any]:
    affects = [str(item).strip() for item in getattr(ref, "affects", ()) or () if str(item).strip()]
    if not changed:
        return {"matched": True, "reason": "no_changed_files", "matched_patterns": []}
    if not affects:
        return {"matched": True, "reason": "no_affects_declared", "matched_patterns": []}
    matched = [pattern for pattern in affects if workflow_affects_changed_path(pattern, changed)]
    return {
        "matched": bool(matched),
        "reason": "affects_matched" if matched else "affects_not_matched",
        "matched_patterns": matched,
    }


def workflow_diff_recommendation(ref: Any, diff_match: dict[str, Any] | None, recommended: bool) -> dict[str, Any]:
    if diff_match is None:
        return {
            "recommended": None,
            "reason": "diff_not_provided",
            "next_action": "Pass changed_files or repo_root/base to list_workflows for diff-aware workflow recommendations.",
        }
    if recommended:
        if diff_match.get("reason") == "no_affects_declared":
            reason = "recommended_fallback_no_affects"
            next_action = "Run this workflow as a fallback, then add affects paths to improve future selection."
        elif diff_match.get("reason") == "no_changed_files":
            reason = "recommended_no_changed_files"
            next_action = "Run as a broad check because no changed files were provided."
        else:
            reason = "recommended_affects_match"
            next_action = "Run this workflow for the current diff."
    else:
        reason = str(diff_match.get("reason") or "not_recommended")
        next_action = "Skip for this diff unless the task scope is broader than changed files."
    return {
        "recommended": recommended,
        "reason": reason,
        "matched_patterns": list(diff_match.get("matched_patterns") or []),
        "next_action": next_action,
    }


def workflow_recommendations_payload(diff_context: dict[str, Any], workflows: list[dict[str, Any]]) -> dict[str, Any]:
    recommended = [
        workflow
        for workflow in workflows
        if isinstance(workflow.get("diff_recommendation"), dict)
        and workflow["diff_recommendation"].get("recommended") is True
    ]
    primary = [
        workflow
        for workflow in recommended
        if isinstance(workflow.get("diff_recommendation"), dict)
        and workflow["diff_recommendation"].get("reason") == "recommended_affects_match"
    ]
    fallback = [
        workflow
        for workflow in recommended
        if isinstance(workflow.get("diff_recommendation"), dict)
        and workflow["diff_recommendation"].get("reason") == "recommended_fallback_no_affects"
    ]
    acceptance_candidates = [
        workflow["name"]
        for workflow in recommended
        if isinstance(workflow.get("agent_readiness"), dict)
        and workflow["agent_readiness"].get("acceptance_candidate") is True
    ]
    needs_improvement = [
        workflow["name"]
        for workflow in recommended
        if isinstance(workflow.get("agent_readiness"), dict)
        and workflow["agent_readiness"].get("acceptance_candidate") is not True
    ]
    if not diff_context.get("enabled"):
        next_action = "Pass changed_files or repo_root/base to get diff-aware recommendations."
    elif recommended:
        next_action = "Run primary_recommended_workflows first; use fallback_no_affects_workflows only when no precise affects match exists or broader coverage is needed."
    else:
        next_action = "No workflow matched the diff; generate or record a workflow for the changed product path."
    coverage = workflow_coverage_payload(diff_context, primary=primary, fallback=fallback)
    return {
        "enabled": bool(diff_context.get("enabled")),
        "source": diff_context.get("source"),
        "base": diff_context.get("base"),
        "repo_root": diff_context.get("repo_root"),
        "changed_files": list(diff_context.get("changed_files") or []),
        "coverage": coverage,
        "recommended_workflows": [workflow["name"] for workflow in recommended],
        "primary_recommended_workflows": [workflow["name"] for workflow in primary],
        "fallback_no_affects_workflows": [workflow["name"] for workflow in fallback],
        "acceptance_candidate_workflows": acceptance_candidates,
        "recommended_but_not_acceptance_candidate": needs_improvement,
        "next_action": next_action,
    }


def workflow_coverage_payload(
    diff_context: dict[str, Any],
    *,
    primary: list[dict[str, Any]],
    fallback: list[dict[str, Any]],
) -> dict[str, Any]:
    changed = [str(item) for item in diff_context.get("changed_files") or [] if str(item)]
    if not diff_context.get("enabled"):
        return {
            "status": "not_evaluated",
            "precise_covered_files": [],
            "fallback_only_files": [],
            "uncovered_files": [],
            "next_action": "Pass changed_files or repo_root/base to evaluate workflow coverage.",
        }
    precise: list[str] = []
    for path in changed:
        if any(workflow_precisely_covers_file(workflow, path) for workflow in primary):
            precise.append(path)
    fallback_only = [path for path in changed if path not in precise and fallback]
    uncovered = [path for path in changed if path not in precise and path not in fallback_only]
    if uncovered:
        status = "uncovered"
        next_action = "Generate or record workflows for uncovered_files, or add affects paths to existing workflows if coverage already exists."
    elif fallback_only:
        status = "fallback_only"
        next_action = "Add precise affects paths to fallback workflows so future diff selection is reliable."
    elif precise:
        status = "covered"
        next_action = "Run primary_recommended_workflows and inspect agent_verdict in each report."
    else:
        status = "no_changed_files"
        next_action = "No changed files were provided; run a broad smoke or pass changed_files for precise coverage."
    return {
        "status": status,
        "precise_covered_files": precise,
        "fallback_only_files": fallback_only,
        "uncovered_files": uncovered,
        "suggested_affects": suggested_affects_for_fallbacks(fallback_only, fallback),
        "suggested_new_workflows": suggested_new_workflows_for_uncovered(uncovered),
        "next_action": next_action,
    }


def workflow_precisely_covers_file(workflow: dict[str, Any], changed_file: str) -> bool:
    affects = workflow.get("affects") if isinstance(workflow.get("affects"), list) else []
    return any(workflow_affects_changed_path(str(pattern), [changed_file]) for pattern in affects)


def suggested_affects_for_fallbacks(changed_files: list[str], fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    if not changed_files or not fallback:
        return suggestions
    patterns = sorted({suggested_affects_pattern(path) for path in changed_files})
    for workflow in fallback[:5]:
        suggestions.append(
            {
                "workflow": workflow.get("name"),
                "path": workflow.get("path"),
                "add_affects": patterns,
                "reason": "fallback workflow has no affects paths but was used for this diff",
            }
        )
    return suggestions


def suggested_new_workflows_for_uncovered(changed_files: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "changed_file": path,
            "suggested_name": suggested_workflow_name(path),
            "affects": [suggested_affects_pattern(path)],
            "reason": "no workflow precisely covers this changed file",
        }
        for path in changed_files
    ]


def suggested_affects_pattern(path: str) -> str:
    normalized = str(path).replace("\\", "/").strip()
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return normalized
    directory_parts = parts[:-1] if "." in parts[-1] else parts
    if len(directory_parts) >= 2:
        return "/".join(directory_parts[:2]) + "/"
    if len(directory_parts) == 1:
        return directory_parts[0] + "/"
    if len(parts) == 1:
        return parts[0]
    return normalized


def suggested_workflow_name(path: str) -> str:
    pattern = suggested_affects_pattern(path).strip("/")
    text = pattern.replace("/", "_").replace("-", "_").replace(".", "_")
    text = "_".join(part for part in text.split("_") if part)
    return f"{text or 'changed_path'}_verification"


def workflow_quality_payload(path: Any) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "score": 0.0,
            "assertion_density": 0.0,
            "business_assertions": 0,
            "covers_success_path": False,
            "covers_error_path": False,
            "has_interaction": False,
            "has_strict_contract": False,
            "gaps": [f"unable to read workflow: {exc}"],
            "recommendation": "Fix workflow file readability.",
        }
    quality = score_workflow_quality(text)
    steps = workflow_steps_from_yaml(text)
    has_interaction = any(str(step.get("action") or "") in MUTATING_ACTIONS for step in steps)
    has_strict_contract = any(str(step.get("action") or "") in STRICT_OUTCOME_ASSERTION_ACTIONS for step in steps)
    return workflow_quality_summary(quality, has_interaction=has_interaction, has_strict_contract=has_strict_contract)


def workflow_quality_summary(
    quality: WorkflowQualityScore,
    *,
    has_interaction: bool,
    has_strict_contract: bool,
) -> dict[str, Any]:
    return {
        "score": quality.total_score,
        "assertion_density": quality.assertion_density,
        "business_assertions": quality.business_assertion_count,
        "covers_success_path": quality.covers_success_path,
        "covers_error_path": quality.covers_error_path,
        "has_interaction": has_interaction,
        "has_strict_contract": has_strict_contract,
        "gaps": list(quality.gaps),
        "recommendation": quality.recommendation,
    }


def workflow_steps_from_yaml(text: str) -> list[dict[str, Any]]:
    try:
        import yaml

        doc = yaml.safe_load(text)
    except Exception:
        return []
    steps = doc.get("steps") if isinstance(doc, dict) else None
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def workflow_agent_readiness(quality: dict[str, Any]) -> dict[str, Any]:
    score = float(quality.get("score") or 0.0)
    gaps = quality.get("gaps") if isinstance(quality.get("gaps"), list) else []
    blockers: list[str] = []
    if score < 0.6:
        blockers.append("quality_below_acceptance_threshold")
    if not bool(quality.get("covers_success_path")):
        blockers.append("missing_success_assertion")
    if not bool(quality.get("covers_error_path")):
        blockers.append("missing_error_or_no_error_contract")
    if not bool(quality.get("has_interaction")):
        blockers.append("missing_user_interaction")
    if not bool(quality.get("has_strict_contract")):
        blockers.append("missing_strict_contract_assertion")
    if blockers:
        if "missing_success_assertion" in blockers or "missing_strict_contract_assertion" in blockers:
            status = "needs_assertions"
        elif "missing_user_interaction" in blockers:
            status = "inspection_only"
        else:
            status = "weak"
    else:
        status = "acceptance_candidate"
    next_action = str(quality.get("recommendation") or "").strip()
    if "missing_user_interaction" in blockers:
        next_action = "Add a real click, paste, type, select, or submit step before claiming product acceptance."
    elif "missing_strict_contract_assertion" in blockers:
        next_action = "Add assert_text_contract or assert_product_contract after the main interaction."
    elif "missing_error_or_no_error_contract" in blockers:
        next_action = "Add forbidden_any/no-error coverage so visible error states cannot pass silently."
    elif not next_action:
        next_action = "Run the workflow, then use get_run_report and follow agent_verdict."
    return {
        "status": status,
        "acceptance_candidate": status == "acceptance_candidate",
        "blockers": blockers,
        "quality_gaps": gaps,
        "next_action": next_action,
    }


def validate_workflow_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    workflow_name = require_str(args, "workflow_name")
    ref = find_workflow(workspace, workflow_name)
    workflow = parse_workflow_file(ref.path)
    validation = validate_workflow_file(ref.path)
    preflight = run_preflight(workflow)
    return {
        "schema_version": 1,
        "workflow": ref.name,
        "path": ref.relative_path,
        "valid": validation.valid,
        "validation": to_jsonable(validation),
        "preflight": preflight_summary(preflight),
    }


def get_run_report_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    run_id = require_str(args, "run_id")
    fmt = str(args.get("format") or "markdown")
    detail = build_report_detail(workspace, run_id)
    if not detail:
        raise FileNotFoundError(f"Run report not found: {run_id}")
    safe_detail = scrub_secrets(detail)
    if isinstance(safe_detail, dict) and safe_detail.get("status") == "upgrade_required":
        return safe_detail
    if fmt == "markdown":
        content, truncated = budget_mcp_text(report_detail_to_markdown(safe_detail), max_chars=MCP_DETAIL_CONTENT_MAX_CHARS)
        return {
            "schema_version": 1,
            "run_id": run_id,
            "format": "markdown",
            "content": content,
            "truncated": truncated,
            "within_budget": len(content) <= MCP_DETAIL_CONTENT_MAX_CHARS,
            "token_estimate": len(content) // 4,
            "report_hint": f"Use list_run_artifacts with run_id='{run_id}' to locate the full report file.",
        }
    if fmt != "json":
        raise ValueError(f"Unsupported report format: {fmt}")
    report, truncated = budget_mcp_report_dict(safe_detail)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "format": "json",
        "report": report,
        "truncated": truncated,
        "within_budget": len(json.dumps(report, ensure_ascii=False, default=str)) <= MCP_DETAIL_RESPONSE_MAX_CHARS,
        "report_hint": f"Use list_run_artifacts with run_id='{run_id}' to locate the full report file.",
    }


def list_run_artifacts_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    run_id = require_str(args, "run_id")
    try:
        report_path = find_report_json_path(workspace, run_id)
    except FileNotFoundError:
        report_path = None
    if report_path is not None:
        access = workspace_report_access_payload(workspace, report_path)
        if not access["allowed"]:
            return {
                "schema_version": 1,
                "status": "upgrade_required",
                "run_id": run_id,
                "history_access": scrub_secrets(access),
                "message": access.get("message"),
            }
    artifacts = []
    for suffix in (".json", ".md"):
        path = workspace.reports_dir / f"{run_id}{suffix}"
        if path.exists():
            artifacts.append(safe_artifact(workspace, path, "report"))
    run_dir = safe_workspace_child(workspace, workspace.runs_dir / run_id)
    if run_dir.exists():
        for path in sorted(run_dir.rglob("*")):
            if not path.is_file():
                continue
            kind = "screenshot" if path.suffix.lower() in {".png", ".jpg", ".jpeg"} else "artifact"
            try:
                artifacts.append(safe_artifact(workspace, path, kind))
            except ValueError:
                continue
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }
    return budget_list_payload(payload, list_key="artifacts", count_key="artifact_count")


def get_workspace_dashboard_payload(args: dict[str, Any]) -> dict[str, Any]:
    workspace = require_workspace(args)
    fmt = str(args.get("format") or "markdown")
    limit = int(args.get("limit") or 5)
    dashboard = scrub_secrets(build_workspace_dashboard(workspace, limit=max(1, min(limit, 25))))
    if fmt == "markdown":
        content, truncated = budget_mcp_text(dashboard_to_markdown(dashboard), max_chars=MCP_DETAIL_CONTENT_MAX_CHARS)
        return {
            "schema_version": 1,
            "workspace": str(workspace.root),
            "format": "markdown",
            "content": content,
            "truncated": truncated,
            "within_budget": len(content) <= MCP_DETAIL_CONTENT_MAX_CHARS,
        }
    if fmt != "json":
        raise ValueError(f"Unsupported dashboard format: {fmt}")
    return {
        "schema_version": 1,
        "workspace": str(workspace.root),
        "format": "json",
        "dashboard": dashboard,
    }
