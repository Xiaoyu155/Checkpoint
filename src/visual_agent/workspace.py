from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from os import chdir
from pathlib import Path
from time import strftime, time
from typing import Any

from .capabilities import build_atomic_capability_manifest
from .locks import RunLock, lock_to_dict, queue_to_dict
from .models import to_jsonable
from .preflight import run_preflight
from .licensing import get_license, report_history_window_days
from .reports import (
    RunSummary,
    list_run_summaries,
    load_run_report,
    run_report_to_dict,
    run_report_to_markdown,
)
from .validation import ValidationResult, validate_workflow, validate_workflow_file
from .workflow import Workflow, WorkflowRunResult, WorkflowRuntime, parse_workflow_file


WORKSPACE_DIRS = ("workflows", "inputs", "fixtures", "runs", "reports", "regression_tests", "queue")
WORKSPACE_RISK_POLICY_PROFILES = ("planner", "local", "ci")
WORKSPACE_RISK_ATTENTION_TREND_DIRECTIONS = (
    "worsening",
    "mixed",
    "improving",
    "stable",
    "insufficient_history",
    "unknown",
)
WORKSPACE_REPAIR_RISK_LEVELS = ("unknown", "low", "medium", "high")
DEFAULT_AUTO_REPAIR_POLICY = {
    "min_confidence": 0.75,
    "max_risk_level": "medium",
    "allow_force": True,
}


@dataclass(frozen=True)
class WorkflowRef:
    name: str
    path: Path
    relative_path: str
    tags: tuple[str, ...] = ()
    affects: tuple[str, ...] = ()
    visibility: str = "private"
    author: str = ""
    description: str = ""
    license: str = ""


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def workflows_dir(self) -> Path:
        return self.root / "workflows"

    @property
    def inputs_dir(self) -> Path:
        return self.root / "inputs"

    @property
    def fixtures_dir(self) -> Path:
        return self.root / "fixtures"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def regression_tests_dir(self) -> Path:
        return self.root / "regression_tests"

    @property
    def queue_dir(self) -> Path:
        return self.root / "queue"

    @property
    def project_root(self) -> Path:
        return infer_project_root(self.root)


@dataclass(frozen=True)
class WorkspaceReportExport:
    run_id: str
    json_path: Path | None
    markdown_path: Path | None
    index_path: Path | None = None


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


def init_workspace(
    root: str | Path,
    *,
    with_demo: bool = True,
    overwrite: bool = False,
    framework_hint: str | None = None,
) -> Workspace:
    workspace = Workspace(root=Path(root).resolve())
    workspace.root.mkdir(parents=True, exist_ok=True)
    for dirname in WORKSPACE_DIRS:
        (workspace.root / dirname).mkdir(parents=True, exist_ok=True)

    manifest_path = workspace.root / "workspace.json"
    if overwrite or not manifest_path.exists():
        manifest = {
            "name": workspace.root.name,
            "version": 1,
            "project_root": str(workspace.project_root),
            "dirs": list(WORKSPACE_DIRS),
            "mcp": {
                "approved_workflows": [],
                "audit_all_calls": True,
                "max_run_profile": "supervised",
            },
        }
        if framework_hint:
            manifest["framework_hint"] = framework_hint
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    elif framework_hint:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
        if isinstance(manifest, dict) and manifest.get("framework_hint") != framework_hint:
            manifest["framework_hint"] = framework_hint
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if with_demo:
        copy_demo_assets(workspace, overwrite=overwrite)
    if framework_hint:
        write_framework_demo_assets(workspace, framework_hint=framework_hint, overwrite=overwrite)
    return workspace


def infer_project_root(workspace_root: Path) -> Path:
    if workspace_root.name.startswith(".agent-workspace"):
        return workspace_root.parent
    return workspace_root.parent


def open_workspace(root: str | Path) -> Workspace:
    workspace = Workspace(root=Path(root).resolve())
    if not workspace.root.exists():
        raise FileNotFoundError(f"Workspace does not exist: {workspace.root}")
    for dirname in WORKSPACE_DIRS:
        (workspace.root / dirname).mkdir(parents=True, exist_ok=True)
    return workspace


