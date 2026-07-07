from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fixtures import observation_from_dict
from .models import Bounds, LocationEvidence, Point, ProviderKind, ResolvedTarget, Target
from .workflow_types import WorkflowContext

@dataclass(frozen=True)
class WorkflowState:
    run_id: str
    workflow_name: str
    completed_steps: tuple[str, ...]
    failed_step: str | None = None


class StateStore:
    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "state.json"

    def load(self) -> WorkflowState | None:
        if not self.path.exists():
            return None
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return WorkflowState(
            run_id=str(payload["run_id"]),
            workflow_name=str(payload["workflow_name"]),
            completed_steps=tuple(payload.get("completed_steps") or ()),
            failed_step=payload.get("failed_step"),
        )

    def save(self, state: WorkflowState) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(state_to_dict(state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def state_to_dict(state: WorkflowState) -> dict[str, Any]:
    return {
        "run_id": state.run_id,
        "workflow_name": state.workflow_name,
        "completed_steps": list(state.completed_steps),
        "failed_step": state.failed_step,
    }


def hydrate_context_from_completed_steps(context: WorkflowContext, completed_steps: tuple[str, ...]) -> None:
    for step_id in completed_steps:
        path = context.run_dir / f"{step_id}.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("observation"):
            context.observations[step_id] = observation_from_dict(payload["observation"])
        if payload.get("resolved_target"):
            context.resolved_targets[step_id] = resolved_target_from_dict(payload["resolved_target"])
        if payload.get("action") == "set_variable" and isinstance(payload.get("metadata"), dict):
            name = payload["metadata"].get("name")
            if name:
                context.variables[str(name)] = payload["metadata"].get("value")


def resolved_target_from_dict(payload: dict[str, Any]) -> ResolvedTarget:
    target_payload = payload["target"]
    evidence_payload = payload["evidence"]
    return ResolvedTarget(
        target=Target(
            text=target_payload.get("text"),
            role=target_payload.get("role"),
            label=target_payload.get("label"),
            window_title=target_payload.get("window_title"),
            preferred=tuple(ProviderKind(item) for item in target_payload.get("preferred", [])) or Target().preferred,
        ),
        evidence=LocationEvidence(
            provider=ProviderKind(evidence_payload["provider"]),
            confidence=float(evidence_payload["confidence"]),
            reason=str(evidence_payload["reason"]),
            point=point_from_dict(evidence_payload.get("point")),
            bounds=bounds_from_dict(evidence_payload.get("bounds")),
            handle=evidence_payload.get("handle"),
            metadata=dict(evidence_payload.get("metadata") or {}),
        ),
    )


def point_from_dict(payload: dict[str, Any] | None) -> Point | None:
    if not payload:
        return None
    return Point(x=int(payload["x"]), y=int(payload["y"]))


def bounds_from_dict(payload: dict[str, Any] | None) -> Bounds | None:
    if not payload:
        return None
    return Bounds(
        left=int(payload["left"]),
        top=int(payload["top"]),
        width=int(payload["width"]),
        height=int(payload["height"]),
    )
