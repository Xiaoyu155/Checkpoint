from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Observation, ProviderKind


FIXTURE_TYPES = ("empty", "standard", "with_data", "error", "boundary")


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


def load_named_fixture(path: str | Path) -> dict[str, Any]:
    fixture_path = Path(path)
    text = fixture_path.read_text(encoding="utf-8")
    if fixture_path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required for YAML fixtures. Run: pip install PyYAML") from exc
        payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ValueError(f"Fixture file must contain an object: {fixture_path}")
    return payload


def fixture_template_payload(
    *,
    name: str,
    page: str,
    fixture_type: str,
    description: str | None = None,
) -> dict[str, Any]:
    if fixture_type not in FIXTURE_TYPES:
        raise ValueError(f"Unsupported fixture type: {fixture_type}")
    return {
        "schema_version": 1,
        "name": name,
        "type": fixture_type,
        "page": page,
        "description": description or f"Generated {fixture_type} fixture for {page}",
        "data": {},
        "metadata": {
            "source": "generate-fixture",
            "page": page,
            "fixture_type": fixture_type,
        },
    }


def render_fixture_template(payload: dict[str, Any]) -> str:
    try:
        import yaml
    except ImportError:
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
