from __future__ import annotations

import json
import asyncio
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, strftime, time
from typing import Any, Literal
from uuid import uuid4

from .models import to_jsonable
from .security import contains_secret_text, redact_secret_text


QualityProfile = Literal["local", "ci"]


@dataclass(frozen=True)
class QualityGateStep:
    name: str
    command: tuple[str, ...]
    required: bool = True
    status: str = "planned"
    exit_code: int | None = None
    elapsed_seconds: float | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class QualityGateResult:
    run_id: str
    profile: str
    status: str
    report_path: Path | None
    markdown_path: Path | None
    steps: tuple[QualityGateStep, ...]
    elapsed_seconds: float = 0.0
    risk_summary: dict[str, Any] = field(default_factory=dict)


def build_quality_gate_plan(
    profile: QualityProfile,
    *,
    workspace_root: str | Path | None = None,
) -> tuple[QualityGateStep, ...]:
    python = sys.executable
    steps = [
        QualityGateStep(name="core_tests", command=(python, "-m", "pytest")),
    ]
    if profile == "ci":
        steps.append(
            QualityGateStep(
                name="workflow_contracts",
                command=(python, "-m", "pytest", "tests/test_workflow_contracts.py"),
            )
        )
    if workspace_root is not None:
        workspace = Path(workspace_root)
        regression_dir = workspace / "regression_tests"
        if any(regression_dir.glob("test_*.py")):
            steps.append(
                QualityGateStep(
                    name="workspace_regression_tests",
                    command=(
                        python,
                        "-m",
                        "visual_agent.cli",
                        "workspace-run-regression-tests",
                        "--root",
                        str(workspace),
                    ),
                )
            )
    return tuple(steps)


