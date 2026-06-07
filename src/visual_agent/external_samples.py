from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from time import strftime, time
from typing import Any
from uuid import uuid4

from .auth_state import inspect_storage_state
from .validation import validate_workflow_file
from .workflow import Workflow, parse_workflow_file
from .workspace import Workspace, run_workspace_workflow
from .workspace import write_workspace_report_index
from .models import ActionStatus
from .scheduler import submit_queue_task
from .scheduler import list_queue_tasks
from .workspace import load_workspace_report_index


MUTATING_ACTIONS = {"click", "type", "paste", "refresh_browser", "expect_download", "save_storage_state"}
SENSITIVE_HINTS = ("password", "passwd", "pwd", "token", "secret", "cookie", "key")
ALLOWED_ENVIRONMENTS = {"sandbox", "staging", "test"}
ALLOWED_STORAGE_STATE_POLICIES = {"required", "optional", "forbidden"}
ALLOWED_DOWNLOAD_POLICIES = {"dry-run-only", "confirm-required", "forbidden"}
ALLOWED_MUTATING_ACTION_POLICIES = {"dry-run-or-confirm", "confirm-required", "forbidden"}
ALLOWED_EXTERNAL_RUN_PROFILES = {"dry-run", "supervised", "semi-auto"}


@dataclass(frozen=True)
class ExternalSampleIssue:
    level: str
    code: str
    message: str
    sample_id: str
    step_id: str | None = None


@dataclass(frozen=True)
class ExternalSampleCheck:
    valid: bool
    sample_id: str
    workflow_name: str | None
    issues: tuple[ExternalSampleIssue, ...]


def load_external_sample_catalog(root: str | Path = "examples/external_samples") -> dict[str, Any]:
    catalog_path = Path(root) / "catalog.json"
    if not catalog_path.exists():
        return {"schema_version": 1, "samples": []}
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def list_external_samples(root: str | Path = "examples/external_samples") -> tuple[dict[str, Any], ...]:
    payload = load_external_sample_catalog(root)
    samples = payload.get("samples") if isinstance(payload, dict) else []
    if not isinstance(samples, list):
        return ()
    policy = external_sample_policy(payload)
    return tuple(sample_with_effective_policy(sample, policy) for sample in samples if isinstance(sample, dict))


def external_sample_policy(catalog: dict[str, Any] | None) -> dict[str, Any]:
    policy = catalog.get("policy") if isinstance(catalog, dict) and isinstance(catalog.get("policy"), dict) else {}
    return {
        "allowed_domains": policy.get("allowed_domains") if isinstance(policy.get("allowed_domains"), list) else [],
        "storage_state_policy": policy.get("storage_state_policy", "optional"),
        "download_policy": policy.get("download_policy", "forbidden"),
        "live_execution_allowed": bool(policy.get("live_execution_allowed", False)),
        "mutating_action_policy": policy.get("mutating_action_policy", "dry-run-or-confirm"),
    }


