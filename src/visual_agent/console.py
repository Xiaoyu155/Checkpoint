from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .quality import load_quality_gate_index, quality_gate_index_to_markdown
from .acceptance import agent_verdict
from .reports import acceptance_status_suffix, agent_verdict_markdown_lines
from .workspace import (
    Workspace,
    load_workspace_gui_action_history_risk_config,
    load_workspace_auto_repair_policy,
    load_workspace_report_index,
    load_workspace_report_tags,
    workspace_report_access_payload,
    validate_workspace_risk_policy,
    workspace_status,
)


DEFAULT_DASHBOARD_RISK_ATTENTION_TRENDS = ("worsening",)


def build_workspace_dashboard(workspace: Workspace, *, limit: int = 5) -> dict[str, Any]:
    status = workspace_status(workspace)
    quality = load_quality_gate_index(workspace_root=workspace.root)
    strict_policy_failed_quality = load_quality_gate_index(
        workspace_root=workspace.root,
        strict_policy_failed=True,
    )
    risk_config = load_workspace_gui_action_history_risk_config(workspace)
    auto_repair_policy = load_workspace_auto_repair_policy(workspace)
    risk_health_policy = dashboard_risk_health_policy(risk_config)
    risk_policy_check = validate_workspace_risk_policy(workspace)
    reports = status.get("reports") if isinstance(status.get("reports"), list) else []
    queue = status.get("queue") if isinstance(status.get("queue"), list) else []
    recent_runs = status.get("recent_runs") if isinstance(status.get("recent_runs"), list) else []
    regression_tests = status.get("regression_tests") if isinstance(status.get("regression_tests"), list) else []
    health = dashboard_health(status, quality, risk_health_policy=risk_health_policy, risk_policy_check=risk_policy_check)
    return {
        "schema_version": 1,
        "workspace": {
            "root": status["root"],
            "workflow_count": status["workflow_count"],
            "valid_workflows": status["valid_workflows"],
            "invalid_workflows": status["invalid_workflows"],
        },
        "health": health,
        "risk_policy_check": risk_policy_check,
        "auto_repair_policy": auto_repair_policy,
        "runs": {
            "shown": status["run_count_shown"],
            "recent": recent_runs[:limit],
        },
        "reports": {
            "total": status["report_count"],
            "failed": sum(1 for item in reports if item.get("status") == "failed"),
            "recent": reports[:limit],
        },
        "quality_gates": {
            "total": quality["total_reports"],
            "successful": quality["successful_reports"],
            "failed": quality["failed_reports"],
            "latest": quality["latest"],
            "risk_warnings": int(quality.get("risk_warnings") or 0),
            "risk_trends": quality.get("risk_trends") if isinstance(quality.get("risk_trends"), dict) else {},
            "latest_risk_trend_direction": latest_quality_risk_trend_direction(quality),
            "strict_policy_gate_enabled": int(quality.get("strict_policy_gate_enabled") or 0),
            "strict_policy_gate_failed": int(quality.get("strict_policy_gate_failed") or 0),
            "strict_policy_gate_policy_errors": int(quality.get("strict_policy_gate_policy_errors") or 0),
            "latest_strict_policy_gate_failed": latest_quality_strict_policy_gate_failed(quality),
            "strict_policy_failed_reports": strict_policy_failed_quality.get("entries")
            if isinstance(strict_policy_failed_quality.get("entries"), list)
            else [],
            "strict_policy_failed_markdown": quality_gate_index_to_markdown(strict_policy_failed_quality),
            "risk_health_policy": risk_health_policy,
        },
        "regression_tests": {
            "total": status["regression_test_count"],
            "recent": regression_tests[:limit],
        },
        "queue": {
            "total": status["queue_task_count"],
            "pending": status["pending_queue_tasks"],
            "running": status["running_queue_tasks"],
            "recent": queue[:limit],
        },
    }


