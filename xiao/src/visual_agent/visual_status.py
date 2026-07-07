from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import ActionStatus
from .structured_failure import HYDRATION_MISMATCH_MARKERS
from .workflow import Workflow, WorkflowRunResult


STATUS_FILE = ".visual-agent-status.md"
RUN_HISTORY_FILE = "run_history.jsonl"
RUN_HISTORY_LIMIT = 500


@dataclass(frozen=True)
class VisualStatus:
    status: str
    passing: tuple[str, ...]
    failing: tuple[dict[str, Any], ...]
    active_task: str
    last_run_minutes_ago: int | None
    environment: str = ""
    path: str = ""


def write_status_file(
    project_root: Path,
    result: WorkflowRunResult,
    *,
    active_task: str = "",
    environment: str = "",
) -> Path:
    project_root = project_root.resolve()
    status_path = project_root / STATUS_FILE
    failed = first_failed_step(result)
    status = "FAILING" if failed else "PASSING"
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    passing = [result.workflow_name] if not failed else []
    failing = []
    if failed:
        diagnosis = failed.metadata.get("failure_diagnosis") if isinstance(failed.metadata, dict) else {}
        root_cause = str(diagnosis.get("root_cause") or diagnosis.get("classification") or "unknown") if isinstance(diagnosis, dict) else "unknown"
        confidence = diagnosis.get("confidence") if isinstance(diagnosis, dict) else None
        hint = str(diagnosis.get("suggested_fix") or diagnosis.get("hint") or failed.message or "") if isinstance(diagnosis, dict) else failed.message
        suffix = f" root_cause={root_cause}"
        if confidence is not None:
            suffix += f" confidence={confidence}"
        if hint:
            suffix += f" suggested_fix={hint}"
        known = " [KNOWN]" if _known_problem_from_history(project_root, result.workflow_name, failed.id, root_cause) else ""
        failing.append(f"{result.workflow_name}: {failed.id}{known} - {suffix}")
    lines = [
        f"<!-- Checkpoint - {timestamp} -->",
        f"## Status: {status}",
        "### Passing:",
        *(f"- {name}" for name in passing),
        "### Failing:",
        *(f"- {item}" for item in failing),
        f"### Active Task: {active_task or 'None'}",
        "### Last Run: 0 minutes ago",
    ]
    if environment:
        lines.extend(["### Environment:", f"- env: {environment}"])
    status_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return status_path


def write_environment_status_file(
    project_root: Path,
    environment: dict[str, Any],
    *,
    active_task: str = "Environment check",
) -> Path:
    project_root = project_root.resolve()
    status_path = project_root / STATUS_FILE
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = str(environment.get("status") or ("OK" if environment.get("ok") else "WARN"))
    warnings = environment.get("warnings") if isinstance(environment.get("warnings"), list) else []
    build_checks = environment.get("build_checks") if isinstance(environment.get("build_checks"), list) else []
    lines = [
        f"<!-- Checkpoint - {timestamp} -->",
        f"## Status: {status}",
        "### Passing:",
        "### Failing:",
        *(f"- env: {warning}" for warning in warnings),
        f"### Active Task: {active_task}",
        "### Last Run: 0 minutes ago",
        "### Environment:",
        f"- env: {status}",
    ]
    project_type = environment.get("project_type")
    if project_type:
        lines.append(f"- project_type: {project_type}")
    port_check = environment.get("port_check") if isinstance(environment.get("port_check"), dict) else None
    if port_check:
        lines.append(f"- port: {port_check.get('port')} {port_check.get('status')}")
        if port_check.get("message"):
            lines.append(f"- port_message: {port_check.get('message')}")
    for check in build_checks:
        if not isinstance(check, dict):
            continue
        lines.append(f"- build: {check.get('path')} {check.get('status')} age={check.get('age_minutes')}")
    if warnings:
        lines.append("- warnings: " + " | ".join(str(warning) for warning in warnings))
    status_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return status_path


def read_status_file(project_root: Path) -> VisualStatus | None:
    path = project_root.resolve() / STATUS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_status_markdown(text, path=path)


