from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


def load_run_summary(run_dir: str | Path) -> RunSummary:
    path = Path(run_dir)
    payload = json.loads((path / "workflow_result.json").read_text(encoding="utf-8"))
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
    payload = json.loads((path / "workflow_result.json").read_text(encoding="utf-8"))
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
        "engine": metadata.get("engine"),
        "engine_available": metadata.get("engine_available"),
    }
