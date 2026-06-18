from __future__ import annotations

import base64
import html
import json
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .visual_status import read_run_history
from .versioning import UnsupportedSchemaVersionError, migrate_report_payload


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    run_dir: Path
    workflow_name: str
    workflow_schema_version: int | None
    runtime_version: str | None
    run_profile: str | None
    status: str
    total_steps: int
    succeeded_steps: int
    failed_step: str | None
    dry_run_actions: int


@dataclass(frozen=True)
class StepReport:
    id: str
    action: str
    status: str
    message: str
    elapsed_seconds: float | None
    attempts: int | None
    provider: str | None
    target: str | None
    selector_resolution: dict[str, Any] | None
    observation_summary: dict[str, Any] | None
    artifact_paths: tuple[str, ...]
    failure_artifacts: dict[str, Any] | None
    failure_diagnosis: dict[str, Any] | None


@dataclass(frozen=True)
class RunReport:
    schema_version: int
    run_id: str
    run_dir: Path
    workflow_name: str
    workflow_schema_version: int | None
    runtime_version: str | None
    run_profile: str | None
    status: str
    total_steps: int
    succeeded_steps: int
    failed_step: str | None
    dry_run_actions: int
    elapsed_seconds: float
    artifacts: dict[str, Any]
    downloads: tuple[dict[str, Any], ...]
    steps: tuple[StepReport, ...]
    run_lock: dict[str, Any] | None = None
    run_queue: dict[str, Any] | None = None
    run_checks: dict[str, Any] | None = None
    acceptance: dict[str, Any] | None = None


def load_run_summary(run_dir: str | Path) -> RunSummary:
    path = Path(run_dir)
    raw_payload = json.loads((path / "workflow_result.json").read_text(encoding="utf-8"))
    try:
        payload = migrate_report_payload(raw_payload)
    except UnsupportedSchemaVersionError:
        return RunSummary(
            run_id=str(raw_payload.get("run_id") or path.name),
            run_dir=path,
            workflow_name=str(raw_payload.get("workflow_name") or ""),
            workflow_schema_version=raw_payload.get("workflow_schema_version"),
            runtime_version=raw_payload.get("runtime_version"),
            run_profile=raw_payload.get("run_profile"),
            status="upgrade_required",
            total_steps=0,
            succeeded_steps=0,
            failed_step=None,
            dry_run_actions=0,
        )
    steps = payload.get("steps", [])
    failed = next((step for step in steps if step.get("status") == "failed"), None)
    return RunSummary(
        run_id=str(payload.get("run_id") or path.name),
        run_dir=path,
        workflow_name=str(payload.get("workflow_name") or ""),
        workflow_schema_version=payload.get("workflow_schema_version"),
        runtime_version=payload.get("runtime_version"),
        run_profile=payload.get("run_profile"),
        status="failed" if failed else "success",
        total_steps=len(steps),
        succeeded_steps=sum(1 for step in steps if step.get("status") in {"success", "dry_run"}),
        failed_step=failed.get("id") if failed else None,
        dry_run_actions=sum(1 for step in steps if step.get("status") == "dry_run"),
    )


def load_run_report(run_dir: str | Path) -> RunReport:
    path = Path(run_dir)
    raw_payload = json.loads((path / "workflow_result.json").read_text(encoding="utf-8"))
    try:
        payload = migrate_report_payload(raw_payload)
    except UnsupportedSchemaVersionError:
        return RunReport(
            schema_version=1,
            run_id=str(raw_payload.get("run_id") or path.name),
            run_dir=path,
            workflow_name=str(raw_payload.get("workflow_name") or ""),
            workflow_schema_version=raw_payload.get("workflow_schema_version"),
            runtime_version=raw_payload.get("runtime_version"),
            run_profile=raw_payload.get("run_profile"),
            status="upgrade_required",
            total_steps=0,
            succeeded_steps=0,
            failed_step=None,
            dry_run_actions=0,
            elapsed_seconds=0.0,
        artifacts={
            "run_dir": str(path),
            "workflow_result": str(path / "workflow_result.json"),
            "state": str(path / "state.json") if (path / "state.json").exists() else None,
            "step_files": [str(item) for item in sorted(path.glob("*.json")) if item.name != "workflow_result.json"],
            "screenshots": [str(item) for item in sorted(path.glob("*.png"))],
            "traces": [str(item) for item in sorted(path.rglob("*.zip"))],
        },
            downloads=tuple(discover_downloads(path)),
            steps=(),
            run_lock=raw_payload.get("run_lock") if isinstance(raw_payload.get("run_lock"), dict) else None,
            run_queue=raw_payload.get("run_queue") if isinstance(raw_payload.get("run_queue"), dict) else None,
        )
    steps_payload = list(payload.get("steps", []))
    steps = tuple(step_report(step) for step in steps_payload)
    failed = next((step for step in steps if step.status == "failed"), None)
    downloads = tuple(discover_downloads(path))
    artifacts = {
        "run_dir": str(path),
        "workflow_result": str(path / "workflow_result.json"),
        "state": str(path / "state.json") if (path / "state.json").exists() else None,
        "step_files": [str(item) for item in sorted(path.glob("*.json")) if item.name != "workflow_result.json"],
        "screenshots": [str(item) for item in sorted(path.glob("*.png"))],
        "traces": [str(item) for item in sorted(path.rglob("*.zip"))],
    }
    return RunReport(
        schema_version=1,
        run_id=str(payload.get("run_id") or path.name),
        run_dir=path,
        workflow_name=str(payload.get("workflow_name") or ""),
        workflow_schema_version=payload.get("workflow_schema_version"),
        runtime_version=payload.get("runtime_version"),
        run_profile=payload.get("run_profile"),
        status="failed" if failed else "success",
        total_steps=len(steps),
        succeeded_steps=sum(1 for step in steps if step.status in {"success", "dry_run"}),
        failed_step=failed.id if failed else None,
        dry_run_actions=sum(1 for step in steps if step.status == "dry_run"),
        elapsed_seconds=round(sum(step.elapsed_seconds or 0.0 for step in steps), 6),
        artifacts=artifacts,
        downloads=downloads,
        steps=steps,
        run_lock=payload.get("run_lock") if isinstance(payload.get("run_lock"), dict) else None,
        run_queue=payload.get("run_queue") if isinstance(payload.get("run_queue"), dict) else None,
        run_checks=payload.get("run_checks") if isinstance(payload.get("run_checks"), dict) else None,
        acceptance=payload.get("acceptance") if isinstance(payload.get("acceptance"), dict) else None,
    )


