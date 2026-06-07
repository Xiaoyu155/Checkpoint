from __future__ import annotations

import json
import shutil
from pathlib import Path
from time import time
from typing import Any

from .console import build_report_detail
from .repair_history import append_repair_history
from .security import scrub_secrets
from .workspace import (
    Workspace,
    build_workspace_report_index,
    export_regression_fixture,
    find_workflow,
    load_workspace_auto_repair_policy,
    load_workspace_inputs,
    promote_regression_fixture,
    run_workspace_regression_tests,
    run_workspace_workflow,
)
from .workflow import parse_workflow_file
from .workflow_diff import workflow_text_diff


DEFAULT_REPAIR_MODEL = "claude-haiku-4-5-20251001"
MAX_EVIDENCE_CHARS = 12000
REPAIR_RISK_ORDER = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
REPAIR_SYSTEM_PROMPT = """You are repairing a Visual Agent workflow or the application code it verifies.

Return concise JSON with this shape:
{
  "root_cause": "...",
  "classification": "workflow_bug|app_bug|timing|selector_drift|environment|unknown",
  "recommended_fix": "...",
  "workflow_patch": "unified diff or empty string",
  "app_patch_hint": "where to inspect or empty string",
  "confidence": 0.0,
  "candidates": [
    {
      "id": "model_workflow_patch|model_app_patch_hint|manual_investigation",
      "kind": "workflow_patch|app_patch_hint|manual_review",
      "recommended_fix": "...",
      "confidence": 0.0
    }
  ]
}

Rules:
1. Do not invent files, selectors, or screenshots that are not present in the evidence.
2. Prefer deterministic selectors/test ids over text-only or coordinate actions.
3. If the evidence is insufficient, say so and lower confidence.
4. Do not recommend automatic application of patches unless confidence is high.
"""


def build_failure_evidence_pack(
    workspace_root: str | Path,
    *,
    run_id: str | None = None,
    max_chars: int = MAX_EVIDENCE_CHARS,
) -> dict[str, Any]:
    workspace = Workspace(Path(workspace_root).resolve())
    selected_run_id = run_id or _latest_failed_run_id(workspace)
    if not selected_run_id:
        return {
            "schema_version": 1,
            "status": "no_failure",
            "workspace": str(workspace.root),
            "message": "No failed workflow reports found.",
        }

    detail = build_report_detail(workspace, selected_run_id)
    if not detail:
        raise FileNotFoundError(f"Run report not found: {selected_run_id}")
    failed_step = _failed_step(detail)
    workflow_name = str(detail.get("workflow_name") or "")
    workflow_source = _workflow_source_excerpt(workspace, workflow_name)
    pack = {
        "schema_version": 1,
        "status": "found",
        "workspace": str(workspace.root),
        "run_id": selected_run_id,
        "workflow": workflow_name,
        "run_profile": detail.get("run_profile"),
        "failed_step": failed_step,
        "previous_steps": _previous_steps(detail, failed_step.get("id") if failed_step else None),
        "artifacts": detail.get("artifacts") if isinstance(detail.get("artifacts"), dict) else {},
        "workflow_source": workflow_source,
        "repair_prompt": "",
    }
    pack["repair_prompt"] = build_repair_prompt(pack)
    safe_pack = scrub_secrets(pack)
    return _budget_pack(safe_pack, max_chars=max_chars)


