from __future__ import annotations

import hashlib
import ipaddress
import re
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
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
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "reasoning_output_tokens",
    "auto_compact_token_limit",
    "uncached_input_tokens",
    "current_context_input_tokens",
    "current_context_total_tokens",
    "accumulated_uncached_input_tokens",
}
SECRET_TEXT_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}"),
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

READ_ONLY_COMMAND_PATTERNS = (
    re.compile(r"^\s*git\s+(status|diff|log|show|branch|rev-parse|ls-files)\b", re.IGNORECASE),
    re.compile(r"^\s*python\s+-m\s+pytest\b", re.IGNORECASE),
    re.compile(r"^\s*python\s+-m\s+visual_agent\.cli\s+(codex-check|verify-now|verify|test-plan|chief-memory|repo-map)\b", re.IGNORECASE),
    re.compile(r"^\s*pytest\b", re.IGNORECASE),
    re.compile(r"^\s*npm\s+(test|run\s+(test|check|lint|build))\b", re.IGNORECASE),
    re.compile(r"^\s*pnpm\s+(test|run\s+(test|check|lint|build))\b", re.IGNORECASE),
    re.compile(r"^\s*yarn\s+(test|run\s+(test|check|lint|build))\b", re.IGNORECASE),
    re.compile(r"^\s*go\s+test\b", re.IGNORECASE),
    re.compile(r"^\s*cargo\s+test\b", re.IGNORECASE),
)
DANGEROUS_COMMAND_PATTERNS = (
    re.compile(r"\brm\s+(-[^\s]*[rf][^\s]*|-[^\s]*[fr][^\s]*)\s+(/|~|\*|[A-Za-z]:\\?)", re.IGNORECASE),
    re.compile(r"\b(?:del|erase)\s+/(?:s|q|f)\b.*(?:[A-Za-z]:\\|\\Windows\\|\\Users\\|\*)", re.IGNORECASE),
    re.compile(r"\bformat\s+[A-Za-z]:", re.IGNORECASE),
    re.compile(r"\bshutdown\s+/(?:s|r|p)\b", re.IGNORECASE),
    re.compile(r"\breg\s+delete\b", re.IGNORECASE),
    re.compile(r"\b(?:chmod|chown)\s+-R\s+(?:/|~|[A-Za-z]:\\)", re.IGNORECASE),
    re.compile(r">\s*(?:/etc/|[A-Za-z]:\\Windows\\)", re.IGNORECASE),
)
ASK_COMMAND_PATTERNS = (
    re.compile(r"\b(?:npm|pnpm|yarn)\s+publish\b", re.IGNORECASE),
    re.compile(r"\b(?:npm|pnpm|yarn|pip|pip3|uv|poetry|cargo|go)\s+(?:install|add|remove|update|upgrade)\b", re.IGNORECASE),
    re.compile(r"\b(?:curl|wget|Invoke-WebRequest|iwr|Invoke-RestMethod)\b", re.IGNORECASE),
    re.compile(r"\b(?:scp|ssh|rsync)\b", re.IGNORECASE),
    re.compile(r"\b(?:docker|kubectl|aws|gcloud|az)\b", re.IGNORECASE),
    re.compile(r"\b(?:python|python3|node|deno|bun|ruby|perl|php)\s+-c\b", re.IGNORECASE),
    re.compile(r"\b(?:bash|sh|zsh|powershell|pwsh)\s+-c\b", re.IGNORECASE),
)


