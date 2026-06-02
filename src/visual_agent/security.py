from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SecretPolicy:
    sensitive: bool = False
    salt: str = "visual-agent"


def text_metadata(text: str, *, sensitive: bool = False, salt: str = "visual-agent") -> dict[str, object]:
    if sensitive:
        return {
            "sensitive": True,
            "sha256": hashlib.sha256(f"{salt}:{text}".encode("utf-8")).hexdigest(),
        }
    preview = text[:3] + "***" if len(text) > 3 else "***"
    return {
        "sensitive": False,
        "text_length": len(text),
        "text_preview": preview,
    }


SECRET_KEY_HINTS = ("password", "passwd", "pwd", "token", "secret", "cookie", "api_key", "apikey", "authorization", "bearer")
SECRET_TEXT_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)([\"']?(?:api[_-]?key|password|passwd|pwd|token|secret|authorization|bearer|cookie)[\"']?\s*:\s*[\"']?)[^\"'}\s,&|;`]{3,}"),
    re.compile(r"(?i)((?:api[_-]?key|password|passwd|pwd|token|secret|authorization|bearer|cookie)\s*[:=]\s*)[^\s,&|;`'\"]{3,}"),
)


def scrub_secrets(value: Any, *, extra_secrets: tuple[str, ...] | list[str] | set[str] = ()) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key).lower()
            if any(hint in text_key for hint in SECRET_KEY_HINTS):
                cleaned[str(key)] = {"redacted": True}
            else:
                cleaned[str(key)] = scrub_secrets(item, extra_secrets=extra_secrets)
        return cleaned
    if isinstance(value, list):
        return [scrub_secrets(item, extra_secrets=extra_secrets) for item in value]
    if isinstance(value, tuple):
        return [scrub_secrets(item, extra_secrets=extra_secrets) for item in value]
    if isinstance(value, str):
        return redact_secret_text(value, extra_secrets=extra_secrets)
    return value


def redact_secret_text(text: str, *, extra_secrets: tuple[str, ...] | list[str] | set[str] = ()) -> str:
    redacted = str(text or "")
    for secret in sorted({str(item) for item in extra_secrets if str(item)}, key=len, reverse=True):
        if len(secret) >= 3:
            redacted = redacted.replace(secret, "[REDACTED]")
    for pattern in SECRET_TEXT_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]", redacted)
    return redacted


def contains_secret_text(text: str, *, extra_secrets: tuple[str, ...] | list[str] | set[str] = ()) -> bool:
    value = str(text or "")
    for secret in extra_secrets:
        if str(secret) and str(secret) in value:
            return True
    return any(pattern.search(value) for pattern in SECRET_TEXT_PATTERNS)