def sample_with_effective_policy(sample: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    effective = dict(sample)
    for key in ("storage_state_policy", "download_policy", "live_execution_allowed", "mutating_action_policy"):
        if key not in effective:
            effective[key] = policy.get(key)
    sample_domains = sample.get("allowed_domains") if isinstance(sample.get("allowed_domains"), list) else []
    policy_domains = policy.get("allowed_domains") if isinstance(policy.get("allowed_domains"), list) else []
    effective["allowed_domains"] = sorted({str(item) for item in [*policy_domains, *sample_domains] if str(item).strip()})
    effective["policy"] = {
        "source": "catalog+sample",
        "catalog_allowed_domains": policy_domains,
        "sample_allowed_domains": sample_domains,
        "storage_state_policy": effective.get("storage_state_policy"),
        "download_policy": effective.get("download_policy"),
        "live_execution_allowed": effective.get("live_execution_allowed"),
        "mutating_action_policy": effective.get("mutating_action_policy"),
    }
    return effective


def find_external_sample(sample_id: str, root: str | Path = "examples/external_samples") -> dict[str, Any]:
    for sample in list_external_samples(root):
        if str(sample.get("id") or "") == sample_id:
            return sample
    raise FileNotFoundError(f"External sample not found: {sample_id}")


def check_external_samples(root: str | Path = "examples/external_samples") -> dict[str, Any]:
    sample_root = Path(root)
    catalog = load_external_sample_catalog(sample_root)
    policy = external_sample_policy(catalog)
    checks = tuple(check_external_sample(sample, sample_root=sample_root) for sample in list_external_samples(sample_root))
    return {
        "schema_version": 1,
        "root": str(sample_root),
        "policy": policy,
        "total_samples": len(checks),
        "valid_samples": sum(1 for check in checks if check.valid),
        "invalid_samples": sum(1 for check in checks if not check.valid),
        "checks": [
            {
                "valid": check.valid,
                "sample_id": check.sample_id,
                "workflow_name": check.workflow_name,
                "issues": [
                    {
                        "level": issue.level,
                        "code": issue.code,
                        "message": issue.message,
                        "step_id": issue.step_id,
                    }
                    for issue in check.issues
                ],
            }
            for check in checks
        ],
    }


def external_samples_readiness(
    root: str | Path = "examples/external_samples",
    *,
    workspace_root: str | Path = ".",
    require_live_auth: bool = False,
) -> dict[str, Any]:
    sample_root = Path(root)
    workspace_path = Path(workspace_root)
    samples = list_external_samples(sample_root)
    checks = {check.sample_id: check for check in (check_external_sample(sample, sample_root=sample_root) for sample in samples)}
    entries = []
    for sample in samples:
        sample_id = str(sample.get("id") or "unknown")
        check = checks[sample_id]
        workflow = parse_workflow_file(sample_root / str(sample["workflow"])) if sample.get("workflow") else None
        entries.append(
            readiness_entry(
                sample,
                check,
                workflow,
                workspace_root=workspace_path,
                require_live_auth=require_live_auth,
            )
        )
    return {
        "schema_version": 1,
        "root": str(sample_root),
        "total_samples": len(entries),
        "ready_samples": sum(1 for entry in entries if entry["ready"]),
        "blocked_samples": sum(1 for entry in entries if not entry["ready"]),
        "require_live_auth": require_live_auth,
        "auth_ready_samples": sum(1 for entry in entries if entry.get("auth_state_ready")),
        "auth_blocked_samples": sum(1 for entry in entries if entry.get("auth_state_ready") is False),
        "entries": entries,
    }


def build_external_sample_run_plan(
    sample_id: str,
    *,
    root: str | Path = "examples/external_samples",
    workspace_root: str | Path = ".",
    run_profile: str = "dry-run",
    require_live_auth: bool = False,
) -> dict[str, Any]:
    if run_profile not in ALLOWED_EXTERNAL_RUN_PROFILES:
        raise ValueError("External samples only support dry-run or supervised run profiles.")
    sample_root = Path(root)
    sample = find_external_sample(sample_id, sample_root)
    workflow_path = sample_root / str(sample.get("workflow") or "")
    check = check_external_sample(sample, sample_root=sample_root)
    readiness = external_samples_readiness(sample_root, workspace_root=workspace_root, require_live_auth=require_live_auth)
    entry = next((item for item in readiness["entries"] if item["sample_id"] == sample_id), None)
    blockers = []
    if not check.valid:
        blockers.extend(issue.code for issue in check.issues if issue.level == "error")
    if entry is None:
        blockers.append("sample_readiness_missing")
    else:
        blockers.extend(str(item) for item in entry.get("blockers", []))
    blockers = sorted(set(blockers))
    return {
        "schema_version": 1,
        "sample_id": sample_id,
        "sample_root": str(sample_root),
        "workspace_root": str(Path(workspace_root)),
        "workflow": str(sample.get("workflow") or ""),
        "workflow_path": str(workflow_path),
        "run_profile": run_profile,
        "dry_run": run_profile == "dry-run",
        "ready": not blockers,
        "blockers": blockers,
        "requires_confirmation": run_profile == "supervised",
        "require_live_auth": require_live_auth,
        "allowed_domains": sample.get("allowed_domains") if isinstance(sample.get("allowed_domains"), list) else [],
        "storage_state_policy": sample.get("storage_state_policy"),
        "download_policy": sample.get("download_policy"),
        "mutating_action_policy": sample.get("mutating_action_policy"),
        "policy": sample.get("policy") if isinstance(sample.get("policy"), dict) else {},
        "readiness": entry,
    }


def run_external_sample(
    workspace: Workspace,
    sample_id: str,
    *,
    root: str | Path = "examples/external_samples",
    run_profile: str = "dry-run",
    preflight: bool = True,
    require_live_auth: bool = False,
) -> dict[str, Any]:
    plan = build_external_sample_run_plan(
        sample_id,
        root=root,
        workspace_root=workspace.root,
        run_profile=run_profile,
        require_live_auth=require_live_auth,
    )
    if not plan["ready"]:
        raise RuntimeError(f"External sample is not ready: {', '.join(plan['blockers'])}")
    workflow_source = Path(plan["workflow_path"])
    if not workflow_source.exists():
        raise FileNotFoundError(f"External sample workflow not found: {workflow_source}")
    workflow_target = workspace.workflows_dir / "external_samples" / workflow_source.name
    workflow_target.parent.mkdir(parents=True, exist_ok=True)
    materialize_external_sample_workflow(workflow_source, workflow_target, sample_root=Path(root))
    result = run_workspace_workflow(
        workspace,
        workflow_target.relative_to(workspace.root).as_posix(),
        dry_run=plan["dry_run"],
        run_profile=run_profile,
        preflight=preflight,
    )
    report_paths = annotate_external_sample_report(workspace, result.run_id, plan=plan, run_status=external_sample_run_status(result))
    return {
        "schema_version": 1,
        "sample_id": sample_id,
        "status": external_sample_run_status(result),
        "run_id": result.run_id,
        "run_dir": str(result.run_dir),
        "workflow": workflow_target.relative_to(workspace.root).as_posix(),
        "run_profile": run_profile,
        "dry_run": plan["dry_run"],
        "plan": plan,
        "report": report_paths,
    }


def build_external_sample_batch_plan(
    *,
    root: str | Path = "examples/external_samples",
    workspace_root: str | Path = ".",
    run_profile: str = "dry-run",
    include_blocked: bool = True,
    require_live_auth: bool = False,
) -> dict[str, Any]:
    plans = []
    for sample in list_external_samples(root):
        sample_id = str(sample.get("id") or "unknown")
        plan = build_external_sample_run_plan(
            sample_id,
            root=root,
            workspace_root=workspace_root,
            run_profile=run_profile,
            require_live_auth=require_live_auth,
        )
        if include_blocked or plan["ready"]:
            plans.append(plan)
    return {
        "schema_version": 1,
        "root": str(Path(root)),
        "workspace_root": str(Path(workspace_root)),
        "run_profile": run_profile,
        "require_live_auth": require_live_auth,
        "total_samples": len(plans),
        "ready_samples": sum(1 for plan in plans if plan["ready"]),
        "blocked_samples": sum(1 for plan in plans if not plan["ready"]),
        "plans": plans,
    }


def submit_external_sample_batch(
    workspace: Workspace,
    *,
    root: str | Path = "examples/external_samples",
    run_profile: str = "dry-run",
    priority: int = 0,
    max_retries: int = 0,
    include_blocked: bool = True,
    require_live_auth: bool = False,
) -> dict[str, Any]:
    batch = build_external_sample_batch_plan(
        root=root,
        workspace_root=workspace.root,
        run_profile=run_profile,
        include_blocked=include_blocked,
        require_live_auth=require_live_auth,
    )
    submitted = []
    skipped = []
    for plan in batch["plans"]:
        if not plan["ready"]:
            skipped.append({"sample_id": plan["sample_id"], "blockers": plan["blockers"]})
            continue
        workflow_source = Path(plan["workflow_path"])
        workflow_target = workspace.workflows_dir / "external_samples" / workflow_source.name
        workflow_target.parent.mkdir(parents=True, exist_ok=True)
        materialize_external_sample_workflow(workflow_source, workflow_target, sample_root=Path(root))
        task = submit_queue_task(
            workspace,
            workflow_target.relative_to(workspace.root).as_posix(),
            priority=priority,
            max_retries=max_retries,
            run_profile=run_profile,
            dry_run=plan["dry_run"],
            metadata={"external_sample": plan},
        )
        submitted.append(
            {
                "sample_id": plan["sample_id"],
                "task_id": task.task_id,
                "workflow": task.workflow,
                "run_profile": task.run_profile,
                "dry_run": task.dry_run,
            }
        )
    return {
        "schema_version": 1,
        "root": batch["root"],
        "workspace_root": batch["workspace_root"],
        "run_profile": run_profile,
        "ready_samples": batch["ready_samples"],
        "blocked_samples": batch["blocked_samples"],
        "submitted_count": len(submitted),
        "skipped_count": len(skipped),
        "submitted": submitted,
        "skipped": skipped,
    }


def build_external_sample_rerun_plan(
    workspace: Workspace,
    *,
    root: str | Path = "examples/external_samples",
    run_profile: str = "dry-run",
) -> dict[str, Any]:
    summary = build_external_sample_run_summary(workspace, root=root)
    candidates = []
    skipped = []
    for entry in summary["entries"]:
        latest_report = entry.get("latest_report") if isinstance(entry.get("latest_report"), dict) else None
        latest_task = entry.get("latest_queue_task") if isinstance(entry.get("latest_queue_task"), dict) else None
        failed = bool(latest_report and latest_report.get("status") == "failed") or bool(
            latest_task and latest_task.get("status") == "failed"
        )
        if not failed:
            continue
        if not entry.get("ready"):
            skipped.append(
                {
                    "sample_id": entry["sample_id"],
                    "reason": "blocked",
                    "blockers": entry["blockers"],
                    "rerun_suggestion": external_sample_rerun_suggestion(
                        sample_id=str(entry["sample_id"]),
                        ready=False,
                        blockers=list(entry.get("blockers") or []),
                        latest_report=latest_report,
                        latest_task=latest_task,
                    ),
                }
            )
            continue
        plan = build_external_sample_run_plan(
            str(entry["sample_id"]),
            root=root,
            workspace_root=workspace.root,
            run_profile=run_profile,
        )
        plan["rerun_suggestion"] = external_sample_rerun_suggestion(
            sample_id=str(entry["sample_id"]),
            ready=True,
            blockers=[],
            latest_report=latest_report,
            latest_task=latest_task,
        )
        candidates.append(plan)
    return {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "root": str(Path(root)),
        "run_profile": run_profile,
        "candidate_count": len(candidates),
        "skipped_count": len(skipped),
        "candidates": candidates,
        "skipped": skipped,
    }


def submit_external_sample_reruns(
    workspace: Workspace,
    *,
    root: str | Path = "examples/external_samples",
    run_profile: str = "dry-run",
    priority: int = 0,
    max_retries: int = 0,
) -> dict[str, Any]:
    plan = build_external_sample_rerun_plan(workspace, root=root, run_profile=run_profile)
    submitted = []
    for candidate in plan["candidates"]:
        workflow_source = Path(candidate["workflow_path"])
        workflow_target = workspace.workflows_dir / "external_samples" / workflow_source.name
        workflow_target.parent.mkdir(parents=True, exist_ok=True)
        materialize_external_sample_workflow(workflow_source, workflow_target, sample_root=Path(root))
        task = submit_queue_task(
            workspace,
            workflow_target.relative_to(workspace.root).as_posix(),
            priority=priority,
            max_retries=max_retries,
            run_profile=run_profile,
            dry_run=candidate["dry_run"],
            metadata={"external_sample": candidate},
        )
        submitted.append(
            {
                "sample_id": candidate["sample_id"],
                "task_id": task.task_id,
                "workflow": task.workflow,
                "run_profile": task.run_profile,
                "dry_run": task.dry_run,
            }
        )
    return {
        "schema_version": 1,
        "workspace_root": plan["workspace_root"],
        "root": plan["root"],
        "run_profile": run_profile,
        "candidate_count": plan["candidate_count"],
        "submitted_count": len(submitted),
        "skipped_count": plan["skipped_count"],
        "submitted": submitted,
        "skipped": plan["skipped"],
    }


def build_external_sample_batch_failure_summary(
    workspace: Workspace,
    report_id: str,
) -> dict[str, Any]:
    payload = load_external_sample_batch_report(workspace, report_id)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    failures = []
    for entry in summary.get("entries", []) if isinstance(summary.get("entries"), list) else []:
        if not isinstance(entry, dict) or entry.get("status") != "failed":
            continue
        latest_report = entry.get("latest_report") if isinstance(entry.get("latest_report"), dict) else {}
        latest_task = entry.get("latest_queue_task") if isinstance(entry.get("latest_queue_task"), dict) else {}
        failed_step = latest_report.get("failed_step") or entry.get("failed_step")
        failure_context = external_sample_failure_context(workspace, latest_report, entry)
        suggestion = external_sample_rerun_suggestion(
            sample_id=str(entry.get("sample_id") or "unknown"),
            ready=bool(entry.get("ready")),
            blockers=list(entry.get("blockers") or []),
            latest_report=latest_report,
            latest_task=latest_task,
            failure_context=failure_context,
            failed_step=failed_step,
            error=str(entry.get("error") or "") or None,
        )
        failures.append(
            {
                "sample_id": str(entry.get("sample_id") or "unknown"),
                "ready": bool(entry.get("ready")),
                "blockers": list(entry.get("blockers") or []),
                "failed_step": failed_step,
                "run_id": latest_report.get("run_id"),
                "task_id": latest_task.get("task_id"),
                "queue_status": latest_task.get("status"),
                "report_status": latest_report.get("status"),
                "failure_context": failure_context,
                "rerun_suggestion": suggestion,
            }
        )
    return {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "report_id": str(payload.get("report_id") or report_id),
        "failed_count": len(failures),
        "ready_failed_count": sum(1 for item in failures if item["ready"]),
        "blocked_failed_count": sum(1 for item in failures if not item["ready"]),
        "failures": failures,
    }


def build_external_sample_batch_rerun_plan(
    workspace: Workspace,
    report_id: str,
    *,
    root: str | Path = "examples/external_samples",
    run_profile: str = "dry-run",
) -> dict[str, Any]:
    failure_summary = build_external_sample_batch_failure_summary(workspace, report_id)
    candidates = []
    skipped = []
    for failure in failure_summary["failures"]:
        sample_id = str(failure["sample_id"])
        if not failure.get("ready"):
            skipped.append(
                {
                    "sample_id": sample_id,
                    "reason": "blocked",
                    "blockers": list(failure.get("blockers") or []),
                    "rerun_suggestion": failure.get("rerun_suggestion"),
                }
            )
            continue
        plan = build_external_sample_run_plan(
            sample_id,
            root=root,
            workspace_root=workspace.root,
            run_profile=run_profile,
        )
        if not plan["ready"]:
            skipped.append(
                {
                    "sample_id": sample_id,
                    "reason": "blocked",
                    "blockers": plan["blockers"],
                    "rerun_suggestion": external_sample_rerun_suggestion(
                        sample_id=sample_id,
                        ready=False,
                        blockers=list(plan.get("blockers") or []),
                        failed_step=failure.get("failed_step"),
                        failure_context=failure.get("failure_context") if isinstance(failure.get("failure_context"), dict) else None,
                    ),
                }
            )
            continue
        plan["rerun_suggestion"] = failure.get("rerun_suggestion") or external_sample_rerun_suggestion(
            sample_id=sample_id,
            ready=True,
            blockers=[],
            failed_step=failure.get("failed_step"),
            failure_context=failure.get("failure_context") if isinstance(failure.get("failure_context"), dict) else None,
        )
        candidates.append(plan)
    return {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "root": str(Path(root)),
        "report_id": failure_summary["report_id"],
        "run_profile": run_profile,
        "failed_count": failure_summary["failed_count"],
        "candidate_count": len(candidates),
        "skipped_count": len(skipped),
        "failures": failure_summary["failures"],
        "candidates": candidates,
        "skipped": skipped,
    }


def submit_external_sample_batch_reruns(
    workspace: Workspace,
    report_id: str,
    *,
    root: str | Path = "examples/external_samples",
    run_profile: str = "dry-run",
    priority: int = 0,
    max_retries: int = 0,
) -> dict[str, Any]:
    plan = build_external_sample_batch_rerun_plan(workspace, report_id, root=root, run_profile=run_profile)
    submitted = []
    for candidate in plan["candidates"]:
        workflow_source = Path(candidate["workflow_path"])
        workflow_target = workspace.workflows_dir / "external_samples" / workflow_source.name
        workflow_target.parent.mkdir(parents=True, exist_ok=True)
        materialize_external_sample_workflow(workflow_source, workflow_target, sample_root=Path(root))
        task = submit_queue_task(
            workspace,
            workflow_target.relative_to(workspace.root).as_posix(),
            priority=priority,
            max_retries=max_retries,
            run_profile=run_profile,
            dry_run=candidate["dry_run"],
            metadata={"external_sample": candidate, "source_batch_report_id": plan["report_id"]},
        )
        submitted.append(
            {
                "sample_id": candidate["sample_id"],
                "task_id": task.task_id,
                "workflow": task.workflow,
                "run_profile": task.run_profile,
                "dry_run": task.dry_run,
            }
        )
    return {
        "schema_version": 1,
        "workspace_root": plan["workspace_root"],
        "root": plan["root"],
        "report_id": plan["report_id"],
        "run_profile": run_profile,
        "failed_count": plan["failed_count"],
        "candidate_count": plan["candidate_count"],
        "submitted_count": len(submitted),
        "skipped_count": plan["skipped_count"],
        "submitted": submitted,
        "skipped": plan["skipped"],
    }


def external_sample_failure_context(
    workspace: Workspace,
    latest_report: dict[str, Any],
    report_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = {
        "failed_step": latest_report.get("failed_step") or (report_entry or {}).get("failed_step"),
        "failed_action": None,
        "message": str((report_entry or {}).get("error") or ""),
        "has_failure_diagnosis": False,
        "failure_expected": None,
        "failure_actual": None,
    }
    report_path_value = latest_report.get("json_report") if isinstance(latest_report, dict) else None
    if not report_path_value:
        return context
    report_path = workspace.root / str(report_path_value)
    try:
        resolved = report_path.resolve()
        resolved.relative_to(workspace.root.resolve())
    except ValueError:
        return context
    if not resolved.exists():
        return context
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return context
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    failed_step = context["failed_step"] or payload.get("failed_step")
    failed = next(
        (
            step
            for step in steps
            if isinstance(step, dict)
            and ((failed_step and step.get("id") == failed_step) or (not failed_step and step.get("status") == "failed"))
        ),
        None,
    )
    if not isinstance(failed, dict):
        return context
    diagnosis = failed.get("failure_diagnosis") if isinstance(failed.get("failure_diagnosis"), dict) else {}
    return {
        "failed_step": failed.get("id") or failed_step,
        "failed_action": failed.get("action"),
        "message": str(failed.get("message") or context["message"] or ""),
        "has_failure_diagnosis": bool(diagnosis),
        "failure_expected": diagnosis.get("expected"),
        "failure_actual": diagnosis.get("actual"),
    }


def external_sample_rerun_suggestion(
    *,
    sample_id: str,
    ready: bool,
    blockers: list[str],
    latest_report: dict[str, Any] | None = None,
    latest_task: dict[str, Any] | None = None,
    failure_context: dict[str, Any] | None = None,
    failed_step: Any = None,
    error: str | None = None,
) -> dict[str, Any]:
    latest_report = latest_report if isinstance(latest_report, dict) else {}
    latest_task = latest_task if isinstance(latest_task, dict) else {}
    failure_context = failure_context if isinstance(failure_context, dict) else {}
    step = str(failed_step or failure_context.get("failed_step") or latest_report.get("failed_step") or "")
    action = str(failure_context.get("failed_action") or "")
    message = str(error or failure_context.get("message") or "")
    normalized = f"{step} {action} {message}".lower()
    if not ready:
        return {
            "category": "fix_readiness",
            "confidence": "high",
            "reason": "Sample is currently blocked by readiness gates.",
            "next_step": blocked_sample_remediation_hint(blockers),
            "commands": [],
        }
    if "executable doesn't exist" in normalized or "playwright install" in normalized or "browser unavailable" in normalized:
        return {
            "category": "fix_runtime_then_rerun",
            "confidence": "high",
            "reason": "Browser runtime is unavailable, so rerun will keep failing until Playwright/browser dependencies are installed.",
            "next_step": "Install or repair Playwright browsers, then submit the sample rerun.",
            "commands": ["python -m playwright install"],
        }
    if action in {"assert_text", "assert_response", "assert_file_exists"} or step.startswith("assert"):
        return {
            "category": "inspect_assertion_then_rerun",
            "confidence": "medium",
            "reason": "The failed step is an assertion, usually caused by fixture drift, selector drift, or expected text changing.",
            "next_step": "Open the run report, compare expected versus actual evidence, update the assertion or fixture, then rerun.",
            "commands": [],
        }
    if action in {"observe_browser", "observe_dom"} or step.startswith("observe"):
        return {
            "category": "inspect_observation_then_rerun",
            "confidence": "medium",
            "reason": "The failed step is an observation, usually caused by route fixture, auth state, or page load readiness issues.",
            "next_step": "Check route fixtures, allowed domain, storage_state readiness, and screenshot evidence before rerun.",
            "commands": [],
        }
    if action == "expect_download" or "download" in normalized:
        return {
            "category": "check_download_policy_then_rerun",
            "confidence": "medium",
            "reason": "The failure involves a download step or download policy.",
            "next_step": "Verify dry-run/confirmation policy, local route download fixture, and expected file assertion before rerun.",
            "commands": [],
        }
    if latest_task.get("status") == "failed" and not latest_report:
        return {
            "category": "inspect_queue_failure_then_rerun",
            "confidence": "medium",
            "reason": "The queue task failed before a detailed run report was available.",
            "next_step": "Inspect queue task status and workspace GUI action history, then rerun once the blocker is fixed.",
            "commands": [],
        }
    return {
        "category": "rerun_with_review",
        "confidence": "low",
        "reason": "Failure details are limited; rerun is allowed but should be reviewed with the latest report.",
        "next_step": "Open the latest run report, inspect failed step evidence, then rerun if the failure looks transient.",
        "commands": [],
    }


def build_external_sample_run_summary(
    workspace: Workspace,
    *,
    root: str | Path = "examples/external_samples",
) -> dict[str, Any]:
    readiness = external_samples_readiness(root, workspace_root=workspace.root)
    report_index = load_workspace_report_index(workspace, rebuild=True)
    queue = list_queue_tasks(workspace)
    workflow_by_sample = {
        str(sample.get("id") or "unknown"): Path(str(sample.get("workflow") or "")).name
        for sample in list_external_samples(root)
    }
    entries = []
    for entry in readiness["entries"]:
        sample_id = str(entry["sample_id"])
        reports = reports_for_external_sample(report_index, sample_id)
        tasks = queue_tasks_for_external_sample(queue, sample_id, workflow_filename=workflow_by_sample.get(sample_id, ""))
        latest_report = reports[0] if reports else None
        latest_task = tasks[0] if tasks else None
        status = external_sample_summary_status(entry, latest_report, latest_task)
        rerun_suggestion = None
        if status == "failed":
            failure_context = external_sample_failure_context(workspace, latest_report or {}, entry)
            rerun_suggestion = external_sample_rerun_suggestion(
                sample_id=sample_id,
                ready=bool(entry.get("ready")),
                blockers=list(entry.get("blockers") or []),
                latest_report=latest_report,
                latest_task=latest_task,
                failure_context=failure_context,
            )
        entries.append(
            {
                "sample_id": sample_id,
                "ready": bool(entry.get("ready")),
                "blockers": list(entry.get("blockers") or []),
                "requirements": list(entry.get("requirements") or []),
                "latest_report": latest_report,
                "latest_queue_task": latest_task,
                "report_count": len(reports),
                "queue_task_count": len(tasks),
                "status": status,
                "rerun_suggestion": rerun_suggestion,
            }
        )
    return {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "root": str(Path(root)),
        "total_samples": len(entries),
        "ready_samples": sum(1 for item in entries if item["ready"]),
        "blocked_samples": sum(1 for item in entries if not item["ready"]),
        "with_reports": sum(1 for item in entries if item["latest_report"]),
        "queued_samples": sum(1 for item in entries if item["latest_queue_task"]),
        "entries": entries,
    }


def export_external_sample_batch_report(
    workspace: Workspace,
    *,
    root: str | Path = "examples/external_samples",
) -> dict[str, Any]:
    summary = build_external_sample_run_summary(workspace, root=root)
    reports_dir = external_sample_batch_report_root(workspace)
    reports_dir.mkdir(parents=True, exist_ok=True)
    generated_at = time()
    report_id = f"external-samples-{strftime('%Y%m%d-%H%M%S')}-{int(generated_at * 1000) % 1000:03d}-{uuid4().hex[:6]}"
    payload = {
        "schema_version": 1,
        "report_id": report_id,
        "generated_at": generated_at,
        "workspace_root": str(workspace.root),
        "summary": summary,
    }
    json_path = reports_dir / f"{report_id}.json"
    markdown_path = reports_dir / f"{report_id}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(external_sample_batch_report_to_markdown(payload), encoding="utf-8")
    index_path = write_external_sample_batch_report_index(workspace)
    return {
        "schema_version": 1,
        "report_id": report_id,
        "json_report": str(json_path),
        "markdown_report": str(markdown_path),
        "index": str(index_path),
        "summary": summary,
    }


def export_external_sample_dry_run_report(
    workspace: Workspace,
    *,
    root: str | Path = "examples/external_samples",
    require_live_auth: bool = False,
    preflight: bool = True,
) -> dict[str, Any]:
    sample_root = Path(root)
    generated_at = time()
    report_id = f"external-samples-dry-run-{strftime('%Y%m%d-%H%M%S')}-{int(generated_at * 1000) % 1000:03d}-{uuid4().hex[:6]}"
    readiness = external_samples_readiness(sample_root, workspace_root=workspace.root, require_live_auth=require_live_auth)
    entries = []
    for readiness_entry_item in readiness["entries"]:
        sample_id = str(readiness_entry_item["sample_id"])
        plan = build_external_sample_run_plan(
            sample_id,
            root=sample_root,
            workspace_root=workspace.root,
            run_profile="dry-run",
            require_live_auth=require_live_auth,
        )
        if not plan["ready"]:
            entries.append(
                external_sample_dry_run_entry(
                    sample_id,
                    status="blocked",
                    ready=False,
                    attempted=False,
                    blockers=list(plan["blockers"]),
                    requirements=list(readiness_entry_item.get("requirements") or []),
                    readiness=readiness_entry_item,
                    plan=plan,
                )
            )
            continue
        try:
            run_result = run_external_sample(
                workspace,
                sample_id,
                root=sample_root,
                run_profile="dry-run",
                preflight=preflight,
                require_live_auth=require_live_auth,
            )
            entries.append(
                external_sample_dry_run_entry(
                    sample_id,
                    status=str(run_result["status"]),
                    ready=True,
                    attempted=True,
                    blockers=[],
                    requirements=list(readiness_entry_item.get("requirements") or []),
                    readiness=readiness_entry_item,
                    plan=plan,
                    run_id=str(run_result["run_id"]),
                    workflow=str(run_result["workflow"]),
                    report=run_result.get("report") if isinstance(run_result.get("report"), dict) else None,
                )
            )
        except Exception as exc:
            entries.append(
                external_sample_dry_run_entry(
                    sample_id,
                    status="failed",
                    ready=True,
                    attempted=True,
                    blockers=[],
                    requirements=list(readiness_entry_item.get("requirements") or []),
                    readiness=readiness_entry_item,
                    plan=plan,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    summary = {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "root": str(sample_root),
        "run_profile": "dry-run",
        "require_live_auth": require_live_auth,
        "total_samples": len(entries),
        "ready_samples": sum(1 for entry in entries if entry["ready"]),
        "blocked_samples": sum(1 for entry in entries if not entry["ready"]),
        "attempted_samples": sum(1 for entry in entries if entry["attempted"]),
        "success_samples": sum(1 for entry in entries if entry["status"] == "success"),
        "failed_samples": sum(1 for entry in entries if entry["status"] == "failed"),
        "entries": entries,
    }
    reports_dir = external_sample_batch_report_root(workspace)
    reports_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "report_id": report_id,
        "report_type": "dry_run_integration",
        "generated_at": generated_at,
        "workspace_root": str(workspace.root),
        "summary": summary,
    }
    json_path = reports_dir / f"{report_id}.json"
    markdown_path = reports_dir / f"{report_id}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(external_sample_dry_run_report_to_markdown(payload), encoding="utf-8")
    index_path = write_external_sample_batch_report_index(workspace)
    return {
        "schema_version": 1,
        "report_id": report_id,
        "report_type": "dry_run_integration",
        "json_report": str(json_path),
        "markdown_report": str(markdown_path),
        "index": str(index_path),
        "summary": summary,
    }


def export_external_sample_live_placeholder(
    workspace: Workspace,
    *,
    root: str | Path = "examples/external_samples",
    require_live_auth: bool = True,
) -> dict[str, Any]:
    sample_root = Path(root)
    generated_at = time()
    report_id = f"external-samples-live-placeholder-{strftime('%Y%m%d-%H%M%S')}-{int(generated_at * 1000) % 1000:03d}-{uuid4().hex[:6]}"
    readiness = external_samples_readiness(sample_root, workspace_root=workspace.root, require_live_auth=require_live_auth)
    entries = []
    for sample in list_external_samples(sample_root):
        sample_id = str(sample.get("id") or "unknown")
        entry = next((item for item in readiness["entries"] if item["sample_id"] == sample_id), None) or {}
        blockers = live_placeholder_blockers(sample, entry)
        entries.append(
            {
                "sample_id": sample_id,
                "status": "ready" if not blockers else "skipped",
                "reason": "ready_for_manual_live_coordination" if not blockers else "missing_live_account_requirements",
                "account_environment": sample.get("account_environment"),
                "owner": sample.get("owner"),
                "allowed_domains": sample.get("allowed_domains") if isinstance(sample.get("allowed_domains"), list) else [],
                "storage_state_policy": sample.get("storage_state_policy"),
                "storage_state_paths": list(entry.get("storage_state_paths") or []),
                "download_policy": sample.get("download_policy"),
                "live_execution_allowed": sample.get("live_execution_allowed"),
                "blockers": blockers,
                "required_accounts": live_placeholder_required_accounts(sample),
                "required_permissions": live_placeholder_required_permissions(sample),
                "manual_steps": live_placeholder_manual_steps(sample, entry),
                "readiness": entry,
            }
        )
    summary = {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "root": str(sample_root),
        "require_live_auth": require_live_auth,
        "total_samples": len(entries),
        "ready_samples": sum(1 for entry in entries if entry["status"] == "ready"),
        "skipped_samples": sum(1 for entry in entries if entry["status"] == "skipped"),
        "entries": entries,
    }
    status = "ready" if summary["ready_samples"] > 0 and summary["skipped_samples"] == 0 else "skipped"
    payload = {
        "schema_version": 1,
        "report_id": report_id,
        "report_type": "live_account_placeholder",
        "status": status,
        "generated_at": generated_at,
        "workspace_root": str(workspace.root),
        "summary": summary,
    }
    reports_dir = external_sample_batch_report_root(workspace)
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / f"{report_id}.json"
    markdown_path = reports_dir / f"{report_id}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(external_sample_live_placeholder_to_markdown(payload), encoding="utf-8")
    index_path = write_external_sample_batch_report_index(workspace)
    return {
        "schema_version": 1,
        "report_id": report_id,
        "report_type": "live_account_placeholder",
        "status": status,
        "json_report": str(json_path),
        "markdown_report": str(markdown_path),
        "index": str(index_path),
        "summary": summary,
    }


def live_placeholder_blockers(sample: dict[str, Any], readiness_entry: dict[str, Any]) -> list[str]:
    blockers = list(readiness_entry.get("blockers") or [])
    if sample.get("live_execution_allowed") is not True:
        blockers.append("live_execution_not_authorized")
    if sample.get("owner") in {None, "", "sample-owner-required"}:
        blockers.append("missing_real_account_owner")
    if sample.get("data_classification") in {None, "", "test-account-only"}:
        blockers.append("missing_real_data_classification")
    allowed_domains = sample.get("allowed_domains") if isinstance(sample.get("allowed_domains"), list) else []
    if not allowed_domains or any(str(domain).endswith(".example.com") for domain in allowed_domains):
        blockers.append("placeholder_allowed_domain")
    return sorted(set(str(item) for item in blockers if str(item)))


def live_placeholder_required_accounts(sample: dict[str, Any]) -> list[str]:
    domains = sample.get("allowed_domains") if isinstance(sample.get("allowed_domains"), list) else []
    environment = str(sample.get("account_environment") or "sandbox")
    return [f"{environment} account for {domain}" for domain in domains] or [f"{environment} external test account"]


def live_placeholder_required_permissions(sample: dict[str, Any]) -> list[str]:
    permissions = ["read-only page access", "permission to store Playwright storage_state locally"]
    if sample.get("download_policy") == "confirm-required":
        permissions.append("explicit confirmation for test download/export")
    if sample.get("storage_state_policy") == "required":
        permissions.append("valid non-expired authenticated session")
    return permissions


def live_placeholder_manual_steps(sample: dict[str, Any], readiness_entry: dict[str, Any]) -> list[str]:
    steps = [
        "Replace placeholder domains with approved sandbox/staging domains in the sample catalog.",
        "Confirm live_execution_allowed=true only after owner approval and scope review.",
        "Run external-samples-readiness --require-live-auth before any supervised run.",
    ]
    if sample.get("storage_state_policy") == "required":
        paths = list(readiness_entry.get("storage_state_paths") or [])
        target = paths[0] if paths else ".agent-auth/<account>.json"
        steps.insert(0, f"Import a valid Playwright storage_state into {target}.")
    return steps


def external_sample_dry_run_entry(
    sample_id: str,
    *,
    status: str,
    ready: bool,
    attempted: bool,
    blockers: list[str],
    requirements: list[str],
    readiness: dict[str, Any],
    plan: dict[str, Any],
    run_id: str | None = None,
    workflow: str | None = None,
    report: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "sample_id": sample_id,
        "status": status,
        "ready": ready,
        "attempted": attempted,
        "blockers": blockers,
        "requirements": requirements,
        "run_id": run_id,
        "workflow": workflow,
        "report": report,
        "error": error,
        "readiness": readiness,
        "plan": plan,
    }


def write_external_sample_batch_report(
    workspace: Workspace,
    *,
    root: str | Path = "examples/external_samples",
) -> dict[str, Any]:
    return export_external_sample_batch_report(workspace, root=root)


def external_sample_batch_report_root(workspace: Workspace) -> Path:
    return workspace.reports_dir / "external_samples"


def load_external_sample_batch_report(workspace: Workspace, report_id: str) -> dict[str, Any]:
    root = external_sample_batch_report_root(workspace)
    path = root / f"{report_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"External sample batch report not found: {report_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_external_sample_batch_reports(
    workspace: Workspace,
    *,
    status: str | None = None,
    sample_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    root = external_sample_batch_report_root(workspace)
    if not root.exists():
        return ()
    entries: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.name == "index.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        entry = external_sample_batch_report_entry(workspace, root, path, payload)
        if status is not None and entry["status"] != status:
            continue
        if sample_id is not None and sample_id not in entry["sample_ids"]:
            continue
        entries.append(entry)
    return tuple(entries)


def build_external_sample_batch_report_index(
    workspace: Workspace,
    *,
    status: str | None = None,
    sample_id: str | None = None,
) -> dict[str, Any]:
    root = external_sample_batch_report_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    entries = list(list_external_sample_batch_reports(workspace, status=status, sample_id=sample_id))
    return {
        "schema_version": 1,
        "generated_at": time(),
        "workspace_root": str(workspace.root),
        "report_root": str(root),
        "filters": {
            "status": status,
            "sample_id": sample_id,
        },
        "total_reports": len(entries),
        "failed_reports": sum(1 for entry in entries if entry["status"] == "failed"),
        "queued_reports": sum(1 for entry in entries if entry["status"] == "queued"),
        "blocked_reports": sum(1 for entry in entries if entry["status"] == "blocked"),
        "success_reports": sum(1 for entry in entries if entry["status"] == "success"),
        "latest": entries[0] if entries else None,
        "entries": entries,
    }


def write_external_sample_batch_report_index(workspace: Workspace) -> Path:
    root = external_sample_batch_report_root(workspace)
    index = build_external_sample_batch_report_index(workspace)
    path = root / "index.json"
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_external_sample_batch_report_index(
    workspace: Workspace,
    *,
    rebuild: bool = False,
    status: str | None = None,
    sample_id: str | None = None,
) -> dict[str, Any]:
    root = external_sample_batch_report_root(workspace)
    if rebuild or status is not None or sample_id is not None:
        index = build_external_sample_batch_report_index(workspace, status=status, sample_id=sample_id)
        if status is None and sample_id is None:
            write_external_sample_batch_report_index(workspace)
        return index
    path = root / "index.json"
    if not path.exists():
        write_external_sample_batch_report_index(workspace)
    return json.loads(path.read_text(encoding="utf-8"))


def external_sample_batch_report_entry(
    workspace: Workspace,
    root: Path,
    report_path: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    entries = summary.get("entries") if isinstance(summary.get("entries"), list) else []
    sample_statuses = {
        str(entry.get("sample_id") or "unknown"): str(entry.get("status") or "unknown")
        for entry in entries
        if isinstance(entry, dict)
    }
    markdown_path = report_path.with_suffix(".md")
    return {
        "report_id": str(payload.get("report_id") or report_path.stem),
        "status": external_sample_batch_status(summary),
        "generated_at": payload.get("generated_at"),
        "total_samples": int(summary.get("total_samples") or 0),
        "ready_samples": int(summary.get("ready_samples") or 0),
        "blocked_samples": int(summary.get("blocked_samples") or 0),
        "queued_samples": int(summary.get("queued_samples") or 0),
        "with_reports": int(summary.get("with_reports") or 0),
        "failed_samples": [sample_id for sample_id, item_status in sample_statuses.items() if item_status == "failed"],
        "sample_ids": list(sample_statuses),
        "sample_statuses": sample_statuses,
        "json_report": report_path.relative_to(workspace.root).as_posix()
        if report_path.is_relative_to(workspace.root)
        else (report_path.relative_to(root).as_posix() if report_path.is_relative_to(root) else str(report_path)),
        "markdown_report": markdown_path.relative_to(workspace.root).as_posix()
        if markdown_path.exists() and markdown_path.is_relative_to(workspace.root)
        else (str(markdown_path) if markdown_path.exists() else None),
        "modified_at": report_path.stat().st_mtime,
    }


def external_sample_batch_status(summary: dict[str, Any]) -> str:
    entries = summary.get("entries") if isinstance(summary.get("entries"), list) else []
    statuses = [str(entry.get("status") or "") for entry in entries if isinstance(entry, dict)]
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status == "running" for status in statuses):
        return "running"
    if any(status in {"pending", "queued"} for status in statuses):
        return "queued"
    if any(status == "blocked" for status in statuses):
        return "blocked"
    if statuses and all(status == "success" for status in statuses):
        return "success"
    return "ready" if statuses else "empty"


def external_sample_batch_report_to_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    status = external_sample_batch_status(summary)
    lines = [
        "# External Sample Batch Report",
        "",
        f"- Report ID: `{payload['report_id']}`",
        f"- Batch status: `{status}`",
        f"- Workspace: `{payload['workspace_root']}`",
        f"- Samples: {summary['total_samples']}",
        f"- Ready: {summary['ready_samples']}",
        f"- Blocked: {summary['blocked_samples']}",
        f"- Queued: {summary['queued_samples']}",
        f"- With reports: {summary['with_reports']}",
        "",
        "## Samples",
        "",
        "| sample_id | status | ready | queue | report | blockers |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in summary.get("entries", []):
        latest_task = entry.get("latest_queue_task") if isinstance(entry.get("latest_queue_task"), dict) else {}
        latest_report = entry.get("latest_report") if isinstance(entry.get("latest_report"), dict) else {}
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(entry.get("sample_id")),
                    markdown_cell(entry.get("status")),
                    markdown_cell(entry.get("ready")),
                    markdown_cell(latest_task.get("status") or ""),
                    markdown_cell(latest_report.get("status") or ""),
                    markdown_cell(", ".join(entry.get("blockers") or [])),
                ]
            )
            + " |"
        )
    append_external_sample_batch_failure_markdown(lines, payload)
    return "\n".join(lines).rstrip() + "\n"


def append_external_sample_batch_failure_markdown(lines: list[str], payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    entries = summary.get("entries") if isinstance(summary.get("entries"), list) else []
    failed_entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("status") == "failed"]
    blocked_entries = [entry for entry in entries if isinstance(entry, dict) and not entry.get("ready")]
    if not failed_entries and not blocked_entries:
        lines.extend(["", "## Review Notes", "", "- No failed or blocked samples in this batch."])
        return

    if failed_entries:
        ready_failures = [entry for entry in failed_entries if entry.get("ready")]
        blocked_failures = [entry for entry in failed_entries if not entry.get("ready")]
        lines.extend(
            [
                "",
                "## Failure Summary",
                "",
                f"- Failed samples: {len(failed_entries)}",
                f"- Ready for rerun: {len(ready_failures)}",
                f"- Blocked from rerun: {len(blocked_failures)}",
                "",
                "| sample_id | ready | run_id | task_id | failed_step | suggestion | blockers |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for entry in failed_entries:
            latest_report = entry.get("latest_report") if isinstance(entry.get("latest_report"), dict) else {}
            latest_task = entry.get("latest_queue_task") if isinstance(entry.get("latest_queue_task"), dict) else {}
            suggestion = entry.get("rerun_suggestion") if isinstance(entry.get("rerun_suggestion"), dict) else {}
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(entry.get("sample_id")),
                        markdown_cell(entry.get("ready")),
                        markdown_cell(latest_report.get("run_id") or ""),
                        markdown_cell(latest_task.get("task_id") or ""),
                        markdown_cell(latest_report.get("failed_step") or ""),
                        markdown_cell(suggestion.get("next_step") or ""),
                        markdown_cell(", ".join(entry.get("blockers") or [])),
                    ]
                )
                + " |"
            )
        lines.extend(
            [
                "",
                "## Rerun Commands",
                "",
                "```powershell",
                ".\\.venv\\Scripts\\python.exe -m visual_agent.cli external-sample-batch-failures --workspace-root .agent-workspace --report-id "
                + str(payload.get("report_id") or ""),
                ".\\.venv\\Scripts\\python.exe -m visual_agent.cli external-sample-batch-rerun-plan --workspace-root .agent-workspace --report-id "
                + str(payload.get("report_id") or ""),
                ".\\.venv\\Scripts\\python.exe -m visual_agent.cli external-sample-batch-rerun-submit --workspace-root .agent-workspace --report-id "
                + str(payload.get("report_id") or ""),
                "```",
            ]
        )

    if blocked_entries:
        lines.extend(
            [
                "",
                "## Blocked Samples",
                "",
                "| sample_id | status | blockers | remediation_hint |",
                "| --- | --- | --- | --- |",
            ]
        )
        for entry in blocked_entries:
            blockers = list(entry.get("blockers") or [])
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(entry.get("sample_id")),
                        markdown_cell(entry.get("status")),
                        markdown_cell(", ".join(blockers)),
                        markdown_cell(blocked_sample_remediation_hint(blockers)),
                    ]
                )
                + " |"
            )


