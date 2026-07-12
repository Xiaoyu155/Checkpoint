from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from .acceptance import INSPECTION_ONLY_WARNING, profile_interactions
from .git_diff import affected_workflows, changed_files, workflow_affects_changed_path
from .mcp_workspace_read import suggested_affects_pattern, suggested_workflow_name
from .models import ActionStatus
from .workspace import Workspace, WorkflowRef, discover_workflows, run_workspace_workflow


RUNTIME_CHANGE_PREFIXES = (
    ".agent-workspace/",
    ".runs/",
    "runs/",
    "reports/",
    "artifacts/",
    ".pytest_cache/",
    ".pytest_cache_local/",
    "__pycache__/",
)
RUNTIME_CHANGE_FILES = {
    ".visual-agent-status.md",
    "agent_session.json",
    "run_history.jsonl",
    "workflow_index.json",
    "强制测试记录.md",
}


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
    coverage: dict[str, object] = field(default_factory=dict)
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
        if self.coverage.get("status") in {"uncovered", "fallback_only"} and self.coverage.get("verification_mode") != "command":
            return "coverage_gap"
        if self.coverage.get("status") == "runtime_only":
            return "no_changed_files"
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
    raw_changed_paths = changed if changed is not None else changed_files(base=base, cwd=Path(repo_root))
    changed_paths, ignored_runtime_paths = filter_runtime_changed_files(raw_changed_paths, repo_root=repo_root)
    all_refs = list(discover_workflows(workspace, include_slow=True))
    tagged_refs = [ref for ref in all_refs if workflow_has_tags(ref, tags)]
    skipped_slow = [ref.name for ref in tagged_refs if "slow" in ref.tags and not include_slow]
    runnable = [ref for ref in tagged_refs if include_slow or "slow" not in ref.tags]
    selected = [] if ignored_runtime_paths and not changed_paths else select_codex_check_workflows(runnable, changed_paths)
    selected = selected[: max(0, int(max_workflows))]
    coverage = codex_coverage_summary(changed_paths, runnable)
    if ignored_runtime_paths:
        coverage["ignored_runtime_files"] = ignored_runtime_paths
        coverage["ignored_runtime_file_count"] = len(ignored_runtime_paths)
        if not changed_paths:
            coverage["status"] = "runtime_only"
            coverage["next_action"] = "Only generated Pacer/Checkpoint runtime files changed; no product workflow is needed."
    results = [
        run_one_codex_workflow(workspace, ref, run_profile=run_profile, from_step=from_step)
        for ref in selected
    ]
    return CodexCheckResult(
        changed_files=changed_paths,
        selected_workflows=[ref.name for ref in selected],
        skipped_slow_workflows=skipped_slow,
        coverage=coverage,
        results=results,
    )


def filter_runtime_changed_files(changed_paths: list[str], *, repo_root: str | Path | None = None) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    ignored: list[str] = []
    for item in changed_paths:
        path = str(item).replace("\\", "/").strip()
        while path.startswith("./"):
            path = path[2:]
        if not path:
            continue
        if is_runtime_changed_file(path, repo_root=repo_root):
            ignored.append(path)
            continue
        kept.append(path)
    return sorted(dict.fromkeys(kept)), sorted(dict.fromkeys(ignored))


def select_codex_check_workflows(workflows: list[WorkflowRef], changed_paths: list[str]) -> list[WorkflowRef]:
    if not changed_paths:
        return affected_workflows(workflows, changed=changed_paths)
    precise = [
        ref
        for ref in workflows
        if ref.affects and any(workflow_ref_covers_path(ref, path) for path in changed_paths)
    ]
    if precise:
        return precise
    return affected_workflows(workflows, changed=changed_paths)


def is_runtime_changed_file(path: str, *, repo_root: str | Path | None = None) -> bool:
    normalized = str(path).replace("\\", "/").strip()
    return any(_is_runtime_path_variant(candidate) for candidate in runtime_path_variants(normalized, repo_root=repo_root))


