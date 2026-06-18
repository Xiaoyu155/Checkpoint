from __future__ import annotations

import json
import asyncio
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
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
        QualityGateStep(name="core_tests", command=(python, "-m", "pytest", "tests", "--ignore=tests/e2e")),
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


def quality_gate_to_junit_xml(result: QualityGateResult) -> str:
    tests = list(result.steps)
    strict_gate = result.risk_summary.get("strict_policy_gate") if isinstance(result.risk_summary.get("strict_policy_gate"), dict) else {}
    strict_failed = bool(strict_gate.get("failed", False))
    failed_steps = [step for step in tests if step.status == "failed"]
    skipped_steps = [step for step in tests if step.status == "planned"]
    synthetic_strict_case = strict_failed and not failed_steps
    total_tests = len(tests) + (1 if synthetic_strict_case else 0)
    failure_count = len(failed_steps) + (1 if synthetic_strict_case else 0)
    skipped_count = len(skipped_steps)
    suite = ET.Element(
        "testsuite",
        attrib={
            "name": f"checkpoint-quality-gate:{result.profile}",
            "tests": str(total_tests),
            "failures": str(failure_count),
            "errors": "0",
            "skipped": str(skipped_count),
            "time": f"{result.elapsed_seconds:.6f}",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    properties = ET.SubElement(suite, "properties")
    for key, value in (
        ("run_id", result.run_id),
        ("profile", result.profile),
        ("status", result.status),
        ("risk_level", result.risk_summary.get("risk_level", "ok")),
        ("warning_count", result.risk_summary.get("warning_count", 0)),
    ):
        ET.SubElement(properties, "property", attrib={"name": str(key), "value": str(value)})
    for step in tests:
        testcase = ET.SubElement(
            suite,
            "testcase",
            attrib={
                "classname": "visual_agent.quality_gate",
                "name": step.name,
                "time": f"{float(step.elapsed_seconds or 0.0):.6f}",
            },
        )
        if step.status == "failed":
            failure = ET.SubElement(
                testcase,
                "failure",
                attrib={"message": f"Step {step.name} failed", "type": "QualityGateStepFailure"},
            )
            failure.text = junit_failure_text(step)
        elif step.status == "planned":
            ET.SubElement(testcase, "skipped", attrib={"message": "Quality gate was planned only."})
    if synthetic_strict_case:
        testcase = ET.SubElement(
            suite,
            "testcase",
            attrib={
                "classname": "visual_agent.quality_gate",
                "name": "strict_policy_gate",
                "time": "0.000000",
            },
        )
        failure = ET.SubElement(
            testcase,
            "failure",
            attrib={"message": "Strict policy gate failed", "type": "StrictPolicyGateFailure"},
        )
        failure.text = junit_strict_policy_text(result.risk_summary)
    return ET.tostring(suite, encoding="utf-8", xml_declaration=True).decode("utf-8")


def quality_gate_to_step_summary(result: QualityGateResult, *, junit_output: str | None = None) -> str:
    lines = [
        "## Checkpoint Quality Gate",
        "",
        f"- Run ID: `{result.run_id}`",
        f"- Profile: `{result.profile}`",
        f"- Status: `{result.status}`",
        f"- Elapsed seconds: {result.elapsed_seconds}",
        f"- Risk level: `{result.risk_summary.get('risk_level', 'ok')}`",
        f"- Risk warnings: {result.risk_summary.get('warning_count', 0)}",
    ]
    strict_gate = result.risk_summary.get("strict_policy_gate") if isinstance(result.risk_summary.get("strict_policy_gate"), dict) else {}
    if strict_gate:
        lines.extend(
            [
                "",
                "### Strict Policy Gate",
                "",
                f"- Enabled: {strict_gate.get('enabled', False)}",
                f"- Failed: {strict_gate.get('failed', False)}",
                f"- Risk policy errors: {strict_gate.get('risk_policy_error_count', 0)}",
                f"- Secret scan findings: {strict_gate.get('secret_scan_finding_count', 0)}",
            ]
        )
    failed_steps = [step for step in result.steps if step.status == "failed"]
    if failed_steps:
        lines.extend(["", "### Failed Steps", ""])
        for step in failed_steps:
            lines.append(f"- `{step.name}` ({step.exit_code})")
    if junit_output:
        lines.extend(["", f"JUnit: `{junit_output}`"])
    lines.append("")
    return "\n".join(lines)


def write_quality_gate_step_summary(result: QualityGateResult, *, junit_output: str | None = None, summary_path: str | Path | None = None) -> Path | None:
    path_value = summary_path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not path_value:
        return None
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(quality_gate_to_step_summary(result, junit_output=junit_output))
    return path


def junit_failure_text(step: QualityGateStep) -> str:
    parts = [
        f"name={step.name}",
        f"status={step.status}",
    ]
    if step.exit_code is not None:
        parts.append(f"exit_code={step.exit_code}")
    if step.stdout:
        parts.append("stdout=" + redact_secret_text(step.stdout)[:2000])
    if step.stderr:
        parts.append("stderr=" + redact_secret_text(step.stderr)[:2000])
    return "\n".join(parts)


def junit_strict_policy_text(risk_summary: dict[str, Any]) -> str:
    strict_gate = risk_summary.get("strict_policy_gate") if isinstance(risk_summary.get("strict_policy_gate"), dict) else {}
    policy_check = risk_summary.get("risk_policy_check") if isinstance(risk_summary.get("risk_policy_check"), dict) else {}
    secret_scan = risk_summary.get("secret_scan") if isinstance(risk_summary.get("secret_scan"), dict) else {}
    return "\n".join(
        [
            f"enabled={strict_gate.get('enabled', False)}",
            f"failed={strict_gate.get('failed', False)}",
            f"risk_policy_error_count={policy_check.get('error_count', 0)}",
            f"risk_policy_warning_count={policy_check.get('warning_count', 0)}",
            f"secret_scan_finding_count={secret_scan.get('finding_count', 0)}",
        ]
    )


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
        if step.status == "failed":
            if step.stdout:
                lines.append("#### stdout")
                lines.append("")
                lines.append("```text")
                lines.append(redact_secret_text(step.stdout)[-4000:])
                lines.append("```")
                lines.append("")
            if step.stderr:
                lines.append("#### stderr")
                lines.append("")
                lines.append("```text")
                lines.append(redact_secret_text(step.stderr)[-4000:])
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
        {"id": "init_workspace", "command": f"{python} -m visual_agent.cli init --root {workspace} --overwrite", "required": True},
        {"id": "release_smoke", "command": f"{python} -m visual_agent.cli release-smoke --run --workspace-root {workspace} --format markdown", "required": True},
        {"id": "demo_workspace_check", "command": f"{python} -m visual_agent.cli demo-workspace-check --root {workspace} --overwrite", "required": True},
        {"id": "release_trial", "command": f"{python} -m visual_agent.cli release-trial --workspace-root {workspace} --format markdown", "required": True},
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


def build_release_smoke_plan(
    *,
    workspace_root: str | Path = ".agent-workspace",
    include_vscode: bool = True,
) -> dict[str, Any]:
    python = sys.executable
    workspace = str(workspace_root)
    checks: list[dict[str, Any]] = [
        {
            "id": "quickstart",
            "command": [python, "-m", "visual_agent.cli", "quickstart"],
            "cwd": ".",
            "required": True,
            "purpose": "Confirm the concise onboarding entrypoint renders.",
        },
        {
            "id": "checkout_l4_demo",
            "command": [
                python,
                "-m",
                "visual_agent.cli",
                "verify-now",
                "--workspace-root",
                workspace,
                "--workflow",
                "checkout_verification",
                "--live",
                "--format",
                "markdown",
            ],
            "cwd": ".",
            "required": True,
            "purpose": "Confirm the first demo reaches L3+ product acceptance with real interaction.",
        },
        {
            "id": "mcp_smoke",
            "command": [
                python,
                "-m",
                "visual_agent.cli",
                "mcp-smoke",
                "--workspace-root",
                workspace,
                "--format",
                "markdown",
            ],
            "cwd": ".",
            "required": True,
            "purpose": "Confirm MCP tools can list, validate, run, and inspect a workflow.",
        },
        {
            "id": "brand_scan",
            "command": [python, "-c", release_smoke_brand_scan_script()],
            "cwd": ".",
            "required": True,
            "purpose": "Reject stale public copy such as old command names, old repository URLs, or screenshot placeholders.",
        },
    ]
    if include_vscode:
        checks.append(
            {
                "id": "vscode_extension_tests",
                "command": [release_smoke_npm_command(), "test"],
                "cwd": "vscode-extension",
                "required": True,
                "purpose": "Compile the extension and verify parser, CLI bridge, and command wiring.",
            }
        )
    return {
        "schema_version": 1,
        "status": "planned",
        "workspace_root": workspace,
        "check_count": len(checks),
        "checks": checks,
    }


def release_smoke_npm_command() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def release_smoke_brand_scan_script() -> str:
    return r'''
from pathlib import Path
patterns = (
    "Screenshot " + "placeholder",
    "x" + "-agent",
    "github.com/Xiaoyu155/" + "visual-agent",
    "Use " + "visual-agent",
)
roots = [Path("README.md"), Path("docs"), Path("vscode-extension"), Path("src")]
skip_parts = {("docs", "archive"), ("vscode-extension", "node_modules")}
matches = []
for root in roots:
    paths = [root] if root.is_file() else root.rglob("*")
    for path in paths:
        if not path.is_file():
            continue
        parts = path.parts
        if any(all(item in parts for item in pair) for pair in skip_parts):
            continue
        if path.suffix.lower() not in {".md", ".py", ".ts", ".json"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in patterns:
            if pattern in text:
                matches.append(f"{path}: {pattern}")
if matches:
    print("\n".join(matches))
    raise SystemExit(1)
print("brand scan passed")
'''


def run_release_smoke(
    *,
    workspace_root: str | Path = ".agent-workspace",
    include_vscode: bool = True,
    timeout_seconds: float = 300.0,
    runner: Any = None,
) -> dict[str, Any]:
    plan = build_release_smoke_plan(workspace_root=workspace_root, include_vscode=include_vscode)
    started = monotonic()
    checks: list[dict[str, Any]] = []
    failed = 0
    command_runner = runner or run_release_smoke_command
    for check in plan["checks"]:
        check_started = monotonic()
        result = command_runner(check["command"], cwd=check.get("cwd") or ".", timeout_seconds=timeout_seconds)
        elapsed = monotonic() - check_started
        exit_code = int(result.get("exit_code", 1))
        status = "success" if exit_code == 0 else "failed"
        if status == "failed":
            failed += 1
        checks.append(
            {
                **check,
                "status": status,
                "exit_code": exit_code,
                "elapsed_seconds": round(elapsed, 3),
                "stdout": truncate_release_smoke_output(str(result.get("stdout") or "")),
                "stderr": truncate_release_smoke_output(str(result.get("stderr") or "")),
            }
        )
    return {
        "schema_version": 1,
        "status": "success" if failed == 0 else "failed",
        "workspace_root": str(workspace_root),
        "check_count": len(checks),
        "failed_count": failed,
        "elapsed_seconds": round(monotonic() - started, 3),
        "checks": checks,
    }


def run_release_smoke_command(command: list[str], *, cwd: str | Path = ".", timeout_seconds: float = 300.0) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": 124,
            "stdout": exc.stdout or "",
            "stderr": f"Timed out after {timeout_seconds:.1f}s",
        }
    except OSError as exc:
        return {"exit_code": 1, "stdout": "", "stderr": str(exc)}


def truncate_release_smoke_output(value: str, *, max_chars: int = 4000) -> str:
    normalized = value.strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3] + "..."