def step_report(step: dict[str, Any]) -> StepReport:
    metadata = step.get("metadata") if isinstance(step.get("metadata"), dict) else {}
    action_result = step.get("action_result") if isinstance(step.get("action_result"), dict) else {}
    resolved_target = step.get("resolved_target") if isinstance(step.get("resolved_target"), dict) else {}
    evidence = resolved_target.get("evidence") if isinstance(resolved_target.get("evidence"), dict) else {}
    evidence_metadata = evidence.get("metadata") if isinstance(evidence.get("metadata"), dict) else {}
    target = resolved_target.get("target") if isinstance(resolved_target.get("target"), dict) else {}
    failure_diagnosis = metadata.get("failure_diagnosis") if isinstance(metadata.get("failure_diagnosis"), dict) else None
    return StepReport(
        id=str(step.get("id") or ""),
        action=str(step.get("action") or ""),
        status=str(step.get("status") or ""),
        message=str(step.get("message") or ""),
        elapsed_seconds=float(metadata["elapsed_seconds"]) if "elapsed_seconds" in metadata else None,
        attempts=int(metadata["run_attempts"]) if "run_attempts" in metadata else None,
        provider=str(action_result.get("provider") or evidence.get("provider") or "") or None,
        target=target_display_name(target) or action_result.get("target"),
        selector_resolution=evidence_metadata.get("selector_resolution") if isinstance(evidence_metadata.get("selector_resolution"), dict) else None,
        observation_summary=step_observation_summary(step),
        artifact_paths=tuple(step_artifact_paths(step, failure_diagnosis)),
        failure_artifacts=failure_artifacts_from_diagnosis(failure_diagnosis),
        failure_diagnosis=failure_diagnosis,
    )


def target_display_name(target: dict[str, Any]) -> str | None:
    for key in (
        "selector",
        "test_id",
        "text",
        "label",
        "contains_text",
        "text_regex",
        "row_text",
        "row_contains_text",
        "column_header",
        "near_text",
        "near_contains_text",
        "scope_text",
        "scope_contains_text",
        "role",
    ):
        value = target.get(key)
        if value:
            return str(value)
    return None


def step_artifact_paths(step: dict[str, Any], failure_diagnosis: dict[str, Any] | None) -> list[str]:
    paths: list[str] = []
    observation = step.get("observation") if isinstance(step.get("observation"), dict) else {}
    screenshot = observation.get("screenshot_path")
    if screenshot:
        paths.append(str(screenshot))
    if failure_diagnosis:
        artifacts = failure_diagnosis.get("artifacts")
        if isinstance(artifacts, dict) and artifacts.get("screenshot"):
            paths.append(str(artifacts["screenshot"]))
    return sorted(set(paths))


