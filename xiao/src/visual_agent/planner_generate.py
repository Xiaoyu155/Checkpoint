from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Any

from .model_credentials import (
    DEFAULT_MIMO_MODEL,
    build_auth_headers,
    build_model_api_probe_plan,
    compact_probe_response,
    load_provider_secret,
    redact_error_text,
)
from .models import to_jsonable
from .planner import check_planner_draft
from .preflight import run_preflight
from .workflow import Workflow, workflow_from_dict
from .workflow_diff import workflow_diff_to_markdown, workflow_text_diff as shared_workflow_text_diff
from .workspace import Workspace, planner_context


def build_planner_draft_prompt(workspace: Workspace, instruction: str, *, run_limit: int = 5) -> str:
    context = planner_context(workspace, run_limit=run_limit)
    compact = {
        "workspace": context.get("workspace"),
        "capabilities": [
            {
                "name": item.get("name"),
                "risk_level": item.get("risk_level"),
                "input_schema": item.get("input_schema"),
            }
            for item in context.get("capabilities", [])
            if isinstance(item, dict)
        ],
        "workflows": context.get("workflows"),
        "fixtures": context.get("fixtures"),
        "reports": context.get("reports"),
        "queue": context.get("queue"),
    }
    return (
        "You generate safe Checkpoint workflow YAML drafts.\n"
        "Return only YAML, no markdown fences and no explanation.\n"
        "Rules:\n"
        "- schema_version must be 1.\n"
        "- min_runtime_version must be \"0.1.0\".\n"
        "- Include at least one observe_* step and one assertion step.\n"
        "- Prefer observe_html or observe_fixture when workspace assets are available.\n"
        "- Do not use high-risk actions such as save_storage_state.\n"
        "- Keep mutating actions dry-run compatible; never include credentials or secrets.\n"
        "- Paths must stay inside the workspace.\n\n"
        f"User instruction:\n{instruction}\n\n"
        "Planner-safe workspace context JSON:\n"
        f"{json.dumps(to_jsonable(compact), ensure_ascii=False, indent=2)}\n"
    )


def generate_planner_draft(
    workspace: Workspace,
    instruction: str,
    *,
    source: str | Path | None = None,
    preferred_provider: str | None = None,
    model: str | None = None,
    timeout_seconds: float = 30.0,
    max_completion_tokens: int = 1200,
    execute: bool = False,
) -> dict[str, Any]:
    prompt = build_planner_draft_prompt(workspace, instruction)
    plan = build_model_api_probe_plan(source=source, preferred_provider=preferred_provider, model=model)
    base = {
        "schema_version": 1,
        "workspace_root": str(workspace.root),
        "instruction": instruction,
        "provider": plan.get("provider"),
        "selected_provider": plan.get("selected_provider"),
        "model": plan.get("probe", {}).get("model") if isinstance(plan.get("probe"), dict) else model,
        "prompt_preview": prompt[:800],
        "redacted": True,
    }
    if not execute:
        return {**base, "status": "planned", "executed": False, "api_plan": plan}
    if not plan["ready"]:
        return {**base, "status": "blocked", "executed": False, "api_plan": plan, "blockers": plan["blockers"]}
    secret = load_provider_secret(Path(str(plan["source"])), str(plan["selected_provider"] or ""))
    if not secret:
        return {**base, "status": "blocked", "executed": False, "blockers": ["provider_secret_unreadable"], "api_plan": plan}
    api = plan["probe"]
    url = str(api["base_url"]).rstrip("/") + "/" + str(api["endpoint"]).lstrip("/")
    body = {
        "model": str(model or api.get("model") or DEFAULT_MIMO_MODEL),
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": max(128, int(max_completion_tokens)),
        "temperature": 0,
        "stream": False,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **build_auth_headers(str(plan["selected_provider"] or ""), secret)},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {
            **base,
            "status": "error",
            "executed": True,
            "error": {"type": exc.__class__.__name__, "message": redact_error_text(str(exc))},
        }
    text = response_text_from_chat_payload(payload)
    parsed = parse_planner_yaml(text)
    result = {
        **base,
        "status": "generated",
        "executed": True,
        "api_response": compact_probe_response(payload),
        "draft_text": text,
        "parse_status": parsed["status"],
        "parse_error": parsed.get("error"),
    }
    workflow = parsed.get("workflow")
    if isinstance(workflow, Workflow):
        check = check_planner_draft(workflow, workspace=workspace)
        result.update(
            {
                "workflow": workflow_to_dict(workflow),
                "check": to_jsonable(check),
                "status": "valid" if check.valid else "invalid",
            }
        )
    result["recovery_suggestions"] = planner_draft_recovery_suggestions(result)
    return result


