from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .acceptance import INSPECTION_ONLY_WARNING, aggregate_cross_platform, profile_interactions
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
    inspection_only: bool = False
    real_interaction_count: int = 0
    invalid_interaction_count: int = 0
    acceptance_level: str = ""
    is_product_acceptance: bool = False


@dataclass(frozen=True)
class VerificationReport:
    total: int
    passed: int
    failed: int
    results: list[WorkflowVerifyResult]
    suggested_prompt: str
    token_estimate: int
    inspection_only: int = 0
    cross_platform: tuple[dict[str, Any], ...] = ()

    @property
    def verdict(self) -> str:
        if self.failed:
            return "fail"
        if not self.total:
            return "no_workflows"
        if self.inspection_only:
            return "inspection_only"
        return "pass"


def run_verify(
    workspace: Workspace,
    *,
    tags: tuple[str, ...] = ("verification",),
    workflow_names: tuple[str, ...] = (),
    max_workflows: int = 10,
    run_profile: str = "dry-run",
    wait_lock: bool = False,
    lock_wait_seconds: float = 30.0,
    include_slow: bool = False,
) -> VerificationReport:
    workflows = [ref for ref in discover_workflows(workspace, include_slow=include_slow) if _has_tag(ref, tags)]
    if workflow_names:
        requested = set(workflow_names)
        workflows = [ref for ref in workflows if ref.name in requested or ref.relative_path in requested]
    results: list[WorkflowVerifyResult] = []
    aggregate_entries: list[tuple[str, tuple[str, ...], int, bool]] = []
    for ref in workflows[:max(0, max_workflows)]:
        try:
            result = run_workspace_workflow(
                workspace,
                ref.name,
                dry_run=run_profile == "dry-run",
                run_profile=run_profile,
                export_report=True,
                queue_when_locked=wait_lock,
                lock_wait_seconds=lock_wait_seconds,
            )
            steps = list(result.steps)
            failed = next((step for step in steps if getattr(step, "status", None) == ActionStatus.FAILED), None)
            interactions = profile_interactions(steps)
            acceptance = result.acceptance if isinstance(getattr(result, "acceptance", None), dict) else {}
            results.append(
                WorkflowVerifyResult(
                    name=ref.name,
                    passed=failed is None,
                    step_count=len(steps),
                    failed_step=str(getattr(failed, "id", "")) if failed else None,
                    hint=_failure_hint(failed),
                    run_id=str(result.run_id),
                    inspection_only=interactions.inspection_only,
                    real_interaction_count=interactions.real_interaction_count,
                    invalid_interaction_count=interactions.invalid_interaction_count,
                    acceptance_level=str(acceptance.get("label") or ""),
                    is_product_acceptance=bool(acceptance.get("is_product_acceptance", False)),
                )
            )
            aggregate_entries.append(
                (
                    ref.name,
                    tuple(getattr(ref, "tags", ()) or ()),
                    int(acceptance.get("level", -1)) if isinstance(acceptance.get("level"), (int, float)) else -1,
                    failed is None,
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
    inspection_only = sum(1 for item in results if item.passed and item.inspection_only)
    prompt = _build_verify_prompt(results)
    return VerificationReport(
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        results=results,
        suggested_prompt=prompt,
        token_estimate=len(prompt) // 4,
        inspection_only=inspection_only,
        cross_platform=tuple(aggregate_cross_platform(aggregate_entries)),
    )


def verify_to_markdown(report: VerificationReport) -> str:
    verified = report.passed - report.inspection_only
    product_accepted = sum(1 for item in report.results if item.passed and item.is_product_acceptance)
    lines = [
        "## Verification Report",
        f"Ran {report.total} workflows: {verified} passed with real interaction, "
        f"{report.inspection_only} inspection-only, {report.failed} failed",
        f"Strict product acceptance (L3+ without blockers): {product_accepted}/{report.total}",
    ]
    failed = [item for item in report.results if not item.passed]
    interacted = [item for item in report.results if item.passed and not item.inspection_only]
    inspected = [item for item in report.results if item.passed and item.inspection_only]

    if failed:
        lines.extend(["", "### Failed"])
        for item in failed[:5]:
            lines.append(f"- {item.name}: {item.failed_step or 'failed'}")
            if item.hint:
                lines.append(f"  Fix: {item.hint}")

    if interacted:
        names = ", ".join(_name_with_level_and_receipts(item) for item in interacted)
        lines.extend(["", "### Passed (real interaction)", names if len(names) < 500 else f"{len(interacted)} workflows passed"])

    if inspected:
        names = ", ".join(_name_with_level_and_receipts(item) for item in inspected)
        lines.extend(
            [
                "",
                "### Inspection Only (NOT product acceptance)",
                names if len(names) < 500 else f"{len(inspected)} workflows",
                INSPECTION_ONLY_WARNING,
            ]
        )

    if report.cross_platform:
        lines.extend(["", "### Cross-Platform (L5)"])
        for entry in report.cross_platform:
            platforms = ", ".join(f"{name}=L{level}" for name, level in entry.get("platforms", {}).items())
            verdict = "L5 achieved" if entry.get("achieved") else f"not yet ({entry.get('label')})"
            lines.append(f"- {entry.get('family')}: {verdict} [{platforms}]")

    lines.extend(["", "### Suggested Action", report.suggested_prompt])
    return clamp_ai_text("\n".join(lines), max_chars=3200, suffix="...[use get_run_report for full details]")


def _name_with_level(item: WorkflowVerifyResult) -> str:
    return f"{item.name} [{item.acceptance_level}]" if item.acceptance_level else item.name


def _name_with_level_and_receipts(item: WorkflowVerifyResult) -> str:
    name = _name_with_level(item)
    if item.invalid_interaction_count:
        return f"{name} ({item.real_interaction_count} valid, {item.invalid_interaction_count} invalid receipt)"
    return name


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
        return "No matching verification workflows found. Add tags: [verification] or pass --workflow with an existing workflow name."
    if not failed:
        if all(item.inspection_only for item in results):
            return (
                "All workflows passed page inspection, but none executed real user interaction. "
                "Do NOT report this as product verification. " + INSPECTION_ONLY_WARNING
            )
        if any(item.inspection_only for item in results):
            return (
                "Real-interaction workflows passed, but some workflows were inspection-only "
                "(no click/type/submit executed). Treat those as page inspection, not product verification."
            )
        return "All verification workflows passed with real user interaction. Code changes look good."
    parts = []
    for item in failed[:2]:
        part = f"{item.name} fails"
        if item.failed_step:
            part += f" at {item.failed_step}"
        if item.hint:
            part += f". {item.hint}"
        parts.append(part)
    return " ".join(parts) + " Fix these issues and run verification again."