def failure_artifacts_from_diagnosis(failure_diagnosis: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(failure_diagnosis, dict):
        return None
    artifacts = failure_diagnosis.get("artifacts") if isinstance(failure_diagnosis.get("artifacts"), dict) else {}
    result = {
        "screenshot": artifacts.get("screenshot"),
        "dom_excerpt": failure_diagnosis.get("dom_excerpt") if isinstance(failure_diagnosis.get("dom_excerpt"), list) else [],
        "selector_summary": failure_diagnosis.get("selector_summary")
        if isinstance(failure_diagnosis.get("selector_summary"), dict)
        else None,
    }
    if not result["screenshot"] and not result["dom_excerpt"] and not result["selector_summary"]:
        return None
    return result


def discover_downloads(run_dir: Path) -> list[dict[str, Any]]:
    downloads_dir = run_dir / "downloads"
    if not downloads_dir.exists():
        return []
    results = []
    for path in sorted(item for item in downloads_dir.rglob("*") if item.is_file()):
        stat = path.stat()
        results.append(
            {
                "path": str(path),
                "filename": path.name,
                "extension": path.suffix.lower(),
                "size_bytes": stat.st_size,
            }
        )
    return results


def list_run_summaries(root_dir: str | Path = ".runs", *, limit: int = 20) -> tuple[RunSummary, ...]:
    root = Path(root_dir)
    if not root.exists():
        return ()
    candidates = sorted(
        (path for path in root.iterdir() if path.is_dir() and (path / "workflow_result.json").exists()),
        key=lambda path: (path / "workflow_result.json").stat().st_mtime,
        reverse=True,
    )
    return tuple(load_run_summary(path) for path in candidates[:limit])


def run_summary_to_dict(summary: RunSummary) -> dict[str, Any]:
    return {
        "run_id": summary.run_id,
        "run_dir": str(summary.run_dir),
        "workflow_name": summary.workflow_name,
        "workflow_schema_version": summary.workflow_schema_version,
        "runtime_version": summary.runtime_version,
        "run_profile": summary.run_profile,
        "status": summary.status,
        "total_steps": summary.total_steps,
        "succeeded_steps": summary.succeeded_steps,
        "failed_step": summary.failed_step,
        "dry_run_actions": summary.dry_run_actions,
    }


def run_report_to_dict(report: RunReport) -> dict[str, Any]:
    return {
        "schema_version": report.schema_version,
        "run_id": report.run_id,
        "run_dir": str(report.run_dir),
        "workflow_name": report.workflow_name,
        "workflow_schema_version": report.workflow_schema_version,
        "runtime_version": report.runtime_version,
        "run_profile": report.run_profile,
        "status": report.status,
        "total_steps": report.total_steps,
        "succeeded_steps": report.succeeded_steps,
        "failed_step": report.failed_step,
        "dry_run_actions": report.dry_run_actions,
        "elapsed_seconds": report.elapsed_seconds,
        "artifacts": report.artifacts,
        "downloads": list(report.downloads),
        "run_lock": report.run_lock,
        "run_queue": report.run_queue,
        "run_checks": report.run_checks,
        "acceptance": report.acceptance,
        "steps": [
            {
                "id": step.id,
                "action": step.action,
                "status": step.status,
                "message": step.message,
                "elapsed_seconds": step.elapsed_seconds,
                "attempts": step.attempts,
                "provider": step.provider,
                "target": step.target,
                "selector_resolution": step.selector_resolution,
                "observation_summary": step.observation_summary,
                "artifact_paths": list(step.artifact_paths),
                "failure_artifacts": step.failure_artifacts,
                "failure_diagnosis": step.failure_diagnosis,
            }
            for step in report.steps
        ],
    }


def compact_run_report(result: Any) -> dict[str, Any]:
    steps = []
    failed = None
    for step in getattr(result, "steps", ()) or ():
        status = getattr(getattr(step, "status", None), "value", getattr(step, "status", ""))
        entry: dict[str, Any] = {
            "id": str(getattr(step, "id", "") or ""),
            "action": str(getattr(step, "action", "") or ""),
            "status": str(status),
        }
        message = str(getattr(step, "message", "") or "")
        if message and str(status) == "failed":
            entry["message"] = message
        metadata = getattr(step, "metadata", {}) if isinstance(getattr(step, "metadata", {}), dict) else {}
        diagnosis = metadata.get("failure_diagnosis") if isinstance(metadata.get("failure_diagnosis"), dict) else {}
        if str(status) == "failed":
            failed = entry
            if diagnosis:
                entry["diagnosis"] = {
                    "expected": diagnosis.get("expected"),
                    "actual": diagnosis.get("actual"),
                    "recovery_suggestions": diagnosis.get("recovery_suggestions"),
                }
                artifacts = diagnosis.get("artifacts") if isinstance(diagnosis.get("artifacts"), dict) else {}
                if artifacts.get("screenshot"):
                    entry["screenshot"] = str(artifacts["screenshot"])
        observation = getattr(step, "observation", None)
        screenshot_path = getattr(observation, "screenshot_path", None)
        if screenshot_path is not None:
            entry["screenshot"] = str(screenshot_path)
        steps.append(entry)
    status = "failed" if failed else "success"
    return {
        "schema_version": 1,
        "run_id": str(getattr(result, "run_id", "") or ""),
        "workflow": str(getattr(result, "workflow_name", "") or ""),
        "run_profile": str(getattr(result, "run_profile", "") or ""),
        "status": status,
        "step_count": len(steps),
        "failed_step": failed.get("id") if failed else None,
        "run_dir": str(getattr(result, "run_dir", "") or ""),
        "steps": steps,
    }


def run_report_to_markdown(report: RunReport) -> str:
    lines = [
        f"# Run Report: {report.workflow_name}",
        "",
        f"- Run ID: `{report.run_id}`",
        f"- Runtime version: `{report.runtime_version}`",
        f"- Workflow schema version: `{report.workflow_schema_version}`",
        f"- Run profile: `{report.run_profile}`",
        f"- Status: `{report.status}`",
        f"- Steps: {report.succeeded_steps}/{report.total_steps} succeeded",
        f"- Dry-run actions: {report.dry_run_actions}",
        f"- Failed step: `{report.failed_step}`" if report.failed_step else "- Failed step: none",
        f"- Elapsed seconds: {report.elapsed_seconds}",
    ]
    if report.acceptance:
        acceptance = report.acceptance
        suffix = acceptance_status_suffix(acceptance)
        lines.append(f"- Acceptance level: `{acceptance.get('label')}` ({acceptance.get('name')}){suffix}")
        if acceptance.get("valid_operation_receipts") is not None:
            lines.append(f"- Valid operation receipts: {acceptance.get('valid_operation_receipts')}")
        if acceptance.get("invalid_operation_receipts"):
            lines.append(f"- Invalid operation receipts: {acceptance.get('invalid_operation_receipts')}")
            failures = acceptance.get("operation_receipt_failures")
            if isinstance(failures, list) and failures:
                first_failure = failures[0] if isinstance(failures[0], dict) else {}
                lines.append(
                    "- First invalid receipt: "
                    f"`{first_failure.get('step_id')}` "
                    f"reason `{first_failure.get('reason')}`"
                )
        blockers = acceptance.get("product_acceptance_blockers")
        if isinstance(blockers, list) and blockers:
            lines.append("- Product acceptance blockers:")
            for blocker in blockers[:8]:
                lines.append(f"  - `{blocker}`")
        if acceptance.get("missing_for_next_level"):
            lines.append(f"- Next level needs: {acceptance.get('missing_for_next_level')}")
    if report.run_checks:
        for check_name in ("product_guard", "visual_guard"):
            check = report.run_checks.get(check_name)
            if isinstance(check, dict):
                lines.append(f"- {check_name}: `{check.get('status')}`")
    if report.run_queue:
        lines.append(f"- Queue waited seconds: {report.run_queue.get('waited_seconds')}")
        lines.append(f"- Queue attempts: {report.run_queue.get('attempts')}")
    if report.run_lock:
        lines.append(f"- Lock owner: `{report.run_lock.get('owner')}`")
    lines.extend(["", "## Steps", ""])
    for step in report.steps:
        lines.append(f"### {step.id}")
        lines.append("")
        lines.append(f"- Action: `{step.action}`")
        lines.append(f"- Status: `{step.status}`")
        if step.provider:
            lines.append(f"- Provider: `{step.provider}`")
        if step.target:
            lines.append(f"- Target: `{step.target}`")
        if step.selector_resolution:
            confidence = step.selector_resolution.get("confidence")
            confidence_level = step.selector_resolution.get("confidence_level")
            fallback_path = step.selector_resolution.get("fallback_path") or []
            stability = step.selector_resolution.get("stability")
            stability_level = stability.get("level") if isinstance(stability, dict) else None
            details = []
            if confidence_level:
                details.append(f"level `{confidence_level}`")
            if confidence is not None:
                details.append(f"confidence `{confidence}`")
            if stability_level:
                details.append(f"stability `{stability_level}`")
            if fallback_path:
                details.append("fallback path `" + " -> ".join(str(item) for item in fallback_path) + "`")
            lines.append("- Selector: " + ", ".join(details))
        if step.elapsed_seconds is not None:
            lines.append(f"- Elapsed seconds: {step.elapsed_seconds}")
        if step.message:
            lines.append(f"- Message: {step.message}")
        if step.artifact_paths:
            lines.append("- Artifacts: " + ", ".join(f"`{path}`" for path in step.artifact_paths))
        if step.observation_summary:
            summary = step.observation_summary
            if summary.get("screenshot_path"):
                lines.append(f"- Screenshot: `{summary.get('screenshot_path')}`")
            if summary.get("visible_text"):
                lines.append("- Visible text: " + " | ".join(str(item) for item in summary["visible_text"][:12]))
            if summary.get("crop_region"):
                lines.append("- Crop region: `" + json.dumps(summary["crop_region"], ensure_ascii=False) + "`")
            if summary.get("uia_window_region"):
                lines.append("- Window region: `" + json.dumps(summary["uia_window_region"], ensure_ascii=False) + "`")
            if summary.get("uia_window_fallback"):
                lines.append("- Window fallback: `" + str(summary["uia_window_fallback"].get("reason")) + "`")
            if summary.get("uia_window_post_capture"):
                lines.append("- Window post-capture: `" + json.dumps(summary["uia_window_post_capture"], ensure_ascii=False) + "`")
            if summary.get("uia_window_scene_restore"):
                lines.append("- Window scene restore: `" + json.dumps(summary["uia_window_scene_restore"], ensure_ascii=False) + "`")
        if step.failure_diagnosis:
            lines.append("- Failure expected: " + str(step.failure_diagnosis.get("expected")))
            lines.append("- Failure actual: " + str(step.failure_diagnosis.get("actual")))
        if step.failure_artifacts:
            screenshot = step.failure_artifacts.get("screenshot")
            if screenshot:
                lines.append(f"- Failure screenshot: `{screenshot}`")
            selector_summary = step.failure_artifacts.get("selector_summary")
            if isinstance(selector_summary, dict):
                lines.append("- Selector summary: " + compact_selector_summary(selector_summary))
            dom_excerpt = step.failure_artifacts.get("dom_excerpt")
            if isinstance(dom_excerpt, list) and dom_excerpt:
                lines.append("- DOM excerpt:")
                for item in dom_excerpt[:5]:
                    if not isinstance(item, dict):
                        continue
                    label = item.get("text") or item.get("selector") or item.get("role") or item.get("index")
                    selector = item.get("selector")
                    role = item.get("role")
                    lines.append(f"  - `{label}` role=`{role}` selector=`{selector}`")
        lines.append("")
    if report.downloads:
        lines.extend(["## Downloads", ""])
        for item in report.downloads:
            lines.append(f"- `{item['filename']}` ({item['size_bytes']} bytes): `{item['path']}`")
    return "\n".join(lines).rstrip() + "\n"


def acceptance_status_suffix(acceptance: dict[str, Any]) -> str:
    if acceptance.get("is_product_acceptance"):
        return ""
    level = int(acceptance.get("level") or 0)
    blockers = acceptance.get("product_acceptance_blockers")
    if level >= 3 and isinstance(blockers, list) and blockers:
        return " — strict product acceptance blocked"
    if acceptance.get("simulated"):
        return " — simulated evidence only"
    return " — below product acceptance (L3+)"


def build_run_history_report(workspace_root: str | Path = ".agent-workspace", *, limit: int = 20) -> dict[str, Any]:
    workspace = Path(workspace_root).resolve()
    records = read_run_history(workspace)
    recent_records = records[-limit:] if limit > 0 else list(records)
    summary = local_run_history_summary(records)
    recent_runs = [enrich_run_history_record(workspace, record) for record in reversed(recent_records)]
    trend_points = build_run_history_trend_points(records[-10:])
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workspace": str(workspace),
        "summary": summary,
        "recent_runs": recent_runs,
        "trend": trend_points,
    }


