from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class CloudIdentityError(ValueError):
    """A request tried to select an identity not bound to its credential."""


@dataclass(frozen=True)
class CloudRequestContext:
    org: str = ""
    user_id: str = ""
    source: str = "default"

    @property
    def scope_key(self) -> str:
        return f"{normalize_scope_component(self.org)}:{normalize_scope_component(self.user_id)}"


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
    claimed_orgs = [
        str(value).strip()
        for value in (
            headers.get("x-visual-agent-org"),
            headers.get("x-visual-agent-tenant"),
            payload.get("org"),
            payload.get("organization"),
        )
        if value is not None and str(value).strip()
    ]
    claimed_user_ids = [
        str(value).strip()
        for value in (
            headers.get("x-visual-agent-user"),
            payload.get("user_id"),
            payload.get("user"),
        )
        if value is not None and str(value).strip()
    ]
    bound_org = str(default_org or "").strip()
    bound_user_id = str(default_user_id or "service-account").strip() or "service-account"
    if any(claimed_org != bound_org for claimed_org in claimed_orgs):
        raise CloudIdentityError("Request organization does not match the server-bound organization.")
    if any(claimed_user_id != bound_user_id for claimed_user_id in claimed_user_ids):
        raise CloudIdentityError("Request user does not match the server-bound user.")
    return CloudRequestContext(org=bound_org, user_id=bound_user_id, source="server")