def suggest_workflow_repair(
    workspace_root: str | Path,
    *,
    run_id: str | None = None,
    provider: str = "none",
    model: str | None = None,
    max_chars: int = MAX_EVIDENCE_CHARS,
    apply: bool = False,
    min_confidence: float = 0.75,
    verify: bool = False,
    verify_run_profile: str = "dry-run",
    inputs_file: str | None = None,
    rollback_on_fail: bool = False,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    evidence = build_failure_evidence_pack(workspace_root, run_id=run_id, max_chars=max_chars)
    if evidence.get("status") != "found":
        return evidence

    provider_name = str(provider or "none").lower()
    if provider_name in {"none", "deterministic", "local"}:
        repair = deterministic_repair_suggestion(evidence)
        plan = build_workflow_repair_plan(
            evidence,
            repair=repair,
            apply=apply,
            min_confidence=min_confidence,
            verify=verify,
            verify_run_profile=verify_run_profile,
            inputs_file=inputs_file,
            rollback_on_fail=rollback_on_fail,
            candidate_id=candidate_id,
        )
        status = "suggested"
        if plan.get("verification", {}).get("status") == "passed":
            status = "verified"
        elif plan.get("rollback", {}).get("status") == "rolled_back":
            status = "rolled_back"
        elif plan.get("rollback", {}).get("status") == "rollback_failed":
            status = "rollback_failed"
        elif plan.get("verification", {}).get("status") == "failed":
            status = "applied_unverified"
        elif plan.get("applied"):
            status = "applied"
        payload = {
            "schema_version": 1,
            "status": status,
            "source": "deterministic",
            "workspace": evidence["workspace"],
            "run_id": evidence["run_id"],
            "workflow": evidence["workflow"],
            "evidence": evidence,
            "repair": repair,
            "workflow_repair_plan": plan,
            "message": "No model provider was used. Pass provider='anthropic' or provider='openai' for model-generated repair advice.",
        }
        return _with_repair_history(payload)
    if provider_name == "anthropic":
        return _with_repair_history(_model_repair_response(evidence, provider_name, model or DEFAULT_REPAIR_MODEL, _repair_with_anthropic))
    if provider_name == "openai":
        return _with_repair_history(_model_repair_response(evidence, provider_name, model or "gpt-4.1-mini", _repair_with_openai))
    raise ValueError(f"Unsupported repair provider: {provider}")


def auto_repair_failure(
    workspace_root: str | Path,
    *,
    run_id: str | None = None,
    max_chars: int = MAX_EVIDENCE_CHARS,
    min_confidence: float = 0.75,
    verify_run_profile: str = "dry-run",
    inputs_file: str | None = None,
    candidate_id: str | None = None,
    health_limit: int = 50,
    dry_run: bool = False,
    force: bool = False,
    promote_regression: bool = False,
    overwrite_regression: bool = False,
    run_regression: bool = False,
    regression_timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    policy = load_auto_repair_policy(workspace_root)
    effective_min_confidence = max(float(min_confidence), float(policy["min_confidence"]))
    effective_force = bool(force and policy["allow_force"])
    preflight_health: dict[str, Any] | None = None
    if not dry_run:
        evidence = build_failure_evidence_pack(workspace_root, run_id=run_id, max_chars=max_chars)
        workflow = str(evidence.get("workflow") or "") or None
        preflight_health = _repair_health(workspace_root, limit=health_limit, workflow=workflow)
        gate_reason = auto_repair_policy_block_reason(preflight_health, policy=policy, force=force)
        if gate_reason and not effective_force:
            repair = suggest_workflow_repair(
                workspace_root,
                run_id=run_id,
                provider="none",
                max_chars=max_chars,
                apply=False,
                min_confidence=effective_min_confidence,
                verify=False,
                verify_run_profile=verify_run_profile,
                inputs_file=inputs_file,
                rollback_on_fail=False,
                candidate_id=candidate_id,
            )
            health = _repair_health(workspace_root, limit=health_limit, workflow=str(repair.get("workflow") or "") or None)
            return _auto_repair_payload(
                workspace_root,
                repair=repair,
                health=health,
                min_confidence=min_confidence,
                verify_run_profile=verify_run_profile,
                candidate_id=candidate_id,
                dry_run=True,
                force=effective_force,
                force_requested=force,
                promote_regression=promote_regression,
                overwrite_regression=overwrite_regression,
                run_regression=run_regression,
                regression_timeout_seconds=regression_timeout_seconds,
                regression_result=None,
                blocked=True,
                block_reason=gate_reason,
                policy=policy,
                preflight_health=preflight_health,
            )
    repair = suggest_workflow_repair(
        workspace_root,
        run_id=run_id,
        provider="none",
        max_chars=max_chars,
        apply=not dry_run,
        min_confidence=effective_min_confidence,
        verify=not dry_run,
        verify_run_profile=verify_run_profile,
        inputs_file=inputs_file,
        rollback_on_fail=not dry_run,
        candidate_id=candidate_id,
    )
    health = _repair_health(workspace_root, limit=health_limit, workflow=str(repair.get("workflow") or "") or None)
    regression_result = maybe_promote_auto_repair_regression(
        workspace_root,
        repair,
        promote=promote_regression,
        overwrite=overwrite_regression,
        run_after_promote=run_regression,
        timeout_seconds=regression_timeout_seconds,
    )
    return _auto_repair_payload(
        workspace_root,
        repair=repair,
        health=health,
        min_confidence=effective_min_confidence,
        verify_run_profile=verify_run_profile,
        candidate_id=candidate_id,
        dry_run=dry_run,
        force=effective_force,
        force_requested=force,
        promote_regression=promote_regression,
        overwrite_regression=overwrite_regression,
        run_regression=run_regression,
        regression_timeout_seconds=regression_timeout_seconds,
        regression_result=regression_result,
        blocked=False,
        policy=policy,
        preflight_health=preflight_health,
    )


def load_auto_repair_policy(workspace_root: str | Path) -> dict[str, Any]:
    return load_workspace_auto_repair_policy(workspace_root)


def auto_repair_policy_block_reason(health: dict[str, Any], *, policy: dict[str, Any], force: bool) -> str | None:
    if force and not bool(policy.get("allow_force", True)):
        return "workspace auto_repair policy does not allow force"
    risk_level = str(health.get("risk_level") or "unknown").lower()
    max_risk = str(policy.get("max_risk_level") or "medium").lower()
    if REPAIR_RISK_ORDER.get(risk_level, 0) > REPAIR_RISK_ORDER.get(max_risk, 2):
        return f"repair health risk `{risk_level}` exceeds workspace policy max_risk_level `{max_risk}`"
    return None


def _repair_health(workspace_root: str | Path, *, limit: int, workflow: str | None) -> dict[str, Any]:
    try:
        from .repair_history import build_repair_health

        return build_repair_health(workspace_root, limit=limit, workflow=workflow)
    except Exception as exc:
        return {
            "status": "unavailable",
            "reason": f"{type(exc).__name__}: {exc}",
        }


def _auto_repair_payload(
    workspace_root: str | Path,
    *,
    repair: dict[str, Any],
    health: dict[str, Any],
    min_confidence: float,
    verify_run_profile: str,
    candidate_id: str | None,
    dry_run: bool,
    force: bool,
    force_requested: bool,
    promote_regression: bool,
    overwrite_regression: bool,
    run_regression: bool,
    regression_timeout_seconds: float,
    regression_result: dict[str, Any] | None,
    blocked: bool,
    policy: dict[str, Any],
    block_reason: str | None = None,
    preflight_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "blocked" if blocked else repair.get("status"),
        "source": "auto_repair",
        "workspace": str(Path(workspace_root).resolve()),
        "run_id": repair.get("run_id"),
        "workflow": repair.get("workflow"),
        "auto_repair": {
            "provider": "none",
            "dry_run": bool(dry_run),
            "force_requested": bool(force_requested),
            "force": bool(force),
            "blocked": bool(blocked),
            "block_reason": block_reason,
            "apply": not dry_run and not blocked,
            "verify": not dry_run and not blocked,
            "rollback_on_fail": not dry_run and not blocked,
            "promote_regression": bool(promote_regression),
            "overwrite_regression": bool(overwrite_regression),
            "run_regression": bool(run_regression),
            "regression_timeout_seconds": regression_timeout_seconds,
            "min_confidence": min_confidence,
            "policy": policy,
            "verify_run_profile": verify_run_profile,
            "candidate_id": candidate_id,
        },
        "repair_result": repair,
        "regression": regression_result,
        "preflight_repair_health": preflight_health,
        "repair_health": health,
    }


def maybe_promote_auto_repair_regression(
    workspace_root: str | Path,
    repair: dict[str, Any],
    *,
    promote: bool,
    overwrite: bool,
    run_after_promote: bool,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    if not promote:
        return None
    if repair.get("status") != "verified":
        return {
            "status": "skipped",
            "reason": f"auto repair status is {repair.get('status')}; regression promotion requires verified",
        }
    run_id = str(repair.get("run_id") or "")
    if not run_id:
        return {"status": "skipped", "reason": "source failed run id is unavailable"}
    workspace = Workspace(Path(workspace_root).resolve())
    try:
        exported = export_regression_fixture(workspace, run_id, overwrite=overwrite)
        promoted = promote_regression_fixture(workspace, run_id, overwrite=overwrite)
        test_run = run_workspace_regression_tests(workspace, timeout_seconds=timeout_seconds) if run_after_promote else None
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
            "run_id": run_id,
        }
    return {
        "status": "promoted",
        "run_id": run_id,
        "fixture_path": str(exported.fixture_path),
        "test_draft_path": str(exported.test_draft_path),
        "manifest_path": str(exported.manifest_path),
        "test_path": str(promoted.test_path),
        "index_path": str(promoted.index_path),
        "test_run": regression_test_run_summary(test_run) if test_run is not None else None,
    }


def regression_test_run_summary(test_run: Any) -> dict[str, Any]:
    return {
        "status": test_run.status,
        "exit_code": test_run.exit_code,
        "run_id": test_run.run_id,
        "report_path": str(test_run.report_path),
        "markdown_path": str(test_run.markdown_path),
        "total_tests": test_run.total_tests,
        "passed_tests": test_run.passed_tests,
        "failed_tests": test_run.failed_tests,
    }


def auto_repair_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Auto Repair",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Workflow: `{payload.get('workflow') or ''}`",
        f"- Run: `{payload.get('run_id') or ''}`",
    ]
    config = payload.get("auto_repair") if isinstance(payload.get("auto_repair"), dict) else {}
    if config:
        lines.extend(
            [
                f"- Apply: `{config.get('apply')}`",
                f"- Verify: `{config.get('verify')}`",
                f"- Rollback on fail: `{config.get('rollback_on_fail')}`",
                f"- Force: `{config.get('force')}`",
                f"- Min confidence: `{config.get('min_confidence')}`",
            ]
        )
        if config.get("blocked"):
            lines.append(f"- Blocked: {config.get('block_reason')}")
    regression = payload.get("regression") if isinstance(payload.get("regression"), dict) else None
    if regression:
        lines.extend(
            [
                "",
                "## Regression Promotion",
                "",
                f"- Status: `{regression.get('status')}`",
                f"- Run: `{regression.get('run_id') or ''}`",
            ]
        )
        if regression.get("test_path"):
            lines.append(f"- Test: `{regression.get('test_path')}`")
        test_run = regression.get("test_run") if isinstance(regression.get("test_run"), dict) else None
        if test_run:
            lines.append(f"- Regression test run: `{test_run.get('status')}` ({test_run.get('passed_tests')} passed, {test_run.get('failed_tests')} failed)")
        if regression.get("reason"):
            lines.append(f"- Reason: {regression.get('reason')}")
    repair = payload.get("repair_result") if isinstance(payload.get("repair_result"), dict) else {}
    if repair:
        lines.extend(["", repair_to_markdown(repair).rstrip()])
    health = payload.get("repair_health") if isinstance(payload.get("repair_health"), dict) else {}
    if health:
        lines.extend(
            [
                "",
                "## Repair Health",
                "",
                f"- Risk: `{health.get('risk_level')}`",
                f"- Reliability: `{health.get('reliability_score')}`",
                f"- Analyzed entries: `{health.get('analyzed_entries')}`",
                f"- Recommendation: {health.get('recommendation')}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _with_repair_history(payload: dict[str, Any]) -> dict[str, Any]:
    workspace = payload.get("workspace")
    if not workspace:
        return payload
    try:
        entry = append_repair_history(Path(str(workspace)), payload)
    except Exception as exc:
        return {
            **payload,
            "history": {
                "status": "error",
                "message": f"{type(exc).__name__}: {exc}",
            },
        }
    return {
        **payload,
        "history": {
            "status": "recorded",
            "history_id": entry.get("history_id"),
        },
    }


def build_repair_prompt(evidence: dict[str, Any]) -> str:
    failed = evidence.get("failed_step") if isinstance(evidence.get("failed_step"), dict) else {}
    diagnosis = failed.get("failure_diagnosis") if isinstance(failed.get("failure_diagnosis"), dict) else {}
    lines = [
        f"Workflow `{evidence.get('workflow')}` failed in run `{evidence.get('run_id')}`.",
        f"Failed step: `{failed.get('id')}` action=`{failed.get('action')}` status=`{failed.get('status')}`.",
    ]
    if failed.get("message"):
        lines.append("Message: " + str(failed.get("message")))
    if diagnosis.get("expected"):
        lines.append("Expected: " + str(diagnosis.get("expected")))
    if diagnosis.get("actual"):
        lines.append("Actual: " + str(diagnosis.get("actual")))
    suggestions = diagnosis.get("recovery_suggestions")
    if isinstance(suggestions, list) and suggestions:
        lines.append("Existing deterministic suggestions: " + "; ".join(str(item) for item in suggestions[:4]))
    artifacts = diagnosis.get("artifacts") if isinstance(diagnosis.get("artifacts"), dict) else {}
    if artifacts.get("screenshot"):
        lines.append("Screenshot: " + str(artifacts.get("screenshot")))
    workflow_source = evidence.get("workflow_source") if isinstance(evidence.get("workflow_source"), dict) else {}
    if workflow_source.get("excerpt"):
        lines.append("Workflow YAML excerpt:\n" + str(workflow_source.get("excerpt")))
    lines.append("Classify the failure and propose the smallest safe repair. Do not apply changes.")
    return "\n".join(lines)


def deterministic_repair_suggestion(evidence: dict[str, Any]) -> dict[str, Any]:
    failed = evidence.get("failed_step") if isinstance(evidence.get("failed_step"), dict) else {}
    action = str(failed.get("action") or "")
    diagnosis = failed.get("failure_diagnosis") if isinstance(failed.get("failure_diagnosis"), dict) else {}
    suggestions = diagnosis.get("recovery_suggestions") if isinstance(diagnosis.get("recovery_suggestions"), list) else []
    classification = "unknown"
    recommended = "Review the failed step evidence and rerun after the smallest workflow or app change."
    if action in {"click", "type", "paste", "wait_for"}:
        classification = "selector_drift"
        recommended = "Check the target selector/test_id/text constraints and prefer a stable selector or test id."
    elif action in {"assert_text", "assert_no_error", "assert_product_contract"}:
        classification = "app_bug"
        recommended = "Compare expected text/state with the latest observation; update the app or assertion only after confirming intended behavior."
    elif action in {"assert_response", "request_api"}:
        classification = "app_bug"
        recommended = "Inspect the captured network event and backend route/status before changing the workflow."
    elif action in {"expect_download", "assert_file_exists"}:
        classification = "environment"
        recommended = "Check download path, file extension, minimum size, and browser download permissions."
    if suggestions:
        recommended = str(suggestions[0])
    patch_plan = propose_deterministic_workflow_patch(evidence)
    confidence = float(patch_plan.get("confidence") or (0.35 if classification != "unknown" else 0.2))
    if patch_plan.get("status") == "proposed":
        classification = str(patch_plan.get("classification") or classification)
        recommended = str(patch_plan.get("recommended_fix") or recommended)
    candidates = build_repair_candidates(
        evidence,
        patch_plan=patch_plan,
        classification=classification,
        recommended_fix=recommended,
        confidence=confidence,
    )
    return {
        "root_cause": "Model not used; deterministic evidence classification only.",
        "classification": classification,
        "recommended_fix": recommended,
        "workflow_patch": str(patch_plan.get("diff") or ""),
        "app_patch_hint": "",
        "confidence": confidence,
        "apply_supported": patch_plan.get("status") == "proposed",
        "patch_reason": patch_plan.get("reason"),
        "selected_candidate_id": "deterministic_workflow_patch" if patch_plan.get("status") == "proposed" else "manual_investigation",
        "candidates": candidates,
    }


def build_repair_candidates(
    evidence: dict[str, Any],
    *,
    patch_plan: dict[str, Any],
    classification: str,
    recommended_fix: str,
    confidence: float,
) -> list[dict[str, Any]]:
    failed = evidence.get("failed_step") if isinstance(evidence.get("failed_step"), dict) else {}
    candidates: list[dict[str, Any]] = []
    if patch_plan.get("status") == "proposed":
        candidates.append(
            {
                "id": "deterministic_workflow_patch",
                "kind": "workflow_patch",
                "source": "deterministic",
                "status": "proposed",
                "classification": patch_plan.get("classification") or classification,
                "confidence": patch_plan.get("confidence", confidence),
                "apply_supported": True,
                "recommended_fix": patch_plan.get("recommended_fix") or recommended_fix,
                "reason": patch_plan.get("reason"),
                "workflow_patch": patch_plan.get("diff") or "",
                "path": patch_plan.get("path"),
                "step_id": patch_plan.get("step_id"),
            }
        )
    else:
        candidates.append(
            {
                "id": "manual_investigation",
                "kind": "manual_review",
                "source": "deterministic",
                "status": patch_plan.get("status") or "unsupported",
                "classification": classification,
                "confidence": confidence,
                "apply_supported": False,
                "recommended_fix": recommended_fix,
                "reason": patch_plan.get("reason") or "No safe deterministic workflow patch was found.",
                "step_id": failed.get("id"),
            }
        )
    candidates.append(
        {
            "id": "app_behavior_check",
            "kind": "app_patch_hint",
            "source": "deterministic",
            "status": "available",
            "classification": "app_bug" if classification != "environment" else "environment",
            "confidence": 0.35 if classification != "app_bug" else min(confidence, 0.65),
            "apply_supported": False,
            "recommended_fix": "Inspect the application behavior behind the failed step before changing product code.",
            "reason": "Workflow evidence can indicate a product regression, but app code changes need repository-specific review.",
            "step_id": failed.get("id"),
        }
    )
    candidates.append(
        {
            "id": "model_review",
            "kind": "model_advice",
            "source": "optional_model",
            "status": "available",
            "classification": "unknown",
            "confidence": 0.0,
            "apply_supported": False,
            "recommended_fix": "Run repair_workflow with provider='anthropic' or provider='openai' for model-generated diagnosis.",
            "reason": "Use a model when deterministic evidence is insufficient or app code needs semantic analysis.",
            "step_id": failed.get("id"),
        }
    )
    return candidates


def build_workflow_repair_plan(
    evidence: dict[str, Any],
    *,
    repair: dict[str, Any] | None = None,
    apply: bool = False,
    min_confidence: float = 0.75,
    verify: bool = False,
    verify_run_profile: str = "dry-run",
    inputs_file: str | None = None,
    rollback_on_fail: bool = False,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    plan = propose_deterministic_workflow_patch(evidence)
    confidence = float((repair or {}).get("confidence") or plan.get("confidence") or 0.0)
    selected_candidate_id = str(candidate_id or (repair or {}).get("selected_candidate_id") or "").strip()
    if selected_candidate_id and selected_candidate_id != "deterministic_workflow_patch":
        return {
            "status": "not_applied",
            "reason": f"candidate `{selected_candidate_id}` is not automatically applicable",
            "candidate_id": selected_candidate_id,
            "confidence": confidence,
            "applied": False,
            "apply_requested": bool(apply),
            "verify_requested": bool(verify),
            "rollback_on_fail": bool(rollback_on_fail),
        }
    if plan.get("status") != "proposed":
        return {
            **plan,
            "candidate_id": selected_candidate_id or "manual_investigation",
            "applied": False,
            "apply_requested": bool(apply),
            "verify_requested": bool(verify),
            "rollback_on_fail": bool(rollback_on_fail),
        }
    plan = {
        **plan,
        "confidence": confidence,
        "candidate_id": selected_candidate_id or "deterministic_workflow_patch",
        "apply_requested": bool(apply),
        "verify_requested": bool(verify),
        "rollback_on_fail": bool(rollback_on_fail),
        "applied": False,
    }
    if not apply:
        return plan
    if confidence < min_confidence:
        return {
            **plan,
            "status": "not_applied",
            "reason": f"confidence {confidence:.2f} is below min_confidence {min_confidence:.2f}",
        }
    applied = apply_workflow_repair_plan(evidence, plan)
    merged = {**plan, **applied}
    if verify and merged.get("applied") is True:
        merged["verification"] = verify_workflow_repair(
            evidence,
            run_profile=verify_run_profile,
            inputs_file=inputs_file,
        )
        if rollback_on_fail and merged["verification"].get("status") == "failed":
            merged["rollback"] = rollback_workflow_repair_plan(evidence, merged)
    return merged


def propose_deterministic_workflow_patch(evidence: dict[str, Any]) -> dict[str, Any]:
    failed = evidence.get("failed_step") if isinstance(evidence.get("failed_step"), dict) else {}
    diagnosis = failed.get("failure_diagnosis") if isinstance(failed.get("failure_diagnosis"), dict) else {}
    if failed.get("action") in {"click", "type", "paste", "expect_download", "wait_for"}:
        selector_plan = propose_selector_drift_patch(evidence, failed=failed, diagnosis=diagnosis)
        if selector_plan.get("status") == "proposed":
            return selector_plan
    if failed.get("action") != "assert_text":
        return {"status": "unsupported", "reason": "deterministic patching currently supports assert_text only", "confidence": 0.0}
    expected = str(diagnosis.get("expected") or "")
    expected_text = expected.removeprefix("expected text:").strip()
    if not expected_text:
        return {"status": "unsupported", "reason": "failed assert_text did not include expected text", "confidence": 0.0}
    visible = _visible_text_candidates(diagnosis)
    candidate, score = _closest_visible_text(expected_text, visible)
    if not candidate or score < 0.72:
        return {
            "status": "no_patch",
            "reason": "no close visible text candidate was found",
            "confidence": round(score, 3),
            "candidate": candidate,
        }
    workflow_source = evidence.get("workflow_source") if isinstance(evidence.get("workflow_source"), dict) else {}
    path_text = workflow_source.get("path")
    if not path_text:
        return {"status": "no_patch", "reason": "workflow source path is unavailable", "confidence": round(score, 3)}
    path = Path(str(path_text)).resolve()
    failed_id = str(failed.get("id") or "")
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"status": "no_patch", "reason": f"workflow source is unreadable: {exc}", "confidence": round(score, 3)}
    proposed = _replace_step_text_value(current, step_id=failed_id, old_value=expected_text, new_value=candidate)
    if proposed is None or proposed == current:
        return {"status": "no_patch", "reason": "failed step text value could not be safely located", "confidence": round(score, 3)}
    try:
        _validate_workflow_text(path, proposed)
    except Exception as exc:
        return {"status": "no_patch", "reason": f"proposed workflow YAML is invalid: {type(exc).__name__}: {exc}", "confidence": round(score, 3)}
    relative_path = str(workflow_source.get("relative_path") or path.name)
    return {
        "status": "proposed",
        "classification": "workflow_bug",
        "reason": f"assert_text expected `{expected_text}` but visible text contains close candidate `{candidate}`",
        "recommended_fix": f"Update assert_text in step `{failed_id}` from `{expected_text}` to `{candidate}`.",
        "path": str(path),
        "relative_path": relative_path,
        "step_id": failed_id,
        "old_text": expected_text,
        "new_text": candidate,
        "confidence": round(min(0.95, max(0.0, score)), 3),
        "diff": workflow_text_diff(path, proposed, relative_path=relative_path),
        "proposed_text": proposed,
    }


def propose_selector_drift_patch(evidence: dict[str, Any], *, failed: dict[str, Any], diagnosis: dict[str, Any]) -> dict[str, Any]:
    target = diagnosis.get("target") if isinstance(diagnosis.get("target"), dict) else {}
    old_selector = str(target.get("selector") or "").strip()
    old_test_id = str(target.get("test_id") or "").strip()
    if not old_selector and not old_test_id:
        return {"status": "unsupported", "reason": "failed target has no selector/test_id to repair", "confidence": 0.0}
    candidate = _best_selector_candidate(target, diagnosis)
    if candidate is None:
        return {"status": "no_patch", "reason": "no matching DOM candidate found for failed target", "confidence": 0.0}
    new_key = "test_id" if candidate.get("test_id") else "selector"
    old_key = "test_id" if old_test_id else "selector"
    old_value = old_test_id or old_selector
    new_value = str(candidate.get("test_id") or candidate.get("selector") or "").strip()
    if not new_value or new_value == old_value:
        return {"status": "no_patch", "reason": "candidate selector is empty or unchanged", "confidence": float(candidate.get("confidence") or 0.0)}
    workflow_source = evidence.get("workflow_source") if isinstance(evidence.get("workflow_source"), dict) else {}
    path_text = workflow_source.get("path")
    if not path_text:
        return {"status": "no_patch", "reason": "workflow source path is unavailable", "confidence": float(candidate.get("confidence") or 0.0)}
    path = Path(str(path_text)).resolve()
    failed_id = str(failed.get("id") or "")
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"status": "no_patch", "reason": f"workflow source is unreadable: {exc}", "confidence": float(candidate.get("confidence") or 0.0)}
    proposed = _replace_step_target_value(
        current,
        step_id=failed_id,
        old_key=old_key,
        old_value=old_value,
        new_key=new_key,
        new_value=new_value,
    )
    if proposed is None or proposed == current:
        return {"status": "no_patch", "reason": "failed target selector could not be safely located", "confidence": float(candidate.get("confidence") or 0.0)}
    try:
        _validate_workflow_text(path, proposed)
    except Exception as exc:
        return {"status": "no_patch", "reason": f"proposed workflow YAML is invalid: {type(exc).__name__}: {exc}", "confidence": float(candidate.get("confidence") or 0.0)}
    relative_path = str(workflow_source.get("relative_path") or path.name)
    confidence = round(float(candidate.get("confidence") or 0.0), 3)
    return {
        "status": "proposed",
        "classification": "selector_drift",
        "reason": f"target {old_key} `{old_value}` did not resolve, but DOM candidate `{new_value}` matched text/role constraints",
        "recommended_fix": f"Update target {old_key} in step `{failed_id}` from `{old_value}` to {new_key} `{new_value}`.",
        "path": str(path),
        "relative_path": relative_path,
        "step_id": failed_id,
        "old_key": old_key,
        "old_text": old_value,
        "new_key": new_key,
        "new_text": new_value,
        "confidence": confidence,
        "diff": workflow_text_diff(path, proposed, relative_path=relative_path),
        "proposed_text": proposed,
    }


def apply_workflow_repair_plan(evidence: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(plan.get("path") or "")).resolve()
    workspace_root = Path(str(evidence.get("workspace") or "")).resolve()
    try:
        path.relative_to(workspace_root)
    except ValueError:
        return {"status": "not_applied", "applied": False, "reason": f"workflow path escapes workspace: {path}"}
    proposed_text = str(plan.get("proposed_text") or "")
    if not proposed_text:
        return {"status": "not_applied", "applied": False, "reason": "no proposed workflow text available"}
    _validate_workflow_text(path, proposed_text)
    backup = path.with_suffix(path.suffix + f".repair-backup-{int(time())}")
    shutil.copy2(path, backup)
    path.write_text(proposed_text.rstrip() + "\n", encoding="utf-8")
    parse_workflow_file(path)
    return {
        "status": "applied",
        "applied": True,
        "path": str(path),
        "backup_path": str(backup),
        "message": f"Workflow repair applied to {path}. Backup: {backup}",
    }


def verify_workflow_repair(
    evidence: dict[str, Any],
    *,
    run_profile: str = "dry-run",
    inputs_file: str | None = None,
) -> dict[str, Any]:
    workspace = Workspace(Path(str(evidence.get("workspace") or "")).resolve())
    workflow_name = str(evidence.get("workflow") or "")
    if not workflow_name:
        return {"status": "skipped", "reason": "workflow name is unavailable"}
    if run_profile not in {"dry-run", "supervised"}:
        return {"status": "skipped", "reason": f"unsupported verify_run_profile: {run_profile}"}
    inputs = load_workspace_inputs(workspace, None, inputs_file) if inputs_file else {}
    try:
        result = run_workspace_workflow(
            workspace,
            workflow_name,
            inputs=inputs,
            dry_run=run_profile == "dry-run",
            run_profile=run_profile,
            export_report=True,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "workflow": workflow_name,
            "run_profile": run_profile,
        }
    failed_steps = [
        {"id": step.id, "action": step.action, "message": step.message}
        for step in result.steps
        if getattr(step.status, "value", str(step.status)) == "failed"
    ]
    return {
        "status": "failed" if failed_steps else "passed",
        "workflow": workflow_name,
        "run_id": result.run_id,
        "run_profile": result.run_profile,
        "failed_steps": failed_steps,
        "report_hint": f"Use get_run_report with run_id='{result.run_id}' for full details.",
    }


def rollback_workflow_repair_plan(evidence: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(plan.get("path") or "")).resolve()
    backup = Path(str(plan.get("backup_path") or "")).resolve()
    workspace_root = Path(str(evidence.get("workspace") or "")).resolve()
    try:
        path.relative_to(workspace_root)
        backup.relative_to(workspace_root)
    except ValueError:
        return {
            "status": "rollback_failed",
            "rolled_back": False,
            "reason": f"repair path or backup escapes workspace: path={path}, backup={backup}",
        }
    if not backup.exists():
        return {"status": "rollback_failed", "rolled_back": False, "reason": f"backup not found: {backup}"}
    try:
        shutil.copy2(backup, path)
        parse_workflow_file(path)
    except Exception as exc:
        return {
            "status": "rollback_failed",
            "rolled_back": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "path": str(path),
            "backup_path": str(backup),
        }
    return {
        "status": "rolled_back",
        "rolled_back": True,
        "path": str(path),
        "backup_path": str(backup),
        "message": f"Verification failed; restored workflow from backup: {backup}",
    }


def repair_to_markdown(payload: dict[str, Any]) -> str:
    if payload.get("status") == "no_failure":
        return str(payload.get("message") or "No failed workflow reports found.") + "\n"
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else payload
    repair = payload.get("repair") if isinstance(payload.get("repair"), dict) else {}
    failed = evidence.get("failed_step") if isinstance(evidence.get("failed_step"), dict) else {}
    lines = [
        "# Workflow Repair Suggestion",
        "",
        f"- Workflow: `{evidence.get('workflow')}`",
        f"- Run: `{evidence.get('run_id')}`",
        f"- Failed step: `{failed.get('id')}` ({failed.get('action')})",
        f"- Source: `{payload.get('source') or evidence.get('source') or 'evidence'}`",
    ]
    if repair:
        lines.extend(
            [
                f"- Classification: `{repair.get('classification')}`",
                f"- Confidence: `{repair.get('confidence')}`",
                f"- Recommended fix: {repair.get('recommended_fix')}",
            ]
        )
        if repair.get("root_cause"):
            lines.append(f"- Root cause: {repair.get('root_cause')}")
        if repair.get("app_patch_hint"):
            lines.append(f"- App patch hint: {repair.get('app_patch_hint')}")
        if repair.get("workflow_patch"):
            lines.extend(["", "```diff", str(repair.get("workflow_patch")).rstrip(), "```"])
        candidates = repair.get("candidates") if isinstance(repair.get("candidates"), list) else []
        if candidates:
            lines.extend(["", "## Repair Candidates", ""])
            lines.append("| id | kind | confidence | auto apply | status |")
            lines.append("| --- | --- | --- | --- | --- |")
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            _markdown_cell(item.get("id")),
                            _markdown_cell(item.get("kind")),
                            _markdown_cell(item.get("confidence")),
                            _markdown_cell(item.get("apply_supported")),
                            _markdown_cell(item.get("status")),
                        ]
                    )
                    + " |"
                )
    plan = payload.get("workflow_repair_plan") if isinstance(payload.get("workflow_repair_plan"), dict) else {}
    if plan:
        lines.extend(["", "## Workflow Repair Plan", ""])
        lines.append(f"- Status: `{plan.get('status')}`")
        lines.append(f"- Apply requested: `{plan.get('apply_requested')}`")
        lines.append(f"- Applied: `{plan.get('applied')}`")
        lines.append(f"- Verify requested: `{plan.get('verify_requested')}`")
        lines.append(f"- Rollback on fail: `{plan.get('rollback_on_fail')}`")
        if plan.get("path"):
            lines.append(f"- Path: `{plan.get('path')}`")
        if plan.get("backup_path"):
            lines.append(f"- Backup: `{plan.get('backup_path')}`")
        if plan.get("reason"):
            lines.append(f"- Reason: {plan.get('reason')}")
        verification = plan.get("verification") if isinstance(plan.get("verification"), dict) else None
        if verification:
            lines.append(f"- Verification: `{verification.get('status')}`")
            if verification.get("run_id"):
                lines.append(f"- Verification run: `{verification.get('run_id')}`")
        rollback = plan.get("rollback") if isinstance(plan.get("rollback"), dict) else None
        if rollback:
            lines.append(f"- Rollback: `{rollback.get('status')}`")
            if rollback.get("backup_path"):
                lines.append(f"- Rollback backup: `{rollback.get('backup_path')}`")
    lines.extend(["", "## Prompt", "", "```text", str(evidence.get("repair_prompt") or "").rstrip(), "```"])
    return "\n".join(lines).rstrip() + "\n"


