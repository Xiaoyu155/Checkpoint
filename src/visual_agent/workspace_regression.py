from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import strftime, time
from typing import Any

from .workspace_reports import tag_workspace_report


@dataclass(frozen=True)
class RegressionFixtureExport:
    run_id: str
    fixture_path: Path
    test_draft_path: Path
    manifest_path: Path


@dataclass(frozen=True)
class RegressionPromotion:
    run_id: str
    test_path: Path
    index_path: Path


@dataclass(frozen=True)
class RegressionTestRun:
    run_id: str
    status: str
    exit_code: int
    report_path: Path
    markdown_path: Path
    total_tests: int | None = None
    passed_tests: int | None = None
    failed_tests: int | None = None


def export_regression_fixture(
    workspace: Any,
    run_id: str,
    *,
    allow_success: bool = False,
    overwrite: bool = False,
) -> RegressionFixtureExport:
    report_path = workspace.reports_dir / f"{run_id}.json"
    if not report_path.exists():
        raise FileNotFoundError(f"Report not found in workspace: {run_id}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "failed" and not allow_success:
        raise ValueError(f"Regression fixture export expects a failed report: {run_id}")
    full_run = load_full_run_payload(workspace, report)
    observation = latest_observation_from_report(full_run)
    if observation is None:
        raise ValueError(f"No observation found in report: {run_id}")

    export_dir = workspace.fixtures_dir / "regression"
    draft_dir = workspace.reports_dir / "regression"
    export_dir.mkdir(parents=True, exist_ok=True)
    draft_dir.mkdir(parents=True, exist_ok=True)
    safe_id = safe_identifier(run_id)
    fixture_path = export_dir / f"{safe_id}_observation.json"
    test_draft_path = draft_dir / f"test_{safe_id}_draft.py"
    manifest_path = draft_dir / f"{safe_id}_manifest.json"
    for path in (fixture_path, test_draft_path, manifest_path):
        if path.exists() and not overwrite:
            raise FileExistsError(f"Regression export already exists: {path}")

    metadata = dict(observation.get("metadata") if isinstance(observation.get("metadata"), dict) else {})
    metadata["regression_source_run_id"] = run_id
    metadata["regression_source_workflow"] = report.get("workflow_name")
    metadata["regression_failed_step"] = report.get("failed_step")
    observation = {**observation, "metadata": metadata}
    fixture_path.write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    annotation = tag_workspace_report(
        workspace,
        run_id,
        review_status="regression_ready",
        tags=("regression",),
        regression_candidate=True,
    )
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "workflow_name": report.get("workflow_name"),
        "status": report.get("status"),
        "failed_step": report.get("failed_step"),
        "source_report": report_path.relative_to(workspace.root).as_posix()
        if report_path.is_relative_to(workspace.root)
        else str(report_path),
        "fixture": fixture_path.relative_to(workspace.root).as_posix(),
        "test_draft": test_draft_path.relative_to(workspace.root).as_posix(),
        "created_at": time(),
        "annotation": annotation,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    test_draft_path.write_text(regression_test_draft(run_id, fixture_path, report), encoding="utf-8")
    return RegressionFixtureExport(
        run_id=run_id,
        fixture_path=fixture_path,
        test_draft_path=test_draft_path,
        manifest_path=manifest_path,
    )


def promote_regression_fixture(
    workspace: Any,
    run_id: str,
    *,
    overwrite: bool = False,
) -> RegressionPromotion:
    safe_id = safe_identifier(run_id)
    manifest_path = workspace.reports_dir / "regression" / f"{safe_id}_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Regression manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ensure_regression_fixture_metadata(workspace, run_id, manifest)
    test_path = workspace.regression_tests_dir / f"test_{safe_id}.py"
    if test_path.exists() and not overwrite:
        raise FileExistsError(f"Promoted regression test already exists: {test_path}")

    workspace.regression_tests_dir.mkdir(parents=True, exist_ok=True)
    test_path.write_text(promoted_regression_test(run_id, manifest), encoding="utf-8")
    tag_workspace_report(
        workspace,
        run_id,
        review_status="regression_ready",
        tags=("promoted", "regression"),
        regression_candidate=True,
    )
    index_path = write_regression_tests_index(workspace)
    return RegressionPromotion(run_id=run_id, test_path=test_path, index_path=index_path)


def list_regression_tests(workspace: Any) -> dict[str, Any]:
    path = workspace.regression_tests_dir / "index.json"
    if not path.exists():
        write_regression_tests_index(workspace)
    return json.loads(path.read_text(encoding="utf-8"))


def run_workspace_regression_tests(
    workspace: Any,
    *,
    pytest_args: tuple[str, ...] = (),
    timeout_seconds: float = 120.0,
) -> RegressionTestRun:
    workspace.regression_tests_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{strftime('%Y%m%d-%H%M%S')}-{safe_identifier(str(time())).split('_')[-1]}"
    report_dir = workspace.reports_dir / "regression_runs"
    report_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        str(workspace.regression_tests_dir),
        *pytest_args,
    ]
    started = time()
    try:
        completed = subprocess.run(
            command,
            cwd=workspace.root,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        timed_out = True
    elapsed = round(time() - started, 6)
    summary = parse_pytest_summary(stdout)
    status = "success" if exit_code == 0 else "failed"
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "elapsed_seconds": elapsed,
        "command": command,
        "workspace_root": str(workspace.root),
        "regression_tests_dir": str(workspace.regression_tests_dir),
        "summary": summary,
        "stdout": stdout,
        "stderr": stderr,
    }
    report_path = report_dir / f"{run_id}.json"
    markdown_path = report_dir / f"{run_id}.md"
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(regression_run_markdown(payload), encoding="utf-8")
    return RegressionTestRun(
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        report_path=report_path,
        markdown_path=markdown_path,
        total_tests=summary.get("total"),
        passed_tests=summary.get("passed"),
        failed_tests=summary.get("failed"),
    )


