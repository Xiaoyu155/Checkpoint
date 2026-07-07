from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CloudRequestContext:
    org: str = ""
    user_id: str = ""
    source: str = "default"

    @property
    def scope_key(self) -> str:
        return normalize_scope_component(self.org)


def normalize_scope_component(value: str, *, default: str = "default") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    cleaned = []
    for char in text.lower():
        if char.isalnum() or char in {"-", "_"}:
            cleaned.append(char)
        elif cleaned and cleaned[-1] != "_":
            cleaned.append("_")
    normalized = "".join(cleaned).strip("_")
    return normalized or default


def resolve_cloud_context(
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    default_org: str = "",
    default_user_id: str = "",
) -> CloudRequestContext:
    headers = headers or {}
    payload = payload or {}
    org = str(
        headers.get("x-visual-agent-org")
        or headers.get("x-visual-agent-tenant")
        or payload.get("org")
        or payload.get("organization")
        or default_org
        or ""
    ).strip()
    user_id = str(
        headers.get("x-visual-agent-user")
        or payload.get("user_id")
        or payload.get("user")
        or default_user_id
        or ""
    ).strip()
    source = "header" if any(headers.get(key) for key in ("x-visual-agent-org", "x-visual-agent-user")) else "payload" if any(
        key in payload for key in ("org", "organization", "user_id", "user")
    ) else "default"
    return CloudRequestContext(org=org, user_id=user_id, source=source)