def _markdown_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("\n", " ")[:120]


def _latest_failed_run_id(workspace: Workspace) -> str | None:
    index = build_workspace_report_index(workspace, failed_only=True)
    entries = index.get("entries") if isinstance(index.get("entries"), list) else []
    if not entries:
        return None
    return str(entries[0].get("run_id") or "") or None


def _failed_step(detail: dict[str, Any]) -> dict[str, Any] | None:
    steps = detail.get("steps") if isinstance(detail.get("steps"), list) else []
    failure = detail.get("failure") if isinstance(detail.get("failure"), dict) else {}
    for step in steps:
        if isinstance(step, dict) and str(step.get("status") or "") == "failed":
            return _step_with_failure_detail(step, failure)
    failed_id = failure.get("failed_step")
    for step in steps:
        if isinstance(step, dict) and step.get("id") == failed_id:
            return _step_with_failure_detail(step, failure)
    return None


def _step_with_failure_detail(step: dict[str, Any], failure: dict[str, Any]) -> dict[str, Any]:
    result = dict(step)
    diagnosis = failure.get("diagnosis") if isinstance(failure.get("diagnosis"), dict) else None
    if diagnosis is not None and not isinstance(result.get("failure_diagnosis"), dict):
        result["failure_diagnosis"] = diagnosis
    for key in ("expected", "actual", "recovery_suggestions", "evidence", "artifacts"):
        if key in failure and key not in result:
            result[key] = failure[key]
    return result