def copy_demo_assets(workspace: Workspace, *, overwrite: bool = False) -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent
    copies = [
        (repo_root / "examples" / "local_html_form_workflow.yaml", workspace.workflows_dir / "local_html_form_workflow.yaml"),
        (
            repo_root / "examples" / "workflows" / "checkout" / "checkout_verification.yaml",
            workspace.workflows_dir / "checkout_verification.yaml",
        ),
        (repo_root / "examples" / "inputs" / "demo_login.json", workspace.inputs_dir / "demo_login.json"),
        (repo_root / "examples" / "web" / "login_demo.html", workspace.fixtures_dir / "login_demo.html"),
    ]
    for source, target in copies:
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    workflow_path = workspace.workflows_dir / "local_html_form_workflow.yaml"
    if workflow_path.exists():
        text = workflow_path.read_text(encoding="utf-8")
        text = text.replace("examples/web/login_demo.html", "fixtures/login_demo.html")
        workflow_path.write_text(text, encoding="utf-8")

    checkout_workflow_path = workspace.workflows_dir / "checkout_verification.yaml"
    if checkout_workflow_path.exists():
        text = checkout_workflow_path.read_text(encoding="utf-8")
        text = text.replace(
            "examples/web/checkout_verification_demo.html",
            "../examples/web/checkout_verification_demo.html",
        )
        checkout_workflow_path.write_text(text, encoding="utf-8")