def dashboard_health(
    status: dict[str, Any],
    quality: dict[str, Any],
    *,
    risk_health_policy: dict[str, Any] | None = None,
    risk_policy_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    if int(status.get("invalid_workflows") or 0) > 0:
        issues.append("invalid_workflows")
    if any(item.get("status") == "failed" for item in status.get("reports", [])):
        issues.append("failed_reports")
    latest_quality = quality.get("latest") if isinstance(quality.get("latest"), dict) else None
    if latest_quality and latest_quality.get("status") == "failed":
        issues.append("failed_quality_gate")
    attention_trends = set(dashboard_risk_health_policy(risk_health_policy).get("attention_trend_directions", []))
    latest_risk_trend = str(latest_quality.get("risk_trend_direction") or "") if latest_quality else ""
    if latest_risk_trend in attention_trends:
        issues.append(f"gui_action_risk_{latest_risk_trend}")
    if latest_quality and latest_quality.get("strict_policy_gate_failed"):
        issues.append("strict_policy_gate_failed")
    if isinstance(risk_policy_check, dict) and int(risk_policy_check.get("error_count") or 0) > 0:
        issues.append("workspace_risk_policy_invalid")
    if int(status.get("running_queue_tasks") or 0) > 0:
        issues.append("running_queue_tasks")
    return {
        "status": "attention" if issues else "ok",
        "issues": issues,
    }


def dashboard_risk_health_policy(config: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(config, dict):
        config = {}
    health_config = config.get("health") if isinstance(config.get("health"), dict) else {}
    raw_directions = health_config.get("attention_trend_directions", config.get("attention_trend_directions"))
    directions = normalize_attention_trend_directions(raw_directions)
    return {"attention_trend_directions": directions}


def normalize_attention_trend_directions(value: Any) -> list[str]:
    if not isinstance(value, list):
        return list(DEFAULT_DASHBOARD_RISK_ATTENTION_TRENDS)
    directions: list[str] = []
    for item in value:
        direction = str(item).strip()
        if direction and direction not in directions:
            directions.append(direction)
    return directions


def dashboard_to_markdown(dashboard: dict[str, Any]) -> str:
    workspace = dashboard["workspace"]
    health = dashboard["health"]
    quality = dashboard["quality_gates"]
    risk_policy_check = dashboard.get("risk_policy_check") if isinstance(dashboard.get("risk_policy_check"), dict) else {}
    auto_repair_policy = dashboard.get("auto_repair_policy") if isinstance(dashboard.get("auto_repair_policy"), dict) else {}
    queue = dashboard["queue"]
    reports = dashboard["reports"]
    regression = dashboard["regression_tests"]
    lines = [
        "# Workspace Dashboard",
        "",
        f"- Root: `{workspace['root']}`",
        f"- Health: `{health['status']}`",
        f"- Issues: {', '.join(health['issues']) if health['issues'] else 'none'}",
        f"- Workflows: {workspace['valid_workflows']}/{workspace['workflow_count']} valid",
        f"- Reports: {reports['total']} total, {reports['failed']} failed",
        f"- Quality gates: {quality['total']} total, {quality['failed']} failed",
        f"- Quality risk warnings: {quality.get('risk_warnings', 0)}",
        f"- Latest quality risk trend: `{quality.get('latest_risk_trend_direction') or 'unknown'}`",
        f"- Strict policy gate failures: {quality.get('strict_policy_gate_failed', 0)}",
        f"- Latest strict policy gate failed: {quality.get('latest_strict_policy_gate_failed', False)}",
        "- Risk health attention trends: "
        + ", ".join(f"`{item}`" for item in quality.get("risk_health_policy", {}).get("attention_trend_directions", [])),
        f"- Risk policy check: `{risk_policy_check.get('status') or 'unknown'}`"
        f" ({risk_policy_check.get('error_count', 0)} errors, {risk_policy_check.get('warning_count', 0)} warnings)",
        f"- Auto repair policy: min_confidence `{auto_repair_policy.get('min_confidence')}`, "
        f"max_risk `{auto_repair_policy.get('max_risk_level')}`, "
        f"allow_force `{auto_repair_policy.get('allow_force')}`, "
        f"source `{auto_repair_policy.get('source')}`",
        f"- Regression tests: {regression['total']}",
        f"- Queue: {queue['pending']} pending, {queue['running']} running, {queue['total']} total",
        "",
    ]
    latest_quality = quality.get("latest")
    if latest_quality:
        lines.extend(
            [
                "## Latest Quality Gate",
                "",
                f"- Run ID: `{latest_quality['run_id']}`",
                f"- Profile: `{latest_quality['profile']}`",
                f"- Status: `{latest_quality['status']}`",
                f"- Risk trend: `{latest_quality.get('risk_trend_direction') or 'unknown'}`",
                f"- Risk warnings: {latest_quality.get('risk_warning_count', 0)}",
                f"- Strict policy gate: enabled={latest_quality.get('strict_policy_gate_enabled', False)}, "
                f"failed={latest_quality.get('strict_policy_gate_failed', False)}, "
                f"policy_errors={latest_quality.get('strict_policy_gate_policy_errors', 0)}",
                "",
            ]
        )
    append_table(
        lines,
        "Strict Policy Failure History",
        quality.get("strict_policy_failed_reports", []),
        ("run_id", "profile", "status", "strict_policy_gate_policy_errors", "json_report", "markdown_report"),
    )
    lines.extend(risk_policy_check_to_markdown_lines(risk_policy_check))
    append_table(lines, "Recent Runs", dashboard["runs"]["recent"], ("run_id", "workflow_name", "status"))
    append_table(lines, "Recent Reports", reports["recent"], ("run_id", "workflow_name", "status"))
    append_table(lines, "Queue", queue["recent"], ("task_id", "workflow", "status"))
    return "\n".join(lines).rstrip() + "\n"


def risk_policy_check_to_markdown(check: dict[str, Any]) -> str:
    return "\n".join(risk_policy_check_to_markdown_lines(check)).rstrip() + "\n"


def risk_policy_check_to_markdown_lines(check: dict[str, Any]) -> list[str]:
    if not isinstance(check, dict):
        check = {}
    lines = [
        "## Risk Policy Check",
        "",
        f"- Status: `{check.get('status') or 'unknown'}`",
        f"- Errors: {int(check.get('error_count') or 0)}",
        f"- Warnings: {int(check.get('warning_count') or 0)}",
        "",
    ]
    issues = check.get("issues") if isinstance(check.get("issues"), list) else []
    if not issues:
        lines.extend(["No risk policy issues.", ""])
        return lines
    lines.append("| level | code | path | message | suggestion |")
    lines.append("| --- | --- | --- | --- | --- |")
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                markdown_cell(issue.get(key))
                for key in ("level", "code", "path", "message", "suggestion")
            )
            + " |"
        )
    lines.append("")
    return lines


