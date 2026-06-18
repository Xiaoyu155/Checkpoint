from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from .acceptance import INSPECTION_ONLY_WARNING, profile_interactions
from .git_diff import affected_workflows, changed_files
from .models import ActionStatus
from .workspace import Workspace, WorkflowRef, discover_workflows, run_workspace_workflow


@dataclass(frozen=True)
class CodexWorkflowCheck:
    name: str
    status: str
    step_count: int
    elapsed_seconds: float
    run_id: str = ""
    failed_step: str | None = None
    message: str | None = None
    hint: str | None = None
    expected: str | None = None
    actual: str | None = None
    screenshot: str | None = None
    real_interaction_count: int = 0
    skipped_interaction_count: int = 0
    invalid_interaction_count: int = 0
    acceptance_level: str = ""
    acceptance_name: str = ""
    is_product_acceptance: bool = False


@dataclass(frozen=True)
class CodexCheckResult:
    changed_files: list[str]
    selected_workflows: list[str]
    skipped_slow_workflows: list[str]
    results: list[CodexWorkflowCheck] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for item in self.results if item.status == "passed")

    @property
    def inspection_only(self) -> int:
        return sum(1 for item in self.results if item.status == "inspection_only")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.results if item.status == "failed")

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def verdict(self) -> str:
        if self.failed:
            return "fail"
        if not self.results:
            return "no_workflows"
        if self.inspection_only:
            return "inspection_only"
        return "pass"


def run_codex_check(
    workspace: Workspace,
    *,
    base: str = "HEAD",
    repo_root: str | Path = ".",
    include_slow: bool = False,
    tags: tuple[str, ...] = ("verification",),
    max_workflows: int = 10,
    run_profile: str = "dry-run",
    changed: list[str] | None = None,
    from_step: str | None = None,
) -> CodexCheckResult:
    changed_paths = changed if changed is not None else changed_files(base=base, cwd=Path(repo_root))
    all_refs = list(discover_workflows(workspace, include_slow=True))
    tagged_refs = [ref for ref in all_refs if workflow_has_tags(ref, tags)]
    skipped_slow = [ref.name for ref in tagged_refs if "slow" in ref.tags and not include_slow]
    runnable = [ref for ref in tagged_refs if include_slow or "slow" not in ref.tags]
    selected = affected_workflows(runnable, changed=changed_paths)
    selected = selected[: max(0, int(max_workflows))]
    results = [
        run_one_codex_workflow(workspace, ref, run_profile=run_profile, from_step=from_step)
        for ref in selected
    ]
    return CodexCheckResult(
        changed_files=changed_paths,
        selected_workflows=[ref.name for ref in selected],
        skipped_slow_workflows=skipped_slow,
        results=results,
    )


def workflow_has_tags(ref: WorkflowRef, tags: tuple[str, ...]) -> bool:
    requested = {str(tag) for tag in tags if str(tag)}
    if not requested:
        return True
    return requested.issubset(set(ref.tags))


def run_one_codex_workflow(workspace: Workspace, ref: WorkflowRef, *, run_profile: str, from_step: str | None = None) -> CodexWorkflowCheck:
    started = monotonic()
    try:
        result = run_workspace_workflow(
            workspace,
            ref.name,
            dry_run=run_profile == "dry-run",
            run_profile=run_profile,
            export_report=True,
            queue_when_locked=True,
            lock_wait_seconds=30.0,
            from_step=from_step,
        )
    except Exception as exc:
        return CodexWorkflowCheck(
            name=ref.name,
            status="failed",
            step_count=0,
            elapsed_seconds=round(monotonic() - started, 6),
            failed_step="execution_error",
            message=str(exc),
            hint=str(exc)[:200],
        )
    failed_step = next((step for step in result.steps if step.status == ActionStatus.FAILED), None)
    interactions = profile_interactions(result.steps)
    acceptance = result.acceptance if isinstance(getattr(result, "acceptance", None), dict) else {}
    if failed_step is None:
        return CodexWorkflowCheck(
            name=ref.name,
            status="inspection_only" if interactions.inspection_only else "passed",
            step_count=len(result.steps),
            elapsed_seconds=round(monotonic() - started, 6),
            run_id=result.run_id,
            real_interaction_count=interactions.real_interaction_count,
            skipped_interaction_count=interactions.skipped_interaction_count,
            invalid_interaction_count=interactions.invalid_interaction_count,
            acceptance_level=str(acceptance.get("label") or ""),
            acceptance_name=str(acceptance.get("name") or ""),
            is_product_acceptance=bool(acceptance.get("is_product_acceptance", False)),
        )
    diagnosis = failed_step.metadata.get("failure_diagnosis") if isinstance(failed_step.metadata, dict) else None
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    artifacts = diagnosis.get("artifacts") if isinstance(diagnosis.get("artifacts"), dict) else {}
    suggestions = diagnosis.get("recovery_suggestions") if isinstance(diagnosis.get("recovery_suggestions"), list) else []
    return CodexWorkflowCheck(
        name=ref.name,
        status="failed",
        step_count=len(result.steps),
        elapsed_seconds=round(monotonic() - started, 6),
        run_id=result.run_id,
        failed_step=failed_step.id,
        message=failed_step.message,
        hint=str(suggestions[0]) if suggestions else None,
        expected=str(diagnosis.get("expected") or "") or None,
        actual=str(diagnosis.get("actual") or "")[:300] or None,
        screenshot=str(artifacts.get("screenshot") or "") or None,
        real_interaction_count=interactions.real_interaction_count,
        skipped_interaction_count=interactions.skipped_interaction_count,
        invalid_interaction_count=interactions.invalid_interaction_count,
        acceptance_level=str(acceptance.get("label") or ""),
        acceptance_name=str(acceptance.get("name") or ""),
        is_product_acceptance=bool(acceptance.get("is_product_acceptance", False)),
    )


