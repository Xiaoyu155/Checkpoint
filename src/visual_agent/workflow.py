from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from .audit import RunAudit
from .diagnostics import diagnose_failure
from .dispatcher import ActionDispatchContext, ActionDispatcher, read_path, resolve_step_value, selector_from_resolved
from .dom import normalize_text
from .locks import RunLock, lock_to_dict, queue_to_dict
from .models import (
    ActionResult,
    ActionStatus,
    Observation,
    ProviderKind,
    ResolvedTarget,
    Target,
    to_jsonable,
)
from .providers import ProviderContext, ProviderRegistry, default_provider_registry
from .run_profile import RunProfileName, ensure_step_allowed, normalize_run_profile, step_should_dry_run
from .selector import SelectorResolver
from .state import StateStore, WorkflowState, hydrate_context_from_completed_steps
from .workflow_types import WorkflowContext


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
    tags: tuple[str, ...] = ()


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
        resume_from: str | Path | None = None,
        use_lock: bool = True,
        lock_ttl_seconds: float = 3600.0,
        queue_when_locked: bool = False,
        lock_wait_seconds: float = 0.0,
        lock_poll_seconds: float = 0.5,
    ) -> WorkflowRunResult:
        profile = normalize_run_profile(str(run_profile) if run_profile is not None else None, dry_run=dry_run)
        lock = RunLock(self.audit.root_dir, ttl_seconds=lock_ttl_seconds) if use_lock else None
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
        lock_info = None
        queue_info = None
        if lock is not None:
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
                sensitive_fields=sensitive_fields or set(),
            )
            hydrate_context_from_completed_steps(context, tuple(completed_steps))
            results: list[WorkflowStepResult] = []

            for step in workflow.steps:
                if step.id in completed_lookup:
                    result = WorkflowStepResult(
                        id=step.id,
                        action=step.action,
                        status=ActionStatus.SUCCESS,
                        message="skipped by resume checkpoint",
                        metadata={"resumed": True},
                    )
                    results.append(result)
                    continue

                result = self._run_step(
                    step,
                    context,
                    run_profile=profile,
                    synthetic_on_capture_fail=synthetic_on_capture_fail,
                )
                results.append(result)
                self._write_step_result(run_dir, result)
                if result.status == ActionStatus.FAILED:
                    state_store.save(
                        WorkflowState(
                            run_id=run_id,
                            workflow_name=workflow.name,
                            completed_steps=tuple(completed_steps),
                            failed_step=step.id,
                        )
                    )
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
            close_context_resources(context)
            return run_result
        finally:
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

        if action.startswith("observe_"):
            observation = self.providers.observe(
                action,
                params,
                ProviderContext(
                    run_dir=context.run_dir,
                    synthetic_on_capture_fail=synthetic_on_capture_fail,
                    resources=context.resources,
                ),
            )
            context.observations[step.id] = observation
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message=f"{action} completed",
                observation=observation,
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

        if action in self.dispatcher.actions_available:
            resolved = self._resolve_for_action(params, context, step.id, action=action)
            action_result = self.dispatcher.execute(
                action,
                resolved,
                params,
                ActionDispatchContext(workflow_context=context, dry_run=step_dry_run),
            )
            context.actions[step.id] = action_result
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=action_result.status,
                message=action_result.message,
                resolved_target=resolved,
                action_result=action_result,
            )

        if action == "assert_text":
            observation = context.observations.get(str(params.get("observation", ""))) or context.latest_observation
            text = normalize_text(require_param(params, "text"))
            if observation is not None and observation.provider == ProviderKind.OCR and observation.metadata.get("engine_available") is False:
                install_hint = observation.metadata.get("install_hint") or "Install and configure OCR before asserting screen text."
                raise AssertionError(f"OCR engine unavailable; cannot assert text: {params['text']}. {install_hint}")
            if not observation_contains_text(observation, text):
                raise AssertionError(f"Text not found in observation: {params['text']}")
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message=f"text found: {params['text']}",
            )

        if action == "assert_text_contract":
            observation = context.observations.get(str(params.get("observation", ""))) or context.latest_observation
            contract = evaluate_text_contract(observation, params)
            if not contract["passed"]:
                raise AssertionError(text_contract_failure_message(contract))
            return WorkflowStepResult(
                id=step.id,
                action=action,
                status=ActionStatus.SUCCESS,
                message="text contract matched",
                metadata={"text_contract": contract},
            )

        if action == "assert_response":
            event = wait_for_network_response(
                context.resources.get("network_events", []),
                params,
                page=context.resources.get("playwright_page"),
            )
            if event is None:
                raise AssertionError(f"Network response not found: {network_assertion_label(params)}")
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

        if action == "save_storage_state":
            return self._save_storage_state(step, context, dry_run=step_dry_run)

        if action == "wait_for":
            return self._wait_for(step, context)

        raise ValueError(f"Unsupported workflow action: {action}")

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
            raise AssertionError(f"File not found: {path}")
        actual = file_metadata(path)
        min_bytes = step.params.get("min_bytes")
        if min_bytes is not None and int(actual["size_bytes"]) < int(min_bytes):
            raise AssertionError(f"File too small: {actual['size_bytes']} < {min_bytes}")
        extension = step.params.get("extension")
        if extension and path.suffix.lower() != normalize_extension(str(extension)):
            raise AssertionError(f"File extension mismatch: {path.suffix} != {extension}")
        return WorkflowStepResult(
            id=step.id,
            action=step.action,
            status=ActionStatus.SUCCESS,
            message="file assertion passed",
            metadata=actual,
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

    return Workflow(
        name=str(payload.get("name") or "unnamed-workflow"),
        version=int(payload.get("version") or 1),
        steps=tuple(steps),
        schema_version=int(payload["schema_version"]) if payload.get("schema_version") not in (None, "") else None,
        min_runtime_version=str(payload["min_runtime_version"]) if payload.get("min_runtime_version") not in (None, "") else None,
        tags=tuple(str(item) for item in payload.get("tags", []) or []),
    )


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
        "assert_response",
        "assert_file_exists",
    }


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
