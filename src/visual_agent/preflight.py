from __future__ import annotations

from dataclasses import dataclass

from .capabilities import Capability, build_capability_manifest
from .validation import ValidationResult, validate_workflow
from .workflow import Workflow


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    workflow_name: str
    strict: bool
    validation: ValidationResult
    missing_required_capabilities: tuple[Capability, ...]
    unavailable_used_capabilities: tuple[Capability, ...]
    warnings: tuple[str, ...]


def run_preflight(
    workflow: Workflow,
    *,
    strict: bool = False,
    allow_high_risk: bool = False,
    require_optional_capabilities: bool = False,
) -> PreflightResult:
    validation = validate_workflow(workflow, strict=strict, allow_high_risk=allow_high_risk)
    manifest = build_capability_manifest()
    missing = tuple(capability for capability in manifest.capabilities if not capability.available)
    missing_required = tuple(
        capability
        for capability in missing
        if capability.required or (require_optional_capabilities and capability.kind != "dependency")
    )
    unavailable_used = unavailable_capabilities_used_by_workflow(workflow, missing)
    warnings = tuple(issue.message for issue in validation.issues if issue.level == "warning")
    ok = validation.valid and not missing_required and not unavailable_used
    return PreflightResult(
        ok=ok,
        workflow_name=workflow.name,
        strict=strict,
        validation=validation,
        missing_required_capabilities=missing_required,
        unavailable_used_capabilities=unavailable_used,
        warnings=warnings,
    )


def unavailable_capabilities_used_by_workflow(
    workflow: Workflow,
    missing_capabilities: tuple[Capability, ...],
) -> tuple[Capability, ...]:
    missing_by_name = {capability.name: capability for capability in missing_capabilities}
    used_actions = {step.action for step in workflow.steps}
    unavailable = []
    for action in sorted(used_actions):
        capability = missing_by_name.get(action)
        if capability is not None:
            unavailable.append(capability)
    return tuple(unavailable)
