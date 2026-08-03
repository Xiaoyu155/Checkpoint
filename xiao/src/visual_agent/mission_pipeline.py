"""Spec-gated mission pipeline state.

This module is intentionally thin: it wraps the existing chief-run mission
machinery with a durable state.json and a strict request spec gate.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .managed_state import (
    RETRYABLE_FAILURE_KINDS,
    ManagedRunState,
    ManagedState,
    RevisionConflict,
    managed_idempotency_key,
    managed_run_from_dict,
    managed_task_idempotency_key,
    new_managed_run,
    transition_managed_run,
)
from .models import to_jsonable
from .scheduler import lock_file, unlock_file


PIPELINE_DRAFT = "DRAFT"
PIPELINE_REVIEW = "REVIEW"
PIPELINE_APPROVED = "APPROVED"
PIPELINE_EXECUTING = "EXECUTING"
PIPELINE_VERIFYING = "VERIFYING"
PIPELINE_REPAIRING = "REPAIRING"
PIPELINE_VERIFIED = "VERIFIED"
PIPELINE_BLOCKED = "BLOCKED"
PIPELINE_FAILED = "FAILED"

PIPELINE_STATES = {
    PIPELINE_DRAFT,
    PIPELINE_REVIEW,
    PIPELINE_APPROVED,
    PIPELINE_EXECUTING,
    PIPELINE_VERIFYING,
    PIPELINE_REPAIRING,
    PIPELINE_VERIFIED,
    PIPELINE_BLOCKED,
    PIPELINE_FAILED,
}

_PIPELINE_TERMINAL_STATES = {PIPELINE_VERIFIED, PIPELINE_BLOCKED, PIPELINE_FAILED}
_PIPELINE_TRANSITIONS = {
    PIPELINE_DRAFT: {
        PIPELINE_DRAFT,
        PIPELINE_REVIEW,
        PIPELINE_APPROVED,
        PIPELINE_EXECUTING,
        PIPELINE_BLOCKED,
        PIPELINE_FAILED,
    },
    PIPELINE_REVIEW: {
        PIPELINE_REVIEW,
        PIPELINE_APPROVED,
        PIPELINE_EXECUTING,
        PIPELINE_BLOCKED,
        PIPELINE_FAILED,
    },
    PIPELINE_APPROVED: {
        PIPELINE_APPROVED,
        PIPELINE_REVIEW,
        PIPELINE_EXECUTING,
        PIPELINE_BLOCKED,
        PIPELINE_FAILED,
    },
    PIPELINE_EXECUTING: {
        PIPELINE_EXECUTING,
        PIPELINE_VERIFYING,
        PIPELINE_REPAIRING,
        PIPELINE_VERIFIED,
        PIPELINE_BLOCKED,
        PIPELINE_FAILED,
    },
    PIPELINE_VERIFYING: {
        PIPELINE_VERIFYING,
        PIPELINE_REPAIRING,
        PIPELINE_VERIFIED,
        PIPELINE_BLOCKED,
        PIPELINE_FAILED,
    },
    PIPELINE_REPAIRING: {
        PIPELINE_REPAIRING,
        PIPELINE_EXECUTING,
        PIPELINE_VERIFYING,
        PIPELINE_BLOCKED,
        PIPELINE_FAILED,
    },
    **{state: set() for state in _PIPELINE_TERMINAL_STATES},
}


class SpecValidationError(ValueError):
    def __init__(self, message: str, *, field: str = "spec") -> None:
        super().__init__(message)
        self.field = field

    def to_response(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": "spec_validation_failed",
            "error": str(self),
            "field": self.field,
        }


class SpecValidator:
    required_sections = ("scope", "plan", "test", "risk", "rollback")

    def validate(self, spec: Any) -> dict[str, Any]:
        if not isinstance(spec, dict):
            raise SpecValidationError("任务请求必须包含 spec JSON 对象。", field="spec")
        normalized: dict[str, Any] = dict(spec)
        for key in self.required_sections:
            value = normalized.get(key)
            if not isinstance(value, list) or not value:
                raise SpecValidationError(f"spec.{key} 必须是非空数组。", field=f"spec.{key}")
            normalized[key] = [self._normalize_item(key, item) for item in value]
        goal = str(normalized.get("goal") or "").strip()
        if goal:
            normalized["goal"] = goal
        try:
            normalized["schema_version"] = int(normalized.get("schema_version") or 1)
        except (TypeError, ValueError) as exc:
            raise SpecValidationError("spec.schema_version 必须是整数。", field="spec.schema_version") from exc
        return normalized

    def derive_request_spec(
        self,
        *,
        goal: str,
        repo_root: str | Path,
        test_command: str | None,
        agent: str | None,
        execute: bool,
    ) -> dict[str, Any]:
        return self.validate(
            {
                "schema_version": 1,
                "goal": str(goal or "").strip(),
                "scope": [
                    {
                        "repo_root": str(repo_root),
                        "agent": str(agent or "").strip() or "default",
                        "mode": "execute" if execute else "preview",
                    }
                ],
                "plan": [str(goal or "").strip()],
                "test": [str(test_command or "").strip() or "auto-detect verification command"],
                "risk": ["Workbench-generated spec; human review is required before merge."],
                "rollback": ["Keep merge policy manual unless explicitly approved."],
            }
        )

    @staticmethod
    def _normalize_item(section: str, item: Any) -> Any:
        if isinstance(item, dict):
            if not item:
                raise SpecValidationError(f"spec.{section} 不能包含空对象。", field=f"spec.{section}")
            return item
        text = str(item).strip()
        if not text:
            raise SpecValidationError(f"spec.{section} 不能包含空字符串。", field=f"spec.{section}")
        return text


def plan_status_to_pipeline_state(status: str) -> str:
    value = str(status or "")
    if value == "ready":
        return PIPELINE_APPROVED
    if value in {"needs_clarification", "needs_workflow_coverage", "blocked"}:
        return PIPELINE_BLOCKED
    return PIPELINE_DRAFT


def mission_result_to_pipeline_state(status: str, stop_reason: str = "") -> str:
    state = str(status or "")
    reason = str(stop_reason or "")
    if state == "verified" or reason == "verified":
        return PIPELINE_VERIFIED
    if state == "verified_blocked":
        return PIPELINE_BLOCKED
    if state in {"background_started", "running"}:
        return PIPELINE_EXECUTING
    if state in {"preview", "created"} or reason == "preview_only":
        return PIPELINE_REVIEW
    if state in {"blocked", "stopped"}:
        return PIPELINE_BLOCKED
    if state == "error":
        return PIPELINE_FAILED
    return PIPELINE_FAILED if reason else PIPELINE_REVIEW


def write_mission_state(
    workspace_root: str | Path,
    mission_id: str,
    *,
    current_state: str,
    event: str,
    spec: dict[str, Any] | None = None,
    launch_id: str = "",
    **fields: Any,
) -> dict[str, Any]:
    if current_state not in PIPELINE_STATES:
        raise ValueError(f"unknown pipeline state: {current_state}")
    mid = str(mission_id or "").strip()
    if not mid:
        return {}
    path = Path(workspace_root).expanduser().resolve() / "missions" / mid / "state.json"
    loaded = _read_json(path)
    state: dict[str, Any] = dict(loaded)
    persisted_revision = int(state.get("revision", -1))
    now = _now()
    context = state.setdefault("context", {})
    if spec is not None or not isinstance(context.get("spec"), dict):
        context["spec"] = spec or {}
    offered_key = str(fields.get("idempotency_key") or "").strip()
    persisted_key = str(state.get("idempotency_key") or "").strip()
    if offered_key and persisted_key and offered_key != persisted_key:
        raise ValueError("mission idempotency key is immutable")
    if offered_key:
        state["idempotency_key"] = offered_key
    managed = _managed_record(
        state,
        run_id=mid,
        idempotency_source={
            "mission_id": mid,
            "spec": context.get("spec") or {},
            "goal": fields.get("goal") or "",
            "plan_id": fields.get("plan_id") or "",
        },
        now=now,
    )
    previous_pipeline_state = str(state.get("current_state") or "")
    if previous_pipeline_state in _PIPELINE_TERMINAL_STATES and not (
        managed.terminal and event == "chief_run_resume_executing"
    ):
        if current_state != previous_pipeline_state:
            raise ValueError(f"terminal pipeline state is immutable: {previous_pipeline_state}")
        history = context.setdefault("history", [])
        if isinstance(history, list):
            entry = {"at": now, "event": str(event), "state": current_state}
            entry.update({key: value for key, value in fields.items() if value is not None})
            history.append(entry)
        state.update(
            {
                "schema_version": int(state.get("schema_version") or 1),
                "mission_id": mid,
                "launch_id": str(state.get("launch_id") or launch_id or ""),
                "current_state": current_state,
                "revision": persisted_revision + 1,
                "updated_at": now,
                "managed": managed.to_dict(),
                "idempotency_key": str(state.get("idempotency_key") or managed.idempotency_key),
            }
        )
        _apply_managed_runtime(state, fields.get("managed_runtime"))
        state["transition_valid"] = True
        state.setdefault("created_at", now)
        state.setdefault(
            "metrics",
            {
                "total_tokens": 0,
                "saved_tokens": 0,
                "last_successful_step": 0,
            },
        )
        _write_json_cas(path, state, expected_revision=persisted_revision)
        return state
    if managed.terminal and event == "chief_run_resume_executing":
        attempts = (
            state.get("managed_attempt_history")
            if isinstance(state.get("managed_attempt_history"), list)
            else []
        )
        attempts.append(managed.to_dict())
        state["managed_attempt_history"] = attempts
        attempt_number = len(attempts) + 1
        managed = new_managed_run(
            run_id=f"{mid}:attempt:{attempt_number}",
            idempotency_key=(
                f"{state.get('idempotency_key') or managed.idempotency_key}:attempt:{attempt_number}"
            ),
            now=now,
        )
        previous_pipeline_state = PIPELINE_DRAFT
    _validate_pipeline_transition(
        previous_pipeline_state,
        current_state,
        event=event,
        fields=fields,
        allow_implicit_execution=event == "chief_run_finished",
    )
    managed = _advance_managed_record(
        managed,
        pipeline_state=current_state,
        event=event,
        fields=fields,
        now=now,
        allow_implicit_execution=event == "chief_run_finished",
    )
    history = context.setdefault("history", [])
    if isinstance(history, list):
        entry = {"at": now, "event": str(event), "state": current_state}
        entry.update({key: value for key, value in fields.items() if value is not None})
        history.append(entry)
    state.update(
        {
            "schema_version": int(state.get("schema_version") or 1),
            "mission_id": mid,
            "launch_id": str(state.get("launch_id") or launch_id or ""),
            "current_state": current_state,
            "revision": persisted_revision + 1,
            "updated_at": now,
            "managed": managed.to_dict(),
            "idempotency_key": str(state.get("idempotency_key") or managed.idempotency_key),
        }
    )
    _apply_managed_runtime(state, fields.get("managed_runtime"))
    state["transition_valid"] = True
    state.setdefault("created_at", now)
    state.setdefault(
        "metrics",
        {
            "total_tokens": 0,
            "saved_tokens": 0,
            "last_successful_step": 0,
        },
    )
    _write_json_cas(path, state, expected_revision=persisted_revision)
    return state


class MissionPipeline:
    def __init__(self, workspace_root: str | Path, *, launch_id: str) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.launch_id = str(launch_id)
        self.path = self.workspace_root / "mission_pipeline" / self.launch_id / "state.json"
        self._mission_state_path: Path | None = None

    def begin(self, *, spec: dict[str, Any], execute: bool, request: dict[str, Any] | None = None) -> dict[str, Any]:
        now = _now()
        request_payload = request or {}
        explicit_key = str(request_payload.get("idempotency_key") or "").strip()
        idempotency_key = explicit_key or _pipeline_task_idempotency_key(
            spec=spec,
            request=request_payload,
        )
        managed = new_managed_run(
            run_id=self.launch_id,
            idempotency_key=idempotency_key,
            now=now,
        )
        state = {
            "schema_version": 1,
            "mission_id": "",
            "launch_id": self.launch_id,
            "current_state": PIPELINE_APPROVED if execute else PIPELINE_REVIEW,
            "revision": 0,
            "idempotency_key": idempotency_key,
            "transition_valid": True,
            "managed": managed.to_dict(),
            "managed_runtime": {
                "budget_status": "not_assessed",
                "routing_evidence": request_payload.get("routing_evidence")
                if isinstance(request_payload.get("routing_evidence"), dict)
                else {},
                "retry": {},
            },
            "context": {
                "spec": spec,
                "history": [
                    {
                        "at": now,
                        "event": "mission_pipeline_begin",
                        "state": PIPELINE_APPROVED if execute else PIPELINE_REVIEW,
                    }
                ],
                "pending_tools": [],
                "request": request or {},
            },
            "metrics": {
                "total_tokens": 0,
                "saved_tokens": 0,
                "last_successful_step": 0,
            },
            "created_at": now,
            "updated_at": now,
        }
        self.write(state, expected_revision=-1)
        return state

    def attach_mission(self, state: dict[str, Any], mission_id: str) -> dict[str, Any]:
        mid = str(mission_id or "").strip()
        if not mid:
            return state
        state["mission_id"] = mid
        self._mission_state_path = self.workspace_root / "missions" / mid / "state.json"
        return self.transition(state, str(state.get("current_state") or PIPELINE_REVIEW), "mission_attached", mission_id=mid)

    def transition(
        self,
        state: dict[str, Any],
        next_state: str,
        event: str,
        *,
        expected_revision: int | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        if next_state not in PIPELINE_STATES:
            raise ValueError(f"unknown pipeline state: {next_state}")
        current_revision = int(state.get("revision", 0))
        expected = current_revision if expected_revision is None else int(expected_revision)
        if expected != current_revision:
            raise RevisionConflict(
                f"pipeline revision conflict: expected {expected}, current {current_revision}"
            )
        now = _now()
        current_pipeline_state = str(state.get("current_state") or "")
        allow_implicit_execution = str(event or "") == "chief_run_error"
        _validate_pipeline_transition(
            current_pipeline_state,
            next_state,
            event=event,
            fields=fields,
            allow_implicit_execution=allow_implicit_execution,
        )
        candidate = copy.deepcopy(state)
        managed = _managed_record(
            candidate,
            run_id=self.launch_id,
            idempotency_source={
                "spec": (candidate.get("context") or {}).get("spec") or {},
                "request": (candidate.get("context") or {}).get("request") or {},
            },
            now=now,
        )
        managed = _advance_managed_record(
            managed,
            pipeline_state=next_state,
            event=event,
            fields=fields,
            now=now,
            allow_implicit_execution=allow_implicit_execution,
        )
        candidate["current_state"] = next_state
        candidate["updated_at"] = now
        candidate["revision"] = current_revision + 1
        candidate["managed"] = managed.to_dict()
        candidate["idempotency_key"] = str(
            candidate.get("idempotency_key") or managed.idempotency_key
        )
        candidate["transition_valid"] = True
        _apply_managed_runtime(candidate, fields.get("managed_runtime"))
        context = candidate.setdefault("context", {})
        history = context.setdefault("history", [])
        if isinstance(history, list):
            entry = {"at": now, "event": str(event), "state": next_state}
            entry.update({key: value for key, value in fields.items() if value is not None})
            history.append(entry)
        self.write(candidate, expected_revision=current_revision)
        state.clear()
        state.update(candidate)
        return state

    def write(self, state: dict[str, Any], *, expected_revision: int) -> None:
        _write_json_cas(self.path, state, expected_revision=expected_revision)
        if self._mission_state_path is not None:
            _atomic_write_json(self._mission_state_path, state)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None


def _validate_pipeline_transition(
    current_state: str,
    next_state: str,
    *,
    event: str,
    fields: dict[str, Any],
    allow_implicit_execution: bool,
) -> None:
    if not str(event or "").strip():
        raise ValueError("pipeline transition event is required")
    if not current_state:
        return
    if current_state in _PIPELINE_TERMINAL_STATES:
        raise ValueError(f"terminal pipeline state is immutable: {current_state}")
    allowed = next_state in _PIPELINE_TRANSITIONS.get(current_state, set())
    implicit_verified = (
        allow_implicit_execution
        and next_state == PIPELINE_VERIFIED
        and _has_verified_result(fields)
    )
    if not allowed and not implicit_verified:
        raise ValueError(f"invalid pipeline transition: {current_state} -> {next_state}")
    if next_state == PIPELINE_VERIFIED and not _has_verified_result(fields):
        raise ValueError("VERIFIED requires status=verified and stop_reason=verified")
    if next_state == PIPELINE_REPAIRING and not _authorized_retry(fields):
        raise ValueError("REPAIRING requires a scheduled retry from the retry whitelist")


def _pipeline_task_idempotency_key(
    *,
    spec: dict[str, Any],
    request: dict[str, Any],
) -> str:
    scope = spec.get("scope") if isinstance(spec.get("scope"), list) else []
    first_scope = scope[0] if scope and isinstance(scope[0], dict) else {}
    tests = spec.get("test") if isinstance(spec.get("test"), list) else []
    first_test = str(tests[0] or "") if tests else ""
    contract = (
        request.get("requirement_contract")
        if isinstance(request.get("requirement_contract"), dict)
        else spec.get("requirement_contract")
        if isinstance(spec.get("requirement_contract"), dict)
        else {}
    )
    return managed_task_idempotency_key(
        goal=str(request.get("goal") or spec.get("goal") or ""),
        repo_root=str(request.get("repo_root") or first_scope.get("repo_root") or ""),
        test_command=str(request.get("test_command") or first_test),
        requirement_contract=contract,
    )


def _managed_record(
    state: dict[str, Any],
    *,
    run_id: str,
    idempotency_source: Any,
    now: str,
) -> ManagedRunState:
    raw = state.get("managed")
    if isinstance(raw, dict):
        return managed_run_from_dict(raw)
    key = str(state.get("idempotency_key") or "").strip() or managed_idempotency_key(
        idempotency_source
    )
    state["idempotency_key"] = key
    return new_managed_run(run_id=run_id, idempotency_key=key, now=now)


def _advance_managed_record(
    managed: ManagedRunState,
    *,
    pipeline_state: str,
    event: str,
    fields: dict[str, Any],
    now: str,
    allow_implicit_execution: bool,
) -> ManagedRunState:
    attempt_id = str(fields.get("attempt_id") or managed.attempt_id or "").strip()
    if managed.state == ManagedState.RETRY_WAIT and pipeline_state == PIPELINE_EXECUTING:
        attempt_id = str(fields.get("attempt_id") or "").strip() or (
            f"{managed.run_id}:attempt:{managed.revision + 1}"
        )
    if not attempt_id:
        attempt_id = f"{managed.run_id}:attempt:1"

    def move(
        target: ManagedState,
        suffix: str,
        *,
        reason_code: str = "",
        scheduled_at: str = "",
    ) -> None:
        nonlocal managed
        managed = transition_managed_run(
            managed,
            expected_revision=managed.revision,
            next_state=target,
            event=f"{event}:{suffix}",
            attempt_id=attempt_id if target != ManagedState.PENDING else None,
            reason_code=reason_code,
            scheduled_at=scheduled_at,
            now=now,
        )

    if pipeline_state == PIPELINE_EXECUTING:
        if managed.state == ManagedState.RETRY_WAIT:
            move(ManagedState.PENDING, "retry_ready")
        if managed.state == ManagedState.PENDING:
            move(ManagedState.RUNNING, "running")
        elif managed.state != ManagedState.RUNNING:
            raise ValueError(
                f"pipeline EXECUTING is incompatible with managed state {managed.state.value}"
            )
        return managed
    if pipeline_state == PIPELINE_REPAIRING:
        retry = _retry_payload(fields)
        if managed.state != ManagedState.VERIFYING:
            raise ValueError(
                f"pipeline REPAIRING is incompatible with managed state {managed.state.value}"
            )
        move(
            ManagedState.RETRY_WAIT,
            "retry_wait",
            reason_code=str(retry.get("failure_kind") or "retry_scheduled"),
            scheduled_at=str(retry.get("scheduled_at") or ""),
        )
        return managed
    if pipeline_state == PIPELINE_VERIFYING:
        if managed.state == ManagedState.PENDING and allow_implicit_execution:
            move(ManagedState.RUNNING, "implicit_running")
        if managed.state == ManagedState.RUNNING:
            move(ManagedState.VERIFYING, "verifying")
        elif managed.state != ManagedState.VERIFYING:
            raise ValueError(
                f"pipeline VERIFYING is incompatible with managed state {managed.state.value}"
            )
        return managed
    if pipeline_state == PIPELINE_VERIFIED:
        if not _has_verified_result(fields):
            raise ValueError("managed success requires a verified mission result")
        if managed.state == ManagedState.PENDING and allow_implicit_execution:
            move(ManagedState.RUNNING, "implicit_running")
        if managed.state == ManagedState.RUNNING:
            move(ManagedState.VERIFYING, "implicit_verifying")
        if managed.state != ManagedState.VERIFYING:
            raise ValueError(
                f"pipeline VERIFIED is incompatible with managed state {managed.state.value}"
            )
        move(ManagedState.SUCCEEDED, "succeeded")
        return managed
    if pipeline_state in {PIPELINE_BLOCKED, PIPELINE_FAILED}:
        reason = str(fields.get("stop_reason") or fields.get("reason_code") or event).strip()
        target = _managed_failure_state(pipeline_state, reason)
        if managed.state == ManagedState.PENDING and target != ManagedState.BLOCKED:
            if not allow_implicit_execution:
                raise ValueError(
                    f"pipeline {pipeline_state} requires a started managed attempt"
                )
            move(ManagedState.RUNNING, "implicit_running")
        if managed.state == ManagedState.PENDING and target == ManagedState.BLOCKED:
            move(target, "blocked", reason_code=reason)
        elif managed.state in {ManagedState.RUNNING, ManagedState.VERIFYING}:
            move(target, target.value.lower(), reason_code=reason)
        elif managed.state != target:
            raise ValueError(
                f"pipeline {pipeline_state} is incompatible with managed state {managed.state.value}"
            )
    return managed


def _managed_failure_state(pipeline_state: str, reason: str) -> ManagedState:
    normalized = str(reason or "").strip().lower()
    if normalized in {
        "worker_error",
        "worker_crashed",
        "process_crash",
        "command_launch_error",
    }:
        return ManagedState.CRASHED
    if pipeline_state == PIPELINE_FAILED or normalized in {
        "command_timeout",
        "provider_5xx",
        "network_timeout",
        "budget_exhausted",
        "verification_failed",
        "merged_verification_failed",
        "evidence_rejected",
        "evidence_resubmitted",
        "scope_violation",
        "workspace_tamper",
        "test_tampering",
    }:
        return ManagedState.FAILED
    return ManagedState.BLOCKED


def _has_verified_result(fields: dict[str, Any]) -> bool:
    return (
        str(fields.get("status") or "") == "verified"
        and str(fields.get("stop_reason") or "") == "verified"
    )


def _authorized_retry(fields: dict[str, Any]) -> bool:
    retry = _retry_payload(fields)
    return bool(
        retry.get("retry") is True
        and str(retry.get("status") or "") == "scheduled"
        and str(retry.get("failure_kind") or "") in RETRYABLE_FAILURE_KINDS
        and str(retry.get("scheduled_at") or "").strip()
    )


def _retry_payload(fields: dict[str, Any]) -> dict[str, Any]:
    runtime = fields.get("managed_runtime") if isinstance(fields.get("managed_runtime"), dict) else {}
    retry = runtime.get("retry") if isinstance(runtime.get("retry"), dict) else fields.get("retry")
    return retry if isinstance(retry, dict) else {}


def _apply_managed_runtime(state: dict[str, Any], value: Any) -> None:
    runtime = value if isinstance(value, dict) else None
    if runtime is not None:
        state["managed_runtime"] = copy.deepcopy(runtime)
    stored = state.get("managed_runtime") if isinstance(state.get("managed_runtime"), dict) else {}
    budget = stored.get("budget") if isinstance(stored.get("budget"), dict) else {}
    routing = (
        stored.get("routing_evidence")
        if isinstance(stored.get("routing_evidence"), dict)
        else {}
    )
    state["reliability"] = {
        "idempotency_key": str(state.get("idempotency_key") or ""),
        "managed_revision": int((state.get("managed") or {}).get("revision") or 0),
        "transition_valid": True,
        "budget_status": str(
            stored.get("budget_status") or budget.get("status") or "not_assessed"
        ),
        "routing_evidence": copy.deepcopy(routing),
        "retry": copy.deepcopy(stored.get("retry"))
        if isinstance(stored.get("retry"), dict)
        else {},
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_cas(path: Path, payload: dict[str, Any], *, expected_revision: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        lock_file(handle)
        try:
            handle.seek(0)
            text = handle.read()
            if text.strip():
                try:
                    current = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise RevisionConflict(f"state file is not valid JSON: {path}") from exc
                if not isinstance(current, dict):
                    raise RevisionConflict(f"state file is not an object: {path}")
                current_revision = int(current.get("revision", -1))
            else:
                current_revision = -1
            if current_revision != int(expected_revision):
                raise RevisionConflict(
                    f"persisted revision conflict for {path}: "
                    f"expected {expected_revision}, current {current_revision}"
                )
            handle.seek(0)
            handle.truncate()
            json.dump(to_jsonable(payload), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            unlock_file(handle)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