def run_quality_gate(
    profile: QualityProfile,
    *,
    workspace_root: str | Path | None = None,
    execute: bool = False,
    timeout_seconds: float = 300.0,
    report_root: str | Path | None = None,
    fail_on_risk_policy_error: bool = False,
    fail_on_secret_leak: bool = False,
) -> QualityGateResult:
    run_id = f"{strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    planned = build_quality_gate_plan(profile, workspace_root=workspace_root)
    risk_summary = build_quality_gate_risk_summary(workspace_root=workspace_root, profile=profile)
    risk_summary = apply_quality_gate_strict_policy(
        risk_summary,
        fail_on_risk_policy_error=fail_on_risk_policy_error,
        fail_on_secret_leak=fail_on_secret_leak,
    )
    if not execute:
        result = QualityGateResult(
            run_id=run_id,
            profile=profile,
            status="planned",
            report_path=None,
            markdown_path=None,
            steps=planned,
            risk_summary=risk_summary,
        )
        return result

    started = monotonic()
    executed: list[QualityGateStep] = []
    for step in planned:
        step_started = monotonic()
        completed = subprocess.run(
            list(step.command),
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        executed.append(
            QualityGateStep(
                name=step.name,
                command=step.command,
                required=step.required,
                status="success" if completed.returncode == 0 else "failed",
                exit_code=completed.returncode,
                elapsed_seconds=round(monotonic() - step_started, 6),
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        )
    status = quality_gate_status(executed, risk_summary=risk_summary)
    elapsed = round(monotonic() - started, 6)
    root = quality_report_root(report_root=report_root, workspace_root=workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / f"{run_id}.json"
    markdown_path = root / f"{run_id}.md"
    result = QualityGateResult(
        run_id=run_id,
        profile=profile,
        status=status,
        report_path=report_path,
        markdown_path=markdown_path,
        steps=tuple(executed),
        elapsed_seconds=elapsed,
        risk_summary=risk_summary,
    )
    report_path.write_text(json.dumps(quality_gate_to_dict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(quality_gate_to_markdown(result), encoding="utf-8")
    write_quality_gate_index(report_root=root)
    return result


def quality_report_root(
    *,
    report_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
) -> Path:
    if report_root is not None:
        return Path(report_root)
    if workspace_root is not None:
        return Path(workspace_root) / "reports" / "quality_gates"
    return Path(".runs") / "quality_gates"


def quality_gate_to_dict(result: QualityGateResult) -> dict[str, Any]:
    return to_jsonable(result)


def build_quality_gate_risk_summary(*, workspace_root: str | Path | None = None, profile: str | None = None) -> dict[str, Any]:
    if workspace_root is None:
        return {
            "schema_version": 1,
            "profile": profile,
            "risk_level": "ok",
            "warning_count": 0,
            "remediation_items": [],
            "warnings": [],
        }
    from .gui import build_gui_action_history_risk_summary
    from .workspace import load_workspace_gui_action_history_risk_config, open_workspace, validate_workspace_risk_policy

    workspace = open_workspace(workspace_root)
    policy_check = validate_workspace_risk_policy(workspace)
    gui_summary = build_gui_action_history_risk_summary(
        workspace,
        config=load_workspace_gui_action_history_risk_config(workspace),
        profile=profile,
    )
    warnings = list(gui_summary.get("warnings") if isinstance(gui_summary.get("warnings"), list) else [])
    secret_scan = scan_workspace_secret_artifacts(workspace.root)
    if int(secret_scan.get("finding_count") or 0) > 0:
        warnings.append(
            {
                "level": "warning",
                "code": "workspace_report_secret_leak",
                "message": f"Workspace reports/runs/artifacts contain {secret_scan['finding_count']} possible secret leak(s).",
                "finding_count": secret_scan["finding_count"],
            }
        )
    if int(policy_check.get("error_count") or 0) > 0:
        warnings.append(
            {
                "level": "warning",
                "code": "workspace_risk_policy_invalid",
                "message": f"Workspace risk policy has {policy_check['error_count']} error(s).",
                "error_count": policy_check["error_count"],
            }
        )
    return {
        "schema_version": 1,
        "profile": profile,
        "workspace_root": str(Path(workspace_root).resolve()),
        "risk_level": "warning" if warnings else "ok",
        "warning_count": len(warnings),
        "remediation_items": gui_summary.get("remediation_items") if isinstance(gui_summary.get("remediation_items"), list) else [],
        "warnings": warnings,
        "gui_action_history": gui_summary,
        "risk_policy_check": policy_check,
        "secret_scan": secret_scan,
    }


def apply_quality_gate_strict_policy(
    risk_summary: dict[str, Any],
    *,
    fail_on_risk_policy_error: bool = False,
    fail_on_secret_leak: bool = False,
) -> dict[str, Any]:
    summary = dict(risk_summary)
    policy_check = summary.get("risk_policy_check") if isinstance(summary.get("risk_policy_check"), dict) else {}
    error_count = int(policy_check.get("error_count") or 0)
    secret_scan = summary.get("secret_scan") if isinstance(summary.get("secret_scan"), dict) else {}
    secret_finding_count = int(secret_scan.get("finding_count") or 0)
    summary["strict_policy_gate"] = {
        "enabled": bool(fail_on_risk_policy_error or fail_on_secret_leak),
        "failed": bool((fail_on_risk_policy_error and error_count > 0) or (fail_on_secret_leak and secret_finding_count > 0)),
        "risk_policy_error_count": error_count,
        "secret_scan_finding_count": secret_finding_count,
        "fail_on_risk_policy_error": fail_on_risk_policy_error,
        "fail_on_secret_leak": fail_on_secret_leak,
    }
    return summary


TEXT_ARTIFACT_SUFFIXES = {".json", ".md", ".txt", ".yaml", ".yml", ".html", ".csv", ".log"}
SECRET_SCAN_DIRS = ("reports", "runs", "artifacts")


def scan_workspace_secret_artifacts(workspace_root: str | Path, *, max_file_bytes: int = 1_000_000) -> dict[str, Any]:
    root = Path(workspace_root)
    findings: list[dict[str, Any]] = []
    scanned_files = 0
    skipped_files = 0
    for dirname in SECRET_SCAN_DIRS:
        base = root / dirname
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in TEXT_ARTIFACT_SUFFIXES:
                continue
            if "quality_gates" in path.parts:
                continue
            try:
                if path.stat().st_size > max_file_bytes:
                    skipped_files += 1
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                skipped_files += 1
                continue
            scanned_files += 1
            for line_number, line in enumerate(text.splitlines(), start=1):
                if contains_secret_text(line):
                    findings.append(
                        {
                            "path": path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path),
                            "line": line_number,
                            "preview": redact_secret_text(line.strip())[:240],
                        }
                    )
    return {
        "schema_version": 1,
        "workspace_root": str(root.resolve()),
        "status": "failed" if findings else "ok",
        "scanned_files": scanned_files,
        "skipped_files": skipped_files,
        "finding_count": len(findings),
        "findings": findings[:50],
        "truncated": len(findings) > 50,
        "redacted": True,
    }


def quality_gate_status(steps: list[QualityGateStep] | tuple[QualityGateStep, ...], *, risk_summary: dict[str, Any]) -> str:
    if any(step.status == "failed" and step.required for step in steps):
        return "failed"
    strict_gate = risk_summary.get("strict_policy_gate") if isinstance(risk_summary.get("strict_policy_gate"), dict) else {}
    if strict_gate.get("failed"):
        return "failed"
    return "success"


def list_quality_gate_reports(
    *,
    report_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
    profile: str | None = None,
    status: str | None = None,
    strict_policy_failed: bool | None = None,
) -> tuple[dict[str, Any], ...]:
    root = quality_report_root(report_root=report_root, workspace_root=workspace_root)
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
        entry = quality_gate_report_entry(root, path, payload)
        if profile is not None and entry["profile"] != profile:
            continue
        if status is not None and entry["status"] != status:
            continue
        if strict_policy_failed is not None and bool(entry.get("strict_policy_gate_failed")) is not strict_policy_failed:
            continue
        entries.append(entry)
    return tuple(entries)


def build_quality_gate_index(
    *,
    report_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
    profile: str | None = None,
    status: str | None = None,
    strict_policy_failed: bool | None = None,
) -> dict[str, Any]:
    root = quality_report_root(report_root=report_root, workspace_root=workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    entries = list(
        list_quality_gate_reports(
            report_root=root,
            profile=profile,
            status=status,
            strict_policy_failed=strict_policy_failed,
        )
    )
    return {
        "schema_version": 1,
        "generated_at": time(),
        "report_root": str(root),
        "filters": {
            "profile": profile,
            "status": status,
            "strict_policy_failed": strict_policy_failed,
        },
        "total_reports": len(entries),
        "successful_reports": sum(1 for entry in entries if entry["status"] == "success"),
        "failed_reports": sum(1 for entry in entries if entry["status"] == "failed"),
        "planned_reports": sum(1 for entry in entries if entry["status"] == "planned"),
        "risk_warnings": sum(int(entry.get("risk_warning_count") or 0) for entry in entries),
        "risk_trends": count_quality_gate_risk_trends(entries),
        "risk_policy_errors": sum(int(entry.get("risk_policy_error_count") or 0) for entry in entries),
        "risk_policy_warnings": sum(int(entry.get("risk_policy_warning_count") or 0) for entry in entries),
        "secret_scan_findings": sum(int(entry.get("secret_scan_finding_count") or 0) for entry in entries),
        "strict_policy_gate_enabled": sum(1 for entry in entries if entry.get("strict_policy_gate_enabled")),
        "strict_policy_gate_failed": sum(1 for entry in entries if entry.get("strict_policy_gate_failed")),
        "strict_policy_gate_policy_errors": sum(int(entry.get("strict_policy_gate_policy_errors") or 0) for entry in entries),
        "strict_policy_gate_secret_findings": sum(int(entry.get("strict_policy_gate_secret_findings") or 0) for entry in entries),
        "latest": entries[0] if entries else None,
        "entries": entries,
    }


def write_quality_gate_index(
    *,
    report_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
) -> Path:
    root = quality_report_root(report_root=report_root, workspace_root=workspace_root)
    index = build_quality_gate_index(report_root=root)
    path = root / "index.json"
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_quality_gate_index(
    *,
    report_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
    rebuild: bool = False,
    profile: str | None = None,
    status: str | None = None,
    strict_policy_failed: bool | None = None,
) -> dict[str, Any]:
    root = quality_report_root(report_root=report_root, workspace_root=workspace_root)
    if rebuild or profile is not None or status is not None or strict_policy_failed is not None:
        index = build_quality_gate_index(
            report_root=root,
            profile=profile,
            status=status,
            strict_policy_failed=strict_policy_failed,
        )
        if profile is None and status is None and strict_policy_failed is None:
            write_quality_gate_index(report_root=root)
        return index
    path = root / "index.json"
    if not path.exists():
        write_quality_gate_index(report_root=root)
    return json.loads(path.read_text(encoding="utf-8"))


def quality_gate_report_entry(root: Path, report_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    failed_steps = [
        str(step.get("name") or "")
        for step in steps
        if isinstance(step, dict) and step.get("status") == "failed"
    ]
    risk = payload.get("risk_summary") if isinstance(payload.get("risk_summary"), dict) else {}
    risk_trend = quality_gate_risk_trend_entry(risk)
    policy_check = risk.get("risk_policy_check") if isinstance(risk.get("risk_policy_check"), dict) else {}
    secret_scan = risk.get("secret_scan") if isinstance(risk.get("secret_scan"), dict) else {}
    strict_gate = risk.get("strict_policy_gate") if isinstance(risk.get("strict_policy_gate"), dict) else {}
    markdown_path = report_path.with_suffix(".md")
    return {
        "run_id": str(payload.get("run_id") or report_path.stem),
        "profile": str(payload.get("profile") or ""),
        "status": str(payload.get("status") or "unknown"),
        "elapsed_seconds": float(payload.get("elapsed_seconds") or 0.0),
        "step_count": len(steps),
        "failed_steps": failed_steps,
        "risk_level": str(risk.get("risk_level") or "ok"),
        "risk_warning_count": int(risk.get("warning_count") or 0),
        "remediation_count": len(risk.get("remediation_items") if isinstance(risk.get("remediation_items"), list) else []),
        "risk_trend": risk_trend,
        "risk_trend_direction": risk_trend.get("direction"),
        "risk_policy_status": str(policy_check.get("status") or "unknown"),
        "risk_policy_error_count": int(policy_check.get("error_count") or 0),
        "risk_policy_warning_count": int(policy_check.get("warning_count") or 0),
        "secret_scan_status": str(secret_scan.get("status") or "unknown"),
        "secret_scan_finding_count": int(secret_scan.get("finding_count") or 0),
        "strict_policy_gate_enabled": bool(strict_gate.get("enabled", False)),
        "strict_policy_gate_failed": bool(strict_gate.get("failed", False)),
        "strict_policy_gate_policy_errors": int(strict_gate.get("risk_policy_error_count") or 0),
        "strict_policy_gate_secret_findings": int(strict_gate.get("secret_scan_finding_count") or 0),
        "json_report": report_path.relative_to(root).as_posix() if report_path.is_relative_to(root) else str(report_path),
        "markdown_report": markdown_path.relative_to(root).as_posix()
        if markdown_path.exists() and markdown_path.is_relative_to(root)
        else (str(markdown_path) if markdown_path.exists() else None),
        "modified_at": report_path.stat().st_mtime,
    }


def quality_gate_risk_trend_entry(risk_summary: dict[str, Any]) -> dict[str, Any]:
    gui = risk_summary.get("gui_action_history") if isinstance(risk_summary.get("gui_action_history"), dict) else {}
    trend = gui.get("trend") if isinstance(gui.get("trend"), dict) else {}
    if not trend:
        return {
            "direction": "unknown",
            "error_rate_delta": 0.0,
            "remediation_count_delta": 0,
            "window_size": 0,
        }
    return {
        "direction": str(trend.get("direction") or "unknown"),
        "error_rate_delta": float(trend.get("error_rate_delta") or 0.0),
        "remediation_count_delta": int(trend.get("remediation_count_delta") or 0),
        "window_size": int(trend.get("window_size") or 0),
    }


def count_quality_gate_risk_trends(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "improving": 0,
        "worsening": 0,
        "stable": 0,
        "mixed": 0,
        "insufficient_history": 0,
        "unknown": 0,
    }
    for entry in entries:
        direction = str(entry.get("risk_trend_direction") or "unknown")
        counts[direction if direction in counts else "unknown"] += 1
    return counts


def quality_gate_to_markdown(result: QualityGateResult) -> str:
    lines = [
        f"# Quality Gate: {result.profile}",
        "",
        f"- Run ID: `{result.run_id}`",
        f"- Status: `{result.status}`",
        f"- Elapsed seconds: {result.elapsed_seconds}",
        f"- Risk level: `{result.risk_summary.get('risk_level', 'ok')}`",
        f"- Risk warnings: {result.risk_summary.get('warning_count', 0)}",
        "",
        "## Steps",
        "",
    ]
    for step in result.steps:
        lines.append(f"### {step.name}")
        lines.append("")
        lines.append(f"- Status: `{step.status}`")
        lines.append(f"- Required: {step.required}")
        lines.append(f"- Exit code: {step.exit_code}")
        if step.elapsed_seconds is not None:
            lines.append(f"- Elapsed seconds: {step.elapsed_seconds}")
        lines.append("- Command:")
        lines.append("")
        lines.append("```text")
        lines.append(" ".join(step.command))
        lines.append("```")
        lines.append("")
    lines.extend(["## Risk Summary", ""])
    strict_gate = result.risk_summary.get("strict_policy_gate") if isinstance(result.risk_summary.get("strict_policy_gate"), dict) else {}
    if strict_gate:
        lines.extend(
            [
                "### Strict Policy Gate",
                "",
                f"- Enabled: {strict_gate.get('enabled', False)}",
                f"- Failed: {strict_gate.get('failed', False)}",
                f"- Risk policy errors: {strict_gate.get('risk_policy_error_count', 0)}",
                f"- Secret scan findings: {strict_gate.get('secret_scan_finding_count', 0)}",
                "",
            ]
        )
    remediation_items = result.risk_summary.get("remediation_items") if isinstance(result.risk_summary.get("remediation_items"), list) else []
    lines.extend(["### Remediation Checklist", ""])
    if remediation_items:
        for item in remediation_items:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- [{item.get('count', 0)}x] `{item.get('action') or 'unknown'}` "
                f"`{item.get('error_type') or 'Error'}`: {item.get('recovery_hint') or ''}"
            )
    else:
        lines.append("- No recovery actions needed.")
    lines.extend(["", "### Warnings", ""])
    warnings = result.risk_summary.get("warnings") if isinstance(result.risk_summary.get("warnings"), list) else []
    if warnings:
        for warning in warnings:
            if not isinstance(warning, dict):
                continue
            lines.append(f"- `{warning.get('code') or 'risk'}`: {warning.get('message') or ''}")
    else:
        lines.append("- No quality risk warnings.")
    lines.extend(["", "### Risk Policy Check", ""])
    policy_check = result.risk_summary.get("risk_policy_check") if isinstance(result.risk_summary.get("risk_policy_check"), dict) else {}
    if policy_check:
        lines.append(f"- Status: `{policy_check.get('status') or 'unknown'}`")
        lines.append(f"- Errors: {policy_check.get('error_count', 0)}")
        lines.append(f"- Warnings: {policy_check.get('warning_count', 0)}")
        issues = policy_check.get("issues") if isinstance(policy_check.get("issues"), list) else []
        if issues:
            lines.extend(["", "| level | code | path | suggestion |", "| --- | --- | --- | --- |"])
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                lines.append(
                    "| "
                    + " | ".join(
                        markdown_cell(issue.get(key))
                        for key in ("level", "code", "path", "suggestion")
                    )
                    + " |"
                )
        else:
            lines.append("- No policy issues.")
    else:
        lines.append("- No workspace policy check available.")
    lines.extend(["", "### Secret Scan", ""])
    secret_scan = result.risk_summary.get("secret_scan") if isinstance(result.risk_summary.get("secret_scan"), dict) else {}
    if secret_scan:
        lines.append(f"- Status: `{secret_scan.get('status') or 'unknown'}`")
        lines.append(f"- Scanned files: {secret_scan.get('scanned_files', 0)}")
        lines.append(f"- Findings: {secret_scan.get('finding_count', 0)}")
        findings = secret_scan.get("findings") if isinstance(secret_scan.get("findings"), list) else []
        if findings:
            lines.extend(["", "| path | line | preview |", "| --- | ---: | --- |"])
            for finding in findings[:10]:
                if not isinstance(finding, dict):
                    continue
                lines.append(
                    "| "
                    + " | ".join(markdown_cell(finding.get(key)) for key in ("path", "line", "preview"))
                    + " |"
                )
        else:
            lines.append("- No possible secret leaks found.")
    else:
        lines.append("- No workspace secret scan available.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def quality_gate_index_to_markdown(index: dict[str, Any]) -> str:
    filters = index.get("filters") if isinstance(index.get("filters"), dict) else {}
    lines = [
        "# Quality Gate Index",
        "",
        f"- Report root: `{index.get('report_root')}`",
        f"- Total reports: {index.get('total_reports', 0)}",
        f"- Successful: {index.get('successful_reports', 0)}",
        f"- Failed: {index.get('failed_reports', 0)}",
        f"- Planned: {index.get('planned_reports', 0)}",
        f"- Risk warnings: {index.get('risk_warnings', 0)}",
        f"- Risk policy errors: {index.get('risk_policy_errors', 0)}",
        f"- Secret scan findings: {index.get('secret_scan_findings', 0)}",
        f"- Strict policy failures: {index.get('strict_policy_gate_failed', 0)}",
        f"- Strict policy policy errors: {index.get('strict_policy_gate_policy_errors', 0)}",
        f"- Strict policy secret findings: {index.get('strict_policy_gate_secret_findings', 0)}",
        "",
        "## Filters",
        "",
    ]
    for key in ("profile", "status", "strict_policy_failed"):
        lines.append(f"- {key}: `{filters.get(key)}`")
    lines.extend(["", "## Reports", ""])
    entries = index.get("entries") if isinstance(index.get("entries"), list) else []
    lines.extend(quality_gate_entries_table(entries))
    return "\n".join(lines).rstrip() + "\n"


def quality_gate_reports_to_markdown(reports: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    lines = [
        "# Quality Gate Reports",
        "",
        f"- Total reports: {len(reports)}",
        f"- Failed reports: {sum(1 for report in reports if report.get('status') == 'failed')}",
        f"- Strict policy failures: {sum(1 for report in reports if report.get('strict_policy_gate_failed'))}",
        "",
        "## Reports",
        "",
    ]
    lines.extend(quality_gate_entries_table(list(reports)))
    return "\n".join(lines).rstrip() + "\n"


def quality_gate_entries_table(entries: list[dict[str, Any]]) -> list[str]:
    if not entries:
        return ["No quality gate reports.", ""]
    lines = [
        "| run_id | profile | status | strict_failed | policy_errors | json_report | markdown_report |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        lines.append(
            "| "
            + " | ".join(
                markdown_cell(value)
                for value in (
                    entry.get("run_id"),
                    entry.get("profile"),
                    entry.get("status"),
                    entry.get("strict_policy_gate_failed"),
                    entry.get("strict_policy_gate_policy_errors"),
                    entry.get("json_report"),
                    entry.get("markdown_report"),
                )
            )
            + " |"
        )
    lines.append("")
    return lines


def markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def build_release_check_plan(*, workspace_root: str | Path = ".agent-workspace") -> dict[str, Any]:
    python = ".\\.venv\\Scripts\\python.exe"
    workspace = str(workspace_root)
    checks = [
        {"id": "install_check", "command": f"{python} -m visual_agent.cli install-check --format markdown", "required": True},
        {"id": "doctor", "command": f"{python} -m visual_agent.cli doctor", "required": True},
        {"id": "capabilities", "command": f"{python} -m visual_agent.cli atomic-capabilities", "required": True},
        {"id": "init_workspace", "command": f"{python} -m visual_agent.cli init-workspace --root {workspace} --overwrite", "required": True},
        {"id": "demo_workspace_check", "command": f"{python} -m visual_agent.cli demo-workspace-check --root {workspace} --overwrite", "required": True},
        {"id": "demo_run", "command": f"{python} -m visual_agent.cli workspace-run --root {workspace} --workflow local_html_form_workflow --inputs-file demo_login.json", "required": True},
        {"id": "report_index", "command": f"{python} -m visual_agent.cli workspace-report-index --root {workspace} --rebuild", "required": True},
        {"id": "mcp_client_config", "command": f"{python} -m visual_agent.cli mcp-client-config --workspace-root {workspace} --client cursor --format json", "required": True},
        {"id": "mcp_smoke_cli", "command": f"{python} -m visual_agent.cli mcp-smoke --workspace-root {workspace} --workflow local_html_form_workflow --inputs-file demo_login.json --format markdown", "required": True},
        {"id": "tests", "command": f"{python} -m pytest", "required": True},
        {"id": "quality_gate", "command": f"{python} -m visual_agent.cli quality-gate --profile ci --workspace-root {workspace} --run --fail-on-secret-leak", "required": True},
        {"id": "mcp_smoke", "command": f"{python} -m pytest tests\\test_mcp_server.py", "required": True},
    ]
    return {
        "schema_version": 1,
        "workspace_root": workspace,
        "status": "planned",
        "check_count": len(checks),
        "checks": checks,
        "docs": [
            "docs/quickstart.md",
            "docs/release_checklist.md",
            "docs/coding_agents.md",
            "docs/vlm_setup.md",
            "docs/codex.md",
            "docs/vscode.md",
            "docs/mcp_vscode.md",
            "README_MCP.md",
            "examples/workflows/README.md",
        ],
    }


def release_check_plan_to_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# Release Check Plan",
        "",
        f"- Workspace root: `{plan.get('workspace_root')}`",
        f"- Checks: {plan.get('check_count', 0)}",
        "",
        "| id | required | command |",
        "| --- | --- | --- |",
    ]
    for check in plan.get("checks", []) if isinstance(plan.get("checks"), list) else []:
        if not isinstance(check, dict):
            continue
        lines.append(
            "| "
            + " | ".join(markdown_cell(value) for value in (check.get("id"), check.get("required"), check.get("command")))
            + " |"
        )
    lines.extend(["", "## Docs", ""])
    for path in plan.get("docs", []) if isinstance(plan.get("docs"), list) else []:
        lines.append(f"- `{path}`")
    lines.append("")
    return "\n".join(lines)


def build_install_check_plan() -> dict[str, Any]:
    python = ".\\.venv\\Scripts\\python.exe"
    checks = [
        {"id": "python", "command": f"{python} --version", "required": True, "purpose": "Confirm the project virtualenv is callable."},
        {"id": "editable_install_web", "command": f"{python} -m pip install -e .[web]", "required": True, "purpose": "Install browser automation dependencies."},
        {"id": "editable_install_mcp", "command": f"{python} -m pip install -e .[mcp]", "required": True, "purpose": "Install MCP server dependencies."},
        {"id": "playwright_browser", "command": f"{python} -m playwright install chromium", "required": False, "purpose": "Install browser binaries for live browser runs."},
        {"id": "doctor", "command": f"{python} -m visual_agent.cli doctor", "required": True, "purpose": "Check local capability availability without leaking secrets."},
        {"id": "atomic_capabilities", "command": f"{python} -m visual_agent.cli atomic-capabilities", "required": True, "purpose": "Confirm planner-visible capabilities are importable."},
    ]
    return {
        "schema_version": 1,
        "status": "planned",
        "check_count": len(checks),
        "checks": checks,
        "notes": [
            "Run from the repository root.",
            "The Playwright browser install is optional for dry-run-only smoke, but required for live browser automation.",
        ],
    }


def install_check_plan_to_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# Install Check Plan",
        "",
        f"- Checks: {plan.get('check_count', 0)}",
        "",
        "| id | required | command | purpose |",
        "| --- | --- | --- | --- |",
    ]
    for check in plan.get("checks", []) if isinstance(plan.get("checks"), list) else []:
        if isinstance(check, dict):
            lines.append(
                "| "
                + " | ".join(
                    markdown_cell(value)
                    for value in (check.get("id"), check.get("required"), check.get("command"), check.get("purpose"))
                )
                + " |"
            )
    notes = plan.get("notes") if isinstance(plan.get("notes"), list) else []
    if notes:
        lines.extend(["", "## Notes", ""])
        for note in notes:
            lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def build_mcp_client_config(
    *,
    workspace_root: str | Path = ".agent-workspace",
    client: str = "cursor",
    python: str = ".\\.venv\\Scripts\\python.exe",
    repo_root: str | Path = ".",
) -> dict[str, Any]:
    workspace = str(workspace_root)
    cwd = str(Path(repo_root).resolve())
    server = {
        "command": python,
        "args": ["-m", "visual_agent.mcp_server", "--workspace-root", workspace],
        "cwd": cwd,
        "env": {"PYTHONPATH": str((Path(repo_root).resolve() / "src"))},
    }
    client_key = client.lower().replace("_", "-")
    if client_key in {"claude", "claude-desktop", "claude_desktop"}:
        config = {"mcpServers": {"visual-agent": server}}
        target = "claude_desktop_config.json"
    elif client_key == "cursor":
        config = {"mcpServers": {"visual-agent": server}}
        target = "cursor_mcp.json"
    elif client_key in {"vscode", "vs-code", "visual-studio-code"}:
        config = {"servers": {"visual-agent": server}}
        target = ".vscode/mcp.json"
    else:
        raise ValueError(f"Unsupported MCP client: {client}")
    return {
        "schema_version": 1,
        "client": client_key,
        "workspace_root": workspace,
        "target_filename": target,
        "config": config,
        "security_notes": [
            "Keep workspace_root local.",
            "approved run_profile remains blocked unless workspace.json mcp policy explicitly allows it.",
        ],
    }


def mcp_client_config_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# MCP Client Config",
        "",
        f"- Client: `{payload.get('client')}`",
        f"- Workspace root: `{payload.get('workspace_root')}`",
        f"- Target filename: `{payload.get('target_filename')}`",
        "",
        "```json",
        json.dumps(payload.get("config", {}), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Security Notes",
        "",
    ]
    for note in payload.get("security_notes", []) if isinstance(payload.get("security_notes"), list) else []:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def build_coding_agent_brief(
    *,
    workspace_root: str | Path = ".agent-workspace",
    repo_root: str | Path = ".",
    client: str = "codex",
    python: str = ".\\.venv\\Scripts\\python.exe",
) -> dict[str, Any]:
    workspace = str(workspace_root)
    repo = str(Path(repo_root).resolve())
    client_key = str(client or "codex").strip().lower().replace("_", "-")
    supported_clients = {"codex", "claude-code", "cursor", "vscode"}
    if client_key not in supported_clients:
        raise ValueError(f"Unsupported coding agent client: {client}")
    config_client = "claude-desktop" if client_key == "claude-code" else ("cursor" if client_key == "codex" else client_key)
    mcp_config = build_mcp_client_config(
        workspace_root=workspace,
        client=config_client,
        python=python,
        repo_root=repo,
    )
    tools = [
        {
            "name": "list_workflows",
            "purpose": "Discover reusable local automation workflows before proposing new code or scripts.",
            "safe_default": True,
        },
        {
            "name": "validate_workflow",
            "purpose": "Check YAML workflow structure and preflight capability requirements.",
            "safe_default": True,
        },
        {
            "name": "run_workflow",
            "purpose": "Run an existing workflow under dry-run by default, then inspect the audited result.",
            "safe_default": True,
        },
        {
            "name": "get_run_report",
            "purpose": "Read the redacted Markdown or JSON run report after execution.",
            "safe_default": True,
        },
        {
            "name": "list_run_artifacts",
            "purpose": "List screenshots, step JSON, downloads, and other workspace-owned artifacts.",
            "safe_default": True,
        },
        {
            "name": "get_workspace_dashboard",
            "purpose": "Read workspace health, queue, recent reports, and quality-gate status before claiming success.",
            "safe_default": True,
        },
        {
            "name": "get_latest_failure",
            "purpose": "Fetch the newest failed report and diagnosis without asking the human to find a run id.",
            "safe_default": True,
        },
    ]
    commands = [
        {"id": "bootstrap", "command": "powershell -ExecutionPolicy Bypass -File scripts\\bootstrap.ps1"},
        {"id": "doctor", "command": f"{python} -m visual_agent.cli doctor"},
        {
            "id": "mcp_smoke",
            "command": f"{python} -m visual_agent.cli mcp-smoke --workspace-root {workspace} --format markdown",
        },
        {
            "id": "demo_workspace",
            "command": f"{python} -m visual_agent.cli demo-workspace-check --root {workspace} --format markdown",
        },
        {
            "id": "dashboard",
            "command": f"{python} -m visual_agent.cli workspace-dashboard --root {workspace} --format markdown",
        },
        {
            "id": "quality_gate",
            "command": f"{python} -m visual_agent.cli quality-gate --profile ci --workspace-root {workspace} --run --fail-on-secret-leak",
        },
    ]
    prompts = [
        "Use visual-agent to list workflows, run local_html_form_workflow as a dry-run, then summarize the report.",
        "Use visual-agent to validate every workflow before suggesting changes.",
        "If a workflow fails, use get_run_report and list_run_artifacts before editing code.",
        "Before and after risky changes, call get_workspace_dashboard and summarize any attention items.",
        "When a run fails, call get_latest_failure first, then inspect artifacts if needed.",
        "Never request approved run_profile unless the workspace policy explicitly allows it and the human asked for it.",
    ]
    rules = [
        "Start with dry-run. Escalate to supervised or approved only after explicit human approval.",
        "Treat missing auth_state as a blocker, not as a reason to bypass login or scrape protected data.",
        "Read run reports before claiming success; the report is the source of truth.",
        "Do not print secrets from inputs, storage_state, cookies, tokens, or model credentials.",
        "Prefer existing workflows over one-off browser clicking when a workflow exists.",
    ]
    return {
        "schema_version": 1,
        "client": client_key,
        "repo_root": repo,
        "workspace_root": workspace,
        "positioning": "Visual Agent is the local execution layer for coding agents: persistent workflows, permission profiles, and audited reports.",
        "mcp": {
            "server_name": "visual-agent",
            "config_client_shape": config_client,
            "config": mcp_config["config"],
        },
        "tools": tools,
        "commands": commands,
        "prompts": prompts,
        "rules": rules,
        "docs": [
            "README.md",
            "README_MCP.md",
            "docs/mcp_claude_code.md",
            "docs/mcp_cursor.md",
            "docs/mcp_vscode.md",
            "docs/codex.md",
            "docs/vscode.md",
            "docs/quickstart.md",
        ],
    }


def coding_agent_brief_to_markdown(brief: dict[str, Any]) -> str:
    lines = [
        "# Coding Agent Brief",
        "",
        f"- Client: `{brief.get('client')}`",
        f"- Repo root: `{brief.get('repo_root')}`",
        f"- Workspace root: `{brief.get('workspace_root')}`",
        f"- Positioning: {brief.get('positioning')}",
        "",
        "## MCP Configuration",
        "",
        "```json",
        json.dumps(brief.get("mcp", {}).get("config", {}), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Tools",
        "",
        "| tool | purpose | safe default |",
        "| --- | --- | --- |",
    ]
    for tool in brief.get("tools", []) if isinstance(brief.get("tools"), list) else []:
        if isinstance(tool, dict):
            lines.append(
                "| "
                + " | ".join(
                    markdown_cell(value)
                    for value in (tool.get("name"), tool.get("purpose"), tool.get("safe_default"))
                )
                + " |"
            )
    lines.extend(["", "## First Commands", "", "| id | command |", "| --- | --- |"])
    for command in brief.get("commands", []) if isinstance(brief.get("commands"), list) else []:
        if isinstance(command, dict):
            lines.append("| " + " | ".join(markdown_cell(value) for value in (command.get("id"), command.get("command"))) + " |")
    lines.extend(["", "## Prompts To Try", ""])
    for prompt in brief.get("prompts", []) if isinstance(brief.get("prompts"), list) else []:
        lines.append(f"- {prompt}")
    lines.extend(["", "## Rules For Coding Agents", ""])
    for rule in brief.get("rules", []) if isinstance(brief.get("rules"), list) else []:
        lines.append(f"- {rule}")
    lines.extend(["", "## Docs", ""])
    for path in brief.get("docs", []) if isinstance(brief.get("docs"), list) else []:
        lines.append(f"- `{path}`")
    lines.append("")
    return "\n".join(lines)


def run_mcp_smoke_check(
    *,
    workspace_root: str | Path,
    workflow: str = "local_html_form_workflow",
    inputs_file: str | None = "demo_login.json",
) -> dict[str, Any]:
    from .mcp_server import call_tool

    root = str(workspace_root)
    checks = []

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = asyncio.run(call_tool(name, args))
        return json.loads(result[0].text)

    list_payload = call("list_workflows", {"workspace_root": root})
    checks.append({"id": "list_workflows", "status": "success" if list_payload.get("workflow_count", 0) >= 1 else "failed", "payload": list_payload})

    validate_payload = call("validate_workflow", {"workspace_root": root, "workflow_name": workflow})
    checks.append({"id": "validate_workflow", "status": "success" if validate_payload.get("valid") else "failed", "payload": validate_payload})

    run_args = {"workspace_root": root, "workflow_name": workflow, "run_profile": "dry-run"}
    if inputs_file:
        run_args["inputs_file"] = inputs_file
    run_payload = call("run_workflow", run_args)
    run_ok = run_payload.get("status") == "success" and bool(run_payload.get("run_id"))
    checks.append({"id": "run_workflow", "status": "success" if run_ok else "failed", "payload": run_payload})

    report_payload: dict[str, Any] = {}
    artifacts_payload: dict[str, Any] = {}
    run_id = run_payload.get("run_id")
    if run_id:
        report_payload = call("get_run_report", {"workspace_root": root, "run_id": str(run_id), "format": "markdown"})
        artifacts_payload = call("list_run_artifacts", {"workspace_root": root, "run_id": str(run_id)})
    checks.append({"id": "get_run_report", "status": "success" if report_payload.get("format") == "markdown" else "failed", "payload": report_payload})
    checks.append({"id": "list_run_artifacts", "status": "success" if artifacts_payload.get("artifact_count", 0) >= 1 else "failed", "payload": artifacts_payload})

    failed = [check for check in checks if check.get("status") != "success"]
    return {
        "schema_version": 1,
        "workspace_root": root,
        "workflow": workflow,
        "inputs_file": inputs_file,
        "status": "success" if not failed else "failed",
        "check_count": len(checks),
        "failed_count": len(failed),
        "run_id": run_id,
        "checks": checks,
    }


def mcp_smoke_check_to_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# MCP Smoke Check",
        "",
        f"- Workspace root: `{result.get('workspace_root')}`",
        f"- Workflow: `{result.get('workflow')}`",
        f"- Status: `{result.get('status')}`",
        f"- Run id: `{result.get('run_id') or ''}`",
        "",
        "| id | status |",
        "| --- | --- |",
    ]
    for check in result.get("checks", []) if isinstance(result.get("checks"), list) else []:
        if isinstance(check, dict):
            lines.append(f"| {markdown_cell(check.get('id'))} | {markdown_cell(check.get('status'))} |")
    lines.append("")
    return "\n".join(lines)


def run_demo_workspace_check(*, root: str | Path = ".agent-workspace", overwrite: bool = False) -> dict[str, Any]:
    from .workspace import init_workspace, run_workspace_workflow, validate_workspace, write_workspace_report_index

    workspace = init_workspace(root, overwrite=overwrite)
    validation = validate_workspace(workspace)
    validation_ok = all(item.valid for item in validation)
    result = run_workspace_workflow(
        workspace,
        "local_html_form_workflow",
        inputs={"username": "demo_user", "password": "demo_password"},
        dry_run=True,
        run_profile="dry-run",
        export_report=True,
    )
    index_path = write_workspace_report_index(workspace)
    failed_steps = [
        {"id": step.id, "action": step.action, "message": step.message}
        for step in result.steps
        if getattr(step.status, "value", str(step.status)) == "failed"
    ]
    return {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "status": "success" if validation_ok and not failed_steps else "failed",
        "validation_ok": validation_ok,
        "workflow": "local_html_form_workflow",
        "run_id": result.run_id,
        "failed_steps": failed_steps,
        "report_index": str(index_path),
    }


def demo_workspace_check_to_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Demo Workspace Check",
        "",
        f"- Workspace root: `{result.get('workspace_root')}`",
        f"- Status: `{result.get('status')}`",
        f"- Validation OK: `{result.get('validation_ok')}`",
        f"- Workflow: `{result.get('workflow')}`",
        f"- Run id: `{result.get('run_id')}`",
        f"- Report index: `{result.get('report_index')}`",
    ]
    failed_steps = result.get("failed_steps") if isinstance(result.get("failed_steps"), list) else []
    if failed_steps:
        lines.extend(["", "## Failed Steps", ""])
        for step in failed_steps:
            if isinstance(step, dict):
                lines.append(f"- `{step.get('id')}` {step.get('message') or ''}")
    lines.append("")
    return "\n".join(lines)