def latest_quality_risk_trend_direction(quality: dict[str, Any]) -> str:
    latest = quality.get("latest") if isinstance(quality.get("latest"), dict) else {}
    return str(latest.get("risk_trend_direction") or "unknown")


def latest_quality_strict_policy_gate_failed(quality: dict[str, Any]) -> bool:
    latest = quality.get("latest") if isinstance(quality.get("latest"), dict) else {}
    return bool(latest.get("strict_policy_gate_failed", False))


def build_report_detail(workspace: Workspace, run_id: str) -> dict[str, Any]:
    report_path = find_report_json_path(workspace, run_id)
    access = workspace_report_access_payload(workspace, report_path)
    if not access["allowed"]:
        return {
            "schema_version": 1,
            "status": "upgrade_required",
            "run_id": run_id,
            "history_access": access,
            "message": access.get("message"),
        }
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    compact_steps = [compact_report_step(step) for step in steps if isinstance(step, dict)]
    failed_step = str(payload.get("failed_step") or "") or next_failed_step_id(compact_steps)
    annotation = load_workspace_report_tags(workspace).get(run_id)
    markdown_path = report_path.with_suffix(".md")
    return {
        "schema_version": 1,
        "run_id": str(payload.get("run_id") or run_id),
        "workflow_name": str(payload.get("workflow_name") or ""),
        "status": str(payload.get("status") or "unknown"),
        "run_profile": payload.get("run_profile"),
        "runtime_version": payload.get("runtime_version"),
        "workflow_schema_version": payload.get("workflow_schema_version"),
        "acceptance": payload.get("acceptance") if isinstance(payload.get("acceptance"), dict) else None,
        "agent_verdict": agent_verdict(
            payload.get("acceptance") if isinstance(payload.get("acceptance"), dict) else None,
            run_status=str(payload.get("status") or "unknown"),
            dry_run_actions=int(payload.get("dry_run_actions") or 0),
        ),
        "run_checks": payload.get("run_checks") if isinstance(payload.get("run_checks"), dict) else None,
        "summary": {
            "total_steps": int(payload.get("total_steps") or len(compact_steps)),
            "succeeded_steps": int(payload.get("succeeded_steps") or 0),
            "failed_step": failed_step or None,
            "dry_run_actions": int(payload.get("dry_run_actions") or 0),
            "elapsed_seconds": float(payload.get("elapsed_seconds") or 0.0),
        },
        "paths": {
            "json_report": relative_workspace_path(workspace, report_path),
            "markdown_report": relative_workspace_path(workspace, markdown_path) if markdown_path.exists() else None,
        },
        "artifacts": payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {},
        "downloads": payload.get("downloads") if isinstance(payload.get("downloads"), list) else [],
        "external_sample": payload.get("external_sample") if isinstance(payload.get("external_sample"), dict) else None,
        "steps": compact_steps,
        "failure": report_failure_detail(failed_step, steps),
        "annotation": annotation if isinstance(annotation, dict) else None,
    }