def external_sample_dry_run_report_to_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# External Sample Dry-Run Report",
        "",
        f"- Report ID: `{payload['report_id']}`",
        f"- Workspace: `{payload['workspace_root']}`",
        f"- Run profile: `{summary['run_profile']}`",
        f"- Require live auth: `{summary['require_live_auth']}`",
        f"- Samples: {summary['total_samples']}",
        f"- Ready: {summary['ready_samples']}",
        f"- Blocked: {summary['blocked_samples']}",
        f"- Attempted: {summary['attempted_samples']}",
        f"- Success: {summary['success_samples']}",
        f"- Failed: {summary['failed_samples']}",
        "",
        "## Samples",
        "",
        "| sample_id | status | ready | attempted | run_id | blockers | error |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in summary.get("entries", []):
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(entry.get("sample_id")),
                    markdown_cell(entry.get("status")),
                    markdown_cell(entry.get("ready")),
                    markdown_cell(entry.get("attempted")),
                    markdown_cell(entry.get("run_id") or ""),
                    markdown_cell(", ".join(entry.get("blockers") or [])),
                    markdown_cell(entry.get("error") or ""),
                ]
            )
            + " |"
        )
    blocked = [entry for entry in summary.get("entries", []) if isinstance(entry, dict) and entry.get("status") == "blocked"]
    if blocked:
        lines.extend(
            [
                "",
                "## Blocked Samples",
                "",
                "| sample_id | requirements | blockers | remediation_hint |",
                "| --- | --- | --- | --- |",
            ]
        )
        for entry in blocked:
            blockers = list(entry.get("blockers") or [])
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(entry.get("sample_id")),
                        markdown_cell(", ".join(entry.get("requirements") or [])),
                        markdown_cell(", ".join(blockers)),
                        markdown_cell(blocked_sample_remediation_hint(blockers)),
                    ]
                )
                + " |"
            )
    failed = [entry for entry in summary.get("entries", []) if isinstance(entry, dict) and entry.get("status") == "failed"]
    if failed:
        lines.extend(
            [
                "",
                "## Failed Dry-Runs",
                "",
                "| sample_id | run_id | error | report |",
                "| --- | --- | --- | --- |",
            ]
        )
        for entry in failed:
            report = entry.get("report") if isinstance(entry.get("report"), dict) else {}
            lines.append(
                "| "
                + " | ".join(
                    [
                        markdown_cell(entry.get("sample_id")),
                        markdown_cell(entry.get("run_id") or ""),
                        markdown_cell(entry.get("error") or ""),
                        markdown_cell(report.get("markdown_report") or report.get("json_report") or ""),
                    ]
                )
                + " |"
            )
    if not blocked and not failed:
        lines.extend(["", "## Review Notes", "", "- All ready external samples completed dry-run without blockers or failures."])
    return "\n".join(lines).rstrip() + "\n"