def parse_pytest_summary(output: str) -> dict[str, int | None]:
    summary_line = ""
    for line in output.splitlines():
        if " passed" in line or " failed" in line or " error" in line:
            summary_line = line
    passed = extract_count(summary_line, "passed")
    failed = extract_count(summary_line, "failed")
    errors = extract_count(summary_line, "errors") or extract_count(summary_line, "error")
    total = sum(value for value in (passed, failed, errors) if value is not None)
    return {
        "total": total if total else None,
        "passed": passed,
        "failed": failed,
        "errors": errors,
    }


def extract_count(line: str, label: str) -> int | None:
    match = re.search(rf"(\d+)\s+{re.escape(label)}", line)
    return int(match.group(1)) if match else None


def regression_run_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        f"# Regression Test Run: {payload['run_id']}",
        "",
        f"- Status: `{payload['status']}`",
        f"- Exit code: {payload['exit_code']}",
        f"- Timed out: {payload['timed_out']}",
        f"- Elapsed seconds: {payload['elapsed_seconds']}",
        f"- Total tests: {summary.get('total')}",
        f"- Passed: {summary.get('passed')}",
        f"- Failed: {summary.get('failed')}",
        "",
        "## Command",
        "",
        "```text",
        " ".join(str(part) for part in payload["command"]),
        "```",
        "",
        "## Output",
        "",
        "```text",
        str(payload.get("stdout") or "").strip(),
        "```",
    ]
    if payload.get("stderr"):
        lines.extend(["", "## Stderr", "", "```text", str(payload["stderr"]).strip(), "```"])
    return "\n".join(lines).rstrip() + "\n"


