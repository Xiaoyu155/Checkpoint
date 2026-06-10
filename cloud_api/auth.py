from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class ApiKey:
    token: str
    salt: str
    sha256: str


def generate_api_key(*, prefix: str = "va_cloud", salt: str | None = None) -> ApiKey:
    resolved_salt = salt or secrets.token_hex(16)
    token = f"{prefix}_{secrets.token_urlsafe(32)}"
    return ApiKey(token=token, salt=resolved_salt, sha256=hash_api_key(token, resolved_salt))


def hash_api_key(token: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{token}".encode("utf-8")).hexdigest()


def verify_api_key(token: str, *, expected_sha256: str, salt: str) -> bool:
    if not token or not expected_sha256 or not salt:
        return False
    return secrets.compare_digest(hash_api_key(token, salt), expected_sha256)


def bearer_token(authorization: str) -> str:
    prefix = "Bearer "
    value = str(authorization or "")
    return value[len(prefix) :].strip() if value.startswith(prefix) else ""
