from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from .capture import capture_visual_region
from .audit import RunAudit
from .diagnostics import diagnose_failure
from .dispatcher import ActionDispatchContext, ActionDispatcher, read_path, selector_from_resolved
from .dom import normalize_text
from .locks import RunLock, VisualLock, lock_to_dict, queue_to_dict
from .models import (
    ActionResult,
    ActionStatus,
    LocationEvidence,
    Observation,
    Point,
    ProviderKind,
    ResolvedTarget,
    Target,
    to_jsonable,
)
from .providers import ProviderContext, ProviderRegistry, default_provider_registry
from .product_state import (
    ai_quality_failure_message,
    browser_readiness_failure_message,
    evaluate_ai_response_quality,
    evaluate_browser_readiness,
    evaluate_no_error_state,
    evaluate_product_contract,
    observation_to_state,
    product_contract_failure_message,
)
from .acceptance import grade_run
from .product_guard import PRODUCT_GUARD_OPT_OUT_TAG, PRODUCT_GUARD_STEP_ID, evaluate_product_guard
from .visual_rules import (
    VISUAL_GUARD_OPT_OUT_TAG,
    VISUAL_GUARD_STEP_ID,
    VisualAuditReport,
    VisualRuleConfig,
    collect_visual_snapshot,
    evaluate_visual_snapshot,
    run_visual_audit,
)
from .run_profile import MUTATING_ACTIONS, RunProfileName, ensure_step_allowed, normalize_run_profile, step_should_dry_run
from .selector import SelectorResolver
from .security import redact_secret_text
from .state import StateStore, WorkflowState, hydrate_context_from_completed_steps
from .vision import build_locator
from .workflow_types import WorkflowContext
from .versioning import UnsupportedSchemaVersionError, migrate_workflow_payload


RUNTIME_VERSION = "0.1.0"
SUPPORTED_WORKFLOW_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Workflow:
    name: str
    version: int
    steps: tuple[WorkflowStep, ...]
    schema_version: int | None = None
    min_runtime_version: str | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    fixtures: tuple[str, ...] = ()
    preconditions: tuple[Any, ...] = ()
    tags: tuple[str, ...] = ()
    affects: tuple[str, ...] = ()
    description: str = ""
    visibility: str = "private"
    author: str = ""
    license: str = ""


@dataclass(frozen=True)
class WorkflowStepResult:
    id: str
    action: str
    status: ActionStatus
    message: str = ""
    observation: Observation | None = None
    resolved_target: ResolvedTarget | None = None
    action_result: ActionResult | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowRunResult:
    run_id: str
    run_dir: Path
    workflow_name: str
    steps: tuple[WorkflowStepResult, ...]
    workflow_schema_version: int | None = None
    runtime_version: str = RUNTIME_VERSION
    run_profile: str = "dry-run"
    run_lock: dict[str, object] | None = None
    run_queue: dict[str, object] | None = None
    run_checks: dict[str, object] | None = None
    acceptance: dict[str, object] | None = None


