from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from time import time
from typing import Any


def build_product_issues(workspace: Any) -> dict[str, Any]:
    reports = failed_reports(workspace.reports_dir)
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for report_path, payload in reports:
        issue = issue_from_report(workspace, report_path, payload)
        grouped[(issue["workflow_name"], issue["failed_step"], issue["message"])].append(issue)

    issues = []
    for (workflow_name, failed_step, message), items in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        latest = max(items, key=lambda item: item["modified_at"])
        issues.append(
            {
                "workflow_name": workflow_name,
                "failed_step": failed_step,
                "message": message,
                "occurrences": len(items),
                "latest_run_id": latest["run_id"],
                "latest_report": latest["json_report"],
                "latest_screenshot": latest.get("screenshot"),
                "expected": latest.get("expected"),
                "actual": latest.get("actual"),
                "visible_text": latest.get("visible_text"),
                "suggestions": latest.get("suggestions", ()),
                "first_seen_at": min(item["modified_at"] for item in items),
                "last_seen_at": latest["modified_at"],
                "run_ids": tuple(item["run_id"] for item in sorted(items, key=lambda item: item["modified_at"], reverse=True)[:20]),
            }
        )
    return {
        "schema_version": 1,
        "generated_at": time(),
        "workspace_root": str(workspace.root),
        "total_failed_reports": len(reports),
        "total_issues": len(issues),
        "issues": issues,
    }


def write_product_issues(workspace: Any) -> Path:
    workspace.reports_dir.mkdir(parents=True, exist_ok=True)
    path = workspace.reports_dir / "product_issues.json"
    payload = build_product_issues(workspace)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def product_issues_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Product Issues",
        "",
        f"- Failed reports: {payload.get('total_failed_reports', 0)}",
        f"- Open issue groups: {payload.get('total_issues', 0)}",
        "",
    ]
    issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
    if not issues:
        lines.append("No failed product issues found.")
        return "\n".join(lines)
    for issue in issues:
        lines.extend(
            [
                f"## {issue.get('workflow_name')} / {issue.get('failed_step')}",
                "",
                f"- Occurrences: {issue.get('occurrences')}",
                f"- Latest run: `{issue.get('latest_run_id')}`",
                f"- Message: {issue.get('message')}",
            ]
        )
        if issue.get("expected"):
            lines.append(f"- Expected: {issue.get('expected')}")
        if issue.get("actual"):
            lines.append(f"- Actual: {issue.get('actual')}")
        if issue.get("visible_text"):
            lines.append(f"- Visible text: {issue.get('visible_text')}")
        if issue.get("latest_screenshot"):
            lines.append(f"- Screenshot: `{issue.get('latest_screenshot')}`")
        suggestions = issue.get("suggestions") if isinstance(issue.get("suggestions"), list) else []
        if suggestions:
            lines.append("- Suggested fix direction: " + "; ".join(str(item) for item in suggestions[:3]))
        lines.append("")
    return "\n".join(lines).rstrip()


def failed_reports(reports_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not reports_dir.exists():
        return []
    result = []
    for path in sorted(reports_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.name in {"index.json", "tags.json", "product_issues.json"}:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict) and str(payload.get("status")) == "failed":
            result.append((path, payload))
    return result


def issue_from_report(workspace: Any, report_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    failed_step_id = str(payload.get("failed_step") or "") or next_failed_step_id(steps)
    failed_step = next((step for step in steps if isinstance(step, dict) and str(step.get("id")) == failed_step_id), {})
    diagnosis = failed_step.get("failure_diagnosis") if isinstance(failed_step.get("failure_diagnosis"), dict) else {}
    metadata = failed_step.get("metadata") if isinstance(failed_step.get("metadata"), dict) else {}
    if not diagnosis and isinstance(metadata.get("failure_diagnosis"), dict):
        diagnosis = metadata["failure_diagnosis"]
    return {
        "run_id": str(payload.get("run_id") or report_path.stem),
        "workflow_name": str(payload.get("workflow_name") or ""),
        "failed_step": failed_step_id or "unknown",
        "message": str(failed_step.get("message") or payload.get("message") or "workflow failed"),
        "expected": diagnosis.get("expected"),
        "actual": diagnosis.get("actual"),
        "visible_text": diagnosis.get("visible_text") or visible_text_from_step(failed_step),
        "suggestions": tuple(str(item) for item in diagnosis.get("recovery_suggestions") or diagnosis.get("suggestions") or ()),
        "screenshot": screenshot_from_step(failed_step, diagnosis),
        "json_report": relative_path(workspace.root, report_path),
        "modified_at": report_path.stat().st_mtime,
    }


def next_failed_step_id(steps: list[Any]) -> str:
    failed = next((step for step in steps if isinstance(step, dict) and step.get("status") == "failed"), None)
    return str(failed.get("id") or "") if isinstance(failed, dict) else ""


def visible_text_from_step(step: dict[str, Any]) -> str | None:
    observation = step.get("observation") if isinstance(step.get("observation"), dict) else {}
    metadata = observation.get("metadata") if isinstance(observation.get("metadata"), dict) else {}
    visible = metadata.get("visible_text")
    if isinstance(visible, str):
        return visible[:500]
    if isinstance(visible, list):
        return " | ".join(str(item) for item in visible[:20])[:500]
    return None


def screenshot_from_step(step: dict[str, Any], diagnosis: dict[str, Any]) -> str | None:
    artifacts = diagnosis.get("artifacts") if isinstance(diagnosis.get("artifacts"), dict) else {}
    screenshot = artifacts.get("screenshot") or (step.get("observation") if isinstance(step.get("observation"), dict) else {}).get("screenshot_path")
    return str(screenshot) if screenshot else None


def relative_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)