def is_loopback_host(host: str) -> bool:
    value = str(host or "").strip().lower()
    if value in PRIVATE_HOSTS:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


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
    if scheme == "file":
        host = str(parsed.hostname or "").strip().lower()
        if host and not is_loopback_host(host):
            return False, f"Blocked non-local file URL host: {host}"
        path = str(parsed.path or "").strip()
        if not path:
            return False, "File URL is missing a path."
        if PureWindowsPath(path.lstrip("/")).is_absolute() or PurePosixPath(path).is_absolute():
            return True, None
        return False, "File URL path must be absolute."
    if scheme not in {"http", "https"}:
        return False, f"Unsupported URL scheme: {parsed.scheme or 'missing'}"
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return False, "URL is missing a host."
    if is_loopback_host(host):
        return True, None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host.endswith(".localhost") or any(host.endswith(suffix) for suffix in PRIVATE_HOST_SUFFIXES):
            return False, f"Blocked private host: {host}"
        return True, None
    if any(ip in network for network in PRIVATE_IP_NETWORKS) or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return False, f"Blocked private IP address: {host}"
    return True, None


def assess_command_risk(command: str, *, repo_root: str | Path | None = None) -> dict[str, Any]:
    text = str(command or "").strip()
    if not text:
        return {"decision": "deny", "risk": "high", "reason": "empty_command", "findings": ["Command is empty."]}
    redacted = redact_secret_text(text)
    findings: list[str] = []
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(text):
            findings.append(f"dangerous pattern: {pattern.pattern}")
            return {
                "decision": "deny",
                "risk": "critical",
                "reason": "dangerous_command",
                "command": redacted,
                "findings": findings,
            }
    if contains_secret_text(text):
        findings.append("command contains text that looks like a secret")
    for pattern in ASK_COMMAND_PATTERNS:
        if pattern.search(text):
            findings.append(f"requires review: {pattern.pattern}")
    if repo_root is not None:
        outside = _redirects_outside_repo(text, Path(repo_root).expanduser().resolve())
        if outside:
            findings.append(f"writes outside repo: {outside}")
    if findings:
        return {
            "decision": "ask",
            "risk": "high" if any("secret" in item or "outside repo" in item for item in findings) else "medium",
            "reason": "review_required",
            "command": redacted,
            "findings": findings,
        }
    if any(pattern.search(text) for pattern in READ_ONLY_COMMAND_PATTERNS):
        return {"decision": "allow", "risk": "low", "reason": "known_readonly_or_test_command", "command": redacted, "findings": []}
    return {
        "decision": "ask",
        "risk": "medium",
        "reason": "unknown_command",
        "command": redacted,
        "findings": ["Command is not in the known safe set."],
    }


def permission_plan(commands: list[str] | tuple[str, ...], *, repo_root: str | Path | None = None) -> dict[str, Any]:
    checks = [assess_command_risk(command, repo_root=repo_root) for command in commands]
    if any(item["decision"] == "deny" for item in checks):
        decision = "deny"
    elif any(item["decision"] == "ask" for item in checks):
        decision = "ask"
    else:
        decision = "allow"
    risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    risk = max((str(item.get("risk") or "low") for item in checks), key=lambda item: risk_order.get(item, 0), default="low")
    return {
        "schema_version": 1,
        "decision": decision,
        "risk": risk,
        "repo_root": str(Path(repo_root).expanduser().resolve()) if repo_root is not None else "",
        "checks": checks,
    }


def permission_plan_to_markdown(payload: dict[str, Any]) -> str:
    lines = ["## Permission Plan", "", f"Decision: `{payload.get('decision')}`", f"Risk: `{payload.get('risk')}`"]
    for item in payload.get("checks") or []:
        if not isinstance(item, dict):
            continue
        lines.extend(["", f"- `{item.get('decision')}` `{item.get('risk')}`: {item.get('command') or ''}", f"  reason: `{item.get('reason')}`"])
        findings = item.get("findings") if isinstance(item.get("findings"), list) else []
        for finding in findings[:5]:
            lines.append(f"  finding: {finding}")
    return "\n".join(lines)


def _redirects_outside_repo(command: str, root: Path) -> str:
    for match in re.finditer(r"(?:>|>>)\s*([^\s;&|]+)", command):
        raw = match.group(1).strip("\"'")
        path = Path(raw)
        if not path.is_absolute():
            continue
        try:
            path.resolve().relative_to(root)
        except ValueError:
            return str(path)
    return ""