def write_regression_tests_index(workspace: Any) -> Path:
    workspace.regression_tests_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for path in sorted(workspace.regression_tests_dir.glob("test_*.py")):
        entries.append(
            {
                "name": path.name,
                "path": path.relative_to(workspace.root).as_posix()
                if path.is_relative_to(workspace.root)
                else str(path),
                "run_id_hint": path.stem.removeprefix("test_"),
                "size_bytes": path.stat().st_size,
                "modified_at": path.stat().st_mtime,
            }
        )
    index = {
        "schema_version": 1,
        "generated_at": time(),
        "workspace_root": str(workspace.root),
        "total_tests": len(entries),
        "entries": entries,
    }
    path = workspace.regression_tests_dir / "index.json"
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def promoted_regression_test(run_id: str, manifest: dict[str, Any]) -> str:
    fixture = str(manifest["fixture"])
    workflow_name = manifest.get("workflow_name") or "unknown"
    failed_step = manifest.get("failed_step") or "unknown"
    test_name = f"test_regression_{safe_identifier(run_id)}"
    return (
        "from pathlib import Path\n\n"
        "from visual_agent.fixtures import load_observation_fixture\n\n\n"
        f"def {test_name}():\n"
        "    workspace_root = Path(__file__).resolve().parents[1]\n"
        f"    observation = load_observation_fixture(workspace_root / {fixture!r})\n"
        "    assert observation.elements\n"
        f"    assert observation.metadata.get('regression_source_run_id') == {run_id!r}\n"
        f"    # Source workflow: {workflow_name}\n"
        f"    # Failed step: {failed_step}\n"
    )


def ensure_regression_fixture_metadata(workspace: Any, run_id: str, manifest: dict[str, Any]) -> None:
    fixture_path = workspace.root / str(manifest["fixture"])
    if not fixture_path.exists():
        return
    try:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    except Exception:
        return
    metadata = dict(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {})
    changed = False
    for key, value in {
        "regression_source_run_id": run_id,
        "regression_source_workflow": manifest.get("workflow_name"),
        "regression_failed_step": manifest.get("failed_step"),
    }.items():
        if metadata.get(key) != value:
            metadata[key] = value
            changed = True
    if changed:
        payload["metadata"] = metadata
        fixture_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def latest_observation_from_report(report: dict[str, Any]) -> dict[str, Any] | None:
    steps = report.get("steps") if isinstance(report.get("steps"), list) else []
    failed_step = report.get("failed_step")
    observation: dict[str, Any] | None = None
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("id") == failed_step:
            break
        candidate = step.get("observation")
        if isinstance(candidate, dict):
            observation = candidate
    if observation is not None:
        return observation
    for step in reversed(steps):
        candidate = step.get("observation") if isinstance(step, dict) else None
        if isinstance(candidate, dict):
            return candidate
    return None


def load_full_run_payload(workspace: Any, report: dict[str, Any]) -> dict[str, Any]:
    artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
    workflow_result = artifacts.get("workflow_result")
    if workflow_result:
        path = Path(str(workflow_result))
        if not path.is_absolute():
            path = workspace.root / path
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    return report


def regression_test_draft(run_id: str, fixture_path: Path, report: dict[str, Any]) -> str:
    failed_step = report.get("failed_step") or "unknown"
    workflow_name = report.get("workflow_name") or "unknown"
    test_name = f"test_regression_{safe_identifier(run_id)}"
    return (
        "from visual_agent.fixtures import load_observation_fixture\n\n\n"
        f"def {test_name}():\n"
        f"    observation = load_observation_fixture(r\"{fixture_path}\")\n"
        f"    assert observation.elements\n"
        f"    # Source workflow: {workflow_name}\n"
        f"    # Failed step: {failed_step}\n"
        "    # Replace this smoke check with the selector or assertion that failed.\n"
    )


def safe_identifier(value: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z_]+", "_", value).strip("_").lower()
    if not safe:
        return "sample"
    if safe[0].isdigit():
        return f"r_{safe}"
    return safe