def codex_check_to_markdown(result: CodexCheckResult) -> str:
    lines = []
    if result.changed_files:
        changed = ", ".join(result.changed_files[:5])
        lines.append(f"[codex-check] Changed files: {changed}{'...' if len(result.changed_files) > 5 else ''}")
    else:
        lines.append("[codex-check] No git changes detected. Running matching workflows.")
    if result.skipped_slow_workflows:
        lines.append("[codex-check] Skipping slow workflows: " + ", ".join(result.skipped_slow_workflows))
    if result.selected_workflows:
        lines.append("[codex-check] Affected workflows: " + ", ".join(result.selected_workflows))
        lines.append(f"[codex-check] Running {len(result.selected_workflows)} workflow(s)...")
    else:
        lines.append("[codex-check] No affected workflows found.")
    for item in result.results:
        level = f" [{item.acceptance_level} {item.acceptance_name}]" if item.acceptance_level else ""
        invalid = f", {item.invalid_interaction_count} invalid receipt(s)" if item.invalid_interaction_count else ""
        if item.status == "passed":
            lines.append(
                f"  OK {item.name} ({item.step_count} steps, {item.elapsed_seconds:.3f}s, "
                f"{item.real_interaction_count} real interaction(s){invalid}){level}"
            )
            continue
        if item.status == "inspection_only":
            skipped = f", {item.skipped_interaction_count} interaction(s) skipped by dry-run" if item.skipped_interaction_count else ""
            lines.append(
                f"  INSPECT {item.name} ({item.step_count} steps, {item.elapsed_seconds:.3f}s{skipped}{invalid}){level}"
                " — page inspection only, no real interaction executed"
            )
            continue
        lines.append(f"  FAIL {item.name} FAILED at '{item.failed_step or '?'}'")
        if item.message:
            lines.append(f"    -> {item.message}")
        if item.expected:
            lines.append(f"    -> Expected: {item.expected}")
        if item.actual:
            lines.append(f"    -> Actual: {item.actual}")
        if item.hint:
            lines.append(f"    -> Fix: {item.hint}")
        if item.screenshot:
            lines.append(f"    -> Screenshot: {item.screenshot}")
    if result.results:
        lines.append(
            f"[codex-check] Real interaction: {result.passed} passed | "
            f"inspection-only: {result.inspection_only} | failed: {result.failed} (total {result.total})."
        )
        product_accepted = sum(1 for item in result.results if item.status != "failed" and item.is_product_acceptance)
        lines.append(
            f"[codex-check] Strict product acceptance (L3+ without blockers): {product_accepted}/{result.total} workflow(s)."
        )
        if result.verdict == "fail":
            lines.append("[codex-check] Verdict: FAIL — fix the failures above before claiming verification.")
        elif result.verdict == "inspection_only":
            if result.passed:
                lines.append(
                    "[codex-check] Verdict: PARTIAL — "
                    f"{result.passed} workflow(s) verified with real interaction, but "
                    f"{result.inspection_only} workflow(s) were inspection-only and prove nothing about real usage."
                )
            else:
                lines.append(f"[codex-check] Verdict: INSPECTION ONLY — {INSPECTION_ONLY_WARNING}")
        else:
            lines.append("[codex-check] Verdict: PASS — real user interaction was executed and verified.")
    return "\n".join(lines)
