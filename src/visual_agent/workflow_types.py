from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import ActionResult, Observation, ResolvedTarget


@dataclass
class WorkflowContext:
    run_id: str
    run_dir: Path
    inputs: dict[str, Any] = field(default_factory=dict)
    sensitive_fields: set[str] = field(default_factory=set)
    resources: dict[str, Any] = field(default_factory=dict)
    observations: dict[str, Observation] = field(default_factory=dict)
    resolved_targets: dict[str, ResolvedTarget] = field(default_factory=dict)
    actions: dict[str, ActionResult] = field(default_factory=dict)

    @property
    def latest_observation(self) -> Observation:
        if not self.observations:
            raise RuntimeError("No observation available. Add an observe_* step first.")
        return next(reversed(self.observations.values()))

    @property
    def latest_observation_or_none(self) -> Observation | None:
        if not self.observations:
            return None
        return next(reversed(self.observations.values()))

    @property
    def latest_resolved_target(self) -> ResolvedTarget:
        if not self.resolved_targets:
            raise RuntimeError("No resolved target available. Add a resolve step first.")
        return next(reversed(self.resolved_targets.values()))

    @property
    def latest_resolved_target_or_none(self) -> ResolvedTarget | None:
        if not self.resolved_targets:
            return None
        return next(reversed(self.resolved_targets.values()))
