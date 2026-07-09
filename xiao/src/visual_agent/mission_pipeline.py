"""Spec-gated mission pipeline state.

This module is intentionally thin: it wraps the existing chief-run mission
machinery with a durable state.json and a strict request spec gate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import to_jsonable


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
    state: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            state = loaded
    now = _now()
    context = state.setdefault("context", {})
    if spec is not None or not isinstance(context.get("spec"), dict):
        context["spec"] = spec or {}
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
            "updated_at": now,
        }
    )
    state.setdefault("created_at", now)
    state.setdefault(
        "metrics",
        {
            "total_tokens": 0,
            "saved_tokens": 0,
            "last_successful_step": 0,
        },
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(state), ensure_ascii=False, indent=2), encoding="utf-8")
    return state


class MissionPipeline:
    def __init__(self, workspace_root: str | Path, *, launch_id: str) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.launch_id = str(launch_id)
        self.path = self.workspace_root / "mission_pipeline" / self.launch_id / "state.json"
        self._mission_state_path: Path | None = None

    def begin(self, *, spec: dict[str, Any], execute: bool, request: dict[str, Any] | None = None) -> dict[str, Any]:
        now = _now()
        state = {
            "schema_version": 1,
            "mission_id": "",
            "launch_id": self.launch_id,
            "current_state": PIPELINE_APPROVED if execute else PIPELINE_REVIEW,
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
        self.write(state)
        return state

    def attach_mission(self, state: dict[str, Any], mission_id: str) -> dict[str, Any]:
        mid = str(mission_id or "").strip()
        if not mid:
            return state
        state["mission_id"] = mid
        self._mission_state_path = self.workspace_root / "missions" / mid / "state.json"
        return self.transition(state, str(state.get("current_state") or PIPELINE_REVIEW), "mission_attached", mission_id=mid)

    def transition(self, state: dict[str, Any], next_state: str, event: str, **fields: Any) -> dict[str, Any]:
        if next_state not in PIPELINE_STATES:
            raise ValueError(f"unknown pipeline state: {next_state}")
        now = _now()
        state["current_state"] = next_state
        state["updated_at"] = now
        context = state.setdefault("context", {})
        history = context.setdefault("history", [])
        if isinstance(history, list):
            entry = {"at": now, "event": str(event), "state": next_state}
            entry.update({key: value for key, value in fields.items() if value is not None})
            history.append(entry)
        self.write(state)
        return state

    def write(self, state: dict[str, Any]) -> None:
        payload = json.dumps(to_jsonable(state), ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(payload, encoding="utf-8")
        if self._mission_state_path is not None:
            self._mission_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._mission_state_path.write_text(payload, encoding="utf-8")

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
