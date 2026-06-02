from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Observation, ProviderKind


def load_observation_fixture(path: str | Path) -> Observation:
    fixture_path = Path(path)
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    return observation_from_dict(payload, source_fallback=str(fixture_path))


def observation_from_dict(payload: dict[str, Any], *, source_fallback: str = "fixture") -> Observation:
    provider = ProviderKind(payload.get("provider", ProviderKind.MOCK.value))
    return Observation(
        provider=provider,
        source=str(payload.get("source") or source_fallback),
        screenshot_path=Path(payload["screenshot_path"]) if payload.get("screenshot_path") else None,
        width=payload.get("width"),
        height=payload.get("height"),
        elements=tuple(payload.get("elements") or ()),
        metadata=dict(payload.get("metadata") or {}),
    )