def parse_status_markdown(text: str, *, path: Path | None = None) -> VisualStatus:
    status = "UNKNOWN"
    passing: list[str] = []
    failing: list[dict[str, Any]] = []
    active_task = ""
    last_run_minutes_ago: int | None = None
    environment = ""
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("## Status:"):
            status = line.split(":", 1)[1].strip()
            continue
        if line.startswith("### "):
            section = line.removeprefix("### ").rstrip(":")
            if section.startswith("Active Task"):
                active_task = line.split(":", 1)[1].strip()
            elif section.startswith("Last Run"):
                last_run_minutes_ago = parse_minutes(line)
            continue
        if not line.startswith("- "):
            continue
        value = line[2:].strip()
        if section == "Passing":
            passing.append(value)
        elif section == "Failing":
            failing.append(parse_failing_line(value))
        elif section == "Environment":
            environment = value
    return VisualStatus(
        status=status,
        passing=tuple(passing),
        failing=tuple(failing),
        active_task=active_task,
        last_run_minutes_ago=last_run_minutes_ago,
        environment=environment,
        path=str(path) if path else "",
    )


def parse_failing_line(value: str) -> dict[str, Any]:
    workflow, _, rest = value.partition(":")
    step, _, detail = rest.strip().partition(" - ")
    return {"workflow": workflow.strip(), "step": step.strip(), "detail": detail.strip(), "raw": value}


def parse_minutes(line: str) -> int | None:
    import re

    match = re.search(r"(\d+)\s+minutes?", line)
    return int(match.group(1)) if match else None


def append_run_history(workspace_root: Path, workflow: Workflow, result: WorkflowRunResult) -> Path:
    history_path = workspace_root / RUN_HISTORY_FILE
    history_path.parent.mkdir(parents=True, exist_ok=True)
    records = read_run_history(workspace_root)
    record = run_history_record(workspace_root, workflow, result)
    if record.get("failed_step") and record.get("root_cause_guess"):
        previous_count = sum(
            1
            for item in records
            if item.get("workflow_name") == record.get("workflow_name")
            and item.get("failed_step") == record.get("failed_step")
            and item.get("root_cause_guess") == record.get("root_cause_guess")
        )
        record["known_problem"] = previous_count >= 2
        if record["known_problem"]:
            record["known_label"] = "[KNOWN]"
    records.append(record)
    records = records[-RUN_HISTORY_LIMIT:]
    history_path.write_text("".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in records), encoding="utf-8")
    return history_path


def append_cloud_run_history(workspace_root: Path, result: dict[str, Any]) -> Path:
    history_path = workspace_root / RUN_HISTORY_FILE
    history_path.parent.mkdir(parents=True, exist_ok=True)
    records = read_run_history(workspace_root)
    status = str(result.get("status") or "unknown")
    workflow_source = str(result.get("workflow_source") or "").strip() or "unknown"
    record = {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "cloud",
        "run_id": str(result.get("run_id") or result.get("id") or ""),
        "workflow_name": str(result.get("workflow_name") or ""),
        "workflow_source": workflow_source,
        "workflow_id": str(result.get("workflow_id") or ""),
        "run_profile": str(result.get("run_profile") or ""),
        "passed": status == "success",
        "status": "passed" if status == "success" else status,
        "step_count": int(result.get("steps_total") or 0),
        "step_types": [],
        "tags": [],
        "visibility": "",
        "duration_ms": int(result.get("duration_ms") or 0),
        "failed_step": str(result.get("failed_step") or "") or None,
        "failed_action": None,
        "root_cause_guess": str(result.get("root_cause_guess") or "") or None,
        "report_url": str(result.get("report_url") or ""),
        "artifact_url": str(result.get("artifact_url") or ""),
        "workspace_root": str(workspace_root),
    }
    records.append(record)
    records = records[-RUN_HISTORY_LIMIT:]
    history_path.write_text("".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in records), encoding="utf-8")
    return history_path


def read_run_history(workspace_root: Path) -> list[dict[str, Any]]:
    path = workspace_root / RUN_HISTORY_FILE
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records[-RUN_HISTORY_LIMIT:]