def runtime_path_variants(path: str, *, repo_root: str | Path | None = None) -> tuple[str, ...]:
    normalized = str(path).replace("\\", "/").strip().lstrip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    variants = [normalized] if normalized else []
    if repo_root is not None:
        root_name = Path(repo_root).expanduser().resolve().name
        prefix = f"{root_name}/" if root_name else ""
        if prefix and normalized.startswith(prefix):
            variants.append(normalized[len(prefix):])
    return tuple(dict.fromkeys(item for item in variants if item))


def _is_runtime_path_variant(path: str) -> bool:
    return path in RUNTIME_CHANGE_FILES or any(
        path == prefix.rstrip("/") or path.startswith(prefix)
        for prefix in RUNTIME_CHANGE_PREFIXES
    )


def workflow_has_tags(ref: WorkflowRef, tags: tuple[str, ...]) -> bool:
    requested = {str(tag) for tag in tags if str(tag)}
    if not requested:
        return True
    return requested.issubset(set(ref.tags))


def codex_coverage_summary(changed_paths: list[str], workflows: list[WorkflowRef]) -> dict[str, object]:
    changed = [str(item).replace("\\", "/").strip() for item in changed_paths if str(item).strip()]
    primary_refs = [ref for ref in workflows if ref.affects]
    fallback_refs = [ref for ref in workflows if not ref.affects]
    precise: list[str] = []
    for path in changed:
        if any(workflow_ref_covers_path(ref, path) for ref in primary_refs):
            precise.append(path)
    fallback_only = [path for path in changed if path not in precise and fallback_refs]
    uncovered = [path for path in changed if path not in precise and path not in fallback_only]
    primary_workflows = [
        ref.name
        for ref in primary_refs
        if any(workflow_ref_covers_path(ref, path) for path in changed)
    ]
    if not changed:
        status = "no_changed_files"
        next_action = "No changed files were detected; run a broad smoke or pass changed files explicitly."
    elif uncovered:
        status = "uncovered"
        next_action = "Generate or record workflows for uncovered_files before claiming verification."
    elif fallback_only:
        status = "fallback_only"
        next_action = "Fallback workflows were selected because no precise affects match exists; add affects paths or generate targeted workflows."
    else:
        status = "covered"
        next_action = "Run selected workflows and inspect agent_verdict in each report."
    return {
        "status": status,
        "precise_covered_files": precise,
        "fallback_only_files": fallback_only,
        "uncovered_files": uncovered,
        "primary_workflows": primary_workflows,
        "fallback_workflows": [ref.name for ref in fallback_refs],
        "suggested_affects": codex_suggested_affects(fallback_only, fallback_refs),
        "suggested_new_workflows": codex_suggested_new_workflows(uncovered),
        "next_action": next_action,
    }


def workflow_ref_covers_path(ref: WorkflowRef, changed_path: str) -> bool:
    return any(workflow_affects_changed_path(str(pattern), [changed_path]) for pattern in ref.affects)


def codex_suggested_affects(changed_files: list[str], fallback_refs: list[WorkflowRef]) -> list[dict[str, object]]:
    if not changed_files or not fallback_refs:
        return []
    patterns = sorted({suggested_affects_pattern(path) for path in changed_files})
    return [
        {
            "workflow": ref.name,
            "path": ref.relative_path,
            "add_affects": patterns,
            "reason": "fallback workflow has no affects paths but was selected for this diff",
        }
        for ref in fallback_refs[:5]
    ]