def release_smoke_to_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Release Smoke",
        "",
        f"- Status: `{result.get('status')}`",
        f"- Workspace root: `{result.get('workspace_root')}`",
        f"- Checks: {result.get('check_count', 0)}",
        f"- Failed: {result.get('failed_count', 0)}",
        f"- Elapsed seconds: {result.get('elapsed_seconds', 0)}",
        "",
        "| id | status | exit_code | elapsed_seconds | command |",
        "| --- | --- | --- | --- | --- |",
    ]
    for check in result.get("checks", []) if isinstance(result.get("checks"), list) else []:
        command = " ".join(str(part) for part in check.get("command", []))
        lines.append(
            "| "
            + " | ".join(
                markdown_cell(value)
                for value in (
                    check.get("id"),
                    check.get("status"),
                    check.get("exit_code"),
                    check.get("elapsed_seconds"),
                    command,
                )
            )
            + " |"
        )
    failed = [check for check in result.get("checks", []) if isinstance(check, dict) and check.get("status") == "failed"]
    if failed:
        lines.extend(["", "## Failures", ""])
        for check in failed:
            lines.append(f"### {check.get('id')}")
            if check.get("stdout"):
                lines.extend(["", "stdout:", "", "```text", str(check.get("stdout")), "```"])
            if check.get("stderr"):
                lines.extend(["", "stderr:", "", "```text", str(check.get("stderr")), "```"])
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
            "name": "get_visual_status",
            "purpose": "Read .visual-agent-status.md as structured JSON for the current verification state.",
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
            "id": "show_status",
            "command": f"{python} -m visual_agent.cli show-status --workspace-root {workspace} --format markdown",
        },
        {
            "id": "quality_gate",
            "command": f"{python} -m visual_agent.cli quality-gate --profile ci --workspace-root {workspace} --run --fail-on-secret-leak",
        },
    ]
    prompts = [
        "Use Checkpoint to list workflows, run verify-now, then summarize the report.",
        "Use Checkpoint to validate every workflow before suggesting changes.",
        "If a workflow fails, use get_run_report and list_run_artifacts before editing code.",
        "Before and after risky changes, call get_workspace_dashboard and summarize any attention items.",
        "When a run fails, call get_latest_failure first, then inspect artifacts if needed.",
        "Never request approved run_profile unless the workspace policy explicitly allows it and the human asked for it.",
    ]
    rules = [
        "Read .visual-agent-status.md for current verification state before planning fixes.",
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
        "positioning": "Checkpoint is the local execution layer for coding agents: persistent workflows, permission profiles, and audited reports.",
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
        "> Read `.visual-agent-status.md` for current verification state before planning fixes.",
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


def run_demo_workspace_check(
    *,
    root: str | Path = ".agent-workspace",
    overwrite: bool = False,
    run_profile: str = "dry-run",
    workflow_name: str | None = None,
    with_demo: bool = True,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .workspace import init_workspace, run_workspace_workflow, validate_workspace, write_workspace_report_index

    if run_profile not in {"dry-run", "supervised", "semi-auto"}:
        raise ValueError(f"Unsupported demo run profile: {run_profile}")
    workspace = init_workspace(root, overwrite=overwrite, with_demo=with_demo)
    validation = validate_workspace(workspace)
    validation_ok = all(item.valid for item in validation)
    workflow_name = workflow_name or ("browser_form_workflow" if run_profile != "dry-run" else "local_html_form_workflow")
    if inputs is None:
        if workflow_name in {"browser_form_workflow", "local_html_form_workflow"}:
            inputs = {"username": "demo_user", "password": "demo_password"}
        else:
            inputs = {}
    result = run_workspace_workflow(
        workspace,
        workflow_name,
        inputs=inputs,
        dry_run=run_profile == "dry-run",
        run_profile=run_profile,
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
        "workflow": workflow_name,
        "run_profile": run_profile,
        "run_id": result.run_id,
        "failed_steps": failed_steps,
        "report_index": str(index_path),
    }


def run_release_trial(
    *,
    workspace_root: str | Path = ".agent-workspace",
    overwrite: bool = True,
    run_profile: str = "supervised",
    cloud_org: str = "team-a",
    cloud_user: str = "release-trial",
    cloud_api_key: str = "release-trial-key",
) -> dict[str, Any]:
    from .cloud import build_http_cloud_transport, execute_remote_workflow_plan
    from .console import build_workspace_dashboard, dashboard_to_markdown
    from .cloud_server import create_cloud_server
    from .reports import build_run_history_report, run_history_report_to_markdown, write_run_history_report
    from .visual_status import append_cloud_run_history
    from .workspace import discover_workflows, init_workspace, open_workspace

    if run_profile not in {"dry-run", "supervised", "semi-auto"}:
        raise ValueError(f"Unsupported release trial run profile: {run_profile}")

    workspace_path = Path(workspace_root).resolve()
    workspace_exists = workspace_path.exists()
    workspace = open_workspace(workspace_path) if workspace_exists else init_workspace(workspace_path, with_demo=True, overwrite=overwrite)
    workflows = discover_workflows(workspace, include_slow=True)
    selected_workflow = "browser_form_workflow" if run_profile != "dry-run" else "local_html_form_workflow"
    if workflows:
        workflow_names = {ref.name for ref in workflows}
        if selected_workflow not in workflow_names:
            selected_workflow = workflows[0].name
    seed_demo = not workspace_exists or not workflows
    demo_inputs = {"username": "demo_user", "password": "demo_password"} if selected_workflow in {"browser_form_workflow", "local_html_form_workflow"} else {}
    demo_result = run_demo_workspace_check(
        root=workspace.root,
        overwrite=overwrite if not workspace_exists else False,
        run_profile=run_profile,
        workflow_name=selected_workflow,
        with_demo=seed_demo,
        inputs=demo_inputs,
    )
    inputs_file = "demo_login.json" if (workspace.inputs_dir / "demo_login.json").exists() else None
    mcp_result = run_mcp_smoke_check(workspace_root=workspace.root, workflow=selected_workflow, inputs_file=inputs_file)

    workflow_name = str(demo_result.get("workflow") or selected_workflow)
    server = create_cloud_server(
        workspace_root=workspace.root,
        port=0,
        api_key=cloud_api_key,
        required_org=cloud_org,
        run_profile=run_profile,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = build_http_cloud_transport(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1/run",
            api_key=cloud_api_key,
            org=cloud_org,
            user_id=cloud_user,
        )
        cloud_result = execute_remote_workflow_plan(
            workflow_name,
            workspace.root,
            run_profile=run_profile,
            inputs_file=inputs_file,
            execute=True,
            transport=transport,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    cloud_run_result = cloud_result.get("result") if isinstance(cloud_result.get("result"), dict) else {}
    if cloud_run_result:
        try:
            append_cloud_run_history(workspace.root, cloud_run_result)
        except Exception:
            pass

    dashboard = build_workspace_dashboard(workspace, limit=5)
    dashboard_markdown = dashboard_to_markdown(dashboard)
    run_history_report = build_run_history_report(workspace.root, limit=10)
    run_history_report_markdown = run_history_report_to_markdown(run_history_report)
    run_history_report["ai_summary"] = {
        "schema_version": 1,
        "provider": "none",
        "model": None,
        "status": "generated",
        "source": "deterministic",
        "text": run_history_report_markdown.splitlines()[0] if run_history_report_markdown else "Release trial report generated.",
        "prompt": None,
        "error": None,
    }
    report_path = write_run_history_report(workspace.root, output_path=workspace.root / "reports" / "release_trial_report.html", limit=10)

    checks = [
        {
            "id": "demo_workspace_check",
            "status": demo_result.get("status") or "failed",
            "workflow": demo_result.get("workflow") or "",
            "run_id": demo_result.get("run_id") or "",
        },
        {
            "id": "mcp_smoke",
            "status": mcp_result.get("status") or "failed",
            "workflow": mcp_result.get("workflow") or selected_workflow,
            "run_id": mcp_result.get("run_id") or "",
            "check_count": mcp_result.get("check_count") or 0,
        },
        {
            "id": "cloud_run",
            "status": cloud_run_result.get("status") or "failed",
            "workflow_name": cloud_run_result.get("workflow_name") or workflow_name,
            "run_id": cloud_run_result.get("run_id") or "",
            "workflow_source": cloud_run_result.get("workflow_source") or cloud_result.get("workflow_source") or "",
            "workflow_id": cloud_run_result.get("workflow_id") or cloud_result.get("workflow_id") or "",
            "network_sent": bool(cloud_result.get("network_sent", False)),
        },
    ]
    failed = [item for item in checks if item.get("status") != "success"]
    status = "success" if not failed else "failed"
    result = {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "status": status,
        "run_profile": run_profile,
        "cloud_org": cloud_org,
        "cloud_user": cloud_user,
        "checks": checks,
        "failed_count": len(failed),
        "demo_workspace_check": demo_result,
        "mcp_smoke": mcp_result,
        "cloud_run": cloud_result,
        "workspace_dashboard": dashboard,
        "workspace_dashboard_markdown": dashboard_markdown,
        "run_history_report": run_history_report,
        "run_history_report_markdown": run_history_report_markdown,
        "run_history_report_path": str(report_path),
    }
    result["release_trial_bundle"] = write_release_trial_bundle(workspace.root, result)
    return result


def demo_workspace_check_to_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Demo Workspace Check",
        "",
        f"- Workspace root: `{result.get('workspace_root')}`",
        f"- Status: `{result.get('status')}`",
        f"- Validation OK: `{result.get('validation_ok')}`",
        f"- Workflow: `{result.get('workflow')}`",
        f"- Run profile: `{result.get('run_profile') or 'dry-run'}`",
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


def release_trial_to_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Release Trial",
        "",
        f"- Workspace root: `{result.get('workspace_root')}`",
        f"- Status: `{result.get('status')}`",
        f"- Run profile: `{result.get('run_profile')}`",
        f"- Cloud org: `{result.get('cloud_org')}`",
        f"- Cloud user: `{result.get('cloud_user')}`",
        "",
        "| id | status | run_id |",
        "| --- | --- | --- |",
    ]
    for check in result.get("checks", []) if isinstance(result.get("checks"), list) else []:
        if isinstance(check, dict):
            lines.append(
                "| "
                + " | ".join(
                    markdown_cell(value)
                    for value in (check.get("id"), check.get("status"), check.get("run_id"))
                )
                + " |"
            )
    demo = result.get("demo_workspace_check") if isinstance(result.get("demo_workspace_check"), dict) else {}
    mcp = result.get("mcp_smoke") if isinstance(result.get("mcp_smoke"), dict) else {}
    cloud = result.get("cloud_run") if isinstance(result.get("cloud_run"), dict) else {}
    dashboard = result.get("workspace_dashboard") if isinstance(result.get("workspace_dashboard"), dict) else {}
    report = result.get("run_history_report") if isinstance(result.get("run_history_report"), dict) else {}
    if demo:
        lines.extend(["", "## Demo Workspace", "", demo_workspace_check_to_markdown(demo).strip()])
    if mcp:
        lines.extend(["", "## MCP Smoke", "", mcp_smoke_check_to_markdown(mcp).strip()])
    if cloud:
        cloud_run = cloud.get("result") if isinstance(cloud.get("result"), dict) else {}
        lines.extend(
            [
                "",
                "## Cloud Run",
                "",
                f"- Status: `{cloud_run.get('status') or cloud.get('status') or 'failed'}`",
                f"- Run id: `{cloud_run.get('run_id') or cloud.get('run_id') or ''}`",
                f"- Workflow source: `{cloud_run.get('workflow_source') or cloud.get('workflow_source') or ''}`",
            ]
        )
    if dashboard:
        health = dashboard.get("health") if isinstance(dashboard.get("health"), dict) else {}
        lines.extend(
            [
                "",
                "## Workspace Dashboard",
                "",
                f"- Health: `{health.get('status') or 'unknown'}`",
                f"- Issues: {', '.join(health.get('issues') or []) or 'none'}",
                f"- Report path: `{result.get('run_history_report_path') or ''}`",
            ]
        )
    if report:
        lines.extend(
            [
                "",
                "## Run History Report",
                "",
                f"- Total runs: `{report.get('summary', {}).get('total_runs', 0) if isinstance(report.get('summary'), dict) else 0}`",
                f"- Pass rate: `{round(float(report.get('summary', {}).get('pass_rate') or 0.0) * 100, 1) if isinstance(report.get('summary'), dict) else 0.0}%`",
            ]
        )
    bundle = result.get("release_trial_bundle") if isinstance(result.get("release_trial_bundle"), dict) else {}
    if bundle:
        lines.extend(
            [
                "",
                "## Bundle",
                "",
                f"- JSON: `{bundle.get('json') or ''}`",
                f"- Markdown: `{bundle.get('markdown') or ''}`",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def write_release_trial_bundle(workspace_root: str | Path, result: dict[str, Any]) -> dict[str, str]:
    workspace = Path(workspace_root).resolve()
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    bundle_json = reports_dir / "release_trial_bundle.json"
    bundle_markdown = reports_dir / "release_trial_bundle.md"
    bundle_json.write_text(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2), encoding="utf-8")
    bundle_markdown.write_text(release_trial_to_markdown(result), encoding="utf-8")
    return {"json": str(bundle_json), "markdown": str(bundle_markdown)}
