from __future__ import annotations

import hashlib
import ipaddress
import re
from urllib.parse import urlparse
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
SAFE_SECRETISH_KEYS = {
    "token_estimate",
    "token_count",
    "tokens_used",
    "total_tokens",
    "max_tokens",
    "max_completion_tokens",
}
SECRET_TEXT_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)([\"']?(?:api[_-]?key|password|passwd|pwd|token|secret|authorization|bearer|cookie)[\"']?\s*:\s*[\"']?)[^\"'}\s,&|;`]{3,}"),
    re.compile(r"(?i)((?:api[_-]?key|password|passwd|pwd|token|secret|authorization|bearer|cookie)\s*[:=]\s*)[^\s,&|;`'\"]{3,}"),
)

PRIVATE_HOST_SUFFIXES = (".local", ".localdomain", ".internal", ".intranet", ".corp", ".lan")
PRIVATE_HOSTS = {"localhost"}
PRIVATE_IP_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def scrub_secrets(value: Any, *, extra_secrets: tuple[str, ...] | list[str] | set[str] = ()) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key).lower()
            if text_key in SAFE_SECRETISH_KEYS:
                cleaned[str(key)] = scrub_secrets(item, extra_secrets=extra_secrets)
            elif any(hint in text_key for hint in SECRET_KEY_HINTS):
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


def validate_workflow_url(url: str) -> tuple[bool, str | None]:
    raw = str(url or "").strip()
    if not raw:
        return False, "URL is empty."
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return False, f"Unsupported URL scheme: {parsed.scheme or 'missing'}"
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return False, "URL is missing a host."
    if host in PRIVATE_HOSTS:
        return True, None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host.endswith(".localhost") or any(host.endswith(suffix) for suffix in PRIVATE_HOST_SUFFIXES):
            return False, f"Blocked private host: {host}"
        return True, None
    if any(ip in network for network in PRIVATE_IP_NETWORKS) or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return False, f"Blocked private IP address: {host}"
    return True, None