def save_planner_draft_result(
    workspace: Workspace,
    result: dict[str, Any],
    save_as: str,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    prepared = prepare_planner_draft_save(workspace, result, save_as)
    save = prepared["save"]
    path = prepared.get("path")
    text = prepared.get("text")
    if save.get("status") == "blocked":
        return {**result, "save": save}
    if not isinstance(path, Path) or not isinstance(text, str):
        return {**result, "save": {**save, "status": "blocked", "reason": "save_prepare_failed"}}
    if path.exists() and not overwrite:
        return {
            **result,
            "save": {
                **save,
                "status": "blocked",
                "reason": "target_exists",
            },
        }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    preflight = planner_preflight_summary(parse_planner_yaml(text).get("workflow"))
    return {
        **result,
        "preflight": preflight,
        "save": {
            **save,
            "requested": True,
            "status": "saved",
            "reason": "",
            "overwrite": bool(overwrite),
        },
    }


def preview_planner_draft_save(workspace: Workspace, result: dict[str, Any], save_as: str) -> dict[str, Any]:
    prepared = prepare_planner_draft_save(workspace, result, save_as)
    save = prepared["save"]
    if save.get("status") == "blocked":
        return {**result, "save": save}
    return {
        **result,
        "save": {
            **save,
            "requested": True,
            "status": "previewed",
            "reason": "",
        },
    }


def prepare_planner_draft_save(workspace: Workspace, result: dict[str, Any], save_as: str) -> dict[str, Any]:
    save = {"requested": True, "status": "blocked", "path": None, "reason": ""}
    if result.get("status") != "valid":
        return {"save": {**save, "reason": "draft_not_valid"}}
    workflow = result.get("workflow")
    if not isinstance(workflow, dict):
        return {"save": {**save, "reason": "missing_workflow"}}

    path_check = planner_draft_save_path(workspace, save_as)
    if path_check.get("status") != "ok":
        return {"save": {**save, "reason": path_check.get("reason") or "invalid_path"}}
    path = path_check["path"]
    text = workflow_dict_to_yaml(workflow)
    relative = path.relative_to(workspace.root).as_posix()
    return {
        "path": path,
        "text": text,
        "save": {
            "requested": True,
            "status": "ready",
            "path": relative,
            "reason": "",
            "target_exists": path.exists(),
            "diff": workflow_text_diff(path, text, relative_path=relative),
        },
    }


def planner_draft_save_path(workspace: Workspace, save_as: str) -> dict[str, Any]:
    raw_text = str(save_as or "").strip()
    if not raw_text:
        return {"status": "error", "reason": "missing_save_as"}
    raw = Path(raw_text)
    if raw.is_absolute():
        return {"status": "error", "reason": "absolute_path_not_allowed"}
    if raw.suffix.lower() not in {"", ".yaml", ".yml"}:
        return {"status": "error", "reason": "unsupported_extension"}
    if raw.suffix == "":
        raw = raw.with_suffix(".yaml")
    root = workspace.workflows_dir.resolve()
    path = (root / raw).resolve()
    if not path.is_relative_to(root):
        return {"status": "error", "reason": "path_outside_workflows"}
    return {"status": "ok", "path": path}


def planner_preflight_summary(workflow: Any) -> dict[str, Any] | None:
    if not isinstance(workflow, Workflow):
        return None
    preflight = to_jsonable(run_preflight(workflow))
    missing = preflight.get("missing_required_capabilities") if isinstance(preflight.get("missing_required_capabilities"), list) else []
    unavailable = preflight.get("unavailable_used_capabilities") if isinstance(preflight.get("unavailable_used_capabilities"), list) else []
    warnings = preflight.get("warnings") if isinstance(preflight.get("warnings"), list) else []
    preflight["missing_required_count"] = len(missing)
    preflight["unavailable_used_count"] = len(unavailable)
    preflight["warning_count"] = len(warnings)
    return preflight


def workflow_text_diff(path: Path, proposed_text: str, *, relative_path: str) -> str:
    return shared_workflow_text_diff(path, proposed_text, relative_path=relative_path)


def workflow_dict_to_yaml(workflow: dict[str, Any]) -> str:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for YAML workflows. Run: pip install PyYAML") from exc
    keys = ("schema_version", "min_runtime_version", "name", "version", "steps")
    ordered = {key: workflow[key] for key in keys if key in workflow}
    for key, value in workflow.items():
        ordered.setdefault(key, value)
    return yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False)


