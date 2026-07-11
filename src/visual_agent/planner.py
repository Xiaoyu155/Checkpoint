from __future__ import annotations

from dataclasses import dataclass

from .capabilities import Capability, build_atomic_capability_manifest
from .validation import validate_workflow
from .workflow import Workflow
from .workspace import Workspace


ASSERTION_ACTIONS = {"assert_text", "assert_response", "assert_file_exists"}
OBSERVATION_ACTION_PREFIX = "observe_"


@dataclass(frozen=True)
class PlannerDraftIssue:
    level: str
    code: str
    message: str
    step_id: str | None = None


@dataclass(frozen=True)
class PlannerDraftCheck:
    valid: bool
    workflow_name: str
    allowed_to_execute: bool
    dry_run_required: bool
    issues: tuple[PlannerDraftIssue, ...]
    atomic_capabilities: tuple[str, ...]


def check_planner_draft(
    workflow: Workflow,
    *,
    workspace: Workspace | None = None,
    allow_high_risk: bool = False,
) -> PlannerDraftCheck:
    """Validate an LLM/planner workflow draft without granting execution rights."""
    capabilities = build_atomic_capability_manifest().capabilities
    capability_by_name = {capability.name: capability for capability in capabilities}
    issues: list[PlannerDraftIssue] = []

    validation = validate_workflow(workflow)
    for issue in validation.issues:
        issues.append(
            PlannerDraftIssue(
                level=issue.level,
                code="workflow_validation",
                message=issue.message,
                step_id=issue.step_id,
            )
        )

    has_observation = False
    has_assertion = False
    for step in workflow.steps:
        capability = capability_by_name.get(step.action)
        if capability is None:
            issues.append(
                PlannerDraftIssue(
                    level="error",
                    code="capability_not_planner_visible",
                    message=f"Action is not available to planner drafts: {step.action}",
                    step_id=step.id,
                )
            )
            continue

        if step.action.startswith(OBSERVATION_ACTION_PREFIX):
            has_observation = True
        if step.action in ASSERTION_ACTIONS:
            has_assertion = True

        issues.extend(risk_issues(step.id, capability, allow_high_risk=allow_high_risk))

    if not has_observation:
        issues.append(
            PlannerDraftIssue(
                level="warning",
                code="missing_observation",
                message="Planner draft has no observation step.",
            )
        )
    if not has_assertion:
        issues.append(
            PlannerDraftIssue(
                level="warning",
                code="missing_assertion",
                message="Planner draft has no verification assertion step.",
            )
        )

    if workspace is not None:
        issues.extend(workspace_path_issues(workflow, workspace))

    valid = not any(issue.level == "error" for issue in issues)
    return PlannerDraftCheck(
        valid=valid,
        workflow_name=workflow.name,
        allowed_to_execute=False,
        dry_run_required=True,
        issues=tuple(issues),
        atomic_capabilities=tuple(sorted(capability_by_name)),
    )


def risk_issues(step_id: str, capability: Capability, *, allow_high_risk: bool) -> tuple[PlannerDraftIssue, ...]:
    if capability.risk_level == "high" and not allow_high_risk:
        return (
            PlannerDraftIssue(
                level="error",
                code="high_risk_blocked",
                message=f"High-risk capability requires explicit human approval: {capability.name}",
                step_id=step_id,
            ),
        )
    if capability.risk_level == "medium":
        return (
            PlannerDraftIssue(
                level="warning",
                code="dry_run_required",
                message=f"Medium-risk capability must remain dry-run until approved: {capability.name}",
                step_id=step_id,
            ),
        )
    return ()


def workspace_path_issues(workflow: Workflow, workspace: Workspace) -> tuple[PlannerDraftIssue, ...]:
    issues: list[PlannerDraftIssue] = []
    workspace_root = workspace.root.resolve()
    for step in workflow.steps:
        if step.action not in {"observe_html", "observe_fixture"}:
            continue
        path_value = step.params.get("path")
        if not path_value:
            continue
        path = workspace_root / str(path_value)
        try:
            path.resolve().relative_to(workspace_root)
        except ValueError:
            issues.append(
                PlannerDraftIssue(
                    level="error",
                    code="path_outside_workspace",
                    message=f"Planner draft path must stay inside workspace: {path_value}",
                    step_id=step.id,
                )
            )
    return tuple(issues)