def codex_suggested_new_workflows(changed_files: list[str]) -> list[dict[str, object]]:
    return [
        {
            "changed_file": path,
            "suggested_name": suggested_workflow_name(path),
            "affects": [suggested_affects_pattern(path)],
            "reason": "no workflow precisely covers this changed file",
        }
        for path in changed_files
    ]


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
    if failed_step is not None and run_profile == "dry-run":
        steps = list(result.steps)
        failed_index = next(i for i, step in enumerate(steps) if step is failed_step)
        first_skip_index = next(
            (i for i, step in enumerate(steps) if step.status == ActionStatus.DRY_RUN),
            None,
        )
        # Dry-run skips real interactions, so a failure that occurs after a skipped
        # interaction is inconclusive (it asserts state the skipped step would have
        # produced), not a real regression. A failure before the first skip is a
        # genuine static/inspection regression and stays failed.
        if first_skip_index is not None and failed_index > first_skip_index:
            return CodexWorkflowCheck(
                name=ref.name,
                status="inspection_only",
                step_count=len(result.steps),
                elapsed_seconds=round(monotonic() - started, 6),
                run_id=result.run_id,
                failed_step=failed_step.id,
                message=(
                    "Inconclusive in dry-run: this step asserts state that follows a "
                    "skipped interaction."
                ),
                hint="Rerun with --run-profile supervised (or verify-now --live) to verify post-interaction acceptance.",
                real_interaction_count=interactions.real_interaction_count,
                skipped_interaction_count=interactions.skipped_interaction_count,
                invalid_interaction_count=interactions.invalid_interaction_count,
                acceptance_level=str(acceptance.get("label") or ""),
                acceptance_name=str(acceptance.get("name") or ""),
                is_product_acceptance=bool(acceptance.get("is_product_acceptance", False)),
            )
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
    if result.coverage:
        lines.append(f"[codex-check] Coverage: {result.coverage.get('status')}")
        command_mode = result.coverage.get("verification_mode") == "command"
        if command_mode:
            lines.append("[codex-check] Verification mode: command — workflow coverage 由显式测试命令接管.")
        if result.coverage.get("ignored_runtime_files"):
            ignored = list(result.coverage.get("ignored_runtime_files", []))
            lines.append(
                "[codex-check] Ignored generated runtime files: "
                + ", ".join(str(item) for item in ignored[:5])
                + ("..." if len(ignored) > 5 else "")
            )
        if result.coverage.get("uncovered_files") and not command_mode:
            lines.append("[codex-check] Uncovered files: " + ", ".join(str(item) for item in result.coverage.get("uncovered_files", [])))
        if result.coverage.get("fallback_only_files") and not command_mode:
            lines.append("[codex-check] Fallback-only files: " + ", ".join(str(item) for item in result.coverage.get("fallback_only_files", [])))
        if result.coverage.get("next_action") and not command_mode:
            lines.append(f"[codex-check] Coverage next action: {result.coverage.get('next_action')}")
        suggested_affects = result.coverage.get("suggested_affects")
        if isinstance(suggested_affects, list) and suggested_affects and not command_mode:
            first = suggested_affects[0] if isinstance(suggested_affects[0], dict) else {}
            lines.append(
                "[codex-check] Suggested affects: "
                f"{first.get('workflow')} -> {', '.join(str(item) for item in first.get('add_affects', []))}"
            )
        suggested_new = result.coverage.get("suggested_new_workflows")
        if isinstance(suggested_new, list) and suggested_new and not command_mode:
            first = suggested_new[0] if isinstance(suggested_new[0], dict) else {}
            lines.append(
                "[codex-check] Suggested new workflow: "
                f"{first.get('suggested_name')} affects {', '.join(str(item) for item in first.get('affects', []))}"
            )
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
        elif result.verdict == "coverage_gap":
            lines.append("[codex-check] Verdict: COVERAGE GAP — add precise workflow coverage before claiming verification.")
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
    elif result.verdict == "coverage_gap":
        lines.append("[codex-check] Verdict: COVERAGE GAP — no precise workflow coverage for this diff.")
    elif result.verdict == "no_changed_files":
        lines.append("[codex-check] Verdict: NO PRODUCT CHANGES — ignored generated runtime files only.")
    return "\n".join(lines)
