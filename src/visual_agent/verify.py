from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import ActionStatus
from .session import clamp_ai_text
from .workspace import Workspace, discover_workflows, run_workspace_workflow


@dataclass(frozen=True)
class WorkflowVerifyResult:
    name: str
    passed: bool
    step_count: int
    failed_step: str | None
    hint: str | None
    run_id: str


@dataclass(frozen=True)
class VerificationReport:
    total: int
    passed: int
    failed: int
    results: list[WorkflowVerifyResult]
    suggested_prompt: str
    token_estimate: int


def run_verify(
    workspace: Workspace,
    *,
    tags: tuple[str, ...] = ("verification",),
    run_profile: str = "dry-run",
) -> VerificationReport:
    workflows = [ref for ref in discover_workflows(workspace) if _has_tag(ref, tags)]
    results: list[WorkflowVerifyResult] = []
    for ref in workflows[:10]:
        try:
            result = run_workspace_workflow(
                workspace,
                ref.name,
                dry_run=run_profile == "dry-run",
                run_profile=run_profile,
                export_report=True,
            )
            steps = list(result.steps)
            failed = next((step for step in steps if getattr(step, "status", None) == ActionStatus.FAILED), None)
            results.append(
                WorkflowVerifyResult(
                    name=ref.name,
                    passed=failed is None,
                    step_count=len(steps),
                    failed_step=str(getattr(failed, "id", "")) if failed else None,
                    hint=_failure_hint(failed),
                    run_id=str(result.run_id),
                )
            )
        except Exception as exc:
            results.append(
                WorkflowVerifyResult(
                    name=ref.name,
                    passed=False,
                    step_count=0,
                    failed_step="execution_error",
                    hint=str(exc)[:100],
                    run_id="",
                )
            )

    passed = sum(1 for item in results if item.passed)
    prompt = _build_verify_prompt(results)
    return VerificationReport(
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        results=results,
        suggested_prompt=prompt,
        token_estimate=len(prompt) // 4,
    )


def verify_to_markdown(report: VerificationReport) -> str:
    lines = ["## Verification Report", f"Ran {report.total} workflows: {report.passed} passed, {report.failed} failed"]
    failed = [item for item in report.results if not item.passed]
    passed = [item for item in report.results if item.passed]

    if failed:
        lines.extend(["", "### Failed"])
        for item in failed[:5]:
            lines.append(f"- {item.name}: {item.failed_step or 'failed'}")
            if item.hint:
                lines.append(f"  Fix: {item.hint}")

    if passed:
        names = ", ".join(item.name for item in passed)
        lines.extend(["", "### Passed", names if len(names) < 500 else f"{len(passed)} workflows passed"])

    lines.extend(["", "### Suggested Action", report.suggested_prompt])
    return clamp_ai_text("\n".join(lines), max_chars=3200, suffix="...[use get_run_report for full details]")


def _has_tag(ref: Any, tags: tuple[str, ...]) -> bool:
    try:
        from .workflow import parse_workflow_file

        workflow = parse_workflow_file(ref.path)
    except Exception:
        return False
    workflow_tags = set(getattr(workflow, "tags", ()))
    requested_tags = set(tags)
    return bool(requested_tags) and requested_tags.issubset(workflow_tags)


def _failure_hint(failed: Any) -> str | None:
    if failed is None:
        return None
    meta = dict(getattr(failed, "metadata", {}) or {})
    diag = meta.get("failure_diagnosis", {}) or {}
    suggestions = diag.get("recovery_suggestions", [])
    if suggestions:
        return str(suggestions[0])[:100]
    message = str(getattr(failed, "message", "") or "")
    return message[:100] if message else None


def _build_verify_prompt(results: list[WorkflowVerifyResult]) -> str:
    failed = [item for item in results if not item.passed]
    if not results:
        return "No verification-tagged workflows found. Add tags: [verification] to workflows that should gate code changes."
    if not failed:
        return "All verification workflows passed. Code changes look good."
    parts = []
    for item in failed[:2]:
        part = f"{item.name} fails"
        if item.failed_step:
            part += f" at {item.failed_step}"
        if item.hint:
            part += f". {item.hint}"
        parts.append(part)
    return " ".join(parts) + " Fix these issues and run verification again."