def report_detail_to_markdown(detail: dict[str, Any]) -> str:
    if detail.get("status") == "upgrade_required":
        access = detail.get("history_access") if isinstance(detail.get("history_access"), dict) else {}
        return "\n".join(
            [
                "# Report Detail",
                "",
                "- Status: `upgrade_required`",
                f"- Run ID: `{detail.get('run_id')}`",
                f"- Feature: `{access.get('feature') or 'workflow_history_unlimited'}`",
                f"- Current tier: `{access.get('tier') or 'free'}`",
                f"- Required tier: `{access.get('required_tier') or 'pro'}`",
                f"- Reason: `{access.get('reason') or 'history_window_exceeded'}`",
                f"- Message: {detail.get('message') or access.get('message') or ''}",
            ]
        ).rstrip() + "\n"
    summary = detail["summary"]
    lines = [
        f"# Report Detail: {detail['workflow_name']}",
        "",
        f"- Run ID: `{detail['run_id']}`",
        f"- Status: `{detail['status']}`",
        f"- Run profile: `{detail.get('run_profile')}`",
        f"- Runtime version: `{detail.get('runtime_version')}`",
        f"- Steps: {summary['succeeded_steps']}/{summary['total_steps']} succeeded",
        f"- Dry-run actions: {summary['dry_run_actions']}",
        f"- Failed step: `{summary['failed_step']}`" if summary.get("failed_step") else "- Failed step: none",
        f"- Elapsed seconds: {summary['elapsed_seconds']}",
    ]
    acceptance = detail.get("acceptance") if isinstance(detail.get("acceptance"), dict) else None
    if acceptance:
        suffix = acceptance_status_suffix(acceptance)
        lines.append(f"- Acceptance level: `{acceptance.get('label')}` ({acceptance.get('name')}){suffix}")
    verdict = detail.get("agent_verdict") if isinstance(detail.get("agent_verdict"), dict) else None
    if verdict:
        lines.extend(agent_verdict_markdown_lines(verdict))
    lines.append("")
    external_sample = detail.get("external_sample") if isinstance(detail.get("external_sample"), dict) else None
    if external_sample:
        lines.extend(
            [
                "## External Sample",
                "",
                f"- Sample ID: `{external_sample.get('sample_id')}`",
                f"- Run status: `{external_sample.get('run_status')}`",
                f"- Ready at run: `{external_sample.get('ready_at_run')}`",
                f"- Blockers at run: {', '.join(external_sample.get('blockers_at_run') or []) or 'none'}",
                f"- Requirements: {', '.join(external_sample.get('requirements') or []) or 'none'}",
                f"- Allowed domains: {', '.join(external_sample.get('allowed_domains') or []) or 'none'}",
                f"- Storage state policy: `{external_sample.get('storage_state_policy')}`",
                f"- Download policy: `{external_sample.get('download_policy')}`",
                "",
            ]
        )
    lines.extend(["## Steps", ""])
    steps = detail.get("steps") if isinstance(detail.get("steps"), list) else []
    if steps:
        lines.append("| id | action | status | target | message |")
        lines.append("| --- | --- | --- | --- | --- |")
        for step in steps:
            lines.append(
                "| "
                + " | ".join(
                    markdown_cell(step.get(key))
                    for key in ("id", "action", "status", "target", "message")
                )
                + " |"
            )
        lines.append("")
    else:
        lines.extend(["none", ""])

    failure = detail.get("failure") if isinstance(detail.get("failure"), dict) else None
    if failure:
        lines.extend(
            [
                "## Failure Diagnosis",
                "",
                f"- Failed step: `{failure.get('failed_step')}`",
                f"- Expected: {failure.get('expected')}",
                f"- Actual: {failure.get('actual')}",
            ]
        )
        suggestions = failure.get("recovery_suggestions")
        if isinstance(suggestions, list) and suggestions:
            lines.append("- Recovery suggestions: " + "; ".join(str(item) for item in suggestions))
        evidence = failure.get("evidence")
        if isinstance(evidence, dict):
            lines.append("- Evidence keys: " + ", ".join(sorted(str(key) for key in evidence.keys())))
        artifacts = failure.get("artifacts")
        if isinstance(artifacts, dict) and artifacts:
            lines.append("- Failure artifacts: " + ", ".join(f"`{value}`" for value in artifacts.values() if value))
        lines.append("")

    downloads = detail.get("downloads") if isinstance(detail.get("downloads"), list) else []
    lines.extend(["## Downloads", ""])
    if downloads:
        for item in downloads:
            if isinstance(item, dict):
                lines.append(f"- `{item.get('filename') or item.get('path')}` ({item.get('size_bytes')} bytes)")
    else:
        lines.append("none")
    lines.append("")

    artifacts = detail.get("artifacts") if isinstance(detail.get("artifacts"), dict) else {}
    lines.extend(["## Artifacts", ""])
    if artifacts:
        for key, value in artifacts.items():
            if isinstance(value, list):
                lines.append(f"- {key}: {len(value)} item(s)")
            elif value:
                lines.append(f"- {key}: `{value}`")
    else:
        lines.append("none")
    return "\n".join(lines).rstrip() + "\n"