def external_sample_live_placeholder_to_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# External Sample Live Account Placeholder",
        "",
        f"- Report ID: `{payload['report_id']}`",
        f"- Status: `{payload['status']}`",
        f"- Workspace: `{payload['workspace_root']}`",
        f"- Require live auth: `{summary['require_live_auth']}`",
        f"- Samples: {summary['total_samples']}",
        f"- Ready: {summary['ready_samples']}",
        f"- Skipped: {summary['skipped_samples']}",
        "",
        "## Samples",
        "",
        "| sample_id | status | accounts | permissions | blockers |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in summary.get("entries", []):
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(entry.get("sample_id")),
                    markdown_cell(entry.get("status")),
                    markdown_cell(", ".join(entry.get("required_accounts") or [])),
                    markdown_cell(", ".join(entry.get("required_permissions") or [])),
                    markdown_cell(", ".join(entry.get("blockers") or [])),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Manual Checklist", ""])
    for entry in summary.get("entries", []):
        lines.extend([f"### {entry.get('sample_id')}", ""])
        for step in entry.get("manual_steps") or []:
            lines.append(f"- {step}")
        if entry.get("status") == "skipped":
            lines.append("- Current state: skipped until the blockers above are resolved.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def blocked_sample_remediation_hint(blockers: list[str]) -> str:
    if "missing_storage_state_file" in blockers:
        return "Import the required storage_state with auth-state-import, then rebuild readiness."
    if blockers:
        return "Resolve the listed readiness blockers before rerun."
    return "Review readiness status before rerun."


def markdown_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ").strip()


def reports_for_external_sample(report_index: dict[str, Any], sample_id: str) -> list[dict[str, Any]]:
    reports = []
    for entry in report_index.get("entries", []) if isinstance(report_index.get("entries"), list) else []:
        if not isinstance(entry, dict):
            continue
        external = entry.get("external_sample") if isinstance(entry.get("external_sample"), dict) else {}
        if external.get("sample_id") == sample_id:
            reports.append(entry)
    return reports


def queue_tasks_for_external_sample(queue: dict[str, Any], sample_id: str, *, workflow_filename: str) -> list[dict[str, Any]]:
    workflow_name = f"external_samples/{workflow_filename}"
    workflow_path = f"workflows/{workflow_name}"
    tasks = []
    for entry in queue.get("entries", []) if isinstance(queue.get("entries"), list) else []:
        if not isinstance(entry, dict):
            continue
        if entry.get("workflow") in {workflow_name, workflow_path}:
            tasks.append(entry)
    return tasks


def external_sample_summary_status(
    readiness_entry: dict[str, Any],
    latest_report: dict[str, Any] | None,
    latest_task: dict[str, Any] | None,
) -> str:
    if latest_report:
        return str(latest_report.get("status") or "reported")
    if latest_task:
        return str(latest_task.get("status") or "queued")
    if not readiness_entry.get("ready"):
        return "blocked"
    return "ready"


def external_sample_run_status(result: Any) -> str:
    return "failed" if any(step.status == ActionStatus.FAILED for step in result.steps) else "success"


def annotate_external_sample_report(
    workspace: Workspace,
    run_id: str,
    *,
    plan: dict[str, Any],
    run_status: str,
) -> dict[str, str | None]:
    json_path = workspace.reports_dir / f"{run_id}.json"
    markdown_path = workspace.reports_dir / f"{run_id}.md"
    external_sample = external_sample_report_block(plan, run_status=run_status)
    if json_path.exists():
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        payload["external_sample"] = external_sample
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if markdown_path.exists():
        markdown = markdown_path.read_text(encoding="utf-8")
        markdown_path.write_text(markdown.rstrip() + "\n\n" + external_sample_markdown(external_sample), encoding="utf-8")
    index_path = write_workspace_report_index(workspace)
    return {
        "json_report": str(json_path) if json_path.exists() else None,
        "markdown_report": str(markdown_path) if markdown_path.exists() else None,
        "index": str(index_path),
    }


def external_sample_report_block(plan: dict[str, Any], *, run_status: str) -> dict[str, Any]:
    readiness = plan.get("readiness") if isinstance(plan.get("readiness"), dict) else {}
    return {
        "schema_version": 1,
        "sample_id": plan.get("sample_id"),
        "sample_root": plan.get("sample_root"),
        "workflow": plan.get("workflow"),
        "run_status": run_status,
        "run_profile": plan.get("run_profile"),
        "dry_run": plan.get("dry_run"),
        "ready_at_run": plan.get("ready"),
        "blockers_at_run": list(plan.get("blockers") or []),
        "requirements": list(readiness.get("requirements") or []),
        "storage_state_paths": list(readiness.get("storage_state_paths") or []),
        "allowed_domains": list(plan.get("allowed_domains") or []),
        "storage_state_policy": plan.get("storage_state_policy"),
        "download_policy": plan.get("download_policy"),
        "mutating_action_policy": plan.get("mutating_action_policy"),
        "policy": plan.get("policy") if isinstance(plan.get("policy"), dict) else {},
        "requires_confirmation": plan.get("requires_confirmation"),
    }


def external_sample_markdown(external_sample: dict[str, Any]) -> str:
    return "\n".join(
        [
            "## External Sample",
            "",
            f"- Sample ID: `{external_sample.get('sample_id')}`",
            f"- Run status: `{external_sample.get('run_status')}`",
            f"- Run profile: `{external_sample.get('run_profile')}`",
            f"- Ready at run: `{external_sample.get('ready_at_run')}`",
            f"- Blockers at run: {', '.join(external_sample.get('blockers_at_run') or []) or 'none'}",
            f"- Requirements: {', '.join(external_sample.get('requirements') or []) or 'none'}",
            f"- Allowed domains: {', '.join(external_sample.get('allowed_domains') or []) or 'none'}",
            f"- Storage state policy: `{external_sample.get('storage_state_policy')}`",
            f"- Download policy: `{external_sample.get('download_policy')}`",
            f"- Mutating action policy: `{external_sample.get('mutating_action_policy')}`",
            "",
        ]
    )


def materialize_external_sample_workflow(source: Path, target: Path, *, sample_root: Path) -> None:
    """Copy a sample workflow into a workspace with stable local fixture paths."""
    try:
        import yaml
    except ImportError:
        shutil.copyfile(source, target)
        return
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        shutil.copyfile(source, target)
        return
    for step in payload.get("steps", []) if isinstance(payload.get("steps"), list) else []:
        if not isinstance(step, dict):
            continue
        for route in step.get("routes", []) if isinstance(step.get("routes"), list) else []:
            if not isinstance(route, dict) or not route.get("body_from_file"):
                continue
            route["body_from_file"] = str(resolve_route_body_file(route["body_from_file"], sample_root=sample_root))
    target.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def resolve_route_body_file(value: Any, *, sample_root: Path) -> Path:
    path = Path(str(value))
    if path.is_absolute():
        return path
    if path.exists():
        return path.resolve()
    candidate = sample_root / path
    if candidate.exists():
        return candidate.resolve()
    return path.resolve()


def check_external_sample(sample: dict[str, Any], *, sample_root: Path) -> ExternalSampleCheck:
    sample_id = str(sample.get("id") or "unknown")
    issues: list[ExternalSampleIssue] = []
    workflow_path_value = sample.get("workflow")
    if not workflow_path_value:
        issues.append(issue(sample_id, "error", "missing_workflow", "Sample is missing workflow path."))
        return ExternalSampleCheck(valid=False, sample_id=sample_id, workflow_name=None, issues=tuple(issues))

    workflow_path = sample_root / str(workflow_path_value)
    if not workflow_path.exists():
        issues.append(issue(sample_id, "error", "workflow_not_found", f"Workflow not found: {workflow_path_value}"))
        return ExternalSampleCheck(valid=False, sample_id=sample_id, workflow_name=None, issues=tuple(issues))

    validation = validate_workflow_file(workflow_path)
    for validation_issue in validation.issues:
        issues.append(
            issue(
                sample_id,
                validation_issue.level,
                "workflow_validation",
                validation_issue.message,
                step_id=validation_issue.step_id,
            )
        )

    workflow = parse_workflow_file(workflow_path)
    issues.extend(sample_metadata_issues(sample_id, sample))
    issues.extend(workflow_external_issues(sample_id, workflow, sample))
    valid = not any(item.level == "error" for item in issues)
    return ExternalSampleCheck(valid=valid, sample_id=sample_id, workflow_name=workflow.name, issues=tuple(issues))


def sample_metadata_issues(sample_id: str, sample: dict[str, Any]) -> list[ExternalSampleIssue]:
    issues: list[ExternalSampleIssue] = []
    if sample.get("live_execution_allowed") is not False:
        issues.append(issue(sample_id, "error", "live_execution_not_disabled", "External samples must set live_execution_allowed: false."))
    if not sample.get("owner"):
        issues.append(issue(sample_id, "warning", "missing_owner", "Sample should name an owner for account/access coordination."))
    if not sample.get("data_classification"):
        issues.append(issue(sample_id, "warning", "missing_data_classification", "Sample should declare data_classification."))
    environment = sample.get("account_environment")
    if environment not in ALLOWED_ENVIRONMENTS:
        issues.append(
            issue(
                sample_id,
                "error",
                "invalid_account_environment",
                "External samples must declare account_environment as sandbox, staging, or test.",
            )
        )
    storage_policy = sample.get("storage_state_policy")
    if storage_policy not in ALLOWED_STORAGE_STATE_POLICIES:
        issues.append(
            issue(
                sample_id,
                "error",
                "invalid_storage_state_policy",
                "External samples must declare storage_state_policy as required, optional, or forbidden.",
            )
        )
    download_policy = sample.get("download_policy")
    if download_policy not in ALLOWED_DOWNLOAD_POLICIES:
        issues.append(
            issue(
                sample_id,
                "error",
                "invalid_download_policy",
                "External samples must declare download_policy as dry-run-only, confirm-required, or forbidden.",
            )
        )
    mutating_policy = sample.get("mutating_action_policy")
    if mutating_policy not in ALLOWED_MUTATING_ACTION_POLICIES:
        issues.append(
            issue(
                sample_id,
                "error",
                "invalid_mutating_action_policy",
                "External samples must declare mutating_action_policy as dry-run-or-confirm, confirm-required, or forbidden.",
            )
        )
    allowed_domains = sample.get("allowed_domains")
    if not isinstance(allowed_domains, list) or not all(isinstance(item, str) and item for item in allowed_domains):
        issues.append(issue(sample_id, "error", "missing_allowed_domains", "External samples must declare allowed_domains."))
    return issues


def workflow_external_issues(sample_id: str, workflow: Workflow, sample: dict[str, Any] | None = None) -> list[ExternalSampleIssue]:
    issues: list[ExternalSampleIssue] = []
    has_external_observation = False
    has_assertion = False
    allowed_domains = tuple(str(item).lower() for item in (sample or {}).get("allowed_domains", []) if isinstance(item, str))
    storage_policy = (sample or {}).get("storage_state_policy")
    download_policy = (sample or {}).get("download_policy")
    mutating_policy = (sample or {}).get("mutating_action_policy") or "dry-run-or-confirm"
    for step in workflow.steps:
        if step.action in {"observe_browser", "observe_dom"}:
            url = str(step.params.get("url") or "")
            if url.startswith("https://") and "example.test" not in url:
                has_external_observation = True
            if not url.startswith("https://"):
                issues.append(issue(sample_id, "warning", "non_https_url", "External sample should use https URL.", step.id))
            if allowed_domains and url and not url_matches_allowed_domain(url, allowed_domains):
                issues.append(issue(sample_id, "error", "url_outside_allowed_domains", "Observed URL is outside allowed_domains.", step.id))
            storage_state = step.params.get("storage_state")
            if storage_policy == "required" and not storage_state:
                issues.append(issue(sample_id, "error", "missing_storage_state", "This sample requires observe_browser storage_state.", step.id))
            if storage_policy == "forbidden" and storage_state:
                issues.append(issue(sample_id, "error", "forbidden_storage_state", "This sample forbids storage_state.", step.id))
        if step.action.startswith("assert_"):
            has_assertion = True
        if step.action == "expect_download":
            if download_policy == "forbidden":
                issues.append(issue(sample_id, "error", "download_forbidden", "This sample forbids downloads.", step.id))
            if download_policy == "dry-run-only" and step.params.get("dry_run") is not True:
                issues.append(issue(sample_id, "error", "download_must_be_dry_run", "This sample only allows dry-run downloads.", step.id))
            if download_policy == "confirm-required" and step.params.get("require_confirm") is not True:
                issues.append(issue(sample_id, "error", "download_requires_confirm", "This sample requires confirmed downloads.", step.id))
        if step.action in MUTATING_ACTIONS:
            mutating_issue = mutating_action_policy_issue(sample_id, step.id, step.action, step.params, str(mutating_policy))
            if mutating_issue is not None:
                issues.append(mutating_issue)
        for key, value in step.params.items():
            if is_sensitive_inline_value(key, value):
                issues.append(issue(sample_id, "error", "inline_secret", f"Inline sensitive value is not allowed: {key}", step.id))
    if not has_external_observation:
        issues.append(issue(sample_id, "error", "missing_external_observation", "Sample must observe an external https business URL."))
    if not has_assertion:
        issues.append(issue(sample_id, "error", "missing_assertion", "Sample must include at least one assertion."))
    return issues


def mutating_action_policy_issue(
    sample_id: str,
    step_id: str,
    action: str,
    params: dict[str, Any],
    policy: str,
) -> ExternalSampleIssue | None:
    if policy == "forbidden":
        return issue(sample_id, "error", "mutating_action_forbidden", f"Policy forbids mutating action: {action}.", step_id)
    if policy == "confirm-required" and params.get("require_confirm") is not True:
        return issue(sample_id, "error", "mutating_action_requires_confirm", "Policy requires mutating steps to set require_confirm: true.", step_id)
    if policy == "dry-run-or-confirm" and params.get("dry_run") is not True and params.get("require_confirm") is not True:
        return issue(
            sample_id,
            "error",
            "unsafe_mutating_step",
            "External sample mutating steps must set dry_run: true or require_confirm: true.",
            step_id,
        )
    return None


def is_sensitive_inline_value(key: str, value: Any) -> bool:
    normalized_key = key.lower()
    if not any(hint in normalized_key for hint in SENSITIVE_HINTS):
        return False
    return value not in (None, "", True, False) and not str(value).startswith("input.")


def readiness_entry(
    sample: dict[str, Any],
    check: ExternalSampleCheck,
    workflow: Workflow | None,
    *,
    workspace_root: Path = Path("."),
    require_live_auth: bool = False,
) -> dict[str, Any]:
    requirements = []
    blockers = []
    storage_state_paths = []
    storage_state_files = []
    if sample.get("storage_state_policy") == "required":
        requirements.append("storage_state_file")
        storage_state_paths = [
            str(step.params["storage_state"])
            for step in workflow.steps
            if step.action == "observe_browser" and step.params.get("storage_state")
        ] if workflow else []
        if not storage_state_paths:
            blockers.append("missing_storage_state")
        for raw_path in storage_state_paths:
            status = storage_state_readiness_status(
                raw_path,
                workspace_root=workspace_root,
                allowed_domains=sample.get("allowed_domains") if isinstance(sample.get("allowed_domains"), list) else [],
            )
            storage_state_files.append(status)
            if not status["exists"]:
                blockers.append("missing_storage_state_file")
            if require_live_auth and not status["auth_ready"]:
                blockers.append("auth_state_not_ready")
    if sample.get("download_policy") == "confirm-required":
        requirements.append("download_confirmation")
    if sample.get("live_execution_allowed") is not False:
        blockers.append("live_execution_not_disabled")
    if not check.valid:
        blockers.extend(issue.code for issue in check.issues if issue.level == "error")
    blockers = sorted(set(blockers))
    return {
        "sample_id": check.sample_id,
        "workflow_name": check.workflow_name,
        "account_environment": sample.get("account_environment"),
        "allowed_domains": sample.get("allowed_domains") if isinstance(sample.get("allowed_domains"), list) else [],
        "storage_state_policy": sample.get("storage_state_policy"),
        "mutating_action_policy": sample.get("mutating_action_policy"),
        "policy": sample.get("policy") if isinstance(sample.get("policy"), dict) else {},
        "storage_state_paths": storage_state_paths,
        "storage_state_files": storage_state_files,
        "auth_state_ready": all(item.get("auth_ready") for item in storage_state_files) if storage_state_files else None,
        "download_policy": sample.get("download_policy"),
        "requirements": sorted(set(requirements)),
        "ready": not blockers,
        "blockers": blockers,
    }


def storage_state_readiness_status(
    raw_path: str,
    *,
    workspace_root: Path,
    allowed_domains: list[Any],
) -> dict[str, Any]:
    resolved = resolve_readiness_path(raw_path, workspace_root)
    base = {
        "path": str(raw_path),
        "resolved_path": str(resolved),
        "exists": resolved.exists(),
        "valid": False,
        "auth_ready": False,
        "status": "missing",
        "domains": [],
        "origin_hosts": [],
        "matched_allowed_domains": [],
        "cookie_count": 0,
        "origin_count": 0,
        "expired_cookie_count": 0,
        "has_session_material": False,
        "redacted": True,
    }
    if not resolved.exists():
        return base
    try:
        metadata = inspect_storage_state(resolved)
    except Exception as exc:
        return {**base, "exists": True, "status": "invalid", "error": exc.__class__.__name__}
    allowed = [str(domain).strip().lower() for domain in allowed_domains if str(domain).strip()]
    hosts = list(metadata.get("domains") or []) + list(metadata.get("origin_hosts") or [])
    matched = sorted({domain for domain in allowed if any(auth_host_matches_allowed(str(host), domain) for host in hosts)})
    has_session = bool(metadata.get("has_session_material"))
    cookie_count = int(metadata.get("cookie_count") or 0)
    expired_count = int(metadata.get("expired_cookie_count") or 0)
    all_cookies_expired = cookie_count > 0 and expired_count >= cookie_count and int(metadata.get("origin_count") or 0) == 0
    auth_ready = bool(has_session and matched and not all_cookies_expired)
    status = "ready"
    if not has_session:
        status = "empty"
    elif not matched:
        status = "domain_mismatch"
    elif all_cookies_expired:
        status = "expired"
    return {
        **base,
        "exists": True,
        "valid": True,
        "auth_ready": auth_ready,
        "status": status,
        "domains": list(metadata.get("domains") or []),
        "origin_hosts": list(metadata.get("origin_hosts") or []),
        "matched_allowed_domains": matched,
        "cookie_count": cookie_count,
        "origin_count": int(metadata.get("origin_count") or 0),
        "expired_cookie_count": expired_count,
        "has_session_material": has_session,
        "earliest_cookie_expires_at": metadata.get("earliest_cookie_expires_at"),
        "redacted": True,
    }


def auth_host_matches_allowed(host: str, allowed_domain: str) -> bool:
    host = host.strip().lower().lstrip(".")
    allowed = allowed_domain.strip().lower().lstrip(".")
    return bool(host and allowed and (host == allowed or host.endswith(f".{allowed}") or allowed.endswith(f".{host}")))


def url_matches_allowed_domain(url: str, allowed_domains: tuple[str, ...]) -> bool:
    text = url.lower()
    return any(text.startswith(f"https://{domain}") or text.startswith(f"https://www.{domain}") for domain in allowed_domains)


def resolve_readiness_path(value: str, workspace_root: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return workspace_root / path


def issue(
    sample_id: str,
    level: str,
    code: str,
    message: str,
    step_id: str | None = None,
) -> ExternalSampleIssue:
    return ExternalSampleIssue(level=level, code=code, message=message, sample_id=sample_id, step_id=step_id)
