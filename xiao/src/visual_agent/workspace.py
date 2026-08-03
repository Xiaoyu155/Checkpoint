from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, replace
from os import chdir
from pathlib import Path
from typing import Any

from .capabilities import build_atomic_capability_manifest
from .locks import RunLock, lock_to_dict, queue_to_dict
from .models import to_jsonable
from .preflight import run_preflight
from .resources import bundled_path
from .reports import (
    RunSummary,
    list_run_summaries,
)
from .validation import ValidationResult, validate_workflow, validate_workflow_file
from .workflow import Workflow, WorkflowRunResult, WorkflowRuntime, parse_workflow_file
from .workspace_reports import (  # noqa: F401
    WorkspaceReportExport,
    build_workspace_report_index,
    export_workspace_run_report,
    list_workspace_reports,
    load_workspace_report_index,
    load_workspace_report_tags,
    report_index_entry,
    save_workspace_report_tags,
    tag_workspace_report,
    filter_workspace_report_index_for_access,
    workspace_report_access_payload,
    workspace_report_tags_path,
    write_workspace_report_index,
)
from .workspace_regression import (  # noqa: F401
    RegressionFixtureExport,
    RegressionPromotion,
    RegressionTestRun,
    export_regression_fixture,
    list_regression_tests,
    promote_regression_fixture,
    run_workspace_regression_tests,
    safe_identifier,
    write_regression_tests_index,
)
from .workspace_risk_policy import (  # noqa: F401
    WORKSPACE_DIRS,
    build_workspace_risk_policy_apply_plan,
    build_workspace_risk_policy_template,
    load_workspace_auto_repair_policy,
    load_workspace_gui_action_history_risk_config,
    load_workspace_manifest,
    validate_workspace_risk_policy,
)


IGNORED_WORKFLOW_DIR_NAMES = {".cloud_inline"}


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
    quality_score: float | None = None
    published_at: float | None = None
    published_url: str = ""


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
    _ensure_gitignored(workspace.root)
    return workspace


# Everything under the workspace is generated runtime state — gitignore all of
# it so DevPacer's churn never clutters git status and merge_worktree_branch
# never hits "untracked files would be overwritten." If users want custom
# workflows or fixtures to survive across branches they should store them in
# the project proper (e.g. tests/fixtures/).
_GITIGNORE_RUNTIME = ("",)


def _ensure_gitignored(workspace_root: Path) -> None:
    """Add DevPacer's workspace paths to the project .gitignore.

    Best-effort only: the workspace must live under a git repo. All workspace
    content is gitignored so worker branches never commit DevPacer internals
    that would block merge_worktree_branch with "untracked files overwritten."
    """
    project_root = workspace_root.parent
    if not (project_root / ".git").exists():
        return
    try:
        base = workspace_root.relative_to(project_root).as_posix().rstrip("/")
    except ValueError:
        return
    if not base:
        return
    entries = [f"{base}/{item}" if item else f"{base}/" for item in _GITIGNORE_RUNTIME]
    gitignore = project_root / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        present = {item.strip().rstrip("/") for item in existing.splitlines()}
        if base in present:
            return
        missing = [entry for entry in entries if entry.rstrip("/") not in present]
        if not missing:
            return
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        with gitignore.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}# DevPacer / Checkpoint generated runtime files\n" + "\n".join(missing) + "\n")
    except OSError:
        return


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


def workspace_framework_hint(workspace: Workspace) -> str | None:
    try:
        from .preflight import detect_project_type
    except Exception:
        detect_project_type = None  # type: ignore[assignment]
    manifest_path = workspace.root / "workspace.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = None
    if not isinstance(manifest, dict):
        manifest = None
    if isinstance(manifest, dict):
        value = manifest.get("framework_hint")
        if value:
            return str(value)
    if detect_project_type is not None:
        try:
            return detect_project_type(workspace.root)
        except Exception:
            return None
    return None


def copy_demo_assets(workspace: Workspace, *, overwrite: bool = False) -> None:
    examples_root = bundled_path("examples")
    copies = [
        (examples_root / "local_html_form_workflow.yaml", workspace.workflows_dir / "local_html_form_workflow.yaml"),
        (examples_root / "browser_form_workflow.yaml", workspace.workflows_dir / "browser_form_workflow.yaml"),
        (
            examples_root / "workflows" / "checkout" / "checkout_verification.yaml",
            workspace.workflows_dir / "checkout_verification.yaml",
        ),
        (
            examples_root / "workflows" / "pacer" / "pacer_workbench_static_acceptance.yaml",
            workspace.workflows_dir / "pacer_workbench_static_acceptance.yaml",
        ),
        (
            examples_root / "workflows" / "checkout" / "pacer_gateway_billing_acceptance.yaml",
            workspace.workflows_dir / "pacer_gateway_billing_acceptance.yaml",
        ),
        (examples_root / "inputs" / "demo_login.json", workspace.inputs_dir / "demo_login.json"),
        (examples_root / "web" / "login_demo.html", workspace.fixtures_dir / "login_demo.html"),
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

    browser_workflow_path = workspace.workflows_dir / "browser_form_workflow.yaml"
    if browser_workflow_path.exists():
        text = browser_workflow_path.read_text(encoding="utf-8")
        text = text.replace("examples/web/login_demo.html", "fixtures/login_demo.html")
        browser_workflow_path.write_text(text, encoding="utf-8")

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
        try:
            relative_parts = path.relative_to(workspace.workflows_dir).parts
        except ValueError:
            relative_parts = path.parts
        if any(part in IGNORED_WORKFLOW_DIR_NAMES for part in relative_parts[:-1]):
            continue
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
                quality_score=metadata.get("quality_score"),
                published_at=metadata.get("published_at"),
                published_url=str(metadata.get("published_url") or ""),
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
            quality_score=metadata.get("quality_score"),
            published_at=metadata.get("published_at"),
            published_url=str(metadata.get("published_url") or ""),
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
    from_step: str | None = None,
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
            workspace_root=workspace.root,
            resume_from=resume_from,
            from_step=from_step,
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
        try:
            from .telemetry import record_run
            from .visual_status import append_run_history, write_status_file

            write_status_file(workspace.project_root, result)
            append_run_history(workspace.root, workflow, result)
            record_run(workspace.root, workflow, result, project_type=workspace_framework_hint(workspace) or None)
        except Exception:
            pass
        return result
    finally:
        chdir(previous_cwd)
        if outer_lock is not None:
            outer_lock.release()


def workspace_run_summaries(workspace: Workspace, *, limit: int = 20) -> tuple[RunSummary, ...]:
    return list_run_summaries(workspace.runs_dir, limit=limit)


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