def find_report_json_path(workspace: Workspace, run_id: str) -> Path:
    direct = workspace.reports_dir / f"{run_id}.json"
    if direct.exists():
        return direct
    index = load_workspace_report_index(workspace, rebuild=True)
    for entry in index.get("entries", []):
        if not isinstance(entry, dict) or entry.get("run_id") != run_id:
            continue
        raw_path = entry.get("json_report")
        if not raw_path:
            break
        path = Path(str(raw_path))
        if not path.is_absolute():
            path = workspace.root / path
        if path.exists():
            return path
    raise FileNotFoundError(f"Report not found in workspace: {run_id}")


def compact_report_step(step: dict[str, Any]) -> dict[str, Any]:
    failure_diagnosis = step.get("failure_diagnosis") if isinstance(step.get("failure_diagnosis"), dict) else None
    return {
        "id": str(step.get("id") or ""),
        "action": str(step.get("action") or ""),
        "status": str(step.get("status") or ""),
        "message": str(step.get("message") or ""),
        "elapsed_seconds": step.get("elapsed_seconds"),
        "attempts": step.get("attempts"),
        "provider": step.get("provider"),
        "target": step.get("target"),
        "artifact_paths": step.get("artifact_paths") if isinstance(step.get("artifact_paths"), list) else [],
        "has_failure_diagnosis": failure_diagnosis is not None,
    }


def report_failure_detail(failed_step: str | None, steps: list[Any]) -> dict[str, Any] | None:
    for step in steps:
        if not isinstance(step, dict):
            continue
        if failed_step and step.get("id") != failed_step:
            continue
        diagnosis = step.get("failure_diagnosis") if isinstance(step.get("failure_diagnosis"), dict) else None
        if diagnosis is None and str(step.get("status") or "") != "failed":
            continue
        return {
            "failed_step": str(step.get("id") or failed_step or ""),
            "action": step.get("action"),
            "provider": step.get("provider"),
            "message": step.get("message"),
            "expected": diagnosis.get("expected") if diagnosis else None,
            "actual": diagnosis.get("actual") if diagnosis else None,
            "recovery_suggestions": diagnosis.get("recovery_suggestions") if diagnosis else [],
            "evidence": diagnosis.get("evidence") if diagnosis else {},
            "artifacts": diagnosis.get("artifacts") if diagnosis else {},
            "diagnosis": diagnosis,
        }
    return None


def next_failed_step_id(steps: list[dict[str, Any]]) -> str | None:
    failed = next((step for step in steps if step.get("status") == "failed"), None)
    return str(failed.get("id")) if failed else None


def relative_workspace_path(workspace: Workspace, path: Path) -> str:
    return path.relative_to(workspace.root).as_posix() if path.is_relative_to(workspace.root) else str(path)


def markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def append_table(lines: list[str], title: str, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    lines.extend([f"## {title}", ""])
    if not rows:
        lines.extend(["none", ""])
        return
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(column) or "") for column in columns) + " |")
    lines.append("")
