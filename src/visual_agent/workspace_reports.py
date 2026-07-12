from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any

from .licensing import get_license, report_history_window_days
from .reports import load_run_report, run_report_to_dict, run_report_to_markdown


@dataclass(frozen=True)
class WorkspaceReportExport:
    run_id: str
    json_path: Path | None
    markdown_path: Path | None
    index_path: Path | None = None


def export_workspace_run_report(
    workspace: Any,
    run_dir: str | Path,
    *,
    json_format: bool = True,
    markdown_format: bool = True,
) -> WorkspaceReportExport:
    report = load_run_report(run_dir)
    workspace.reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = workspace.reports_dir / f"{report.run_id}.json" if json_format else None
    markdown_path = workspace.reports_dir / f"{report.run_id}.md" if markdown_format else None
    if json_path is not None:
        json_path.write_text(json.dumps(run_report_to_dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    if markdown_path is not None:
        markdown_path.write_text(run_report_to_markdown(report), encoding="utf-8")
    index_path = write_workspace_report_index(workspace)
    return WorkspaceReportExport(run_id=report.run_id, json_path=json_path, markdown_path=markdown_path, index_path=index_path)


def list_workspace_reports(workspace: Any) -> tuple[dict[str, Any], ...]:
    if not workspace.reports_dir.exists():
        return ()
    reports = []
    for path in sorted(workspace.reports_dir.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.suffix.lower() not in {".json", ".md"}:
            continue
        if path.name in {"index.json", "tags.json"}:
            continue
        if not workspace_report_access_payload(workspace, path)["allowed"]:
            continue
        reports.append(
            {
                "name": path.name,
                "path": str(path),
                "extension": path.suffix.lower(),
                "size_bytes": path.stat().st_size,
                "modified_at": path.stat().st_mtime,
            }
        )
    return tuple(reports)


def build_workspace_report_index(
    workspace: Any,
    *,
    status: str | None = None,
    workflow: str | None = None,
    failed_only: bool = False,
    include_inaccessible: bool = False,
) -> dict[str, Any]:
    workspace.reports_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for path in sorted(workspace.reports_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.name in {"index.json", "tags.json"}:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not include_inaccessible and not workspace_report_access_payload(workspace, path)["allowed"]:
            continue
        entry = report_index_entry(workspace, path, payload)
        annotation = load_workspace_report_tags(workspace).get(entry["run_id"])
        if annotation:
            entry["annotation"] = annotation
        if status is not None and entry["status"] != status:
            continue
        if workflow is not None and entry["workflow_name"] != workflow:
            continue
        if failed_only and entry["status"] != "failed":
            continue
        entries.append(entry)
    return {
        "schema_version": 1,
        "generated_at": time(),
        "workspace_root": str(workspace.root),
        "filters": {
            "status": status,
            "workflow": workflow,
            "failed_only": failed_only,
        },
        "total_reports": len(entries),
        "failed_reports": sum(1 for entry in entries if entry["status"] == "failed"),
        "entries": entries,
        "history_access": {
            "tier": get_license().tier,
            "window_days": report_history_window_days(),
        },
    }


def write_workspace_report_index(workspace: Any) -> Path:
    index = build_workspace_report_index(workspace, include_inaccessible=True)
    path = workspace.reports_dir / "index.json"
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_workspace_report_index(
    workspace: Any,
    *,
    rebuild: bool = False,
    status: str | None = None,
    workflow: str | None = None,
    failed_only: bool = False,
) -> dict[str, Any]:
    if rebuild or status is not None or workflow is not None or failed_only:
        index = build_workspace_report_index(
            workspace,
            status=status,
            workflow=workflow,
            failed_only=failed_only,
            include_inaccessible=True,
        )
        if status is None and workflow is None and not failed_only:
            write_workspace_report_index(workspace)
        return filter_workspace_report_index_for_access(workspace, index)
    path = workspace.reports_dir / "index.json"
    if not path.exists():
        write_workspace_report_index(workspace)
    return filter_workspace_report_index_for_access(workspace, json.loads(path.read_text(encoding="utf-8")))


def workspace_report_access_payload(workspace: Any, report_path: Path) -> dict[str, Any]:
    license_ = get_license()
    window_days = report_history_window_days(license_)
    modified_at = report_path.stat().st_mtime
    age_days = max(0.0, (time() - modified_at) / 86400)
    allowed = window_days is None or age_days <= float(window_days)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "feature": "workflow_history_unlimited",
        "tier": license_.tier,
        "allowed": allowed,
        "window_days": window_days,
        "age_days": round(age_days, 3),
        "modified_at": modified_at,
        "path": report_path.relative_to(workspace.root).as_posix()
        if report_path.is_relative_to(workspace.root)
        else str(report_path),
    }
    if not allowed:
        payload.update(
            {
                "status": "upgrade_required",
                "reason": "history_window_exceeded",
                "required_tier": "pro",
                "message": (
                    f"Report is {age_days:.1f} days old; free tier can query reports from the last "
                    f"{window_days} days. Upgrade to pro for unlimited report history."
                ),
            }
        )
    return payload


def filter_workspace_report_index_for_access(workspace: Any, index: dict[str, Any]) -> dict[str, Any]:
    entries = index.get("entries") if isinstance(index.get("entries"), list) else []
    visible_entries: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_path = entry.get("json_report")
        if raw_path:
            path = Path(str(raw_path))
            if not path.is_absolute():
                path = workspace.root / path
            if path.exists() and not workspace_report_access_payload(workspace, path)["allowed"]:
                continue
        visible_entries.append(entry)
    return {
        **index,
        "total_reports": len(visible_entries),
        "failed_reports": sum(1 for entry in visible_entries if entry.get("status") == "failed"),
        "entries": visible_entries,
        "history_access": {
            "tier": get_license().tier,
            "window_days": report_history_window_days(),
        },
    }


def report_index_entry(workspace: Any, report_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    markdown_path = report_path.with_suffix(".md")
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    failed_step = payload.get("failed_step")
    if failed_step is None:
        failed = next((step for step in steps if isinstance(step, dict) and step.get("status") == "failed"), None)
        failed_step = failed.get("id") if isinstance(failed, dict) else None
    return {
        "run_id": str(payload.get("run_id") or report_path.stem),
        "workflow_name": str(payload.get("workflow_name") or ""),
        "status": str(payload.get("status") or "unknown"),
        "run_profile": payload.get("run_profile"),
        "runtime_version": payload.get("runtime_version"),
        "workflow_schema_version": payload.get("workflow_schema_version"),
        "total_steps": int(payload.get("total_steps") or len(steps)),
        "succeeded_steps": int(payload.get("succeeded_steps") or 0),
        "failed_step": failed_step,
        "dry_run_actions": int(payload.get("dry_run_actions") or 0),
        "elapsed_seconds": float(payload.get("elapsed_seconds") or 0.0),
        "json_report": report_path.relative_to(workspace.root).as_posix()
        if report_path.is_relative_to(workspace.root)
        else str(report_path),
        "markdown_report": markdown_path.relative_to(workspace.root).as_posix()
        if markdown_path.exists() and markdown_path.is_relative_to(workspace.root)
        else (str(markdown_path) if markdown_path.exists() else None),
        "download_count": len(payload.get("downloads") if isinstance(payload.get("downloads"), list) else []),
        "failure_diagnosis_count": sum(
            1 for step in steps if isinstance(step, dict) and step.get("failure_diagnosis")
        ),
        "external_sample": payload.get("external_sample") if isinstance(payload.get("external_sample"), dict) else None,
        "annotation": None,
        "modified_at": report_path.stat().st_mtime,
    }


def workspace_report_tags_path(workspace: Any) -> Path:
    workspace.reports_dir.mkdir(parents=True, exist_ok=True)
    return workspace.reports_dir / "tags.json"


def load_workspace_report_tags(workspace: Any) -> dict[str, Any]:
    path = workspace_report_tags_path(workspace)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    tags = payload.get("reports") if isinstance(payload, dict) else None
    return tags if isinstance(tags, dict) else {}


def save_workspace_report_tags(workspace: Any, tags: dict[str, Any]) -> Path:
    path = workspace_report_tags_path(workspace)
    payload = {
        "schema_version": 1,
        "updated_at": time(),
        "reports": tags,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_workspace_report_index(workspace)
    return path


def tag_workspace_report(
    workspace: Any,
    run_id: str,
    *,
    review_status: str | None = None,
    tags: tuple[str, ...] = (),
    note: str | None = None,
    regression_candidate: bool | None = None,
) -> dict[str, Any]:
    known_run_ids = {entry["run_id"] for entry in build_workspace_report_index(workspace)["entries"]}
    if run_id not in known_run_ids:
        raise FileNotFoundError(f"Report not found in workspace: {run_id}")
    current = load_workspace_report_tags(workspace)
    previous = current.get(run_id) if isinstance(current.get(run_id), dict) else {}
    annotation = {
        **previous,
        "run_id": run_id,
        "updated_at": time(),
    }
    if review_status is not None:
        annotation["review_status"] = review_status
    if tags:
        existing = previous.get("tags") if isinstance(previous.get("tags"), list) else []
        annotation["tags"] = sorted({str(item) for item in [*existing, *tags] if str(item)})
    elif "tags" not in annotation:
        annotation["tags"] = []
    if note is not None:
        annotation["note"] = note
    if regression_candidate is not None:
        annotation["regression_candidate"] = regression_candidate
    if "review_status" not in annotation:
        annotation["review_status"] = "unreviewed"
    if "regression_candidate" not in annotation:
        annotation["regression_candidate"] = False
    current[run_id] = annotation
    save_workspace_report_tags(workspace, current)
    return annotation