def response_text_from_chat_payload(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    return str(message.get("content") or "").strip()


def parse_planner_yaml(text: str) -> dict[str, Any]:
    body = strip_markdown_fence(text)
    try:
        import yaml

        payload = yaml.safe_load(body)
        if not isinstance(payload, dict):
            raise ValueError("Planner response must be a YAML object.")
        workflow = workflow_from_dict(normalize_planner_workflow_payload(payload))
        return {"status": "success", "workflow": workflow}
    except Exception as exc:
        return {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"}


def normalize_planner_workflow_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    raw_steps = normalized.get("steps")
    if not isinstance(raw_steps, list):
        return normalized
    steps = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            steps.append(raw)
            continue
        step = dict(raw)
        params = step.pop("params", None)
        if not isinstance(params, dict):
            params = step.pop("input", None)
        if isinstance(params, dict):
            merged = dict(params)
            for key, value in step.items():
                if key not in {"name"}:
                    merged.setdefault(key, value)
            step = merged
        if "id" not in step and "name" in raw:
            step["id"] = safe_step_id(str(raw.get("name") or f"step_{index + 1}"))
        steps.append(step)
    normalized["steps"] = steps
    return normalized


def safe_step_id(value: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z_]+", "_", value.strip()).strip("_")
    if not safe:
        return "step"
    if safe[0].isdigit():
        return f"step_{safe}"
    return safe


def strip_markdown_fence(text: str) -> str:
    stripped = text.strip()
    match = re.match(r"^```(?:yaml|yml)?\s*(.*?)\s*```$", stripped, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else stripped


def workflow_to_dict(workflow: Workflow) -> dict[str, Any]:
    return {
        "schema_version": workflow.schema_version,
        "min_runtime_version": workflow.min_runtime_version,
        "name": workflow.name,
        "version": workflow.version,
        "steps": [{"id": step.id, "action": step.action, **step.params} for step in workflow.steps],
    }


def planner_draft_recovery_suggestions(result: dict[str, Any]) -> list[str]:
    suggestions: list[str] = []
    parse_status = str(result.get("parse_status") or "")
    if parse_status == "error":
        suggestions.append(
            "Regenerate the draft with YAML-only output and a top-level workflow object containing schema_version, min_runtime_version, name, version, and steps."
        )
        draft_text = str(result.get("draft_text") or "").strip()
        if not draft_text:
            suggestions.append("Check the provider response shape; the chat message content was empty.")
        parse_error = str(result.get("parse_error") or "")
        if "ScannerError" in parse_error or "ParserError" in parse_error:
            suggestions.append("Fix YAML indentation, quoting, and list markers before retrying the planner check.")
        if "must be a YAML object" in parse_error:
            suggestions.append("Return a YAML mapping instead of a list, string, or explanatory prose.")

    check = result.get("check") if isinstance(result.get("check"), dict) else {}
    issues = check.get("issues") if isinstance(check.get("issues"), list) else []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        code = str(issue.get("code") or "")
        if code == "high_risk_blocked":
            suggestions.append("Remove high-risk actions from the draft; generate observe/assert dry-run steps and leave credential or state-changing work for explicit human approval.")
        elif code == "capability_not_planner_visible":
            suggestions.append("Replace unsupported actions with planner-visible atomic capabilities such as observe_html, observe_fixture, wait_for, assert_text, or assert_file_exists.")
        elif code == "path_outside_workspace":
            suggestions.append("Change file paths to workspace-relative paths under workflows, fixtures, reports, or other workspace-owned directories.")
        elif code == "workflow_validation":
            suggestions.append("Fix the workflow schema: every step needs a stable id, an action, and parameters matching that action.")
        elif code == "missing_observation":
            suggestions.append("Add an observe_* step before assertions so the draft has fresh state to verify.")
        elif code == "missing_assertion":
            suggestions.append("Add an assertion step such as assert_text or assert_file_exists so the draft has an explicit success condition.")
        elif code == "dry_run_required":
            suggestions.append("Keep medium-risk steps in dry-run mode until the workflow is reviewed and explicitly approved.")

    if result.get("status") == "invalid" and not suggestions:
        suggestions.append("Review the planner check issues, regenerate the draft, and keep it within planner-visible dry-run capabilities.")
    return dedupe_text(suggestions)


def dedupe_text(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def planner_draft_result_to_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Planner Draft Generation",
        "",
        f"- Status: `{result.get('status') or 'unknown'}`",
        f"- Executed: `{bool(result.get('executed'))}`",
        f"- Provider: `{result.get('selected_provider') or result.get('provider') or 'none'}`",
        f"- Model: `{result.get('model') or ''}`",
        f"- Parse status: `{result.get('parse_status') or ''}`",
    ]
    check = result.get("check") if isinstance(result.get("check"), dict) else {}
    if check:
        lines.append(f"- Planner check valid: `{bool(check.get('valid'))}`")
        issues = check.get("issues") if isinstance(check.get("issues"), list) else []
        lines.append(f"- Issues: {len(issues)}")
    if result.get("blockers"):
        lines.append("- Blockers: " + ", ".join(f"`{item}`" for item in result.get("blockers", [])))
    if result.get("parse_error"):
        lines.append(f"- Parse error: {result.get('parse_error')}")
    suggestions = result.get("recovery_suggestions") if isinstance(result.get("recovery_suggestions"), list) else []
    if suggestions:
        lines.append(f"- Recovery suggestions: {len(suggestions)}")
    save = result.get("save") if isinstance(result.get("save"), dict) else {}
    if save:
        lines.append(f"- Save status: `{save.get('status') or ''}`")
        if save.get("path"):
            lines.append(f"- Saved path: `{save.get('path')}`")
        if save.get("reason"):
            lines.append(f"- Save reason: `{save.get('reason')}`")
    preflight = result.get("preflight") if isinstance(result.get("preflight"), dict) else {}
    if preflight:
        lines.append(f"- Preflight OK: `{bool(preflight.get('ok'))}`")
        lines.append(f"- Preflight warnings: {int(preflight.get('warning_count') or 0)}")
    lines.extend(["", "## Draft", "", "```yaml", str(result.get("draft_text") or "").strip(), "```", ""])
    if check and check.get("issues"):
        lines.extend(["## Check Issues", "", "| level | code | step | message |", "| --- | --- | --- | --- |"])
        for issue in check.get("issues", []):
            if not isinstance(issue, dict):
                continue
            lines.append(
                "| "
                + " | ".join(
                    markdown_cell(issue.get(key))
                    for key in ("level", "code", "step_id", "message")
                )
                + " |"
            )
        lines.append("")
    if suggestions:
        lines.extend(["## Recovery Suggestions", ""])
        lines.extend(f"- {markdown_cell(item)}" for item in suggestions)
        lines.append("")
    if preflight:
        lines.extend(["## Preflight", "", f"- OK: `{bool(preflight.get('ok'))}`"])
        lines.append(f"- Missing required capabilities: {int(preflight.get('missing_required_count') or 0)}")
        lines.append(f"- Unavailable used capabilities: {int(preflight.get('unavailable_used_count') or 0)}")
        warnings = preflight.get("warnings") if isinstance(preflight.get("warnings"), list) else []
        if warnings:
            lines.extend(["", "### Warnings", ""])
            lines.extend(f"- {markdown_cell(item)}" for item in warnings)
        lines.append("")
    if save and save.get("diff"):
        lines.append(workflow_diff_to_markdown(str(save.get("diff") or "")).rstrip())
    return "\n".join(lines)


def markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()