def write_framework_demo_assets(workspace: Workspace, *, framework_hint: str, overwrite: bool = False) -> None:
    framework = framework_hint.strip().lower()
    if not framework:
        return
    fixture_path = workspace.fixtures_dir / f"{framework}_demo.html"
    workflow_path = workspace.workflows_dir / f"{framework}_verification.yaml"
    title = {
        "nextjs": "Next.js profile demo",
        "react": "React profile demo",
        "vue": "Vue profile demo",
        "remix": "Remix profile demo",
        "django": "Django profile demo",
        "fastapi": "FastAPI profile demo",
        "flask": "Flask profile demo",
        "html": "HTML profile demo",
    }.get(framework, f"{framework} profile demo")
    if overwrite or not fixture_path.exists():
        fixture_path.write_text(
            "\n".join(
                [
                    "<!doctype html>",
                    "<html>",
                    "<head><meta charset=\"utf-8\"><title>" + title + "</title></head>",
                    "<body>",
                    "  <form action=\"/profile/saved\">",
                    "    <label for=\"display_name\">Display name</label>",
                    "    <input id=\"display_name\" name=\"display_name\" required>",
                    "    <button type=\"submit\">Save profile</button>",
                    "  </form>",
                    "  <p>Profile saved successfully</p>",
                    "</body>",
                    "</html>",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    if overwrite or not workflow_path.exists():
        workflow_path.write_text(
            "\n".join(
                [
                    "schema_version: 1",
                    f"name: {framework}_verification",
                    "version: 1",
                    "tags:",
                    "  - verification",
                    f"  - {framework}",
                    "steps:",
                    "  - id: open_demo",
                    "    action: observe_html",
                    f"    path: fixtures/{framework}_demo.html",
                    "  - id: assert_success",
                    "    action: assert_text",
                    "    text: Profile saved successfully",
                    "",
                ]
            ),
            encoding="utf-8",
        )


def discover_workflows(workspace: Workspace, *, include_slow: bool = False) -> tuple[WorkflowRef, ...]:
    paths = sorted(
        [
            *workspace.workflows_dir.rglob("*.yaml"),
            *workspace.workflows_dir.rglob("*.yml"),
            *workspace.workflows_dir.rglob("*.json"),
        ]
    )
    refs: list[WorkflowRef] = []
    for path in paths:
        metadata = workflow_metadata(path)
        tags = tuple(metadata["tags"])
        if not include_slow and "slow" in tags:
            continue
        refs.append(
            WorkflowRef(
                name=path.stem,
                path=path,
                relative_path=path.relative_to(workspace.root).as_posix(),
                tags=tags,
                affects=tuple(metadata["affects"]),
                visibility=str(metadata["visibility"]),
                author=str(metadata["author"]),
                description=str(metadata["description"]),
                license=str(metadata["license"]),
            )
        )
    return tuple(refs)


def workflow_tags(path: Path) -> tuple[str, ...]:
    return tuple(workflow_metadata(path)["tags"])


def workflow_metadata(path: Path) -> dict[str, Any]:
    try:
        workflow = parse_workflow_file(path)
        return {
            "tags": tuple(str(tag) for tag in workflow.tags),
            "affects": tuple(str(item) for item in workflow.affects),
            "visibility": workflow.visibility,
            "author": workflow.author,
            "description": workflow.description,
            "license": workflow.license,
        }
    except Exception:
        return {"tags": (), "affects": (), "visibility": "private", "author": "", "description": "", "license": ""}


def find_workflow(workspace: Workspace, name_or_path: str) -> WorkflowRef:
    raw = Path(name_or_path)
    candidates = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.extend(
            [
                workspace.root / raw,
                workspace.workflows_dir / raw,
                workspace.workflows_dir / f"{name_or_path}.yaml",
                workspace.workflows_dir / f"{name_or_path}.yml",
                workspace.workflows_dir / f"{name_or_path}.json",
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            metadata = workflow_metadata(candidate)
            return WorkflowRef(
                name=candidate.stem,
                path=candidate,
                relative_path=candidate.relative_to(workspace.root).as_posix()
                if candidate.is_relative_to(workspace.root)
                else str(candidate),
                tags=tuple(metadata["tags"]),
                affects=tuple(metadata["affects"]),
                visibility=str(metadata["visibility"]),
                author=str(metadata["author"]),
                description=str(metadata["description"]),
                license=str(metadata["license"]),
            )

    for ref in discover_workflows(workspace, include_slow=True):
        if ref.name == name_or_path or ref.relative_path == name_or_path:
            return ref
    raise FileNotFoundError(f"Workflow not found in workspace: {name_or_path}")


def validate_workspace(
    workspace: Workspace,
    *,
    strict: bool = False,
    allow_high_risk: bool = False,
) -> tuple[ValidationResult, ...]:
    if not strict:
        return tuple(validate_workflow_file(ref.path) for ref in discover_workflows(workspace))
    return tuple(
        validate_workflow(parse_workflow_file(ref.path), strict=True, allow_high_risk=allow_high_risk)
        for ref in discover_workflows(workspace)
    )


def load_workspace_inputs(workspace: Workspace, raw_inputs: str | None, inputs_file: str | None) -> dict[str, Any]:
    if raw_inputs and inputs_file:
        raise ValueError("Use either inline inputs or an inputs file, not both.")
    if raw_inputs:
        return json.loads(raw_inputs)
    if inputs_file:
        path = Path(inputs_file)
        if not path.is_absolute():
            path = workspace.root / path
            if not path.exists():
                path = workspace.inputs_dir / inputs_file
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def validate_workflow_inputs(
    workflow: Workflow,
    inputs: dict[str, Any],
    *,
    sensitive_fields: set[str] | None = None,
) -> dict[str, Any]:
    required = sensitive_input_requirements(workflow, sensitive_fields=sensitive_fields)
    missing = []
    empty = []
    for item in required:
        path = str(item["path"])
        exists, value = read_input_path(inputs, path)
        if not exists:
            missing.append(item)
        elif is_empty_input_value(value):
            empty.append(item)
    return {
        "ok": not missing and not empty,
        "required_sensitive_inputs": required,
        "missing_sensitive_inputs": missing,
        "empty_sensitive_inputs": empty,
        "message": workflow_inputs_check_message(missing, empty),
    }


def sensitive_input_requirements(
    workflow: Workflow,
    *,
    sensitive_fields: set[str] | None = None,
) -> list[dict[str, Any]]:
    explicit_fields = sensitive_fields or set()
    requirements = []
    seen: set[str] = set()
    for step in workflow.steps:
        value_from = str(step.params.get("value_from") or "")
        if not value_from.startswith("input."):
            continue
        path = value_from.removeprefix("input.")
        is_sensitive = bool(step.params.get("sensitive", False)) or path in explicit_fields
        if not is_sensitive or path in seen:
            continue
        seen.add(path)
        requirements.append(
            {
                "path": path,
                "value_from": value_from,
                "step_id": step.id,
                "action": step.action,
            }
        )
    return requirements


def read_input_path(inputs: dict[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = inputs
    for part in str(path).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return False, None
    return True, current


def is_empty_input_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def workflow_inputs_check_message(missing: list[dict[str, Any]], empty: list[dict[str, Any]]) -> str:
    parts = []
    if missing:
        parts.append("missing: " + ", ".join(str(item["path"]) for item in missing))
    if empty:
        parts.append("empty: " + ", ".join(str(item["path"]) for item in empty))
    if not parts:
        return "Workflow inputs are ready."
    return "Sensitive workflow inputs are not ready (" + "; ".join(parts) + "). Fill the inputs template before running."


def run_workspace_workflow(
    workspace: Workspace,
    workflow_name: str,
    *,
    inputs: dict[str, Any] | None = None,
    dry_run: bool = True,
    run_profile: str | None = None,
    preflight: bool = True,
    strict_preflight: bool = False,
    allow_high_risk: bool = False,
    synthetic_on_capture_fail: bool = False,
    sensitive_fields: set[str] | None = None,
    resume_from: str | Path | None = None,
    use_lock: bool = True,
    lock_ttl_seconds: float = 3600.0,
    queue_when_locked: bool = False,
    lock_wait_seconds: float = 0.0,
    lock_poll_seconds: float = 0.5,
    export_report: bool = True,
) -> WorkflowRunResult:
    ref = find_workflow(workspace, workflow_name)
    workflow = parse_workflow_file(ref.path)
    input_check = validate_workflow_inputs(workflow, inputs or {}, sensitive_fields=sensitive_fields)
    if not input_check["ok"]:
        raise ValueError(str(input_check["message"]))
    outer_lock = None
    outer_lock_info = None
    outer_queue_info = None
    if use_lock and queue_when_locked:
        outer_lock = RunLock(workspace.runs_dir, ttl_seconds=lock_ttl_seconds)
        outer_lock_info, outer_queue_info = outer_lock.acquire_with_wait(
            owner=f"{workflow.name}:workspace-run",
            wait_seconds=lock_wait_seconds,
            poll_seconds=lock_poll_seconds,
        )

    previous_cwd = Path.cwd()
    try:
        if preflight:
            preflight_result = run_preflight(
                workflow,
                strict=strict_preflight,
                allow_high_risk=allow_high_risk,
            )
            if not preflight_result.ok:
                raise RuntimeError(f"Preflight failed for workflow '{workflow.name}'.")
        runtime = WorkflowRuntime(output_dir=workspace.runs_dir)
        chdir(workspace.root)
        result = runtime.run(
            workflow,
            dry_run=dry_run,
            run_profile=run_profile,
            synthetic_on_capture_fail=synthetic_on_capture_fail,
            inputs=inputs or {},
            sensitive_fields=sensitive_fields,
            resume_from=resume_from,
            use_lock=use_lock and outer_lock is None,
            lock_ttl_seconds=lock_ttl_seconds,
            queue_when_locked=queue_when_locked and outer_lock is None,
            lock_wait_seconds=lock_wait_seconds,
            lock_poll_seconds=lock_poll_seconds,
        )
        if outer_lock_info is not None and outer_queue_info is not None:
            result = replace(
                result,
                run_lock=lock_to_dict(outer_lock_info),
                run_queue=queue_to_dict(outer_queue_info),
            )
        if export_report:
            export_workspace_run_report(workspace, result.run_dir)
        try:
            from .session import update_agent_session

            update_agent_session(workspace.root, result)
        except Exception:
            pass
        try:
            from .workflow_index import update_workflow_index

            update_workflow_index(workspace.root, ref)
        except Exception:
            pass
        return result
    finally:
        chdir(previous_cwd)
        if outer_lock is not None:
            outer_lock.release()


def workspace_run_summaries(workspace: Workspace, *, limit: int = 20) -> tuple[RunSummary, ...]:
    return list_run_summaries(workspace.runs_dir, limit=limit)


def export_workspace_run_report(
    workspace: Workspace,
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


def list_workspace_reports(workspace: Workspace) -> tuple[dict[str, Any], ...]:
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
    workspace: Workspace,
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


def write_workspace_report_index(workspace: Workspace) -> Path:
    index = build_workspace_report_index(workspace, include_inaccessible=True)
    path = workspace.reports_dir / "index.json"
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_workspace_report_index(
    workspace: Workspace,
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


def workspace_report_access_payload(workspace: Workspace, report_path: Path) -> dict[str, Any]:
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


def filter_workspace_report_index_for_access(workspace: Workspace, index: dict[str, Any]) -> dict[str, Any]:
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


def report_index_entry(workspace: Workspace, report_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
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


def workspace_report_tags_path(workspace: Workspace) -> Path:
    workspace.reports_dir.mkdir(parents=True, exist_ok=True)
    return workspace.reports_dir / "tags.json"


def load_workspace_report_tags(workspace: Workspace) -> dict[str, Any]:
    path = workspace_report_tags_path(workspace)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    tags = payload.get("reports") if isinstance(payload, dict) else None
    return tags if isinstance(tags, dict) else {}


def save_workspace_report_tags(workspace: Workspace, tags: dict[str, Any]) -> Path:
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
    workspace: Workspace,
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


def export_regression_fixture(
    workspace: Workspace,
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
    workspace: Workspace,
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


def list_regression_tests(workspace: Workspace) -> dict[str, Any]:
    path = workspace.regression_tests_dir / "index.json"
    if not path.exists():
        write_regression_tests_index(workspace)
    return json.loads(path.read_text(encoding="utf-8"))


def run_workspace_regression_tests(
    workspace: Workspace,
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


def write_regression_tests_index(workspace: Workspace) -> Path:
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


def ensure_regression_fixture_metadata(workspace: Workspace, run_id: str, manifest: dict[str, Any]) -> None:
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


def load_full_run_payload(workspace: Workspace, report: dict[str, Any]) -> dict[str, Any]:
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
        "    # TODO: replace this smoke check with the selector/assertion that failed.\n"
    )


def safe_identifier(value: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z_]+", "_", value).strip("_").lower()
    if not safe:
        return "sample"
    if safe[0].isdigit():
        return f"r_{safe}"
    return safe


def workspace_status(workspace: Workspace) -> dict[str, Any]:
    from .scheduler import list_queue_tasks

    workflows = discover_workflows(workspace)
    runs = workspace_run_summaries(workspace, limit=10)
    validations = validate_workspace(workspace)
    queue = list_queue_tasks(workspace)
    manifest = load_workspace_manifest(workspace)
    return {
        "root": str(workspace.root),
        "project_root": str(workspace.project_root),
        "framework_hint": manifest.get("framework_hint") if isinstance(manifest, dict) else None,
        "workflow_count": len(workflows),
        "run_count_shown": len(runs),
        "report_count": load_workspace_report_index(workspace)["total_reports"],
        "regression_test_count": list_regression_tests(workspace)["total_tests"],
        "queue_task_count": queue["total_tasks"],
        "pending_queue_tasks": queue["pending_tasks"],
        "running_queue_tasks": queue["running_tasks"],
        "valid_workflows": sum(1 for result in validations if result.valid),
        "invalid_workflows": sum(1 for result in validations if not result.valid),
        "workflows": [to_jsonable(ref) for ref in workflows],
        "recent_runs": [to_jsonable(summary) for summary in runs],
        "reports": load_workspace_report_index(workspace)["entries"][:10],
        "regression_tests": list_regression_tests(workspace)["entries"][:10],
        "queue": queue["entries"][:10],
    }


def planner_context(workspace: Workspace, *, run_limit: int = 5) -> dict[str, Any]:
    from .gui import build_gui_action_history_risk_summary
    from .scheduler import list_queue_tasks

    workflows = discover_workflows(workspace)
    validations = validate_workspace(workspace)
    validation_by_name = {result.workflow_name: result for result in validations}
    return {
        "workspace": {
            "root": str(workspace.root),
            "name": workspace.root.name,
            "dirs": list(WORKSPACE_DIRS),
        },
        "capabilities": to_jsonable(build_atomic_capability_manifest().capabilities),
        "workflows": [
            {
                **to_jsonable(ref),
                "valid": validation_by_name.get(read_workflow_name(ref.path), None).valid
                if validation_by_name.get(read_workflow_name(ref.path), None)
                else None,
            }
            for ref in workflows
        ],
        "inputs": list_workspace_files(workspace.inputs_dir, suffixes={".json"}),
        "fixtures": list_workspace_files(workspace.fixtures_dir),
        "recent_runs": [to_jsonable(summary) for summary in workspace_run_summaries(workspace, limit=run_limit)],
        "reports": load_workspace_report_index(workspace)["entries"][:run_limit],
        "regression_tests": list_regression_tests(workspace)["entries"][:run_limit],
        "queue": list_queue_tasks(workspace)["entries"][:run_limit],
        "gui_action_history": build_gui_action_history_risk_summary(
            workspace,
            config=load_workspace_gui_action_history_risk_config(workspace),
            profile="planner",
        ),
    }


def load_workspace_manifest(workspace: Workspace) -> dict[str, Any]:
    path = workspace.root / "workspace.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_workspace_gui_action_history_risk_config(workspace: Workspace) -> dict[str, Any]:
    manifest = load_workspace_manifest(workspace)
    quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
    config = quality.get("gui_action_history") if isinstance(quality.get("gui_action_history"), dict) else {}
    return config


def load_workspace_auto_repair_policy(workspace: Workspace | str | Path) -> dict[str, Any]:
    root = workspace.root if isinstance(workspace, Workspace) else Path(workspace).resolve()
    manifest = load_workspace_manifest(Workspace(root)) if Path(root).exists() else {}
    raw = manifest.get("auto_repair") if isinstance(manifest.get("auto_repair"), dict) else {}
    policy = dict(DEFAULT_AUTO_REPAIR_POLICY)
    if "min_confidence" in raw:
        try:
            policy["min_confidence"] = min(1.0, max(0.0, float(raw["min_confidence"])))
        except (TypeError, ValueError):
            pass
    if str(raw.get("max_risk_level") or "").lower() in WORKSPACE_REPAIR_RISK_LEVELS:
        policy["max_risk_level"] = str(raw.get("max_risk_level")).lower()
    if "allow_force" in raw:
        policy["allow_force"] = bool(raw.get("allow_force"))
    return {
        **policy,
        "source": "workspace.json" if isinstance(raw, dict) and raw else "defaults",
    }


def build_workspace_risk_policy_template() -> dict[str, Any]:
    return {
        "auto_repair": dict(DEFAULT_AUTO_REPAIR_POLICY),
        "quality": {
            "gui_action_history": {
                "error_rate_threshold": 0.25,
                "history_limit": 50,
                "failed_action_limit": 2,
                "profiles": {
                    "planner": {
                        "error_rate_threshold": 0.25,
                        "history_limit": 50,
                        "failed_action_limit": 2,
                    },
                    "local": {
                        "error_rate_threshold": 0.3,
                        "history_limit": 50,
                        "failed_action_limit": 3,
                    },
                    "ci": {
                        "error_rate_threshold": 0.15,
                        "history_limit": 100,
                        "failed_action_limit": 1,
                    },
                },
                "health": {
                    "attention_trend_directions": ["worsening"],
                },
            },
        },
    }


def build_workspace_risk_policy_apply_plan(
    workspace: Workspace,
    *,
    overwrite: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    manifest_path = workspace.root / "workspace.json"
    manifest = load_workspace_manifest(workspace)
    if not manifest:
        manifest = {
            "name": workspace.root.name,
            "version": 1,
            "dirs": list(WORKSPACE_DIRS),
        }
    before_quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
    template = build_workspace_risk_policy_template()
    template_quality = template["quality"]
    proposed_quality = (
        merge_json_object(before_quality, template_quality)
        if overwrite
        else merge_json_object(template_quality, before_quality)
    )
    proposed_manifest = dict(manifest)
    proposed_manifest["quality"] = proposed_quality
    before_auto_repair = manifest.get("auto_repair") if isinstance(manifest.get("auto_repair"), dict) else {}
    template_auto_repair = template["auto_repair"]
    proposed_auto_repair = (
        merge_json_object(before_auto_repair, template_auto_repair)
        if overwrite
        else merge_json_object(template_auto_repair, before_auto_repair)
    )
    proposed_manifest["auto_repair"] = proposed_auto_repair
    changed_paths = diff_json_paths(before_quality, proposed_quality, path="quality")
    changed_paths.extend(diff_json_paths(before_auto_repair, proposed_auto_repair, path="auto_repair"))
    if apply and changed_paths:
        manifest_path.write_text(json.dumps(proposed_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    validation_before = validate_workspace_risk_policy(workspace)
    validation_after = validate_risk_policy_manifest(proposed_manifest, workspace=workspace, manifest_path=manifest_path)
    return {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "manifest_path": str(manifest_path),
        "mode": "overwrite" if overwrite else "fill_missing",
        "applied": bool(apply and changed_paths),
        "changed": bool(changed_paths),
        "changed_paths": changed_paths,
        "patch": {"auto_repair": proposed_auto_repair, "quality": proposed_quality},
        "validation_before": {
            "status": validation_before["status"],
            "error_count": validation_before["error_count"],
            "warning_count": validation_before["warning_count"],
        },
        "validation_after": {
            "status": validation_after["status"],
            "error_count": validation_after["error_count"],
            "warning_count": validation_after["warning_count"],
        },
    }


def merge_json_object(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = merge_json_object(existing, value)
        else:
            merged[key] = value
    return merged


def diff_json_paths(before: Any, after: Any, *, path: str) -> list[str]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        paths: list[str] = []
        for key in sorted(set(before) | set(after)):
            paths.extend(diff_json_paths(before.get(key), after.get(key), path=f"{path}.{key}"))
        return paths
    return [path]


def validate_workspace_risk_policy(workspace: Workspace) -> dict[str, Any]:
    manifest_path = workspace.root / "workspace.json"
    manifest: Any = {}
    if manifest_path.exists():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return invalid_workspace_manifest_risk_policy_result(workspace, manifest_path, exc.msg)
        manifest = payload
    return validate_risk_policy_manifest(manifest, workspace=workspace, manifest_path=manifest_path)


def invalid_workspace_manifest_risk_policy_result(workspace: Workspace, manifest_path: Path, message: str) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    add_risk_policy_issue(
        issues,
        "error",
        "workspace_manifest_invalid_json",
        "workspace.json",
        f"Workspace manifest is not valid JSON: {message}.",
        "Fix workspace.json syntax, then rerun workspace-risk-policy-check.",
    )
    return workspace_risk_policy_result(workspace, manifest_path, issues)


def validate_risk_policy_manifest(
    manifest: Any,
    *,
    workspace: Workspace,
    manifest_path: Path,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if not manifest_path.exists():
        add_risk_policy_issue(
            issues,
            "error",
            "workspace_manifest_missing",
            "workspace.json",
            "Workspace manifest is missing.",
            "Run init-workspace or restore workspace.json before applying risk policy.",
        )
    if manifest and not isinstance(manifest, dict):
        add_risk_policy_issue(
            issues,
            "error",
            "workspace_manifest_not_object",
            "workspace.json",
            "Workspace manifest must be a JSON object.",
            "Replace workspace.json with an object containing workspace metadata.",
        )
        manifest = {}

    quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else None
    if quality is None:
        if "quality" in manifest:
            add_risk_policy_issue(
                issues,
                "error",
                "quality_policy_not_object",
                "quality",
                "quality must be a JSON object.",
                "Use workspace-risk-policy-template and copy its quality object.",
            )
        elif manifest:
            add_risk_policy_issue(
                issues,
                "warning",
                "quality_policy_missing",
                "quality",
                "No workspace quality policy is configured; built-in defaults will be used.",
                "Run workspace-risk-policy-template and copy the quality object into workspace.json.",
            )
        quality = {}
    config = quality.get("gui_action_history") if isinstance(quality.get("gui_action_history"), dict) else None
    if config is None:
        if "gui_action_history" in quality:
            add_risk_policy_issue(
                issues,
                "error",
                "gui_action_history_policy_not_object",
                "quality.gui_action_history",
                "quality.gui_action_history must be a JSON object.",
                "Use workspace-risk-policy-template for the expected structure.",
            )
        elif quality:
            add_risk_policy_issue(
                issues,
                "warning",
                "gui_action_history_policy_missing",
                "quality.gui_action_history",
                "No GUI action history risk policy is configured; built-in defaults will be used.",
                "Copy quality.gui_action_history from workspace-risk-policy-template.",
            )
        config = {}
    validate_gui_action_history_policy_config(issues, config, "quality.gui_action_history")
    auto_repair = manifest.get("auto_repair") if isinstance(manifest.get("auto_repair"), dict) else None
    if auto_repair is None:
        if "auto_repair" in manifest:
            add_risk_policy_issue(
                issues,
                "error",
                "auto_repair_policy_not_object",
                "auto_repair",
                "auto_repair must be a JSON object.",
                "Use workspace-risk-policy-template for the expected auto_repair structure.",
            )
        elif manifest:
            add_risk_policy_issue(
                issues,
                "warning",
                "auto_repair_policy_missing",
                "auto_repair",
                "No auto_repair policy is configured; built-in defaults will be used.",
                "Copy auto_repair from workspace-risk-policy-template.",
            )
        auto_repair = {}
    validate_auto_repair_policy_config(issues, auto_repair, "auto_repair")
    return workspace_risk_policy_result(workspace, manifest_path, issues)


def workspace_risk_policy_result(workspace: Workspace, manifest_path: Path, issues: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [issue for issue in issues if issue["level"] == "error"]
    warnings = [issue for issue in issues if issue["level"] == "warning"]
    return {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "manifest_path": str(manifest_path),
        "ok": not errors,
        "status": "error" if errors else "warning" if warnings else "ok",
        "error_count": len(errors),
        "warning_count": len(warnings),
        "issues": issues,
        "supported_profiles": list(WORKSPACE_RISK_POLICY_PROFILES),
        "supported_attention_trend_directions": list(WORKSPACE_RISK_ATTENTION_TREND_DIRECTIONS),
        "supported_repair_risk_levels": list(WORKSPACE_REPAIR_RISK_LEVELS),
    }


def validate_auto_repair_policy_config(issues: list[dict[str, Any]], config: dict[str, Any], path: str) -> None:
    validate_risk_float(issues, config, "min_confidence", path, minimum=0.0, maximum=1.0)
    if "allow_force" in config and not isinstance(config.get("allow_force"), bool):
        add_risk_policy_issue(
            issues,
            "error",
            "auto_repair_allow_force_invalid",
            f"{path}.allow_force",
            "allow_force must be a boolean.",
            "Set allow_force to true or false.",
        )
    if "max_risk_level" in config:
        value = config.get("max_risk_level")
        if not isinstance(value, str) or value not in WORKSPACE_REPAIR_RISK_LEVELS:
            add_risk_policy_issue(
                issues,
                "error",
                "auto_repair_max_risk_level_invalid",
                f"{path}.max_risk_level",
                "max_risk_level must be one of the supported repair risk levels.",
                "Use one of: " + ", ".join(WORKSPACE_REPAIR_RISK_LEVELS) + ".",
            )


def validate_gui_action_history_policy_config(issues: list[dict[str, Any]], config: dict[str, Any], path: str) -> None:
    validate_risk_float(issues, config, "error_rate_threshold", path, minimum=0.0, maximum=1.0)
    validate_risk_int(issues, config, "history_limit", path, minimum=1)
    validate_risk_int(issues, config, "limit", path, minimum=1)
    validate_risk_int(issues, config, "failed_action_limit", path, minimum=0)
    profiles = config.get("profiles")
    if "profiles" in config and not isinstance(profiles, dict):
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_profiles_not_object",
            f"{path}.profiles",
            "profiles must be a JSON object.",
            "Use profiles.planner, profiles.local, and profiles.ci objects.",
        )
    elif isinstance(profiles, dict):
        for profile, profile_config in profiles.items():
            profile_path = f"{path}.profiles.{profile}"
            if profile not in WORKSPACE_RISK_POLICY_PROFILES:
                add_risk_policy_issue(
                    issues,
                    "warning",
                    "risk_policy_unknown_profile",
                    profile_path,
                    f"Unknown profile '{profile}' will not be consumed by current quality gates.",
                    "Use planner, local, or ci profile names.",
                )
            if not isinstance(profile_config, dict):
                add_risk_policy_issue(
                    issues,
                    "error",
                    "risk_policy_profile_not_object",
                    profile_path,
                    "Profile override must be a JSON object.",
                    "Replace the profile value with threshold fields.",
                )
                continue
            validate_risk_float(issues, profile_config, "error_rate_threshold", profile_path, minimum=0.0, maximum=1.0)
            validate_risk_int(issues, profile_config, "history_limit", profile_path, minimum=1)
            validate_risk_int(issues, profile_config, "limit", profile_path, minimum=1)
            validate_risk_int(issues, profile_config, "failed_action_limit", profile_path, minimum=0)
    health = config.get("health")
    if "health" in config and not isinstance(health, dict):
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_health_not_object",
            f"{path}.health",
            "health must be a JSON object.",
            "Use health.attention_trend_directions as a list of trend direction strings.",
        )
    elif isinstance(health, dict):
        validate_attention_trend_directions(issues, health, f"{path}.health")


def validate_attention_trend_directions(issues: list[dict[str, Any]], config: dict[str, Any], path: str) -> None:
    directions = config.get("attention_trend_directions")
    if "attention_trend_directions" not in config:
        return
    if not isinstance(directions, list):
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_attention_trends_not_list",
            f"{path}.attention_trend_directions",
            "attention_trend_directions must be a list.",
            "Use values such as ['worsening'] or ['worsening', 'mixed'].",
        )
        return
    if not directions:
        add_risk_policy_issue(
            issues,
            "warning",
            "risk_policy_attention_trends_empty",
            f"{path}.attention_trend_directions",
            "No risk trend direction will trigger dashboard attention.",
            "Keep ['worsening'] unless the workspace deliberately disables trend health attention.",
        )
        return
    supported = set(WORKSPACE_RISK_ATTENTION_TREND_DIRECTIONS)
    seen: set[str] = set()
    for index, item in enumerate(directions):
        item_path = f"{path}.attention_trend_directions[{index}]"
        if not isinstance(item, str) or not item.strip():
            add_risk_policy_issue(
                issues,
                "error",
                "risk_policy_attention_trend_invalid",
                item_path,
                "Each attention trend direction must be a non-empty string.",
                "Use one of the supported trend direction names.",
            )
            continue
        direction = item.strip()
        if direction in seen:
            add_risk_policy_issue(
                issues,
                "warning",
                "risk_policy_attention_trend_duplicate",
                item_path,
                f"Duplicate attention trend direction '{direction}'.",
                "Keep each direction only once.",
            )
        seen.add(direction)
        if direction not in supported:
            add_risk_policy_issue(
                issues,
                "error",
                "risk_policy_attention_trend_unsupported",
                item_path,
                f"Unsupported attention trend direction '{direction}'.",
                "Use one of: " + ", ".join(WORKSPACE_RISK_ATTENTION_TREND_DIRECTIONS) + ".",
            )


def validate_risk_float(
    issues: list[dict[str, Any]],
    config: dict[str, Any],
    key: str,
    path: str,
    *,
    minimum: float,
    maximum: float,
) -> None:
    if key not in config:
        return
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_float_invalid",
            f"{path}.{key}",
            f"{key} must be a number between {minimum:g} and {maximum:g}.",
            f"Set {key} to a numeric value such as 0.2.",
        )
        return
    if float(value) < minimum or float(value) > maximum:
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_float_out_of_range",
            f"{path}.{key}",
            f"{key} must be between {minimum:g} and {maximum:g}.",
            f"Set {key} within the supported range.",
        )


def validate_risk_int(
    issues: list[dict[str, Any]],
    config: dict[str, Any],
    key: str,
    path: str,
    *,
    minimum: int,
) -> None:
    if key not in config:
        return
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, int):
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_int_invalid",
            f"{path}.{key}",
            f"{key} must be an integer greater than or equal to {minimum}.",
            f"Set {key} to an integer value.",
        )
        return
    if value < minimum:
        add_risk_policy_issue(
            issues,
            "error",
            "risk_policy_int_out_of_range",
            f"{path}.{key}",
            f"{key} must be greater than or equal to {minimum}.",
            f"Increase {key} to at least {minimum}.",
        )


def add_risk_policy_issue(
    issues: list[dict[str, Any]],
    level: str,
    code: str,
    path: str,
    message: str,
    suggestion: str,
) -> None:
    issues.append(
        {
            "level": level,
            "code": code,
            "path": path,
            "message": message,
            "suggestion": suggestion,
        }
    )


def list_workspace_files(root: Path, *, suffixes: set[str] | None = None) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    files = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if suffixes is not None and path.suffix.lower() not in suffixes:
            continue
        files.append(
            {
                "name": path.name,
                "relative_path": path.relative_to(root.parent).as_posix(),
                "extension": path.suffix.lower(),
                "size_bytes": path.stat().st_size,
            }
        )
    return files


def read_workflow_name(path: Path) -> str:
    try:
        return parse_workflow_file(path).name
    except Exception:
        return path.stem
