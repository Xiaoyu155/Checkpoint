from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import ActionResult, Observation, ResolvedTarget, to_jsonable


@dataclass
class WorkflowContext:
    run_id: str
    run_dir: Path
    inputs: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    sensitive_fields: set[str] = field(default_factory=set)
    resources: dict[str, Any] = field(default_factory=dict)
    observations: dict[str, Observation] = field(default_factory=dict)
    _observation_cache: dict[str, Observation] = field(default_factory=dict, repr=False)
    resolved_targets: dict[str, ResolvedTarget] = field(default_factory=dict)
    actions: dict[str, ActionResult] = field(default_factory=dict)

    def observation_cache_key(self, action: str, params: dict[str, Any]) -> str:
        payload = {"action": action, "params": to_jsonable(params)}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def get_cached_observation(self, key: str) -> Observation | None:
        return self._observation_cache.get(key)

    def set_cached_observation(self, key: str, observation: Observation) -> None:
        self._observation_cache[key] = observation

    def invalidate_observation_cache(self) -> None:
        self._observation_cache.clear()

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