class WorkflowRuntime:
    def __init__(
        self,
        output_dir: str | Path = ".runs",
        *,
        providers: ProviderRegistry | None = None,
        dispatcher: ActionDispatcher | None = None,
    ) -> None:
        self.audit = RunAudit(output_dir)
        self.selector = SelectorResolver()
        self.providers = providers or default_provider_registry()
        self.dispatcher = dispatcher or ActionDispatcher()

    def run(
        self,
        workflow: Workflow,
        *,
        dry_run: bool = True,
        run_profile: RunProfileName | str | None = None,
        synthetic_on_capture_fail: bool = False,
        inputs: dict[str, Any] | None = None,
        sensitive_fields: set[str] | None = None,
        workspace_root: str | Path | None = None,
        resume_from: str | Path | None = None,
        from_step: str | None = None,
        use_lock: bool = True,
        lock_ttl_seconds: float = 3600.0,
        queue_when_locked: bool = False,
        lock_wait_seconds: float = 0.0,
        lock_poll_seconds: float = 0.5,
        progress_state: dict[str, Any] | None = None,
    ) -> WorkflowRunResult:
        profile = normalize_run_profile(str(run_profile) if run_profile is not None else None, dry_run=dry_run)
        lock = RunLock(self.audit.root_dir, ttl_seconds=lock_ttl_seconds) if use_lock else None
        lock_info = None
        queue_info = None

        def update_progress(stage: str, *, current_step: str = "", current_index: int = -1, message: str = "") -> None:
            if progress_state is None:
                return
            progress_state.update(
                {
                    "workflow_name": workflow.name,
                    "total_steps": len(workflow.steps),
                    "stage": stage,
                    "current_step": current_step,
                    "current_index": current_index,
                    "message": message,
                    "done": False,
                    "updated_at": monotonic(),
                }
            )

        update_progress("initializing")
        if lock is not None and resume_from is None:
            if queue_when_locked:
                lock_info, queue_info = lock.acquire_with_wait(
                    owner=f"{workflow.name}:pending",
                    wait_seconds=lock_wait_seconds,
                    poll_seconds=lock_poll_seconds,
                )
            else:
                lock_info = lock.acquire(owner=f"{workflow.name}:pending")
        if resume_from is not None:
            run_dir = Path(resume_from)
            run_id = run_dir.name
            state_store = StateStore(run_dir)
            state = state_store.load()
            completed_steps = list(state.completed_steps) if state else []
        else:
            run_id, run_dir = self.audit.create_run_dir()
            state_store = StateStore(run_dir)
            completed_steps = []
            state_store.save(WorkflowState(run_id=run_id, workflow_name=workflow.name, completed_steps=()))
        update_progress("run_directory_ready")
        if lock is not None and lock_info is None:
            if queue_when_locked:
                lock_info, queue_info = lock.acquire_with_wait(
                    owner=f"{workflow.name}:{run_id}",
                    wait_seconds=lock_wait_seconds,
                    poll_seconds=lock_poll_seconds,
                )
            else:
                lock_info = lock.acquire(owner=f"{workflow.name}:{run_id}")
        completed_lookup = set(completed_steps)

        try:
            context = WorkflowContext(
                run_id=run_id,
                run_dir=run_dir,
                inputs=inputs or {},
                variables={},
                sensitive_fields=sensitive_fields or set(),
                resources={"workspace_root": str(Path(workspace_root).resolve())} if workspace_root is not None else {},
            )
            context.variables.update(resolve_workflow_variables(workflow.variables, context))
            hydrate_context_from_completed_steps(context, tuple(completed_steps))
            results: list[WorkflowStepResult] = []
            soft_assert_errors: list[dict[str, Any]] = []
            sensitive_values = workflow_sensitive_input_values(workflow, context.inputs, context.sensitive_fields)

            if workflow.fixtures:
                update_progress("loading_fixtures")
                load_workflow_fixtures(workflow.fixtures, context)
            precondition_failed: WorkflowStepResult | None = None
            if workflow.preconditions:
                for precondition_index, precondition in enumerate(workflow.preconditions):
                    update_progress(
                        "running_preconditions",
                        current_step=f"precondition {precondition_index + 1}/{len(workflow.preconditions)}",
                        current_index=precondition_index,
                    )
                    precondition_result = self._run_precondition(
                        precondition,
                        context,
                        run_profile=profile,
                        synthetic_on_capture_fail=synthetic_on_capture_fail,
                    )
                    safe_precondition = redact_workflow_step_result(precondition_result, sensitive_values)
                    results.append(safe_precondition)
                    self._write_step_result(run_dir, safe_precondition)
                    if precondition_result.status == ActionStatus.FAILED:
                        if bool(precondition_result.metadata.get("soft_assert")):
                            soft_assert_errors.append(
                                {
                                    "id": precondition_result.id,
                                    "action": precondition_result.action,
                                    "message": precondition_result.message,
                                    "metadata": precondition_result.metadata,
                                }
                            )
                            continue
                        state_store.save(
                            WorkflowState(
                                run_id=run_id,
                                workflow_name=workflow.name,
                                completed_steps=tuple(completed_steps),
                                failed_step=precondition_result.id,
                            )
                        )
                        precondition_failed = precondition_result
                        break

            if precondition_failed is not None:
                run_result = WorkflowRunResult(
                    run_id=run_id,
                    run_dir=run_dir,
                    workflow_name=workflow.name,
                    steps=tuple(results),
                    workflow_schema_version=workflow.schema_version,
                    runtime_version=RUNTIME_VERSION,
                    run_profile=profile,
                    run_lock=lock_to_dict(lock_info) if lock_info is not None else None,
                    run_queue=queue_to_dict(queue_info) if queue_info is not None else None,
                )
                (run_dir / "workflow_result.json").write_text(
                    json.dumps(to_jsonable(run_result), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                if progress_state is not None:
                    progress_state["done"] = True
                    update_progress("finished", current_step="preconditions failed", current_index=-1, message=precondition_failed.id)
                close_context_resources(context)
                return run_result

            step_lookup = {step.id: index for index, step in enumerate(workflow.steps)}
            step_index = 0
            hard_failure = False
            if from_step is not None:
                if from_step not in step_lookup:
                    raise ValueError(f"from_step not found in workflow: {from_step}")
                step_index = step_lookup[from_step]
            while step_index < len(workflow.steps):
                step = workflow.steps[step_index]
                update_progress(
                    "running_step",
                    current_step=f"{step.id} ({step.action})",
                    current_index=step_index,
                )
                if step.id in completed_lookup:
                    result = WorkflowStepResult(
                        id=step.id,
                        action=step.action,
                        status=ActionStatus.SUCCESS,
                        message="skipped by resume checkpoint",
                        metadata={"resumed": True},
                    )
                    results.append(result)
                    step_index += 1
                    continue

                result = self._run_step(
                    step,
                    context,
                    run_profile=profile,
                    synthetic_on_capture_fail=synthetic_on_capture_fail,
                )
                safe_result = redact_workflow_step_result(result, sensitive_values)
                results.append(safe_result)
                self._write_step_result(run_dir, safe_result)
                if result.status == ActionStatus.FAILED:
                    if bool(result.metadata.get("soft_assert")):
                        soft_assert_errors.append(
                            {
                                "id": result.id,
                                "action": result.action,
                                "message": result.message,
                                "metadata": result.metadata,
                            }
                        )
                        completed_steps.append(step.id)
                        completed_lookup.add(step.id)
                        state_store.save(
                            WorkflowState(
                                run_id=run_id,
                                workflow_name=workflow.name,
                                completed_steps=tuple(completed_steps),
                            )
                        )
                        continue
                    state_store.save(
                        WorkflowState(
                            run_id=run_id,
                            workflow_name=workflow.name,
                            completed_steps=tuple(completed_steps),
                            failed_step=step.id,
                        )
                    )
                    hard_failure = True
                    break
                completed_steps.append(step.id)
                completed_lookup.add(step.id)
                state_store.save(
                    WorkflowState(
                        run_id=run_id,
                        workflow_name=workflow.name,
                        completed_steps=tuple(completed_steps),
                    )
                )
                jump_to = str(result.metadata.get("jump_to") or "") if isinstance(result.metadata, dict) else ""
                if jump_to and jump_to in step_lookup:
                    step_index = step_lookup[jump_to]
                    continue
                step_index += 1

            run_checks: dict[str, Any] = {}
            if hard_failure:
                run_checks["product_guard"] = {"status": "skipped", "reason": "step_failure"}
            elif PRODUCT_GUARD_OPT_OUT_TAG in workflow.tags:
                run_checks["product_guard"] = {"status": "skipped", "reason": "opt_out_tag"}
            if not hard_failure and PRODUCT_GUARD_OPT_OUT_TAG not in workflow.tags:
                guard = evaluate_product_guard(context.resources)
                run_checks["product_guard"] = {
                    "status": "passed" if guard.passed else "failed",
                    "violation_count": guard.violation_count,
                }
                if not guard.passed:
                    guard_step = redact_workflow_step_result(
                        WorkflowStepResult(
                            id=PRODUCT_GUARD_STEP_ID,
                            action="product_guard",
                            status=ActionStatus.FAILED,
                            message=guard.summary(),
                            metadata={"product_guard": guard.to_metadata()},
                        ),
                        sensitive_values,
                    )
                    results.append(guard_step)
                    self._write_step_result(run_dir, guard_step)
                    state_store.save(
                        WorkflowState(
                            run_id=run_id,
                            workflow_name=workflow.name,
                            completed_steps=tuple(completed_steps),
                            failed_step=guard_step.id,
                        )
                    )

            visual_passed: bool | None = None
            if hard_failure:
                run_checks["visual_guard"] = {"status": "skipped", "reason": "step_failure"}
            elif VISUAL_GUARD_OPT_OUT_TAG in workflow.tags:
                run_checks["visual_guard"] = {"status": "skipped", "reason": "opt_out_tag"}
            if not hard_failure and VISUAL_GUARD_OPT_OUT_TAG not in workflow.tags:
                visual_report = run_visual_audit(context.resources)
                if visual_report is None:
                    run_checks["visual_guard"] = {"status": "skipped", "reason": "no_live_page"}
                else:
                    visual_passed = visual_report.passed
                    run_checks["visual_guard"] = {
                        "status": "passed" if visual_report.passed else "failed",
                        "error_count": visual_report.error_count,
                        "warning_count": visual_report.warning_count,
                    }
                if visual_report is not None and visual_report.findings:
                    artifact_path = write_visual_audit_artifact(run_dir, VISUAL_GUARD_STEP_ID, visual_report)
                    visual_step = redact_workflow_step_result(
                        WorkflowStepResult(
                            id=VISUAL_GUARD_STEP_ID,
                            action="visual_guard",
                            status=ActionStatus.SUCCESS if visual_report.passed else ActionStatus.FAILED,
                            message=visual_report.summary(),
                            metadata={"visual_audit": visual_report.to_metadata(), "artifact_path": str(artifact_path)},
                        ),
                        sensitive_values,
                    )
                    results.append(visual_step)
                    self._write_step_result(run_dir, visual_step)
                    if not visual_report.passed:
                        state_store.save(
                            WorkflowState(
                                run_id=run_id,
                                workflow_name=workflow.name,
                                completed_steps=tuple(completed_steps),
                                failed_step=visual_step.id,
                            )
                        )

            if soft_assert_errors:
                summary_step = WorkflowStepResult(
                    id="soft_assert_summary",
                    action="soft_assert_summary",
                    status=ActionStatus.FAILED,
                    message=f"{len(soft_assert_errors)} soft assertion(s) failed",
                    metadata={"soft_assert": True, "soft_assert_errors": soft_assert_errors},
                )
                results.append(summary_step)
                state_store.save(
                    WorkflowState(
                        run_id=run_id,
                        workflow_name=workflow.name,
                        completed_steps=tuple(completed_steps),
                        failed_step=summary_step.id,
                    )
                )

            run_result = WorkflowRunResult(
                run_id=run_id,
                run_dir=run_dir,
                workflow_name=workflow.name,
                steps=tuple(results),
                workflow_schema_version=workflow.schema_version,
                runtime_version=RUNTIME_VERSION,
                run_profile=profile,
                run_lock=lock_to_dict(lock_info) if lock_info is not None else None,
                run_queue=queue_to_dict(queue_info) if queue_info is not None else None,
                run_checks=run_checks or None,
                acceptance=grade_run(results, visual_passed=visual_passed).to_dict(),
            )
            (run_dir / "workflow_result.json").write_text(
                json.dumps(to_jsonable(run_result), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            update_progress("finished", current_step="workflow complete", current_index=len(workflow.steps))
            if progress_state is not None:
                progress_state["done"] = True
            close_context_resources(context)
            return run_result
        finally:
            if progress_state is not None:
                progress_state["done"] = True
            if lock is not None:
                lock.release()

    def _run_step(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        *,
        run_profile: RunProfileName,
        synthetic_on_capture_fail: bool,
    ) -> WorkflowStepResult:
        retry = retry_config(step.params)
        requested_attempts = int(retry["attempts"])
        retry_safe = is_retry_safe_action(step.action)
        retry_disabled = requested_attempts > 1 and not retry_safe
        attempts = 1 if retry_disabled else requested_attempts
        last_error: Exception | None = None
        retry_errors: list[dict[str, Any]] = []
        started = monotonic()

        for attempt in range(1, attempts + 1):
            try:
                if step_needs_visual_lock(step):
                    with VisualLock():
                        result = self._run_step_or_raise(
                            step,
                            context,
                            run_profile=run_profile,
                            synthetic_on_capture_fail=synthetic_on_capture_fail,
                        )
                else:
                    result = self._run_step_or_raise(
                        step,
                        context,
                        run_profile=run_profile,
                        synthetic_on_capture_fail=synthetic_on_capture_fail,
                    )
                elapsed = monotonic() - started
                timeout_seconds = optional_float(step.params.get("timeout_seconds"))
                if timeout_seconds is not None and elapsed > timeout_seconds:
                    raise TimeoutError(f"Step timed out after {elapsed:.3f}s > {timeout_seconds:.3f}s.")
                return with_metadata(
                    result,
                    {
                        **result.metadata,
                        "run_attempt": attempt,
                        "run_attempts": attempts,
                        "retry_requested_attempts": requested_attempts,
                        "retry_safe": retry_safe,
                        "retry_disabled": retry_disabled,
                        **({"retry_disabled_reason": "automatic retry is only allowed for observe/wait/assert steps"} if retry_disabled else {}),
                        **({"retry_errors": retry_errors} if retry_errors else {}),
                        "elapsed_seconds": round(elapsed, 6),
                    },
                )
            except Exception as exc:
                last_error = exc
                retry_errors.append({"attempt": attempt, "error": f"{exc.__class__.__name__}: {exc}"})
                if attempt < attempts:
                    sleep(retry["delay_seconds"])

        elapsed = monotonic() - started
        try:
            diagnosis_params = resolve_step_params(step.params, context)
        except Exception:
            diagnosis_params = step.params
        diagnosis = diagnose_failure(
            step_id=step.id,
            action=step.action,
            params=diagnosis_params,
            error=last_error,
            context=context,
        )
        return WorkflowStepResult(
            id=step.id,
            action=step.action,
            status=ActionStatus.FAILED,
            message=str(last_error) if last_error else "Step failed.",
            metadata={
                "run_attempt": attempts,
                "run_attempts": attempts,
                "retry_requested_attempts": requested_attempts,
                "retry_safe": retry_safe,
                "retry_disabled": retry_disabled,
                **({"retry_disabled_reason": "automatic retry is only allowed for observe/wait/assert steps"} if retry_disabled else {}),
                "retry_errors": retry_errors,
                "elapsed_seconds": round(elapsed, 6),
                "failure_diagnosis": diagnosis,
            },
        )

    def _run_step_or_raise(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        *,
        run_profile: RunProfileName,
        synthetic_on_capture_fail: bool,
    ) -> WorkflowStepResult:
        action = step.action
        params = resolve_step_params(step.params, context)
        step = WorkflowStep(step.id, step.action, params)
        ensure_step_allowed(run_profile, action, params)
        step_dry_run = step_should_dry_run(run_profile, action, params)
        if run_profile == "semi-auto" and action in MUTATING_ACTIONS and not step_dry_run:
            print(f"[semi-auto] About to execute: {action} on step {step.id}. Press Enter to continue or Ctrl+C to abort.")
            input()

        if action == "observe_state":
            source_observation = context.observations.get(str(params.get("observation", ""))) or context.latest_observation
            state = observation_to_state(source_observation, max_text_items=int(params.get("max_text_items", 80) or 80))
            observation = Observation(
                provider=source_observation.provider,
                source=source_observation.source,
                screenshot_path=source_observation.screenshot_path,
                width=source_observation.width,
                height=source_observation.height,
                elements=tuple({"kind": key, "value": value} for key, value in state.items() if isinstance(value, (tuple, list)) and value),
                metadata={"provider": "state", "state": state, "source_provider": source_observation.provider.value},
            )
            context.observations[step.id] = observation
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message="state observed",
                observation=observation,
                metadata={"state": state},
            )

        if action.startswith("observe_"):
            cache_enabled = params.get("cache") is not False
            cache_key = context.observation_cache_key(action, params) if cache_enabled else ""
            if cache_enabled:
                cached = context.get_cached_observation(cache_key)
                if cached is not None:
                    context.observations[step.id] = cached
                    return WorkflowStepResult(
                        id=step.id,
                        action=action,
                        status=ActionStatus.SUCCESS,
                        message=f"{action} completed (cache hit)",
                        observation=cached,
                        metadata={"observation_cache": "hit", "cache_key": cache_key},
                    )
            observation = self.providers.observe(
                action,
                params,
                ProviderContext(
                    run_dir=context.run_dir,
                    synthetic_on_capture_fail=synthetic_on_capture_fail,
                    resources=context.resources,
                ),
            )
            if cache_enabled:
                context.set_cached_observation(cache_key, observation)
            context.observations[step.id] = observation
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message=f"{action} completed",
                observation=observation,
                metadata={
                    "observation_cache": "miss" if cache_enabled else "disabled",
                    **({"cache_key": cache_key} if cache_enabled else {}),
                },
            )

        if action == "resolve":
            observation = context.observations.get(str(params.get("observation", ""))) or context.latest_observation
            target = target_from_config(require_param(params, "target"))
            resolved = self.selector.resolve(target, observation)
            context.resolved_targets[step.id] = resolved
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message="target resolved",
                resolved_target=resolved,
            )

        if action == "set_variable":
            name = str(require_param(params, "name")).strip()
            if not name:
                raise ValueError("set_variable requires name.")
            value = extract_variable_value(params, context)
            context.variables[name] = value
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message=f"variable set: {name}",
                metadata={"name": name, "value": value},
            )

        if action == "if_text_exists":
            observation = context.observations.get(str(params.get("observation", ""))) or context.latest_observation
            text = normalize_text(require_param(params, "text"))
            passed = observation_contains_text(observation, text)
            branch = "then" if passed else "else"
            next_step = params.get(branch)
            metadata = {"text": text, "branch_taken": branch, "matched": passed}
            if next_step:
                metadata["jump_to"] = str(next_step)
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message=f"{branch} branch selected",
                metadata=metadata,
            )

        if action == "run_workflow":
            nested_name = str(require_param(params, "workflow")).strip()
            nested_inputs = dict(context.inputs)
            extra_inputs = params.get("inputs")
            if isinstance(extra_inputs, dict):
                nested_inputs.update(extra_inputs)
            nested_dry_run = bool(params.get("dry_run", run_profile == "dry-run"))
            nested_result = self._run_nested_workflow(
                nested_name,
                context,
                run_profile=run_profile,
                synthetic_on_capture_fail=synthetic_on_capture_fail,
                dry_run=nested_dry_run,
                inputs=nested_inputs,
                from_step=str(params.get("from_step") or "") or None,
            )
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS if all(item.status != ActionStatus.FAILED for item in nested_result.steps) else ActionStatus.FAILED,
                message=f"nested workflow completed: {nested_result.workflow_name}",
                metadata={"nested_run": to_jsonable(nested_result)},
            )

        if action == "click_visual":
            return self._click_visual(
                step,
                context,
                dry_run=step_dry_run,
                synthetic_on_capture_fail=synthetic_on_capture_fail,
            )

        if action == "assert_visual_text":
            return self._assert_visual_text(
                step,
                context,
                synthetic_on_capture_fail=synthetic_on_capture_fail,
            )

        if action in self.dispatcher.actions_available:
            resolved = placeholder_resolved_target(action) if action in SELF_CONTAINED_ACTIONS and "target" not in params else self._resolve_for_action(params, context, step.id, action=action)
            action_result = self.dispatcher.execute(
                action,
                resolved,
                params,
                ActionDispatchContext(workflow_context=context, dry_run=step_dry_run),
            )
            context.actions[step.id] = action_result
            context.invalidate_observation_cache()
            metadata: dict[str, Any] = {}
            before_observation = context.latest_observation_or_none
            browser_observation = run_browser_post_action_observe(
                context,
                step_id=step.id,
                enabled=action_result.status == ActionStatus.SUCCESS
                and action_result.metadata.get("execution") == "playwright"
                and bool(params.get("browser_post_action_observe", True)),
                assert_text=str(params.get("post_action_assert_text") or params.get("assert_text") or ""),
            )
            if browser_observation is not None:
                metadata["browser_post_action_observe"] = browser_observation
            post_action_observe = run_post_action_observe(
                params.get("post_action_observe"),
                context,
                step_id=step.id,
                enabled=action_result.status == ActionStatus.SUCCESS,
            )
            if post_action_observe is not None:
                metadata["post_action_observe"] = post_action_observe
            operation_receipt = operation_receipt_for_action(
                context,
                action=action,
                resolved=resolved,
                action_result=action_result,
                before_observation=before_observation,
                browser_post_action_observe=browser_observation,
                post_action_observe=post_action_observe,
                live=not step_dry_run,
            )
            if operation_receipt is not None:
                metadata["operation_receipt"] = operation_receipt
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=action_result.status,
                message=action_result.message,
                resolved_target=resolved,
                action_result=action_result,
                metadata=metadata,
            )

        if action == "assert_text":
            observation = context.observations.get(str(params.get("observation", ""))) or context.latest_observation
            text = normalize_text(require_param(params, "text"))
            if observation is not None and observation.provider == ProviderKind.OCR and observation.metadata.get("engine_available") is False:
                install_hint = observation.metadata.get("install_hint") or "Install and configure OCR before asserting screen text."
                return self._assertion_failed(step, action, f"OCR engine unavailable; cannot assert text: {params['text']}. {install_hint}", soft=bool(params.get("soft_assert")), metadata={"expected": text, "reason": "ocr_unavailable"})
            passed = observation_contains_text(observation, text)
            if not passed:
                if bool(params.get("ocr_verify")):
                    passed = self._ocr_verify_text(context, text)
            if not passed:
                return self._assertion_failed(step, action, f"Text not found in observation: {params['text']}", soft=bool(params.get("soft_assert")), metadata={"expected": text, "ocr_verify": bool(params.get("ocr_verify"))})
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message=f"text found: {params['text']}",
                metadata={"ocr_verify": bool(params.get("ocr_verify"))},
            )

        if action == "assert_text_contract":
            observation = context.observations.get(str(params.get("observation", ""))) or context.latest_observation
            contract = evaluate_text_contract(observation, params)
            if not contract["passed"]:
                if bool(params.get("ocr_verify")):
                    contract = dict(contract)
                    contract["ocr_verify"] = self._ocr_verify_text_contract(context, params)
                    if contract["ocr_verify"]:
                        return WorkflowStepResult(
                            id=step.id,
                            action=action,
                            status=ActionStatus.SUCCESS,
                            message="text contract matched",
                            metadata={"text_contract": contract},
                        )
                return self._assertion_failed(step, action, text_contract_failure_message(contract), soft=bool(params.get("soft_assert")), metadata={"text_contract": contract})
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message="text contract matched",
                metadata={"text_contract": contract},
            )

        if action == "assert_no_error":
            observation = context.observations.get(str(params.get("observation", ""))) or context.latest_observation
            result = evaluate_no_error_state(observation, network_events=context.resources.get("network_events", []))
            if not result["passed"]:
                return self._assertion_failed(step, action, "error state detected", soft=bool(params.get("soft_assert")), metadata={"no_error": result})
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message="no error state detected",
                metadata={"no_error": result},
            )

        if action == "assert_browser_ready":
            observation = context.observations.get(str(params.get("observation", ""))) or context.latest_observation
            result = evaluate_browser_readiness(observation, params, network_events=context.resources.get("network_events", []))
            if not result.passed:
                return self._assertion_failed(step, action, browser_readiness_failure_message(result), soft=bool(params.get("soft_assert")), metadata={"browser_ready": result})
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message="browser ready",
                metadata={"browser_ready": result},
            )

        if action == "assert_product_contract":
            observation = context.observations.get(str(params.get("observation", ""))) or context.latest_observation
            contract = evaluate_product_contract(observation, params, network_events=context.resources.get("network_events", []))
            if not contract.passed:
                return self._assertion_failed(step, action, product_contract_failure_message(contract), soft=bool(params.get("soft_assert")), metadata={"product_contract": contract})
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message="product contract matched",
                metadata={"product_contract": contract},
            )

        if action == "assert_ai_response_quality":
            observation = context.observations.get(str(params.get("observation", ""))) or context.latest_observation_or_none
            result = evaluate_ai_response_quality(
                params,
                observation=observation,
                network_events=context.resources.get("network_events", []),
            )
            if not result.passed:
                return self._assertion_failed(step, action, ai_quality_failure_message(result), soft=bool(params.get("soft_assert")), metadata={"ai_response_quality": result})
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message="AI response quality matched",
                metadata={"ai_response_quality": result},
            )

        if action == "request_api":
            return self._request_api(step, context, dry_run=step_dry_run)

        if action == "assert_response":
            event = wait_for_network_response(
                context.resources.get("network_events", []),
                params,
                page=context.resources.get("playwright_page"),
            )
            if event is None:
                return self._assertion_failed(step, action, f"Network response not found: {network_assertion_label(params)}", soft=bool(params.get("soft_assert")), metadata={"event": None})
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message="network response matched",
                metadata={"event": event},
            )

        if action == "expect_download":
            return self._expect_download(step, context, dry_run=step_dry_run)

        if action == "assert_file_exists":
            return self._assert_file_exists(step, context)

        if action == "assert_element_exists":
            return self._assert_element_exists(step, context)

        if action == "assert_url_contains":
            return self._assert_url_contains(step, context)

        if action == "assert_count":
            return self._assert_count(step, context)

        if action == "assert_attribute":
            return self._assert_attribute(step, context)

        if action == "assert_no_layout_overlap":
            return self._assert_no_layout_overlap(step, context)

        if action == "assert_visual_quality":
            return self._assert_visual_quality(step, context)

        if action == "save_storage_state":
            return self._save_storage_state(step, context, dry_run=step_dry_run)

        if action == "wait_for":
            return self._wait_for(step, context)

        raise ValueError(f"Unsupported workflow action: {action}")

    def _click_visual(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        *,
        dry_run: bool,
        synthetic_on_capture_fail: bool,
    ) -> WorkflowStepResult:
        description = str(step.params.get("description") or step.params.get("text") or step.params.get("label") or "").strip()
        if not description:
            raise ValueError("click_visual requires description, text, or label.")
        provider = str(step.params.get("provider") or "omniparser").strip().lower()
        image, path, metadata = capture_visual_region(
            step.params,
            output_dir=context.run_dir,
            label=f"{step.id}-visual",
            synthetic_on_capture_fail=synthetic_on_capture_fail,
        )
        locator = build_locator(provider)
        location = locator.locate(image, path, description)
        target = Target(text=description, preferred=(ProviderKind.VISION,))
        action_result = self.dispatcher.actions.click(
            Point(x=location.x, y=location.y),
            target,
            provider=ProviderKind.VISION,
            dry_run=dry_run,
        )
        action_result = ActionResult(
            action="click_visual",
            status=action_result.status,
            target=action_result.target,
            point=action_result.point,
            provider=ProviderKind.VISION,
            message="click skipped by dry-run" if dry_run else f"visual click: {description}",
            metadata={
                **metadata,
                "provider": provider,
                "confidence": location.confidence,
                "reason": location.reason,
                "screenshot_path": str(path),
                "location": {"x": location.x, "y": location.y},
            },
        )
        context.actions[step.id] = action_result
        return WorkflowStepResult(
            id=step.id,
            action=step.action,
            status=action_result.status,
            message=action_result.message,
            action_result=action_result,
            metadata=action_result.metadata,
        )

    def _assert_visual_text(
        self,
        step: WorkflowStep,
        context: WorkflowContext,
        *,
        synthetic_on_capture_fail: bool,
    ) -> WorkflowStepResult:
        text = normalize_text(require_param(step.params, "text"))
        provider = str(step.params.get("provider") or "omniparser").strip().lower()
        region = step.params.get("region")
        image, path, metadata = capture_visual_region(
            step.params,
            output_dir=context.run_dir,
            label=f"{step.id}-visual",
            synthetic_on_capture_fail=synthetic_on_capture_fail,
        )
        locator = build_locator(provider)
        elements = locator.detect(image, path, text)
        matched_elements = [element for element in elements if self._element_matches_visual_text(element, text, region=region)]
        if not matched_elements:
            return self._assertion_failed(
                step,
                step.action,
                f"Visual text not found: {step.params['text']}",
                soft=bool(step.params.get("soft_assert")),
                metadata={
                    **metadata,
                    "provider": provider,
                    "screenshot_path": str(path),
                    "text": text,
                    "elements": list(elements)[:20],
                },
            )
        return WorkflowStepResult(
            id=step.id,
            action=step.action,
            status=ActionStatus.SUCCESS,
            message=f"visual text found: {step.params['text']}",
            metadata={
                **metadata,
                "provider": provider,
                "screenshot_path": str(path),
                "text": text,
                "matched_elements": matched_elements,
            },
        )

    def _element_matches_visual_text(self, element: dict[str, Any], text: str, *, region: Any = None) -> bool:
        normalized = normalize_text(text)
        haystack = normalize_text(" ".join(str(element.get(key) or "") for key in ("text", "label", "name", "role", "status")))
        if normalized not in haystack:
            return False
        if not isinstance(region, dict):
            return True
        bounds = element.get("bounds") if isinstance(element.get("bounds"), dict) else None
        if not isinstance(bounds, dict):
            return False
        try:
            left = int(bounds.get("left", 0))
            top = int(bounds.get("top", 0))
            width = int(bounds.get("width", 0))
            height = int(bounds.get("height", 0))
            region_left = int(region.get("left", 0))
            region_top = int(region.get("top", 0))
            region_width = int(region.get("width", 0))
            region_height = int(region.get("height", 0))
        except (TypeError, ValueError):
            return False
        return not (
            left + width <= region_left
            or region_left + region_width <= left
            or top + height <= region_top
            or region_top + region_height <= top
        )

    def _wait_for(self, step: WorkflowStep, context: WorkflowContext) -> WorkflowStepResult:
        params = step.params
        timeout_seconds = float(params.get("timeout_seconds", 5.0))
        interval_seconds = float(params.get("interval_seconds", 0.2))
        conditions = wait_for_conditions(params)
        match_mode = str(params.get("match") or params.get("mode") or "all").lower()
        if match_mode not in {"all", "any"}:
            raise ValueError(f"Unsupported wait_for match mode: {match_mode}")
        started = monotonic()
        attempts = 0
        last_error = ""
        last_results: list[dict[str, Any]] = []

        while monotonic() - started <= timeout_seconds:
            attempts += 1
            try:
                last_results = [self._evaluate_wait_condition(condition, context, step.id) for condition in conditions]
                matched = all(result["matched"] for result in last_results) if match_mode == "all" else any(
                    result["matched"] for result in last_results
                )
                if matched:
                    resolved = next((result.get("resolved_target") for result in last_results if result.get("resolved_target") is not None), None)
                    if resolved is not None:
                        context.resolved_targets[step.id] = resolved
                    metadata = {
                        "attempts": attempts,
                        "match": match_mode,
                        "conditions": [wait_condition_metadata(result) for result in last_results],
                    }
                    if len(conditions) == 1:
                        message = f"wait_for {conditions[0]['condition']} matched"
                    else:
                        message = f"wait_for {match_mode} conditions matched"
                    return WorkflowStepResult(
                        id=step.id,
                        action=step.action,
                        status=ActionStatus.SUCCESS,
                        message=message,
                        resolved_target=resolved,
                        metadata=metadata,
                    )
                last_error = wait_for_last_error(last_results, match_mode)
            except Exception as exc:
                last_error = str(exc)

            page = context.resources.get("playwright_page")
            if page is not None:
                page.wait_for_timeout(int(interval_seconds * 1000))
            else:
                sleep(interval_seconds)

        condition_labels = ", ".join(wait_condition_label(condition) for condition in conditions)
        raise TimeoutError(
            f"wait_for timed out after {timeout_seconds:.3f}s. "
            f"Conditions: {condition_labels}. Last error: {last_error}"
        )

    def _evaluate_wait_condition(
        self,
        condition: dict[str, Any],
        context: WorkflowContext,
        step_id: str,
    ) -> dict[str, Any]:
        condition_type = str(condition.get("condition") or condition.get("type") or "").strip()
        if condition_type == "text":
            observation = context.observations.get(str(condition.get("observation", ""))) or context.latest_observation
            text = normalize_text(require_param(condition, "text"))
            matched = observation_contains_text(observation, text)
            return {
                "condition": "text",
                "matched": matched,
                "label": str(condition.get("text")),
                "reason": "text found" if matched else f"Text not found: {condition.get('text')}",
            }
        if condition_type == "target":
            observation = context.observations.get(str(condition.get("observation", ""))) or context.latest_observation
            resolved = self.selector.resolve(target_from_config(require_param(condition, "target")), observation)
            return {
                "condition": "target",
                "matched": True,
                "label": target_from_config(condition["target"]).display_name,
                "reason": "target resolved",
                "resolved_target": resolved,
            }
        if condition_type == "selector":
            selector = str(require_param(condition, "selector"))
            matched = wait_selector_exists(selector, condition, context)
            return {
                "condition": "selector",
                "matched": matched,
                "label": selector,
                "reason": "selector found" if matched else f"Selector not found: {selector}",
            }
        if condition_type == "url":
            current_url = current_wait_url(condition, context)
            matched = url_matches_condition(current_url, condition)
            return {
                "condition": "url",
                "matched": matched,
                "label": wait_condition_label(condition),
                "actual_url": current_url,
                "reason": "url matched" if matched else f"URL did not match: {current_url}",
            }
        if condition_type == "response":
            event = wait_for_network_response(
                context.resources.get("network_events", []),
                condition,
                page=context.resources.get("playwright_page"),
            )
            return {
                "condition": "response",
                "matched": event is not None,
                "label": network_assertion_label(condition),
                "reason": "response matched" if event is not None else f"Response not found: {network_assertion_label(condition)}",
                "event": event,
            }
        raise ValueError(f"Unsupported wait_for condition: {condition_type}")

    def _resolve_for_action(
        self,
        params: dict[str, Any],
        context: WorkflowContext,
        step_id: str,
        *,
        action: str = "action",
    ) -> ResolvedTarget:
        if "target" in params:
            observation = context.observations.get(str(params.get("observation", ""))) or context.latest_observation
            resolved = self.selector.resolve(target_from_config(params["target"]), observation)
            ensure_action_target_exists(action, resolved, params)
            context.resolved_targets[step_id] = resolved
            return resolved
        resolved = context.latest_resolved_target
        ensure_action_target_exists(action, resolved, params)
        return resolved

    def _run_precondition(
        self,
        precondition: Any,
        context: WorkflowContext,
        *,
        run_profile: RunProfileName,
        synthetic_on_capture_fail: bool,
    ) -> WorkflowStepResult:
        if isinstance(precondition, str):
            item = precondition.strip()
            if item.startswith("workflow:"):
                nested = self._run_nested_workflow(
                    item.removeprefix("workflow:").strip(),
                    context,
                    run_profile=run_profile,
                    synthetic_on_capture_fail=synthetic_on_capture_fail,
                    dry_run=True,
                    inputs=context.inputs,
                )
                return WorkflowStepResult(
                    id=f"precondition_{sanitize_filename(item)}",
                    action="precondition_workflow",
                    status=ActionStatus.SUCCESS if all(step.status != ActionStatus.FAILED for step in nested.steps) else ActionStatus.FAILED,
                    message=f"precondition workflow completed: {nested.workflow_name}",
                    metadata={"nested_run": to_jsonable(nested)},
                )
            fixture_name = item.removeprefix("fixture:").strip() if item.startswith("fixture:") else item
            loaded = load_named_fixture(context, fixture_name)
            return WorkflowStepResult(
                id=f"precondition_{sanitize_filename(fixture_name)}",
                action="load_fixture",
                status=ActionStatus.SUCCESS,
                message=f"fixture loaded: {fixture_name}",
                metadata={"fixture": fixture_name, "fixture_type": loaded.get("type"), "source": loaded.get("page")},
            )
        if isinstance(precondition, dict):
            kind = str(precondition.get("type") or precondition.get("kind") or "").strip()
            value = str(precondition.get("workflow") or precondition.get("fixture") or precondition.get("value") or "").strip()
            if kind == "workflow" or value.startswith("workflow:"):
                workflow_name = value.removeprefix("workflow:").strip()
                nested = self._run_nested_workflow(
                    workflow_name,
                    context,
                    run_profile=run_profile,
                    synthetic_on_capture_fail=synthetic_on_capture_fail,
                    dry_run=bool(precondition.get("dry_run", True)),
                    inputs=context.inputs,
                    from_step=str(precondition.get("from_step") or "") or None,
                )
                return WorkflowStepResult(
                    id=str(precondition.get("id") or f"precondition_{sanitize_filename(workflow_name)}"),
                    action="precondition_workflow",
                    status=ActionStatus.SUCCESS if all(step.status != ActionStatus.FAILED for step in nested.steps) else ActionStatus.FAILED,
                    message=f"precondition workflow completed: {nested.workflow_name}",
                    metadata={"nested_run": to_jsonable(nested)},
                )
            fixture_name = value.removeprefix("fixture:").strip() if value.startswith("fixture:") else value
            loaded = load_named_fixture(context, fixture_name)
            return WorkflowStepResult(
                id=str(precondition.get("id") or f"precondition_{sanitize_filename(fixture_name)}"),
                action="load_fixture",
                status=ActionStatus.SUCCESS,
                message=f"fixture loaded: {fixture_name}",
                metadata={"fixture": fixture_name, "fixture_type": loaded.get("type"), "source": loaded.get("page"), "precondition": precondition.get("type") or precondition.get("kind")},
            )
        raise ValueError("precondition entries must be strings or objects.")

    def _run_nested_workflow(
        self,
        workflow_name: str,
        context: WorkflowContext,
        *,
        run_profile: RunProfileName,
        synthetic_on_capture_fail: bool,
        dry_run: bool,
        inputs: dict[str, Any],
        from_step: str | None = None,
    ) -> WorkflowRunResult:
        workflow_path = resolve_workflow_reference(workflow_name, context)
        nested_workflow = parse_workflow_file(workflow_path)
        nested_runtime = WorkflowRuntime(output_dir=self.audit.root_dir, providers=self.providers, dispatcher=self.dispatcher)
        return nested_runtime.run(
            nested_workflow,
            dry_run=dry_run,
            run_profile=run_profile,
            synthetic_on_capture_fail=synthetic_on_capture_fail,
            inputs=inputs,
            sensitive_fields=context.sensitive_fields,
            workspace_root=context.resources.get("workspace_root"),
            from_step=from_step,
            use_lock=False,
        )

    def _expect_download(self, step: WorkflowStep, context: WorkflowContext, *, dry_run: bool) -> WorkflowStepResult:
        params = step.params
        page = context.resources.get("playwright_page")
        if page is None:
            raise RuntimeError("expect_download requires observe_browser.")

        resolved = self._resolve_for_action(params, context, step.id)
        selector = selector_from_resolved(resolved)
        if not selector:
            raise RuntimeError("expect_download requires a DOM selector handle.")

        is_dry_run = bool(params.get("dry_run", dry_run))
        if is_dry_run:
            action_result = ActionResult(
                action="expect_download",
                status=ActionStatus.DRY_RUN,
                target=resolved.target.display_name,
                point=None,
                provider=resolved.evidence.provider,
                message="download skipped by dry-run",
                metadata={"execution": "playwright", "selector": selector},
            )
            context.actions[step.id] = action_result
            return WorkflowStepResult(
                id=step.id,
                action=step.action,
                status=ActionStatus.DRY_RUN,
                message=action_result.message,
                resolved_target=resolved,
                action_result=action_result,
            )

        timeout_ms = int(float(params.get("timeout_seconds", 10.0)) * 1000)
        with page.expect_download(timeout=timeout_ms) as download_info:
            page.locator(selector).click()
        download = download_info.value
        downloads_dir = context.run_dir / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        save_name = str(params.get("save_as") or download.suggested_filename)
        save_path = downloads_dir / sanitize_filename(save_name)
        download.save_as(save_path)
        metadata = file_metadata(save_path)
        metadata.update(
            {
                "execution": "playwright",
                "selector": selector,
                "suggested_filename": download.suggested_filename,
                "path": str(save_path),
            }
        )
        downloads = context.resources.setdefault("downloads", {})
        if isinstance(downloads, dict):
            downloads[step.id] = metadata
            downloads["latest"] = metadata

        action_result = ActionResult(
            action="expect_download",
            status=ActionStatus.SUCCESS,
            target=resolved.target.display_name,
            point=None,
            provider=resolved.evidence.provider,
            message="download saved",
            metadata=metadata,
        )
        context.actions[step.id] = action_result
        return WorkflowStepResult(
            id=step.id,
            action=step.action,
            status=ActionStatus.SUCCESS,
            message="download saved",
            resolved_target=resolved,
            action_result=action_result,
        )

    def _assert_file_exists(self, step: WorkflowStep, context: WorkflowContext) -> WorkflowStepResult:
        metadata = resolve_file_assertion_target(step.params, context)
        path = Path(str(metadata["path"]))
        if not path.exists() or not path.is_file():
            return self._assertion_failed(step, step.action, f"File not found: {path}", soft=bool(step.params.get("soft_assert")), metadata={"path": str(path)})
        actual = file_metadata(path)
        min_bytes = step.params.get("min_bytes")
        if min_bytes is not None and int(actual["size_bytes"]) < int(min_bytes):
            return self._assertion_failed(
                step,
                step.action,
                f"File too small: {actual['size_bytes']} < {min_bytes}",
                soft=bool(step.params.get("soft_assert")),
                metadata={"path": str(path), **actual},
            )
        extension = step.params.get("extension")
        if extension and path.suffix.lower() != normalize_extension(str(extension)):
            return self._assertion_failed(
                step,
                step.action,
                f"File extension mismatch: {path.suffix} != {extension}",
                soft=bool(step.params.get("soft_assert")),
                metadata={"path": str(path), **actual},
            )
        return WorkflowStepResult(
            id=step.id,
            action=step.action,
            status=ActionStatus.SUCCESS,
            message="file assertion passed",
            metadata=actual,
        )

    def _assert_element_exists(self, step: WorkflowStep, context: WorkflowContext) -> WorkflowStepResult:
        page = context.resources.get("playwright_page")
        selector = str(step.params.get("selector") or "").strip()
        if not selector:
            raise ValueError("assert_element_exists requires selector.")
        count = self._count_selector(selector, page=page, observation=context.latest_observation)
        if count <= 0:
            return self._assertion_failed(
                step,
                step.action,
                f"Element not found: {selector}",
                soft=bool(step.params.get("soft_assert")),
                metadata={"selector": selector, "count": count},
            )
        return WorkflowStepResult(id=step.id, action=step.action, status=ActionStatus.SUCCESS, message=f"element found: {selector}", metadata={"selector": selector, "count": count})

    def _assert_url_contains(self, step: WorkflowStep, context: WorkflowContext) -> WorkflowStepResult:
        fragment = str(step.params.get("fragment") or "").strip()
        if not fragment:
            raise ValueError("assert_url_contains requires fragment.")
        current_url = self._current_url(context)
        if fragment not in current_url:
            return self._assertion_failed(
                step,
                step.action,
                f"URL does not contain fragment: {fragment}",
                soft=bool(step.params.get("soft_assert")),
                metadata={"fragment": fragment, "current_url": current_url},
            )
        return WorkflowStepResult(id=step.id, action=step.action, status=ActionStatus.SUCCESS, message=f"url contains: {fragment}", metadata={"fragment": fragment, "current_url": current_url})

    def _assert_count(self, step: WorkflowStep, context: WorkflowContext) -> WorkflowStepResult:
        selector = str(step.params.get("selector") or "").strip()
        if not selector:
            raise ValueError("assert_count requires selector.")
        page = context.resources.get("playwright_page")
        count = self._count_selector(selector, page=page, observation=context.latest_observation)
        min_count = int(step.params.get("min", 0) or 0)
        max_count = int(step.params.get("max", 10**9) or 10**9)
        if count < min_count or count > max_count:
            return self._assertion_failed(
                step,
                step.action,
                f"Count out of range for {selector}: {count} not in [{min_count}, {max_count}]",
                soft=bool(step.params.get("soft_assert")),
                metadata={"selector": selector, "count": count, "min": min_count, "max": max_count},
            )
        return WorkflowStepResult(id=step.id, action=step.action, status=ActionStatus.SUCCESS, message=f"count ok: {selector}={count}", metadata={"selector": selector, "count": count, "min": min_count, "max": max_count})

    def _assert_attribute(self, step: WorkflowStep, context: WorkflowContext) -> WorkflowStepResult:
        selector = str(step.params.get("selector") or "").strip()
        attr = str(step.params.get("attr") or "").strip()
        expected = step.params.get("value")
        if not selector or not attr:
            raise ValueError("assert_attribute requires selector and attr.")
        page = context.resources.get("playwright_page")
        actual = self._read_selector_attribute(selector, attr, page=page, observation=context.latest_observation)
        if str(actual) != str(expected):
            return self._assertion_failed(
                step,
                step.action,
                f"Attribute mismatch for {selector}[{attr}]: {actual} != {expected}",
                soft=bool(step.params.get("soft_assert")),
                metadata={"selector": selector, "attr": attr, "value": expected, "actual": actual},
            )
        return WorkflowStepResult(id=step.id, action=step.action, status=ActionStatus.SUCCESS, message=f"attribute matched: {selector}[{attr}]", metadata={"selector": selector, "attr": attr, "value": expected, "actual": actual})

    def _assert_no_layout_overlap(self, step: WorkflowStep, context: WorkflowContext) -> WorkflowStepResult:
        page = context.resources.get("playwright_page")
        if page is None:
            raise RuntimeError("assert_no_layout_overlap requires observe_browser.")
        overlaps = page.evaluate(
            """
            () => {
              const nodes = Array.from(document.querySelectorAll('*')).filter((el) => {
                const r = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              });
              const rects = nodes.map((el) => {
                const r = el.getBoundingClientRect();
                return {
                  tag: el.tagName,
                  text: (el.innerText || el.textContent || '').trim().slice(0, 80),
                  left: r.left,
                  top: r.top,
                  right: r.right,
                  bottom: r.bottom,
                };
              });
              const overlaps = [];
              for (let i = 0; i < rects.length; i++) {
                for (let j = i + 1; j < rects.length; j++) {
                  const a = rects[i];
                  const b = rects[j];
                  const intersect = !(a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top);
                  if (intersect) {
                    overlaps.push({a, b});
                    if (overlaps.length >= 8) return overlaps;
                  }
                }
              }
              return overlaps;
            }
            """
        )
        overlap_list = overlaps if isinstance(overlaps, list) else []
        if overlap_list:
            return self._assertion_failed(
                step,
                step.action,
                f"Layout overlap detected: {len(overlap_list)}",
                soft=bool(step.params.get("soft_assert")),
                metadata={"overlaps": overlap_list},
            )
        return WorkflowStepResult(id=step.id, action=step.action, status=ActionStatus.SUCCESS, message="no layout overlap detected", metadata={"overlaps": []})

    def _assert_visual_quality(self, step: WorkflowStep, context: WorkflowContext) -> WorkflowStepResult:
        page = context.resources.get("playwright_page")
        if page is None:
            raise RuntimeError("assert_visual_quality requires a live browser session. Run observe_browser first.")
        config = VisualRuleConfig.from_params(step.params)
        snapshot = collect_visual_snapshot(page)
        if snapshot is None:
            raise RuntimeError("assert_visual_quality could not collect a visual snapshot from the current page.")
        report = evaluate_visual_snapshot(snapshot, config)
        artifact_path = write_visual_audit_artifact(context.run_dir, step.id, report)
        metadata = {"visual_audit": report.to_metadata(), "artifact_path": str(artifact_path)}
        if not report.passed:
            return self._assertion_failed(
                step,
                step.action,
                report.summary(),
                soft=bool(step.params.get("soft_assert")),
                metadata=metadata,
            )
        return WorkflowStepResult(
            id=step.id,
            action=step.action,
            status=ActionStatus.SUCCESS,
            message=report.summary(),
            metadata=metadata,
        )

    def _assertion_failed(self, step: WorkflowStep, action: str, message: str, *, soft: bool, metadata: dict[str, Any] | None = None) -> WorkflowStepResult:
        payload = {"soft_assert": soft}
        if metadata:
            payload.update(metadata)
        if soft:
            return WorkflowStepResult(id=step.id, action=action, status=ActionStatus.FAILED, message=message, metadata=payload)
        raise AssertionError(message)

    def _current_url(self, context: WorkflowContext) -> str:
        page = context.resources.get("playwright_page")
        if page is not None and getattr(page, "url", None):
            return str(page.url)
        observation = context.latest_observation
        if observation is not None:
            return str(observation.metadata.get("url") or observation.source or "")
        return ""

    def _count_selector(self, selector: str, *, page: Any, observation: Observation | None) -> int:
        if page is not None:
            try:
                return int(page.locator(selector).count())
            except Exception:
                pass
        if observation is not None and observation.elements:
            return sum(1 for item in observation.elements if isinstance(item, dict) and selector in str(item.get("selector") or item.get("text") or ""))
        return 0

    def _read_selector_attribute(self, selector: str, attr: str, *, page: Any, observation: Observation | None) -> Any:
        if page is not None:
            try:
                return page.locator(selector).first.get_attribute(attr)
            except Exception:
                pass
        if observation is not None and observation.elements:
            for item in observation.elements:
                if not isinstance(item, dict):
                    continue
                if selector in str(item.get("selector") or ""):
                    return item.get(attr)
        return None

    def _ocr_verify_text(self, context: WorkflowContext, text: str) -> bool:
        try:
            observation = self.providers.observe(
                "observe_ocr",
                {},
                ProviderContext(
                    run_dir=context.run_dir,
                    synthetic_on_capture_fail=False,
                    resources=context.resources,
                ),
            )
        except Exception:
            return False
        return observation_contains_text(observation, text)

    def _ocr_verify_text_contract(self, context: WorkflowContext, params: dict[str, Any]) -> bool:
        text = str(params.get("text") or "").strip()
        if not text:
            return False
        return self._ocr_verify_text(context, text)

    def _request_api(self, step: WorkflowStep, context: WorkflowContext, *, dry_run: bool) -> WorkflowStepResult:
        params = step.params
        url = str(require_param(params, "url"))
        method = str(params.get("method") or "GET").upper()
        headers = {str(key): str(value) for key, value in dict(params.get("headers") or {}).items()}
        body = api_request_body(params)
        timeout = float(params.get("timeout_seconds", 10.0))
        event: dict[str, Any] = {
            "type": "response",
            "url": url,
            "method": method,
            "status": 0,
            "ok": False,
            "dry_run": dry_run,
        }
        if dry_run:
            status = int(params.get("mock_status", 200) or 200)
            event.update({"status": status, "ok": status < 400})
            append_network_event(context, event)
            return WorkflowStepResult(
                id=step.id,
                action=step.action,
                status=ActionStatus.DRY_RUN,
                message=f"would request {method} {url}",
                metadata={"event": event},
            )
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(max(0, int(params.get("max_body_bytes", 4096) or 4096)))
                status = int(getattr(response, "status", response.getcode()))
                event.update(
                    {
                        "status": status,
                        "ok": status < 400,
                        "response_body_preview": raw.decode("utf-8", errors="replace"),
                        "content_type": response.headers.get("content-type"),
                    }
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read(max(0, int(params.get("max_body_bytes", 4096) or 4096)))
            event.update(
                {
                    "status": int(exc.code),
                    "ok": False,
                    "response_body_preview": raw.decode("utf-8", errors="replace"),
                    "failure": str(exc),
                }
            )
        except Exception as exc:
            event.update({"type": "request_failed", "failure": str(exc)})
        append_network_event(context, event)
        if event["type"] == "request_failed":
            raise AssertionError(f"API request failed: {event['failure']}")
        return WorkflowStepResult(
            id=step.id,
            action=step.action,
            status=ActionStatus.SUCCESS,
            message=f"API response {event['status']}: {method} {url}",
            metadata={"event": event},
        )

    def _save_storage_state(self, step: WorkflowStep, context: WorkflowContext, *, dry_run: bool) -> WorkflowStepResult:
        browser_context = context.resources.get("playwright_context")
        if browser_context is None:
            raise RuntimeError("save_storage_state requires observe_browser.")
        path = resolve_output_path(step.params.get("path") or "storage_state.json", context.run_dir)
        if dry_run:
            return WorkflowStepResult(
                id=step.id,
                action=step.action,
                status=ActionStatus.DRY_RUN,
                message="storage state save skipped by run profile",
                metadata={"path": str(path)},
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        browser_context.storage_state(path=str(path))
        metadata = file_metadata(path)
        context.resources["storage_state"] = metadata
        return WorkflowStepResult(
            id=step.id,
            action=step.action,
            status=ActionStatus.SUCCESS,
            message="storage state saved",
            metadata=metadata,
        )

    def _write_step_result(self, run_dir: Path, result: WorkflowStepResult) -> None:
        path = run_dir / f"{result.id}.json"
        path.write_text(
            json.dumps(to_jsonable(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def ensure_action_target_exists(action: str, resolved: ResolvedTarget, params: dict[str, Any]) -> None:
    if bool(params.get("allow_mock_target", False)) or bool(params.get("allow_fallback_target", False)):
        return
    resolution = resolved.evidence.metadata.get("selector_resolution")
    if not isinstance(resolution, dict):
        return
    selected_provider = str(resolution.get("selected_provider") or resolved.evidence.provider.value)
    if selected_provider != ProviderKind.MOCK.value:
        return
    fallback_path = [str(item) for item in resolution.get("fallback_path") or []]
    non_mock_attempted = [provider for provider in fallback_path if provider != ProviderKind.MOCK.value]
    if not non_mock_attempted:
        return
    failed_attempts = [
        f"{attempt.get('provider')}:{attempt.get('status')}"
        for attempt in resolution.get("attempts") or []
        if isinstance(attempt, dict) and attempt.get("provider") != ProviderKind.MOCK.value
    ]
    path = " -> ".join(fallback_path) if fallback_path else selected_provider
    detail = ", ".join(failed_attempts) if failed_attempts else "structured providers did not match"
    raise LookupError(
        f"{action} target existence check failed for '{resolved.target.display_name}': "
        f"structured providers did not locate the target before mock fallback. "
        f"fallback_path={path}; attempts={detail}. "
        "Use a more stable target or set allow_mock_target=true for intentional mock-only execution."
    )


def wait_for_conditions(params: dict[str, Any]) -> list[dict[str, Any]]:
    raw_conditions = params.get("conditions")
    if raw_conditions is not None:
        if not isinstance(raw_conditions, list) or not raw_conditions:
            raise ValueError("wait_for conditions must be a non-empty list.")
        return [wait_condition_from_dict(item, params) for item in raw_conditions]
    condition = str(require_param(params, "condition"))
    return [wait_condition_from_dict({**params, "condition": condition}, params)]


def wait_condition_from_dict(raw: Any, parent: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("wait_for condition entries must be objects.")
    condition = str(raw.get("condition") or raw.get("type") or parent.get("condition") or "").strip()
    if not condition:
        raise ValueError("wait_for condition entry is missing condition/type.")
    item = {key: value for key, value in raw.items() if key not in {"timeout_seconds", "interval_seconds", "match", "mode"}}
    item["condition"] = condition
    if "observation" not in item and parent.get("observation"):
        item["observation"] = parent["observation"]
    return item


def wait_condition_metadata(result: dict[str, Any]) -> dict[str, Any]:
    metadata = {key: value for key, value in result.items() if key not in {"resolved_target"}}
    if isinstance(metadata.get("event"), dict):
        metadata["event"] = dict(metadata["event"])
    return metadata


def wait_for_last_error(results: list[dict[str, Any]], match_mode: str) -> str:
    if not results:
        return "no conditions evaluated"
    misses = [str(result.get("reason") or result.get("label") or result.get("condition")) for result in results if not result.get("matched")]
    if not misses and match_mode == "any":
        return "no condition matched"
    return "; ".join(misses) if misses else "conditions not satisfied"


def wait_condition_label(condition: dict[str, Any]) -> str:
    condition_type = str(condition.get("condition") or condition.get("type") or "")
    if condition_type == "text":
        return f"text={condition.get('text')}"
    if condition_type == "target":
        return f"target={condition.get('target')}"
    if condition_type == "selector":
        return f"selector={condition.get('selector')}"
    if condition_type == "url":
        parts = []
        for key in ("url", "url_contains", "url_regex"):
            if key in condition:
                parts.append(f"{key}={condition[key]}")
        return "url(" + ", ".join(parts) + ")"
    if condition_type == "response":
        return "response(" + network_assertion_label(condition) + ")"
    return condition_type or "unknown"


def wait_selector_exists(selector: str, condition: dict[str, Any], context: WorkflowContext) -> bool:
    page = context.resources.get("playwright_page")
    if page is not None:
        try:
            return page.locator(selector).count() > 0
        except Exception:
            return False
    observation = context.observations.get(str(condition.get("observation", ""))) or context.latest_observation
    for element in observation.elements:
        if str(element.get("selector") or "") == selector:
            return True
    return False


def current_wait_url(condition: dict[str, Any], context: WorkflowContext) -> str:
    page = context.resources.get("playwright_page")
    if page is not None:
        return str(page.url)
    observation = context.observations.get(str(condition.get("observation", ""))) or context.latest_observation
    return str(observation.metadata.get("url") or observation.source or "")


def url_matches_condition(current_url: str, condition: dict[str, Any]) -> bool:
    expected = condition.get("url")
    if expected is not None and str(expected) != current_url:
        return False
    contains = condition.get("url_contains")
    if contains is not None and str(contains) not in current_url:
        return False
    regex = condition.get("url_regex")
    if regex is not None:
        try:
            if re.search(str(regex), current_url) is None:
                return False
        except re.error:
            return False
    return any(key in condition for key in ("url", "url_contains", "url_regex"))


def parse_workflow_file(path: str | Path) -> Workflow:
    workflow_path = Path(path)
    text = workflow_path.read_text(encoding="utf-8")
    if workflow_path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required for YAML workflows. Run: pip install PyYAML") from exc
        payload = yaml.safe_load(text)
    return workflow_from_dict(payload)


def workflow_from_dict(payload: dict[str, Any]) -> Workflow:
    try:
        payload = migrate_workflow_payload(payload)
    except UnsupportedSchemaVersionError:
        payload = dict(payload)
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("Workflow requires a non-empty steps list.")

    steps: list[WorkflowStep] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise ValueError(f"Workflow step {index} must be an object.")
        step_id = str(raw.get("id") or f"step_{index + 1}")
        action = str(raw.get("action") or "").strip()
        if not action:
            raise ValueError(f"Workflow step '{step_id}' is missing action.")
        params = {key: value for key, value in raw.items() if key not in {"id", "action"}}
        steps.append(WorkflowStep(id=step_id, action=action, params=params))

    raw_variables = payload.get("variables") or {}
    if not isinstance(raw_variables, dict):
        raise ValueError("Workflow variables must be an object.")
    fixtures = tuple(str(item) for item in string_sequence(payload.get("fixtures")))
    preconditions = tuple(any_sequence(payload.get("preconditions") if payload.get("preconditions") is not None else payload.get("precondition")))

    return Workflow(
        name=str(payload.get("name") or "unnamed-workflow"),
        version=int(payload.get("version") or 1),
        steps=tuple(steps),
        schema_version=int(payload["schema_version"]) if payload.get("schema_version") not in (None, "") else None,
        min_runtime_version=str(payload["min_runtime_version"]) if payload.get("min_runtime_version") not in (None, "") else None,
        variables={str(key): value for key, value in raw_variables.items()},
        fixtures=fixtures,
        preconditions=preconditions,
        tags=tuple(str(item) for item in payload.get("tags", []) or []),
        affects=tuple(str(item) for item in payload.get("affects", []) or []),
        description=str(payload.get("description") or ""),
        visibility=str(payload.get("visibility") or "private"),
        author=str(payload.get("author") or ""),
        license=str(payload.get("license") or ""),
    )


def string_sequence(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    raise ValueError("Expected a string or list of strings.")


def any_sequence(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def target_from_config(value: Any) -> Target:
    if isinstance(value, str):
        return Target.from_text(value)
    if not isinstance(value, dict):
        raise ValueError("target must be a string or object.")

    preferred = value.get("preferred")
    preferred_tuple = Target().preferred
    if preferred is not None:
        if not isinstance(preferred, list):
            raise ValueError("target.preferred must be a list.")
        preferred_tuple = tuple(ProviderKind(item) for item in preferred)

    return Target(
        text=value.get("text"),
        role=value.get("role"),
        label=value.get("label"),
        selector=value.get("selector"),
        test_id=value.get("test_id"),
        contains_text=value.get("contains_text"),
        text_regex=value.get("text_regex"),
        row_text=value.get("row_text"),
        row_contains_text=value.get("row_contains_text"),
        row_text_regex=value.get("row_text_regex"),
        column_header=value.get("column_header"),
        column_contains_text=value.get("column_contains_text"),
        column_text_regex=value.get("column_text_regex"),
        near_text=value.get("near_text"),
        near_contains_text=value.get("near_contains_text"),
        near_text_regex=value.get("near_text_regex"),
        scope_role=value.get("scope_role"),
        scope_text=value.get("scope_text"),
        scope_contains_text=value.get("scope_contains_text"),
        window_title=value.get("window_title"),
        preferred=preferred_tuple,
    )


def require_param(params: dict[str, Any], name: str) -> Any:
    if name not in params or params[name] in (None, ""):
        raise ValueError(f"Missing required parameter: {name}")
    return params[name]


def retry_config(params: dict[str, Any]) -> dict[str, float | int]:
    raw_retry = params.get("retry", 0)
    if isinstance(raw_retry, dict):
        retries = int(raw_retry.get("count", 0))
        delay_seconds = float(raw_retry.get("delay_seconds", 0.0))
    else:
        retries = int(raw_retry or 0)
        delay_seconds = float(params.get("retry_delay_seconds", 0.0))
    return {"attempts": max(1, retries + 1), "delay_seconds": max(0.0, delay_seconds)}


def is_retry_safe_action(action: str) -> bool:
    return action.startswith("observe_") or action == "wait_for" or action in {
        "assert_text",
        "assert_text_contract",
        "assert_no_error",
        "assert_browser_ready",
        "assert_product_contract",
        "assert_ai_response_quality",
        "assert_response",
        "assert_file_exists",
        "assert_visual_quality",
    }


def write_visual_audit_artifact(run_dir: Path, label: str, report: VisualAuditReport) -> Path:
    visual_dir = run_dir / "visual"
    visual_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = visual_dir / f"{sanitize_filename(label)}.json"
    artifact_path.write_text(
        json.dumps(report.to_metadata(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return artifact_path


def api_request_body(params: dict[str, Any]) -> bytes | None:
    if "body" in params:
        value = params["body"]
    elif "json" in params:
        value = json.dumps(params["json"], ensure_ascii=False)
    else:
        return None
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def append_network_event(context: WorkflowContext, event: dict[str, Any]) -> None:
    events = context.resources.setdefault("network_events", [])
    if isinstance(events, list):
        events.append(event)


def run_browser_post_action_observe(
    context: WorkflowContext,
    *,
    step_id: str,
    enabled: bool,
    assert_text: str = "",
) -> dict[str, Any] | None:
    if not enabled:
        return None
    page = context.resources.get("playwright_page")
    if page is None:
        return None
    from .providers import browser_page_observation

    observation = browser_page_observation(
        page,
        run_dir=context.run_dir,
        label=f"{step_id}-browser-after",
        network_events=context.resources.get("network_events", []),
        console_events=context.resources.get("console_events", []),
        page_errors=context.resources.get("page_errors", []),
    )
    observation_id = f"{step_id}.browser_after"
    context.observations[observation_id] = observation
    payload = {
        "status": "observed",
        "observation": observation_id,
        "source": observation.source,
        "screenshot_path": str(observation.screenshot_path) if observation.screenshot_path is not None else None,
        "url": observation.metadata.get("url") or observation.source,
        "visible_text_length": observation.metadata.get("visible_text_length"),
        "interactive_count": observation.metadata.get("interactive_count"),
        "network_event_count": observation.metadata.get("network_event_count"),
        "failed_request_count": observation.metadata.get("failed_request_count"),
    }
    if assert_text.strip():
        payload["assert_text"] = assert_text.strip()
        payload["assertion"] = "matched" if observation_contains_text(observation, assert_text) else "missing"
    return payload


def operation_receipt_for_action(
    context: WorkflowContext,
    *,
    action: str,
    resolved: ResolvedTarget,
    action_result: ActionResult,
    before_observation: Observation | None,
    browser_post_action_observe: dict[str, Any] | None,
    post_action_observe: dict[str, Any] | None,
    live: bool,
) -> dict[str, Any] | None:
    if action_result.metadata.get("execution") == "playwright":
        return browser_operation_receipt(
            context,
            action=action,
            resolved=resolved,
            action_result=action_result,
            before_observation=before_observation,
            post_action_observe=browser_post_action_observe,
            live=live,
        )
    return desktop_operation_receipt(
        action=action,
        resolved=resolved,
        action_result=action_result,
        post_action_observe=post_action_observe,
        live=live,
    )


def browser_operation_receipt(
    context: WorkflowContext,
    *,
    action: str,
    resolved: ResolvedTarget,
    action_result: ActionResult,
    before_observation: Observation | None,
    post_action_observe: dict[str, Any] | None,
    live: bool,
) -> dict[str, Any] | None:
    before = before_observation
    before_metadata = before.metadata if before is not None else {}
    after = post_action_observe or {}
    before_url = str(before_metadata.get("url") or before.source if before is not None else "")
    after_url = str(after.get("url") or after.get("source") or before_url)
    before_text_length = int(before_metadata.get("visible_text_length") or 0)
    after_text_length = int(after.get("visible_text_length") or before_text_length)
    before_network_count = len(context.resources.get("network_events", []) or [])
    after_network_count = int(after.get("network_event_count") or before_network_count)
    receipt = {
        "schema_version": 1,
        "engine": "playwright",
        "action": action,
        "live": bool(live and action_result.status == ActionStatus.SUCCESS),
        "selector": action_result.metadata.get("selector") or selector_from_resolved(resolved) or "",
        "target": resolved.target.display_name,
        "status": action_result.status.value,
        "actionability": action_result.metadata.get("actionability") if isinstance(action_result.metadata.get("actionability"), dict) else None,
        "before": {
            "url": before_url,
            "visible_text_length": before_text_length,
            "interactive_count": int(before_metadata.get("interactive_count") or 0),
        },
        "after": {
            "url": after_url,
            "visible_text_length": after_text_length,
            "interactive_count": int(after.get("interactive_count") or before_metadata.get("interactive_count") or 0),
            "failed_request_count": int(after.get("failed_request_count") or 0),
        },
        "observed_after_action": bool(post_action_observe and post_action_observe.get("status") == "observed"),
        "changed": {
            "url": after_url != before_url,
            "visible_text_length": after_text_length != before_text_length,
            "network_event_count": after_network_count != before_network_count,
        },
    }
    screenshot = after.get("screenshot_path")
    if screenshot:
        receipt["after"]["screenshot_path"] = screenshot
    assertion = post_action_assertion_from_metadata(after)
    if assertion:
        receipt["post_action_assertion"] = assertion
    return receipt


def desktop_operation_receipt(
    *,
    action: str,
    resolved: ResolvedTarget,
    action_result: ActionResult,
    post_action_observe: dict[str, Any] | None,
    live: bool,
) -> dict[str, Any] | None:
    if action_result.status not in {ActionStatus.SUCCESS, ActionStatus.DRY_RUN}:
        return None
    after = post_action_observe or {}
    evidence = action_result.metadata.get("selector_evidence")
    selector_evidence = evidence if isinstance(evidence, dict) else {}
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "engine": str(action_result.provider.value if action_result.provider is not None else resolved.evidence.provider.value),
        "action": action,
        "live": bool(live and action_result.status == ActionStatus.SUCCESS),
        "target": action_result.target or resolved.target.display_name,
        "status": action_result.status.value,
        "actionability": desktop_actionability_snapshot(resolved, action_result, selector_evidence=selector_evidence),
        "observed_after_action": bool(post_action_observe and post_action_observe.get("status") == "observed"),
        "after": {
            "source": after.get("source"),
            "screenshot_path": after.get("screenshot_path"),
            "text_count": int(after.get("text_count") or 0),
            "engine": after.get("engine"),
            "engine_available": after.get("engine_available"),
        },
    }
    if selector_evidence:
        receipt["evidence"] = dict(selector_evidence)
    if action_result.point is not None:
        receipt["point"] = {"x": action_result.point.x, "y": action_result.point.y}
    assertion = post_action_assertion_from_metadata(after)
    if assertion:
        receipt["post_action_assertion"] = assertion
    return receipt


def post_action_assertion_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    assert_text = str(metadata.get("assert_text") or "").strip()
    assertion = str(metadata.get("assertion") or "").strip()
    if not assert_text and not assertion:
        return {}
    return {
        "type": "text",
        "expected": assert_text,
        "status": assertion or "unknown",
    }


def desktop_actionability_snapshot(
    resolved: ResolvedTarget,
    action_result: ActionResult,
    *,
    selector_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    point = action_result.point or resolved.evidence.click_point
    bounds = resolved.evidence.bounds
    evidence = selector_evidence or {}
    snapshot: dict[str, Any] = {
        "checked": True,
        "mode": "screen_point",
        "count": 1,
        "visible": point is not None,
        "enabled": True,
        "confidence": evidence.get("confidence", resolved.evidence.confidence),
        "reason": evidence.get("reason", resolved.evidence.reason),
    }
    if point is not None:
        snapshot["point"] = {"x": point.x, "y": point.y}
    if bounds is not None:
        snapshot["bounding_box"] = {
            "x": bounds.left,
            "y": bounds.top,
            "width": bounds.width,
            "height": bounds.height,
        }
    return snapshot


def run_post_action_observe(
    raw_params: Any,
    context: WorkflowContext,
    *,
    step_id: str,
    enabled: bool,
) -> dict[str, Any] | None:
    if raw_params in (None, False):
        return None
    if raw_params is True:
        params: dict[str, Any] = {}
    elif isinstance(raw_params, dict):
        params = dict(raw_params)
    else:
        raise ValueError("post_action_observe must be an object or boolean.")

    if not enabled:
        return {"status": "skipped", "reason": "action did not complete successfully"}

    wait_seconds = max(0.0, float(params.get("wait_seconds", 0.3)))
    if wait_seconds > 0:
        sleep(wait_seconds)

    from .ocr import observe_ocr as observe_ocr_image

    observation = observe_ocr_image(
        {key: value for key, value in params.items() if key not in {"assert_text", "wait_seconds"}},
        context.run_dir,
        synthetic_on_capture_fail=bool(params.get("synthetic_on_capture_fail", True)),
    )
    observation_id = f"{step_id}.post_action_observe"
    context.observations[observation_id] = observation
    metadata = {
        "status": "observed",
        "observation": observation_id,
        "source": observation.source,
        "screenshot_path": str(observation.screenshot_path) if observation.screenshot_path is not None else None,
        "engine": observation.metadata.get("engine"),
        "engine_available": observation.metadata.get("engine_available"),
        "text_count": len(observation.elements),
    }

    assert_text = str(params.get("assert_text") or "").strip()
    if assert_text:
        if not observation_contains_text(observation, normalize_text(assert_text)):
            raise AssertionError(f"post_action_observe: text not found after step '{step_id}': {assert_text}")
        metadata["assert_text"] = assert_text
        metadata["assertion"] = "matched"
    return metadata


SELF_CONTAINED_ACTIONS = {"press_key", "refresh_browser", "click_text", "wait_for_text", "upload_file", "select_option", "drag"}
VISUAL_LOCK_ACTIONS = {"observe_screen", "observe_ocr", "observe_vision", "observe_uia", "click_text", "wait_for_text"}


def step_needs_visual_lock(step: WorkflowStep) -> bool:
    if step.action in VISUAL_LOCK_ACTIONS:
        return True
    return bool(step.params.get("uia_window") or step.params.get("window"))


def placeholder_resolved_target(action: str) -> ResolvedTarget:
    return ResolvedTarget(
        target=Target(label=action),
        evidence=LocationEvidence(
            provider=ProviderKind.MOCK,
            confidence=1.0,
            reason="global action does not require a target",
        ),
    )


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def with_metadata(result: WorkflowStepResult, metadata: dict[str, Any]) -> WorkflowStepResult:
    return WorkflowStepResult(
        id=result.id,
        action=result.action,
        status=result.status,
        message=result.message,
        observation=result.observation,
        resolved_target=result.resolved_target,
        action_result=result.action_result,
        metadata=metadata,
    )


def observation_contains_text(observation: Observation, normalized_text: str) -> bool:
    if normalize_text(observation.source).find(normalized_text) >= 0:
        return True
    if normalize_text(observation.metadata).find(normalized_text) >= 0:
        return True
    text_parts: list[str] = []
    for element in observation.elements:
        element_text = normalize_text(element)
        if element_text.find(normalized_text) >= 0:
            return True
        if isinstance(element, dict):
            raw_text = element.get("text")
            if raw_text not in (None, ""):
                text_parts.append(str(raw_text))
        else:
            text_parts.append(str(element))
    compact_expected = compact_text(normalized_text)
    if compact_expected and compact_text("".join(text_parts)).find(compact_expected) >= 0:
        return True
    return False


def compact_text(value: Any) -> str:
    return "".join(str(value).lower().split())


def evaluate_text_contract(observation: Observation, params: dict[str, Any]) -> dict[str, Any]:
    if observation.provider == ProviderKind.OCR and observation.metadata.get("engine_available") is False:
        install_hint = observation.metadata.get("install_hint") or "Install and configure OCR before asserting screen text."
        return {
            "passed": False,
            "failure_type": "ocr_unavailable",
            "reason": f"OCR engine unavailable. {install_hint}",
            "required_all": list(text_list(params.get("required_all") or params.get("text"))),
            "required_any": list(text_list(params.get("required_any"))),
            "forbidden_any": list(text_list(params.get("forbidden_any") or params.get("forbidden_text"))),
            "matched_required": [],
            "missing_required": [],
            "matched_forbidden": [],
            "visible_text": [],
            "screenshot_path": str(observation.screenshot_path) if observation.screenshot_path else None,
        }

    region = normalize_text_region(params.get("text_region") or params.get("region"), observation)
    min_confidence = optional_float(params.get("min_confidence") or params.get("confidence_min"))
    entries = observation_text_entries(observation, region=region, min_confidence=min_confidence)
    visible_text = [entry["text"] for entry in entries[:30]]
    required_all = tuple(text_list(params.get("required_all") or params.get("text")))
    required_any = tuple(text_list(params.get("required_any")))
    forbidden_any = tuple(text_list(params.get("forbidden_any") or params.get("forbidden_text")))

    matched_required = [text for text in required_all if text_matches_entries(text, entries, observation)]
    missing_required = [text for text in required_all if text not in matched_required]
    matched_any = [text for text in required_any if text_matches_entries(text, entries, observation)]
    matched_forbidden = [text for text in forbidden_any if text_matches_entries(text, entries, observation)]
    required_any_ok = not required_any or bool(matched_any)
    passed = not missing_required and required_any_ok and not matched_forbidden
    reason = "text contract matched"
    if missing_required:
        reason = "missing required text: " + ", ".join(missing_required)
    elif not required_any_ok:
        reason = "none of required_any matched: " + ", ".join(required_any)
    elif matched_forbidden:
        reason = "forbidden text matched: " + ", ".join(matched_forbidden)

    return {
        "passed": passed,
        "reason": reason,
        "required_all": list(required_all),
        "required_any": list(required_any),
        "forbidden_any": list(forbidden_any),
        "matched_required": matched_required,
        "matched_any": matched_any,
        "missing_required": missing_required,
        "matched_forbidden": matched_forbidden,
        "visible_text": visible_text,
        "text_region": region,
        "min_confidence": min_confidence,
        "screenshot_path": str(observation.screenshot_path) if observation.screenshot_path else None,
        "provider": str(observation.provider.value if hasattr(observation.provider, "value") else observation.provider),
        "source": observation.source,
    }


def text_contract_failure_message(contract: dict[str, Any]) -> str:
    parts = [str(contract.get("reason") or "text contract failed")]
    if contract.get("visible_text"):
        parts.append("visible_text=" + " | ".join(str(item) for item in contract["visible_text"][:8]))
    if contract.get("screenshot_path"):
        parts.append(f"screenshot={contract['screenshot_path']}")
    return "; ".join(parts)


def text_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return (str(value),)


def observation_text_entries(
    observation: Observation,
    *,
    region: dict[str, int] | None,
    min_confidence: float | None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for element in observation.elements:
        if not isinstance(element, dict):
            continue
        raw_text = str(element.get("text") or "").strip()
        if not raw_text:
            continue
        confidence = optional_float(element.get("confidence"))
        if min_confidence is not None and confidence is not None and confidence < min_confidence:
            continue
        bounds = element.get("bounds") if isinstance(element.get("bounds"), dict) else None
        if region is not None and not bounds_in_region(bounds, region):
            continue
        entries.append({"text": raw_text, "confidence": confidence, "bounds": bounds})
    if region is None:
        metadata_text = normalize_text(observation.metadata)
        if metadata_text:
            entries.append({"text": metadata_text, "confidence": None, "bounds": None})
    return entries


def text_matches_entries(text: str, entries: list[dict[str, Any]], observation: Observation) -> bool:
    expected = normalize_text(text)
    if not expected:
        return True
    for entry in entries:
        if normalize_text(entry.get("text", "")).find(expected) >= 0:
            return True
    joined = "".join(str(entry.get("text") or "") for entry in entries)
    if compact_text(joined).find(compact_text(expected)) >= 0:
        return True
    if entries:
        return False
    return observation_contains_text(observation, expected)


def normalize_text_region(region: Any, observation: Observation) -> dict[str, int] | None:
    if not isinstance(region, dict):
        return None
    width = int(observation.width or 0)
    height = int(observation.height or 0)
    if any(key in region for key in ("left_percent", "top_percent", "width_percent", "height_percent")) and width > 0 and height > 0:
        return {
            "left": int(float(region.get("left_percent", 0.0)) * width),
            "top": int(float(region.get("top_percent", 0.0)) * height),
            "width": int(float(region.get("width_percent", 1.0)) * width),
            "height": int(float(region.get("height_percent", 1.0)) * height),
        }
    try:
        return {
            "left": int(region.get("left", 0)),
            "top": int(region.get("top", 0)),
            "width": int(region.get("width", width)),
            "height": int(region.get("height", height)),
        }
    except (TypeError, ValueError):
        return None


def bounds_in_region(bounds: Any, region: dict[str, int]) -> bool:
    if not isinstance(bounds, dict):
        return False
    try:
        center_x = int(bounds.get("left", 0)) + int(bounds.get("width", 0)) // 2
        center_y = int(bounds.get("top", 0)) + int(bounds.get("height", 0)) // 2
    except (TypeError, ValueError):
        return False
    return (
        region["left"] <= center_x <= region["left"] + region["width"]
        and region["top"] <= center_y <= region["top"] + region["height"]
    )


def find_network_response(events: Any, params: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(events, list):
        return None
    url_contains = str(params.get("url_contains") or "")
    method = str(params.get("method") or "").upper()
    status = params.get("status")
    status_min = params.get("status_min")
    status_max = params.get("status_max")
    ok = params.get("ok")

    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        if event.get("type") != "response":
            continue
        if url_contains and url_contains not in str(event.get("url") or ""):
            continue
        if method and method != str(event.get("method") or "").upper():
            continue
        event_status = event.get("status")
        if status is not None and int(event_status) != int(status):
            continue
        if status_min is not None and int(event_status) < int(status_min):
            continue
        if status_max is not None and int(event_status) > int(status_max):
            continue
        if ok is not None and bool(event.get("ok")) is not bool(ok):
            continue
        return event
    return None


def wait_for_network_response(events: Any, params: dict[str, Any], *, page: Any = None) -> dict[str, Any] | None:
    timeout_seconds = float(params.get("timeout_seconds", 0.0) or 0.0)
    interval_seconds = float(params.get("interval_seconds", 0.05) or 0.05)
    started = monotonic()

    while True:
        event = find_network_response(events, params)
        if event is not None:
            return event
        if monotonic() - started >= timeout_seconds:
            return None
        if page is not None:
            page.wait_for_timeout(int(interval_seconds * 1000))
        else:
            sleep(interval_seconds)


def network_assertion_label(params: dict[str, Any]) -> str:
    parts = []
    for key in ("url_contains", "method", "status", "status_min", "status_max", "ok"):
        if key in params:
            parts.append(f"{key}={params[key]}")
    return ", ".join(parts) if parts else "any response"


def resolve_step_params(params: dict[str, Any], context: WorkflowContext) -> dict[str, Any]:
    resolved = resolve_input_refs(params, context)
    resolved = resolve_runtime_refs(resolved, context)
    if "screenshot_from" in params and "path" not in resolved:
        resolved["path"] = str(resolve_screenshot_source(params["screenshot_from"], context))
    return resolved


def resolve_input_refs(value: Any, context: WorkflowContext) -> Any:
    if isinstance(value, dict):
        resolved: dict[str, Any] = {}
        for key, item in value.items():
            if key.endswith("_from"):
                if not str(item).startswith("input."):
                    resolved[key] = resolve_input_refs(item, context)
                    continue
                target_key = key[: -len("_from")]
                if target_key in value:
                    continue
                try:
                    resolved[target_key] = resolve_input_ref(str(item), key, context)
                except KeyError:
                    default_key = f"{target_key}_default"
                    if default_key not in value:
                        raise
                    resolved[target_key] = resolve_input_refs(value[default_key], context)
                continue
            resolved[key] = resolve_input_refs(item, context)
        return resolved
    if isinstance(value, list):
        return [resolve_input_refs(item, context) for item in value]
    return value


def resolve_input_ref(value_from: str, key: str, context: WorkflowContext) -> Any:
    if value_from.startswith("input."):
        return read_path(context.inputs, value_from.removeprefix("input."))
    raise ValueError(f"Unsupported {key} path: {value_from}")


def resolve_runtime_refs(value: Any, context: WorkflowContext) -> Any:
    if isinstance(value, dict):
        return {key: resolve_runtime_refs(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_runtime_refs(item, context) for item in value]
    if not isinstance(value, str):
        return value
    return replace_runtime_placeholders(value, context)


def replace_runtime_placeholders(text: str, context: WorkflowContext) -> Any:
    pattern = re.compile(r"\$\{([A-Za-z0-9_.-]+)\}")
    matches = list(pattern.finditer(text))
    if not matches:
        return text
    if len(matches) == 1 and matches[0].span() == (0, len(text)):
        name = matches[0].group(1)
        if name in context.variables:
            return context.variables[name]
        if name.startswith("input."):
            try:
                return read_path(context.inputs, name.removeprefix("input."))
            except KeyError:
                return ""
        return ""

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in context.variables:
            return str(context.variables[name])
        if name.startswith("input."):
            try:
                return str(read_path(context.inputs, name.removeprefix("input.")))
            except KeyError:
                return ""
        return ""

    return pattern.sub(substitute, text)


def resolve_workflow_variables(raw_variables: dict[str, Any], context: WorkflowContext) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for key, value in raw_variables.items():
        resolved[key] = resolve_runtime_refs(resolve_input_refs(value, context), replace(context, variables=resolved))
    return resolved


def load_workflow_fixtures(fixture_names: tuple[str, ...], context: WorkflowContext) -> dict[str, Any]:
    loaded: dict[str, Any] = {}
    fixtures = context.resources.setdefault("fixtures", {})
    if not isinstance(fixtures, dict):
        fixtures = {}
        context.resources["fixtures"] = fixtures
    for fixture_name in fixture_names:
        payload = load_named_fixture(context, fixture_name)
        fixtures[fixture_name] = payload
        loaded[fixture_name] = payload
    return loaded


def load_named_fixture(context: WorkflowContext, fixture_name: str) -> dict[str, Any]:
    candidate = Path(fixture_name)
    roots: list[Path] = []
    workspace_root = context.resources.get("workspace_root")
    if isinstance(workspace_root, str) and workspace_root:
        roots.append(Path(workspace_root))
    roots.append(context.run_dir.parent)
    roots.append(Path.cwd())
    if candidate.is_absolute() and candidate.exists():
        return load_fixture_file(candidate)
    suffixes = ("", ".yaml", ".yml", ".json")
    for root in roots:
        for suffix in suffixes:
            path = candidate if suffix == "" else candidate.with_suffix(suffix) if candidate.suffix else root / "fixtures" / f"{fixture_name}{suffix}"
            if suffix == "" and not candidate.is_absolute():
                path = root / "fixtures" / fixture_name
            if path.exists():
                return load_fixture_file(path)
    raise FileNotFoundError(f"Fixture not found: {fixture_name}")


def load_fixture_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required for YAML fixtures. Run: pip install PyYAML") from exc
        payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ValueError(f"Fixture file must contain an object: {path}")
    return payload


def resolve_workflow_reference(workflow_name: str, context: WorkflowContext) -> Path:
    candidate = Path(workflow_name)
    roots: list[Path] = []
    workspace_root = context.resources.get("workspace_root")
    if isinstance(workspace_root, str) and workspace_root:
        roots.append(Path(workspace_root))
    roots.append(context.run_dir.parent)
    roots.append(Path.cwd())
    suffixes = ("", ".yaml", ".yml", ".json")
    if candidate.is_absolute() and candidate.exists():
        return candidate
    for root in roots:
        for suffix in suffixes:
            if candidate.suffix and suffix == "":
                path = root / candidate
            else:
                path = root / "workflows" / (candidate.name if candidate.suffix else f"{workflow_name}{suffix}")
            if path.exists():
                return path
    raise FileNotFoundError(f"Workflow not found: {workflow_name}")


def extract_variable_value(params: dict[str, Any], context: WorkflowContext) -> Any:
    if "value" in params:
        return params["value"]
    if "value_from" in params:
        return resolve_runtime_refs(resolve_input_ref(str(params["value_from"]), "value_from", context), context)
    source = str(params.get("from_text") or "").strip()
    if not source:
        raise ValueError("set_variable requires value, value_from, or from_text.")
    page = context.resources.get("playwright_page")
    if page is not None:
        try:
            locator = page.locator(source)
            if locator.count() > 0:
                text = locator.first.text_content()
                if text is not None:
                    return str(text).strip()
        except Exception:
            pass
    observation = context.latest_observation_or_none
    if observation is not None:
        for element in observation.elements:
            if not isinstance(element, dict):
                continue
            selector = str(element.get("selector") or "")
            text = str(element.get("text") or "").strip()
            if source == selector or source in text or source in selector:
                if text:
                    return text
                for key in ("value", "label", "name", "aria_label"):
                    if element.get(key):
                        return str(element[key])
    return source


def workflow_sensitive_input_values(workflow: Workflow, inputs: dict[str, Any], sensitive_fields: set[str]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()

    def add_value(path: str) -> None:
        try:
            value = read_path(inputs, path)
        except KeyError:
            return
        if isinstance(value, (str, int, float)):
            text = str(value)
            if text and text not in seen:
                seen.add(text)
                values.append(text)

    for path in sensitive_fields:
        add_value(path)
    for step in workflow.steps:
        value_from = str(step.params.get("value_from") or "")
        if not value_from.startswith("input."):
            continue
        path = value_from.removeprefix("input.")
        if step.action in {"type", "paste"} or step.params.get("sensitive") is True or path in sensitive_fields:
            add_value(path)
    return tuple(values)


def redact_workflow_step_result(result: WorkflowStepResult, sensitive_values: tuple[str, ...]) -> WorkflowStepResult:
    if not sensitive_values:
        return result
    return replace(
        result,
        message=redact_workflow_value(result.message, sensitive_values),
        observation=redact_observation(result.observation, sensitive_values),
        resolved_target=redact_resolved_target(result.resolved_target, sensitive_values),
        action_result=redact_action_result(result.action_result, sensitive_values),
        metadata=redact_workflow_value(result.metadata, sensitive_values),
    )


def redact_observation(observation: Observation | None, sensitive_values: tuple[str, ...]) -> Observation | None:
    if observation is None:
        return None
    return replace(
        observation,
        source=redact_workflow_value(observation.source, sensitive_values),
        elements=tuple(redact_workflow_value(element, sensitive_values) for element in observation.elements),
        metadata=redact_workflow_value(observation.metadata, sensitive_values),
    )


def redact_resolved_target(resolved: ResolvedTarget | None, sensitive_values: tuple[str, ...]) -> ResolvedTarget | None:
    if resolved is None:
        return None
    evidence = replace(
        resolved.evidence,
        reason=redact_workflow_value(resolved.evidence.reason, sensitive_values),
        handle=redact_workflow_value(resolved.evidence.handle, sensitive_values),
        metadata=redact_workflow_value(resolved.evidence.metadata, sensitive_values),
    )
    return replace(resolved, evidence=evidence)


def redact_action_result(result: ActionResult | None, sensitive_values: tuple[str, ...]) -> ActionResult | None:
    if result is None:
        return None
    return replace(
        result,
        target=redact_workflow_value(result.target, sensitive_values),
        message=redact_workflow_value(result.message, sensitive_values),
        metadata=redact_workflow_value(result.metadata, sensitive_values),
    )


def redact_workflow_value(value: Any, sensitive_values: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return redact_secret_text(value, extra_secrets=sensitive_values)
    if isinstance(value, dict):
        return {key: redact_workflow_value(item, sensitive_values) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_workflow_value(item, sensitive_values) for item in value)
    if isinstance(value, list):
        return [redact_workflow_value(item, sensitive_values) for item in value]
    return value


def resolve_screenshot_source(value: Any, context: WorkflowContext) -> Path:
    ref = str(value or "latest").strip()
    if ref in {"page", "browser", "current_page", "playwright_page"}:
        return capture_page_screenshot(context, "current_page")
    if ref in {"latest", "latest_observation"}:
        observation = context.latest_observation_or_none
        if observation is not None and observation.screenshot_path is not None:
            return observation.screenshot_path
        if context.resources.get("playwright_page") is not None:
            return capture_page_screenshot(context, "latest")
        raise ValueError("No latest screenshot is available.")
    if ref.startswith("observation."):
        parts = ref.split(".")
        if len(parts) >= 2:
            ref = parts[1]
    observation = context.observations.get(ref)
    if observation is not None and observation.screenshot_path is not None:
        return observation.screenshot_path
    raise ValueError(f"Screenshot source not found or has no screenshot_path: {value}")


def capture_page_screenshot(context: WorkflowContext, label: str) -> Path:
    page = context.resources.get("playwright_page")
    if page is None:
        raise ValueError("No Playwright page is available for screenshot capture.")
    path = context.run_dir / f"{sanitize_filename(label)}_vision_input.png"
    page.screenshot(path=str(path), full_page=True)
    return path


def resolve_file_assertion_target(params: dict[str, Any], context: WorkflowContext) -> dict[str, Any]:
    if "path" in params:
        return {"path": str(params["path"])}
    from_download = str(params.get("from_download") or "latest")
    downloads = context.resources.get("downloads")
    if isinstance(downloads, dict) and from_download in downloads and isinstance(downloads[from_download], dict):
        return downloads[from_download]
    raise ValueError(f"Download metadata not found: {from_download}")


def resolve_output_path(value: Any, run_dir: Path) -> Path:
    path = Path(str(value))
    if path.is_absolute():
        return path
    if str(value).startswith(".runs/") or str(value).startswith(".runs\\"):
        return path
    if str(value).startswith(".agent-") or path.parts[:1] in {(".agent-auth",), (".agent-workspace",)}:
        return path
    return run_dir / path


def file_metadata(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "filename": path.name,
        "extension": path.suffix.lower(),
        "size_bytes": stat.st_size,
    }


def sanitize_filename(value: str) -> str:
    text = value.strip() or "download"
    return "".join("_" if char in '<>:"/\\|?*' else char for char in text)


def normalize_extension(value: str) -> str:
    text = value.strip().lower()
    return text if text.startswith(".") else f".{text}"


def close_context_resources(context: WorkflowContext) -> None:
    trace_error = stop_playwright_trace(context)
    if trace_error:
        context.resources["playwright_trace_error"] = trace_error
    for key in ("playwright_page", "playwright_context", "playwright_browser", "playwright"):
        resource = context.resources.get(key)
        if resource is None:
            continue
        closer = getattr(resource, "close", None) or getattr(resource, "stop", None)
        if closer is None:
            continue
        try:
            closer()
        except Exception:
            pass


def stop_playwright_trace(context: WorkflowContext) -> str:
    if not context.resources.get("playwright_trace_started"):
        return ""
    browser_context = context.resources.get("playwright_context")
    trace_path = str(context.resources.get("playwright_trace_path") or "")
    tracing = getattr(browser_context, "tracing", None)
    stopper = getattr(tracing, "stop", None)
    if browser_context is None or stopper is None or not trace_path:
        return "trace resources unavailable"
    try:
        stopper(path=trace_path)
        context.resources["playwright_trace_stopped"] = True
        return ""
    except Exception as exc:
        return str(exc)[:500]