def build_run_history_ai_summary(
    report: dict[str, Any],
    *,
    provider: str = "none",
    model: str | None = None,
) -> dict[str, Any]:
    provider_name = str(provider or "none").lower()
    compact_report = compact_run_history_report(report)
    if provider_name in {"none", "deterministic", "local"}:
        return {
            "schema_version": 1,
            "provider": "none",
            "model": None,
            "status": "generated",
            "source": "deterministic",
            "text": deterministic_run_history_summary(compact_report),
            "prompt": None,
            "error": None,
        }
    prompt = build_run_history_summary_prompt(compact_report)
    if provider_name == "openai":
        try:
            return {
                "schema_version": 1,
                "provider": provider_name,
                "model": model or "gpt-4o-mini",
                "status": "generated",
                "source": "llm",
                "text": _run_history_summary_with_openai(prompt, model=model or "gpt-4o-mini"),
                "prompt": prompt,
                "error": None,
            }
        except Exception as exc:
            return {
                "schema_version": 1,
                "provider": provider_name,
                "model": model or "gpt-4o-mini",
                "status": "fallback",
                "source": "deterministic",
                "text": deterministic_run_history_summary(compact_report),
                "prompt": prompt,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
    if provider_name == "anthropic":
        try:
            return {
                "schema_version": 1,
                "provider": provider_name,
                "model": model or "claude-haiku-4-5-20251001",
                "status": "generated",
                "source": "llm",
                "text": _run_history_summary_with_anthropic(prompt, model=model or "claude-haiku-4-5-20251001"),
                "prompt": prompt,
                "error": None,
            }
        except Exception as exc:
            return {
                "schema_version": 1,
                "provider": provider_name,
                "model": model or "claude-haiku-4-5-20251001",
                "status": "fallback",
                "source": "deterministic",
                "text": deterministic_run_history_summary(compact_report),
                "prompt": prompt,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
    return {
        "schema_version": 1,
        "provider": provider_name,
        "model": model,
        "status": "unsupported",
        "source": "deterministic",
        "text": deterministic_run_history_summary(compact_report),
        "prompt": None,
        "error": f"Unsupported provider: {provider}",
    }


def compact_run_history_report(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    recent_runs = report.get("recent_runs") if isinstance(report.get("recent_runs"), list) else []
    return {
        "workspace": report.get("workspace"),
        "generated_at": report.get("generated_at"),
        "summary": {
            "total_runs": summary.get("total_runs", 0),
            "passed_runs": summary.get("passed_runs", 0),
            "failed_runs": summary.get("failed_runs", 0),
            "pass_rate": summary.get("pass_rate", 0.0),
            "slowest_run": compact_run_history_entry(summary.get("slowest_run")),
            "most_failed_steps": summary.get("most_failed_steps", [])[:5] if isinstance(summary.get("most_failed_steps"), list) else [],
        },
        "recent_runs": [compact_run_history_entry(item) for item in recent_runs[:10]],
        "trend": report.get("trend") if isinstance(report.get("trend"), dict) else {},
    }


def compact_run_history_entry(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    return {
        "run_id": item.get("run_id"),
        "workflow_name": item.get("workflow_name"),
        "status": item.get("display_status") or item.get("status"),
        "passed": item.get("passed"),
        "duration_ms": item.get("duration_ms"),
        "display_duration": item.get("display_duration"),
        "failed_step": item.get("failed_step"),
        "root_cause_guess": item.get("root_cause_guess"),
        "tags": item.get("tags"),
        "visibility": item.get("visibility"),
    }


def deterministic_run_history_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    recent_runs = report.get("recent_runs") if isinstance(report.get("recent_runs"), list) else []
    total = int(summary.get("total_runs") or 0)
    passed = int(summary.get("passed_runs") or 0)
    failed = int(summary.get("failed_runs") or 0)
    pass_rate = round(float(summary.get("pass_rate") or 0.0) * 100, 1)
    slowest = summary.get("slowest_run") if isinstance(summary.get("slowest_run"), dict) else {}
    top_failure = summary.get("most_failed_steps")[0] if isinstance(summary.get("most_failed_steps"), list) and summary.get("most_failed_steps") else {}
    trend = report.get("trend") if isinstance(report.get("trend"), dict) else {}
    trend_rate = round(float(trend.get("pass_rate") or 0.0) * 100, 1)
    lines = [
        f"Run history covers {total} runs with a {pass_rate}% pass rate ({passed} passed, {failed} failed).",
    ]
    if slowest:
        lines.append(
            f"The slowest run was {slowest.get('workflow_name') or slowest.get('run_id') or 'n/a'} "
            f"at {format_duration_ms(slowest.get('duration_ms'))}."
        )
    if top_failure:
        lines.append(
            f"The most common failure target is {top_failure.get('step') or 'unknown'} "
            f"with {top_failure.get('count') or 0} occurrences."
        )
    if recent_runs:
        latest = recent_runs[0]
        lines.append(
            f"The latest run is {latest.get('workflow_name') or 'run'} and is "
            f"{str(latest.get('display_status') or latest.get('status') or 'unknown')}. "
            f"The last-{len(recent_runs)} trend pass rate is {trend_rate}%."
        )
    if failed == 0:
        lines.append("The workspace is currently stable with no recorded failures.")
    elif failed > passed:
        lines.append("Failures outnumber successes, so workflow reliability needs attention.")
    else:
        lines.append("Successes currently outnumber failures, but the failure trend still deserves review.")
    return " ".join(lines)


def build_run_history_summary_prompt(report: dict[str, Any]) -> str:
    return (
        "You summarize Checkpoint run histories for engineers.\n"
        "Return one concise paragraph, no bullets, no markdown, no code blocks.\n"
        "Mention pass rate, the dominant failure pattern, and any notable trend or warning.\n\n"
        f"Run history JSON:\n{json.dumps(report, ensure_ascii=False, indent=2)}\n"
    )


def _run_history_summary_with_anthropic(prompt: str, *, model: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=256,
        system="You summarize workflow run reports for engineers.",
        messages=[{"role": "user", "content": prompt}],
    )
    content = getattr(message, "content", [])
    if not content:
        raise RuntimeError("Anthropic returned an empty response.")
    first = content[0]
    text = getattr(first, "text", None)
    if text is None and isinstance(first, dict):
        text = first.get("text")
    return str(text or "").strip()


def _run_history_summary_with_openai(prompt: str, *, model: str) -> str:
    from openai import OpenAI

    client = OpenAI()
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": "You summarize workflow run reports for engineers."},
            {"role": "user", "content": prompt},
        ],
    )
    return str(getattr(response, "output_text", "")).strip()


def local_run_history_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    passed = sum(1 for item in records if item.get("passed") is True)
    failed = total - passed
    slowest = max(records, key=lambda item: int(item.get("duration_ms") or 0), default=None)
    failures: dict[str, int] = {}
    for item in records:
        key = str(item.get("failed_step") or item.get("failed_action") or "")
        if key:
            failures[key] = failures.get(key, 0) + 1
    most_failed = sorted(failures.items(), key=lambda item: item[1], reverse=True)
    return {
        "total_runs": total,
        "passed_runs": passed,
        "failed_runs": failed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "slowest_run": slowest,
        "most_failed_steps": [{"step": step, "count": count} for step, count in most_failed[:5]],
    }


def enrich_run_history_record(workspace_root: Path, record: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(record)
    run_dir = record.get("run_dir")
    run_report = None
    if run_dir:
        path = Path(str(run_dir))
        if path.exists() and (path / "workflow_result.json").exists():
            try:
                run_report = load_run_report(path)
            except OSError:
                run_report = None
    if run_report is not None:
        enriched["run_dir"] = str(run_report.run_dir)
        enriched["report"] = run_report_to_dict(run_report)
        enriched["thumbnail"] = run_report_thumbnail_data_uri(run_report)
    else:
        enriched["thumbnail"] = None
    enriched["display_status"] = "passed" if enriched.get("passed") is True else "failed" if enriched.get("passed") is False else str(enriched.get("status") or "unknown")
    enriched["display_duration"] = format_duration_ms(enriched.get("duration_ms"))
    return enriched


def build_run_history_trend_points(records: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = list(records)
    labels = [format_history_label(item) for item in ordered]
    values = [1 if item.get("passed") is True else 0 for item in ordered]
    durations = [int(item.get("duration_ms") or 0) for item in ordered]
    return {
        "labels": labels,
        "values": values,
        "durations": durations,
        "pass_rate": round(sum(values) / len(values), 4) if values else 0.0,
    }


def format_history_label(record: dict[str, Any]) -> str:
    workflow = str(record.get("workflow_name") or "run")
    run_id = str(record.get("run_id") or "")
    if run_id:
        return f"{workflow} ({run_id[:8]})"
    return workflow


def format_duration_ms(value: Any) -> str:
    try:
        duration_ms = int(value or 0)
    except (TypeError, ValueError):
        return "unknown"
    if duration_ms <= 0:
        return "0 ms"
    if duration_ms < 1000:
        return f"{duration_ms} ms"
    return f"{duration_ms / 1000:.1f} s"


def run_report_thumbnail_data_uri(report: RunReport) -> str | None:
    for step in report.steps:
        for path in step.artifact_paths:
            data_uri = image_file_to_data_uri(Path(path))
            if data_uri:
                return data_uri
    for item in report.artifacts.get("screenshots", []) if isinstance(report.artifacts.get("screenshots"), list) else []:
        data_uri = image_file_to_data_uri(Path(str(item)))
        if data_uri:
            return data_uri
    return None


def image_file_to_data_uri(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    suffix = path.suffix.lower()
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix)
    if not media_type:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


def run_history_report_to_html(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    recent_runs = report.get("recent_runs") if isinstance(report.get("recent_runs"), list) else []
    trend = report.get("trend") if isinstance(report.get("trend"), dict) else {}
    ai_summary = report.get("ai_summary") if isinstance(report.get("ai_summary"), dict) else {}
    trend_svg = render_trend_svg(trend)
    most_failed = summary.get("most_failed_steps") if isinstance(summary.get("most_failed_steps"), list) else []
    slowest = summary.get("slowest_run") if isinstance(summary.get("slowest_run"), dict) else {}
    lines = [
        "<!doctype html>",
        "<html lang=\"en\">",
        "<head>",
        "<meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        f"<title>Checkpoint Run Report - {html.escape(str(report.get('workspace') or 'workspace'))}</title>",
        "<style>",
        "body{font-family:Inter,Segoe UI,Arial,sans-serif;margin:0;background:#0f172a;color:#e2e8f0;}",
        ".wrap{max-width:1280px;margin:0 auto;padding:24px;}",
        "h1,h2,h3,p{margin:0 0 12px 0;}",
        ".muted{color:#94a3b8;}",
        ".grid{display:grid;gap:12px;}",
        ".stats{grid-template-columns:repeat(auto-fit,minmax(180px,1fr));margin:16px 0 24px;}",
        ".card{background:#111827;border:1px solid #1f2937;border-radius:10px;padding:16px;box-shadow:0 1px 2px rgba(0,0,0,.2);}",
        ".stat-value{font-size:1.6rem;font-weight:700;line-height:1.1;}",
        ".section{margin:24px 0;}",
        ".table{width:100%;border-collapse:collapse;}",
        ".table th,.table td{padding:10px 12px;border-bottom:1px solid #1f2937;vertical-align:top;text-align:left;}",
        ".table th{font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;color:#94a3b8;}",
        ".badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.78rem;font-weight:600;}",
        ".passed{background:#0f3d2e;color:#86efac;}",
        ".failed{background:#3b1d1d;color:#fca5a5;}",
        ".unknown{background:#374151;color:#cbd5e1;}",
        ".trend-svg{width:100%;height:auto;display:block;background:#0b1120;border:1px solid #1f2937;border-radius:10px;}",
        ".run{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(260px,.9fr);gap:16px;}",
        ".thumb{max-width:100%;border-radius:8px;border:1px solid #1f2937;background:#0b1120;}",
        ".details{display:grid;gap:12px;}",
        "details{background:#111827;border:1px solid #1f2937;border-radius:10px;padding:12px;}",
        "summary{cursor:pointer;font-weight:600;}",
        ".small{font-size:.88rem;}",
        "@media (max-width:900px){.run{grid-template-columns:1fr;}}",
        "</style>",
        "</head>",
        "<body>",
        "<div class=\"wrap\">",
        "<h1>Checkpoint Run Report</h1>",
        f"<p class=\"muted small\">Workspace: <code>{html.escape(str(report.get('workspace') or ''))}</code></p>",
        f"<p class=\"muted small\">Generated: <code>{html.escape(str(report.get('generated_at') or ''))}</code></p>",
        "<div class=\"grid stats\">",
        stat_card("Total runs", summary.get("total_runs", 0)),
        stat_card("Passed", summary.get("passed_runs", 0)),
        stat_card("Failed", summary.get("failed_runs", 0)),
        stat_card("Pass rate", f"{round(float(summary.get('pass_rate') or 0.0) * 100, 1)}%"),
        stat_card("Slowest", slowest.get("workflow_name") or slowest.get("run_id") or "n/a"),
        stat_card("Slowest duration", format_duration_ms(slowest.get("duration_ms"))),
        "</div>",
        "<div class=\"section card\">",
        "<h2>Summary</h2>",
        f"<p>{html.escape(str(ai_summary.get('text') or deterministic_run_history_summary(report)))}</p>",
        f"<p class=\"muted small\">Summary source: <code>{html.escape(str(ai_summary.get('source') or 'deterministic'))}</code>"
        f"{' - model ' + html.escape(str(ai_summary.get('model'))) if ai_summary.get('model') else ''}</p>",
        "</div>",
        "<div class=\"section card\">",
        "<h2>Trend</h2>",
        f"<p class=\"muted small\">Last {len(trend.get('values', []) or [])} runs, pass rate {round(float(trend.get('pass_rate') or 0.0) * 100, 1)}%.</p>",
        trend_svg,
        "</div>",
        "<div class=\"section card\">",
        "<h2>Failure Focus</h2>",
        "<div class=\"grid\" style=\"grid-template-columns:repeat(auto-fit,minmax(220px,1fr));\">",
    ]
    if most_failed:
        for item in most_failed[:5]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"<div class=\"card\"><div class=\"stat-value\">{html.escape(str(item.get('count') or 0))}</div>"
                f"<p>{html.escape(str(item.get('step') or 'unknown step'))}</p></div>"
            )
    else:
        lines.append("<div class=\"card\">No failed steps recorded.</div>")
    lines.extend(
        [
            "</div>",
            "</div>",
            "<div class=\"section card\">",
            "<h2>Recent Runs</h2>",
            "<table class=\"table\">",
            "<thead><tr><th>Status</th><th>Workflow</th><th>Run</th><th>Duration</th><th>Failure</th><th>Tags</th><th>Artifact</th></tr></thead>",
            "<tbody>",
        ]
    )
    for run in recent_runs:
        status = str(run.get("display_status") or run.get("status") or "unknown")
        badge_class = "passed" if run.get("passed") is True else "failed" if run.get("passed") is False else "unknown"
        thumbnail = run.get("thumbnail")
        artifact_html = (
            f"<img class=\"thumb\" src=\"{thumbnail}\" alt=\"{html.escape(str(run.get('workflow_name') or 'run'))} screenshot\" />"
            if thumbnail
            else "<span class=\"muted\">No screenshot</span>"
        )
        tags = ", ".join(str(tag) for tag in (run.get("tags") or []))
        failure = html.escape(str(run.get("failed_step") or run.get("root_cause_guess") or ""))
        lines.append(
            "<tr>"
            f"<td><span class=\"badge {badge_class}\">{html.escape(status)}</span></td>"
            f"<td>{html.escape(str(run.get('workflow_name') or ''))}</td>"
            f"<td><code>{html.escape(str(run.get('run_id') or ''))}</code></td>"
            f"<td>{html.escape(str(run.get('display_duration') or 'unknown'))}</td>"
            f"<td>{failure}</td>"
            f"<td>{html.escape(tags)}</td>"
            f"<td>{artifact_html}</td>"
            "</tr>"
        )
    lines.extend(
        [
            "</tbody>",
            "</table>",
            "</div>",
            "<div class=\"section details\">",
        ]
    )
    for run in recent_runs[:5]:
        lines.extend(
            [
                "<details>",
                f"<summary>{html.escape(str(run.get('workflow_name') or 'run'))} - {html.escape(str(run.get('display_status') or 'unknown'))}</summary>",
                "<div class=\"small\" style=\"margin-top:10px;line-height:1.5;\">",
                f"<p>Run ID: <code>{html.escape(str(run.get('run_id') or ''))}</code></p>",
                f"<p>Profile: <code>{html.escape(str(run.get('run_profile') or ''))}</code></p>",
                f"<p>Visibility: <code>{html.escape(str(run.get('visibility') or ''))}</code></p>",
                f"<p>Failed step: <code>{html.escape(str(run.get('failed_step') or 'none'))}</code></p>",
                f"<p>Root cause: <code>{html.escape(str(run.get('root_cause_guess') or 'unknown'))}</code></p>",
                f"<p>Run dir: <code>{html.escape(str(run.get('run_dir') or ''))}</code></p>",
                "</div>",
                "</details>",
            ]
        )
    lines.extend(["</div>", "</div>", "</body>", "</html>"])
    return "\n".join(lines)


def stat_card(label: str, value: Any) -> str:
    return (
        "<div class=\"card\">"
        f"<p class=\"muted small\">{html.escape(str(label))}</p>"
        f"<div class=\"stat-value\">{html.escape(str(value))}</div>"
        "</div>"
    )


def render_trend_svg(trend: dict[str, Any]) -> str:
    values = trend.get("values") if isinstance(trend.get("values"), list) else []
    labels = trend.get("labels") if isinstance(trend.get("labels"), list) else []
    if not values:
        return "<div class=\"muted\">No trend data available.</div>"
    width = 1000
    height = 260
    padding = 36
    usable_width = width - padding * 2
    usable_height = height - padding * 2
    points: list[tuple[float, float]] = []
    denominator = max(len(values) - 1, 1)
    for index, value in enumerate(values):
        x = padding + usable_width * (index / denominator)
        y = padding + usable_height * (1 - float(value))
        points.append((x, y))
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    bars = []
    for index, value in enumerate(values):
        x = padding + usable_width * (index / max(len(values), 1))
        bar_width = max(usable_width / max(len(values), 1) * 0.6, 4)
        bar_height = usable_height * float(value)
        y = padding + usable_height - bar_height
        fill = "#22c55e" if value else "#ef4444"
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" rx="4" fill="{fill}" opacity="0.35" />')
    label_nodes = []
    for index, label in enumerate(labels[-8:]):
        x = padding + usable_width * (index / max(min(len(values), 8) - 1, 1)) if len(values) <= 8 else padding + usable_width * ((len(values) - 8 + index) / max(len(values) - 1, 1))
        label_nodes.append(
            f'<text x="{x:.1f}" y="{height - 10}" fill="#94a3b8" font-size="12" text-anchor="middle">{html.escape(str(label)[:14])}</text>'
        )
    points_nodes = []
    for index, (x, y) in enumerate(points):
        fill = "#22c55e" if values[index] else "#ef4444"
        points_nodes.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{fill}" stroke="#0f172a" stroke-width="2" />')
    return (
        f'<svg class="trend-svg" viewBox="0 0 {width} {height}" role="img" aria-label="Run trend chart">'
        '<rect x="0" y="0" width="100%" height="100%" fill="#0b1120" rx="10" />'
        f'<line x1="{padding}" y1="{padding + usable_height}" x2="{width - padding}" y2="{padding + usable_height}" stroke="#334155" stroke-width="1" />'
        f'<line x1="{padding}" y1="{padding}" x2="{padding}" y2="{padding + usable_height}" stroke="#334155" stroke-width="1" />'
        f'<text x="10" y="{padding + 6}" fill="#94a3b8" font-size="12">1</text>'
        f'<text x="10" y="{padding + usable_height}" fill="#94a3b8" font-size="12">0</text>'
        + "".join(bars)
        + f'<polyline points="{polyline}" fill="none" stroke="#60a5fa" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" />'
        + "".join(points_nodes)
        + "".join(label_nodes)
        + "</svg>"
    )


def write_run_history_report(
    workspace_root: str | Path = ".agent-workspace",
    output_path: str | Path | None = None,
    *,
    limit: int = 20,
    summary_provider: str = "none",
    summary_model: str | None = None,
) -> Path:
    workspace = Path(workspace_root).resolve()
    report = build_run_history_report(workspace, limit=limit)
    report["ai_summary"] = build_run_history_ai_summary(report, provider=summary_provider, model=summary_model)
    html_text = run_history_report_to_html(report)
    if output_path is None:
        output = workspace / "reports" / "run_history_report.html"
    else:
        output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_text, encoding="utf-8")
    return output


def build_run_history_share_payload(
    workspace_root: str | Path,
    output_path: str | Path,
    *,
    report: dict[str, Any] | None = None,
    cloud_share_url: str | None = None,
) -> dict[str, Any]:
    workspace = Path(workspace_root).resolve()
    output = Path(output_path).resolve()
    report = report or build_run_history_report(workspace)
    if "ai_summary" not in report:
        report = dict(report)
        report["ai_summary"] = build_run_history_ai_summary(report)
    return {
        "schema_version": 1,
        "workspace": str(workspace),
        "output_path": str(output),
        "local_url": output.as_uri(),
        "share_status": "available" if cloud_share_url else "placeholder",
        "cloud_share_url": cloud_share_url,
        "cloud_share_note": "Cloud sharing is not enabled yet. Use the local file URL for now." if cloud_share_url is None else None,
        "summary": report.get("summary"),
        "ai_summary": report.get("ai_summary"),
        "generated_at": report.get("generated_at"),
    }


def run_history_report_to_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    recent_runs = report.get("recent_runs") if isinstance(report.get("recent_runs"), list) else []
    ai_summary = report.get("ai_summary") if isinstance(report.get("ai_summary"), dict) else {}
    lines = [
        "# Checkpoint Run Report",
        "",
        f"- Workspace: `{report.get('workspace')}`",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Total runs: {summary.get('total_runs', 0)}",
        f"- Passed: {summary.get('passed_runs', 0)}",
        f"- Failed: {summary.get('failed_runs', 0)}",
        f"- Pass rate: {round(float(summary.get('pass_rate') or 0.0) * 100, 1)}%",
        "",
        "## Summary",
        "",
        ai_summary.get("text") or deterministic_run_history_summary(report),
        "",
        "## Recent Runs",
        "",
    ]
    for run in recent_runs[:10]:
        lines.append(
            f"- `{run.get('workflow_name')}` `{run.get('display_status') or run.get('status')}` "
            f"`{run.get('run_id')}` `{run.get('display_duration') or 'unknown'}`"
        )
    lines.extend(
        [
            "",
            "## Sharing",
            "",
            f"- Local URL: `{Path(report.get('output_path') or '').resolve().as_uri() if report.get('output_path') else ''}`",
            "- Cloud share: placeholder",
            f"- Summary source: `{ai_summary.get('source') or 'deterministic'}`",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def compact_selector_summary(summary: dict[str, Any]) -> str:
    parts = []
    if summary.get("target") is not None:
        parts.append(f"target=`{summary.get('target')}`")
    if summary.get("latest_resolved_target"):
        parts.append(f"latest=`{summary.get('latest_resolved_target')}`")
    if summary.get("provider"):
        parts.append(f"provider=`{summary.get('provider')}`")
    if summary.get("confidence") is not None:
        parts.append(f"confidence=`{summary.get('confidence')}`")
    if summary.get("handle"):
        parts.append(f"handle=`{summary.get('handle')}`")
    resolution = summary.get("selector_resolution")
    if isinstance(resolution, dict) and resolution.get("fallback_path"):
        parts.append("fallback_path=`" + " -> ".join(str(item) for item in resolution.get("fallback_path") or []) + "`")
    return ", ".join(parts) if parts else str(summary)


def step_observation_summary(step: dict[str, Any]) -> dict[str, Any] | None:
    observation = step.get("observation") if isinstance(step.get("observation"), dict) else None
    if not isinstance(observation, dict):
        return None
    metadata = observation.get("metadata") if isinstance(observation.get("metadata"), dict) else {}
    elements = observation.get("elements") if isinstance(observation.get("elements"), list) else []
    visible_text = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        text = str(element.get("text") or "").strip()
        if text:
            visible_text.append(text)
        if len(visible_text) >= 30:
            break
    return {
        "provider": observation.get("provider"),
        "source": observation.get("source"),
        "screenshot_path": observation.get("screenshot_path"),
        "width": observation.get("width"),
        "height": observation.get("height"),
        "visible_text": visible_text,
        "crop_region": metadata.get("crop_region"),
        "uia_window_region": metadata.get("uia_window_region"),
        "uia_window_fallback": metadata.get("uia_window_fallback"),
        "uia_window_post_capture": metadata.get("uia_window_post_capture"),
        "uia_window_scene_restore": metadata.get("uia_window_scene_restore"),
        "engine": metadata.get("engine"),
        "engine_available": metadata.get("engine_available"),
    }