def run_history_record(workspace_root: Path, workflow: Workflow, result: WorkflowRunResult) -> dict[str, Any]:
    failed = first_failed_step(result)
    duration_ms = int(sum(float(step.metadata.get("elapsed_seconds") or 0.0) for step in result.steps if isinstance(step.metadata, dict)) * 1000)
    return {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "workspace",
        "run_id": result.run_id,
        "workflow_name": result.workflow_name,
        "workflow_source": "workspace",
        "workflow_id": "",
        "workflow_schema_version": result.workflow_schema_version,
        "run_profile": result.run_profile,
        "passed": failed is None,
        "status": "passed" if failed is None else "failed",
        "step_count": len(result.steps),
        "step_types": [step.action for step in result.steps],
        "tags": list(workflow.tags),
        "visibility": workflow.visibility,
        "duration_ms": duration_ms,
        "failed_step": failed.id if failed else None,
        "failed_action": failed.action if failed else None,
        "root_cause_guess": root_cause_guess(failed),
        "run_dir": str(result.run_dir),
        "workspace_root": str(workspace_root),
    }


def local_stats(workspace_root: Path) -> dict[str, Any]:
    records = read_run_history(workspace_root)
    total = len(records)
    passed = sum(1 for item in records if item.get("passed") is True)
    slowest = max(records, key=lambda item: int(item.get("duration_ms") or 0), default=None)
    failures: dict[str, int] = {}
    for item in records:
        key = str(item.get("failed_step") or item.get("failed_action") or "")
        if key:
            failures[key] = failures.get(key, 0) + 1
    most_failed = sorted(failures.items(), key=lambda item: item[1], reverse=True)
    return {
        "schema_version": 1,
        "workspace": str(workspace_root),
        "total_runs": total,
        "passed_runs": passed,
        "failed_runs": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "slowest_workflow": slowest,
        "most_failed_steps": [{"step": step, "count": count} for step, count in most_failed[:5]],
    }


def export_run_history(workspace_root: Path, output_path: Path, *, fmt: str = "json") -> Path:
    records = read_run_history(workspace_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        fieldnames = sorted({key for item in records for key in item.keys()}) or ["run_id"]
        with output_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for item in records:
                writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value for key, value in item.items()})
        return output_path
    output_path.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return output_path


def first_failed_step(result: WorkflowRunResult) -> Any | None:
    for step in result.steps:
        if step.status == ActionStatus.FAILED or getattr(step.status, "value", str(step.status)) == "failed":
            return step
    return None


def root_cause_guess(step: Any | None) -> str | None:
    if step is None:
        return None
    diagnosis = step.metadata.get("failure_diagnosis") if isinstance(getattr(step, "metadata", None), dict) else None
    if isinstance(diagnosis, dict):
        cause = str(diagnosis.get("root_cause") or diagnosis.get("classification") or "") or None
        if cause:
            return cause
    message = str(getattr(step, "message", "")).lower()
    if any(marker in message for marker in HYDRATION_MISMATCH_MARKERS):
        return "known_issue"
    action = str(getattr(step, "action", ""))
    if "connection refused" in message or "timeout" in message:
        return "env_error"
    if action.startswith("assert"):
        return "assertion_wrong"
    if action in {"click", "type", "paste"}:
        return "element_missing"
    return "unknown"


def _known_problem_from_history(project_root: Path, workflow_name: str, step_id: str, root_cause: str) -> bool:
    workspace_root = project_root / ".agent-workspace"
    records = read_run_history(workspace_root)
    return (
        sum(
            1
            for item in records
            if item.get("workflow_name") == workflow_name
            and item.get("failed_step") == step_id
            and item.get("root_cause_guess") == root_cause
        )
        >= 2
    )


def visual_status_to_dict(status: VisualStatus | None) -> dict[str, Any]:
    if status is None:
        return {"status": "not_found"}
    return {
        "status": status.status,
        "passing": list(status.passing),
        "failing": [dict(item) for item in status.failing],
        "active_task": status.active_task,
        "last_run_minutes_ago": status.last_run_minutes_ago,
        "environment": status.environment,
        "path": status.path,
    }