def _previous_steps(detail: dict[str, Any], failed_step_id: str | None, *, limit: int = 5) -> list[dict[str, Any]]:
    steps = detail.get("steps") if isinstance(detail.get("steps"), list) else []
    previous: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if failed_step_id is not None and step.get("id") == failed_step_id:
            break
        previous.append(
            {
                "id": step.get("id"),
                "action": step.get("action"),
                "status": step.get("status"),
                "message": step.get("message"),
                "target": step.get("target"),
                "provider": step.get("provider"),
            }
        )
    return previous[-limit:]


def _workflow_source_excerpt(workspace: Workspace, workflow_name: str, *, max_lines: int = 120) -> dict[str, Any]:
    if not workflow_name:
        return {"available": False}
    try:
        ref = find_workflow(workspace, workflow_name)
    except Exception as exc:
        return {"available": False, "reason": str(exc)}
    try:
        lines = ref.path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {"available": False, "path": str(ref.path), "reason": str(exc)}
    excerpt = "\n".join(lines[:max_lines])
    return {
        "available": True,
        "path": str(ref.path),
        "relative_path": ref.relative_path,
        "truncated": len(lines) > max_lines,
        "excerpt": excerpt,
    }


def _visible_text_candidates(diagnosis: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    observation = diagnosis.get("observation") if isinstance(diagnosis.get("observation"), dict) else {}
    for item in observation.get("visible_text") if isinstance(observation.get("visible_text"), list) else []:
        text = str(item).strip()
        if text and text not in candidates:
            candidates.append(text)
    dom_excerpt = diagnosis.get("dom_excerpt") if isinstance(diagnosis.get("dom_excerpt"), list) else []
    for item in dom_excerpt:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text and text not in candidates:
            candidates.append(text)
    return candidates


def _closest_visible_text(expected: str, candidates: list[str]) -> tuple[str | None, float]:
    from difflib import SequenceMatcher

    best_text: str | None = None
    best_score = 0.0
    expected_norm = expected.strip().lower()
    for candidate in candidates:
        candidate_norm = candidate.strip().lower()
        if not candidate_norm:
            continue
        score = SequenceMatcher(None, expected_norm, candidate_norm).ratio()
        if expected_norm in candidate_norm or candidate_norm in expected_norm:
            score = max(score, min(len(expected_norm), len(candidate_norm)) / max(len(expected_norm), len(candidate_norm)))
        if score > best_score:
            best_text = candidate
            best_score = score
    return best_text, best_score


def _best_selector_candidate(target: dict[str, Any], diagnosis: dict[str, Any]) -> dict[str, Any] | None:
    dom_excerpt = diagnosis.get("dom_excerpt") if isinstance(diagnosis.get("dom_excerpt"), list) else []
    target_text = str(target.get("text") or target.get("label") or target.get("contains_text") or "").strip().lower()
    target_role = str(target.get("role") or "").strip().lower()
    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in dom_excerpt:
        if not isinstance(item, dict):
            continue
        selector = str(item.get("selector") or "").strip()
        test_id = str(item.get("test_id") or "").strip()
        if not selector and not test_id:
            continue
        text = str(item.get("text") or "").strip().lower()
        role = str(item.get("role") or "").strip().lower()
        score = 0.0
        if target_text:
            if text == target_text:
                score += 0.65
            elif target_text in text or text in target_text:
                score += 0.45
            else:
                continue
        if target_role:
            if role == target_role:
                score += 0.25
            elif target_role in role or role in target_role:
                score += 0.15
            else:
                continue
        if test_id:
            score += 0.1
        elif _is_stable_selector(selector):
            score += 0.08
        if score > best_score:
            best_score = score
            best = {**item, "confidence": min(score, 0.95)}
    if best is None or best_score < 0.75:
        return None
    return best


def _is_stable_selector(selector: str) -> bool:
    value = str(selector or "").strip()
    if not value:
        return False
    lowered = value.lower()
    if any(token in lowered for token in ("[data-testid=", "[data-test=", "[aria-label=", "[name=")):
        return True
    if value.startswith("#") and " " not in value and ">" not in value:
        return True
    return False


def _replace_step_text_value(text: str, *, step_id: str, old_value: str, new_value: str) -> str | None:
    try:
        import yaml
    except ImportError:
        return None
    lines = text.splitlines()
    step_start = None
    for index, line in enumerate(lines):
        if line.strip() == f"- id: {step_id}" or line.strip() == f"id: {step_id}":
            step_start = index
            break
    if step_start is None:
        return None
    step_end = len(lines)
    for index in range(step_start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("- id: "):
            step_end = index
            break
    for index in range(step_start, step_end):
        line = lines[index]
        stripped = line.strip()
        if not stripped.startswith("text:"):
            continue
        _, raw = line.split("text:", 1)
        try:
            current_value = yaml.safe_load(raw.strip()) if raw.strip() else ""
        except Exception:
            current_value = raw.strip().strip("\"'")
        if str(current_value) != old_value:
            continue
        indent = line[: len(line) - len(line.lstrip())]
        lines[index] = f"{indent}text: {json.dumps(new_value, ensure_ascii=False)}"
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return None


def _replace_step_target_value(
    text: str,
    *,
    step_id: str,
    old_key: str,
    old_value: str,
    new_key: str,
    new_value: str,
) -> str | None:
    try:
        import yaml
    except ImportError:
        return None
    lines = text.splitlines()
    step_start = _find_step_start(lines, step_id)
    if step_start is None:
        return None
    step_end = _find_step_end(lines, step_start)
    target_start = None
    for index in range(step_start, step_end):
        if lines[index].strip() == "target:":
            target_start = index
            break
    if target_start is None:
        return None
    target_indent = len(lines[target_start]) - len(lines[target_start].lstrip())
    target_end = step_end
    for index in range(target_start + 1, step_end):
        stripped = lines[index].strip()
        indent = len(lines[index]) - len(lines[index].lstrip())
        if stripped and indent <= target_indent:
            target_end = index
            break
    for index in range(target_start + 1, target_end):
        stripped = lines[index].strip()
        if not stripped.startswith(f"{old_key}:"):
            continue
        _, raw = lines[index].split(f"{old_key}:", 1)
        try:
            current_value = yaml.safe_load(raw.strip()) if raw.strip() else ""
        except Exception:
            current_value = raw.strip().strip("\"'")
        if str(current_value) != old_value:
            continue
        indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
        lines[index] = f"{indent}{new_key}: {json.dumps(new_value, ensure_ascii=False)}"
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return None


def _find_step_start(lines: list[str], step_id: str) -> int | None:
    for index, line in enumerate(lines):
        if line.strip() == f"- id: {step_id}" or line.strip() == f"id: {step_id}":
            return index
    return None


def _find_step_end(lines: list[str], step_start: int) -> int:
    for index in range(step_start + 1, len(lines)):
        if lines[index].strip().startswith("- id: "):
            return index
    return len(lines)


def _validate_workflow_text(original_path: Path, text: str) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / original_path.name
        candidate.write_text(text.rstrip() + "\n", encoding="utf-8")
        parse_workflow_file(candidate)


def _budget_pack(pack: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    encoded = json.dumps(pack, ensure_ascii=False, default=str)
    if len(encoded) <= max_chars:
        return {**pack, "truncated": False, "within_budget": True}
    compact = dict(pack)
    workflow_source = compact.get("workflow_source")
    if isinstance(workflow_source, dict) and workflow_source.get("excerpt"):
        excerpt = str(workflow_source["excerpt"])
        workflow_source = dict(workflow_source)
        workflow_source["excerpt"] = excerpt[:2000].rstrip() + "\n...[truncated]"
        workflow_source["truncated"] = True
        compact["workflow_source"] = workflow_source
    failed = compact.get("failed_step")
    if isinstance(failed, dict):
        diagnosis = failed.get("failure_diagnosis")
        if isinstance(diagnosis, dict):
            diagnosis = dict(diagnosis)
            diagnosis["dom_excerpt"] = diagnosis.get("dom_excerpt", [])[:5] if isinstance(diagnosis.get("dom_excerpt"), list) else []
            evidence = diagnosis.get("evidence") if isinstance(diagnosis.get("evidence"), dict) else {}
            diagnosis["evidence"] = {
                key: value
                for key, value in evidence.items()
                if key in {"ocr", "vision"} and isinstance(value, dict) and value.get("available") is True
            }
            failed = dict(failed)
            failed["failure_diagnosis"] = diagnosis
            compact["failed_step"] = failed
    compact["repair_prompt"] = build_repair_prompt(compact)
    compact["truncated"] = True
    compact["within_budget"] = len(json.dumps(compact, ensure_ascii=False, default=str)) <= max_chars
    return compact


def _model_repair_response(
    evidence: dict[str, Any],
    provider: str,
    model: str,
    callback: Any,
) -> dict[str, Any]:
    try:
        repair = callback(evidence["repair_prompt"], model=model)
    except ImportError as exc:
        return _model_unavailable(evidence, provider, model, f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        if _is_auth_configuration_error(exc):
            return _model_unavailable(evidence, provider, model, f"{type(exc).__name__}: {exc}")
        return {
            "schema_version": 1,
            "status": "error",
            "source": provider,
            "model": model,
            "workspace": evidence["workspace"],
            "run_id": evidence["run_id"],
            "workflow": evidence["workflow"],
            "message": f"repair model call failed: {type(exc).__name__}: {exc}",
            "evidence": evidence,
        }
    repair = normalize_model_repair(repair, provider=provider)
    return {
        "schema_version": 1,
        "status": "suggested",
        "source": provider,
        "model": model,
        "workspace": evidence["workspace"],
        "run_id": evidence["run_id"],
        "workflow": evidence["workflow"],
        "evidence": evidence,
        "repair": repair,
    }


def _model_unavailable(evidence: dict[str, Any], provider: str, model: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "needs_model",
        "source": provider,
        "model": model,
        "workspace": evidence["workspace"],
        "run_id": evidence["run_id"],
        "workflow": evidence["workflow"],
        "evidence": evidence,
        "repair": deterministic_repair_suggestion(evidence),
        "message": f"{provider} repair is not configured: {reason}",
    }


def normalize_model_repair(repair: dict[str, Any], *, provider: str) -> dict[str, Any]:
    normalized = {
        "root_cause": str(repair.get("root_cause") or ""),
        "classification": str(repair.get("classification") or "unknown"),
        "recommended_fix": str(repair.get("recommended_fix") or ""),
        "workflow_patch": str(repair.get("workflow_patch") or ""),
        "app_patch_hint": str(repair.get("app_patch_hint") or ""),
        "confidence": float(repair.get("confidence") or 0.0),
    }
    raw_candidates = repair.get("candidates") if isinstance(repair.get("candidates"), list) else []
    candidates = normalize_model_candidates(raw_candidates, normalized, provider=provider)
    if not candidates:
        candidates = fallback_model_candidates(normalized, provider=provider)
    selected = next((item.get("id") for item in candidates if item.get("workflow_patch")), None) or candidates[0]["id"]
    return {
        **normalized,
        "apply_supported": False,
        "selected_candidate_id": selected,
        "candidates": candidates,
    }


def normalize_model_candidates(raw_candidates: list[Any], repair: dict[str, Any], *, provider: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(raw_candidates):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "manual_review")
        candidate_id = str(item.get("id") or f"{provider}_candidate_{index + 1}")
        candidates.append(
            {
                "id": candidate_id,
                "kind": kind,
                "source": provider,
                "status": "suggested",
                "classification": str(item.get("classification") or repair.get("classification") or "unknown"),
                "confidence": float(item.get("confidence") or repair.get("confidence") or 0.0),
                "apply_supported": False,
                "recommended_fix": str(item.get("recommended_fix") or repair.get("recommended_fix") or ""),
                "reason": str(item.get("reason") or "Model-generated repair candidate; review before applying."),
                "workflow_patch": str(item.get("workflow_patch") or ""),
                "app_patch_hint": str(item.get("app_patch_hint") or ""),
            }
        )
    return candidates


def fallback_model_candidates(repair: dict[str, Any], *, provider: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if repair.get("workflow_patch"):
        candidates.append(
            {
                "id": "model_workflow_patch",
                "kind": "workflow_patch",
                "source": provider,
                "status": "suggested",
                "classification": repair.get("classification"),
                "confidence": repair.get("confidence"),
                "apply_supported": False,
                "recommended_fix": repair.get("recommended_fix") or "Review and apply the model-proposed workflow patch manually.",
                "reason": "Model-generated diffs are not applied automatically.",
                "workflow_patch": repair.get("workflow_patch"),
                "app_patch_hint": "",
            }
        )
    if repair.get("app_patch_hint"):
        candidates.append(
            {
                "id": "model_app_patch_hint",
                "kind": "app_patch_hint",
                "source": provider,
                "status": "suggested",
                "classification": repair.get("classification"),
                "confidence": repair.get("confidence"),
                "apply_supported": False,
                "recommended_fix": repair.get("recommended_fix") or repair.get("app_patch_hint"),
                "reason": "Application code changes require repository-specific review.",
                "workflow_patch": "",
                "app_patch_hint": repair.get("app_patch_hint"),
            }
        )
    if not candidates:
        candidates.append(
            {
                "id": "model_manual_investigation",
                "kind": "manual_review",
                "source": provider,
                "status": "suggested",
                "classification": repair.get("classification"),
                "confidence": repair.get("confidence"),
                "apply_supported": False,
                "recommended_fix": repair.get("recommended_fix") or "Review the model diagnosis and failure evidence.",
                "reason": "Model response did not include a workflow patch or app patch hint.",
                "workflow_patch": "",
                "app_patch_hint": "",
            }
        )
    return candidates


def _repair_with_anthropic(prompt: str, *, model: str) -> dict[str, Any]:
    import anthropic

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=1200,
        system=REPAIR_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    content = getattr(message, "content", [])
    if not content:
        raise RuntimeError("Anthropic returned an empty response.")
    first = content[0]
    text = getattr(first, "text", None)
    if text is None and isinstance(first, dict):
        text = first.get("text")
    return _parse_repair_json(str(text or ""))


def _repair_with_openai(prompt: str, *, model: str) -> dict[str, Any]:
    from openai import OpenAI

    client = OpenAI()
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    text = getattr(response, "output_text", "")
    return _parse_repair_json(str(text or ""))


def _parse_repair_json(text: str) -> dict[str, Any]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.strip("`")
        if clean.lower().startswith("json"):
            clean = clean[4:].strip()
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        return {
            "root_cause": clean[:1000],
            "classification": "unknown",
            "recommended_fix": clean[:1000],
            "workflow_patch": "",
            "app_patch_hint": "",
            "confidence": 0.2,
        }
    if not isinstance(parsed, dict):
        raise ValueError("Repair model response must be a JSON object.")
    return {
        "root_cause": str(parsed.get("root_cause") or ""),
        "classification": str(parsed.get("classification") or "unknown"),
        "recommended_fix": str(parsed.get("recommended_fix") or ""),
        "workflow_patch": str(parsed.get("workflow_patch") or ""),
        "app_patch_hint": str(parsed.get("app_patch_hint") or ""),
        "confidence": float(parsed.get("confidence") or 0.0),
        "candidates": parsed.get("candidates") if isinstance(parsed.get("candidates"), list) else [],
    }


def _is_auth_configuration_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(part in text for part in ("api_key", "auth_token", "authentication", "credentials", "environment variable"))
