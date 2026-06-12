from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .env import env_get, provider_api_key_env_names

DEFAULT_MODEL_CREDENTIAL_FILE = "model_api_keys.txt"
DEFAULT_MODEL_PROVIDER = "openai"
DEFAULT_MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
DEFAULT_MIMO_ENDPOINT = "/chat/completions"
DEFAULT_MIMO_MODEL = "mimo-v2.5"
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "xiaomimimo": {
        "base_url": DEFAULT_MIMO_BASE_URL,
        "endpoint": DEFAULT_MIMO_ENDPOINT,
        "model": DEFAULT_MIMO_MODEL,
        "auth_style": "api-key",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "endpoint": "/chat/completions",
        "model": "qwen-max",
        "auth_style": "bearer",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "endpoint": "/chat/completions",
        "model": "moonshot-v1-8k",
        "auth_style": "bearer",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "endpoint": "/chat/completions",
        "model": "deepseek-chat",
        "auth_style": "bearer",
    },
    "volcengine": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "endpoint": "/chat/completions",
        "model": "",
        "auth_style": "bearer",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "endpoint": "/chat/completions",
        "model": "gpt-4o",
        "auth_style": "bearer",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "endpoint": "/messages",
        "model": "claude-3-5-haiku-latest",
        "auth_style": "x-api-key",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "endpoint": "/models/gemini-1.5-flash:generateContent",
        "model": "gemini-1.5-flash",
        "auth_style": "api-key",
    },
}
PROVIDER_ALIASES = {
    "xiaomimimo": ("xiaomimimo", "xiaomi", "mimo", "小米", "小米mimo"),
    "qwen": ("qwen", "千问", "通义", "dashscope"),
    "kimi": ("kimi", "moonshot"),
    "deepseek": ("deepseek",),
    "volcengine": ("火山", "volcengine", "ark", "doubao"),
    "openai": ("openai", "gpt", "gpt-4", "gpt-4o"),
    "anthropic": ("anthropic", "claude"),
    "gemini": ("gemini", "google"),
}


def inspect_model_credentials(
    *,
    source: str | Path | None = None,
    preferred_provider: str | None = None,
) -> dict[str, Any]:
    path = Path(source or env_get("VISUAL_AGENT_MODEL_CREDENTIAL_FILE") or DEFAULT_MODEL_CREDENTIAL_FILE)
    configured_preferred = preferred_provider or env_get("VISUAL_AGENT_MODEL_PROVIDER")
    preferred = normalize_provider(configured_preferred or DEFAULT_MODEL_PROVIDER)
    allow_auto_select = configured_preferred is None
    env_secret = load_provider_secret_from_env(preferred)
    if not path.exists():
        providers = [env_credential_entry(preferred)] if env_secret else []
        result = {
            "schema_version": 1,
            "source": str(path),
            "source_exists": False,
            "preferred_provider": preferred,
            "preferred_available": bool(env_secret),
            "selected_provider": preferred if env_secret else None,
            "auto_selected": False,
            "providers": providers,
            "redacted": True,
        }
        result["suggestion"] = model_credentials_suggestion(result)
        return result
    text = path.read_text(encoding="utf-8-sig")
    providers = discover_model_credentials(text)
    if env_secret and not any(item["provider"] == preferred for item in providers):
        providers.append(env_credential_entry(preferred))
    preferred_entry = next((item for item in providers if item["provider"] == preferred), None)
    fallback_entry = next((item for item in providers if int(item.get("secret_count") or 0) > 0), None)
    selected_provider = preferred if preferred_entry else None
    auto_selected = False
    if selected_provider is None and allow_auto_select and fallback_entry:
        selected_provider = str(fallback_entry.get("provider") or "")
        auto_selected = True
    result = {
        "schema_version": 1,
        "source": str(path),
        "source_exists": True,
        "preferred_provider": preferred,
        "preferred_available": preferred_entry is not None,
        "selected_provider": selected_provider or None,
        "auto_selected": auto_selected,
        "providers": providers,
        "redacted": True,
    }
    result["suggestion"] = model_credentials_suggestion(result)
    return result


def model_credentials_suggestion(result: dict[str, Any]) -> str:
    if result.get("preferred_available"):
        return ""
    providers = result.get("providers") if isinstance(result.get("providers"), list) else []
    available = [
        str(item.get("provider") or "")
        for item in providers
        if isinstance(item, dict) and int(item.get("secret_count") or 0) > 0 and str(item.get("provider") or "")
    ]
    if "anthropic" in available:
        return "Anthropic key detected, use --preferred anthropic or choose a Claude model such as --model claude-3-5-haiku-latest."
    if "openai" in available:
        return "OpenAI key detected, use --preferred openai or choose an OpenAI model such as --model gpt-4o."
    if "gemini" in available:
        return "Gemini key detected, use --preferred gemini or choose a Gemini model such as --model gemini-1.5-flash."
    preferred = str(result.get("preferred_provider") or DEFAULT_MODEL_PROVIDER)
    env_name, _ = provider_api_key_env_names(normalize_provider(preferred))
    return f"No {preferred} key found. Add it to {result.get('source') or DEFAULT_MODEL_CREDENTIAL_FILE} or set {env_name}."


def build_model_api_probe_plan(
    *,
    source: str | Path | None = None,
    preferred_provider: str | None = None,
    base_url: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    credentials = inspect_model_credentials(source=source, preferred_provider=preferred_provider)
    selected_provider = credentials.get("selected_provider")
    selected_entry = next(
        (
            item
            for item in credentials.get("providers", [])
            if isinstance(item, dict) and item.get("provider") == selected_provider
        ),
        None,
    )
    blockers: list[str] = []
    env_secret = load_provider_secret_from_env(str(selected_provider or credentials.get("preferred_provider") or ""))
    if not credentials.get("source_exists") and not env_secret:
        blockers.append("credential_source_missing")
    if not credentials.get("preferred_available") and not credentials.get("auto_selected"):
        blockers.append("preferred_provider_missing")
    if (not selected_entry or int(selected_entry.get("secret_count") or 0) <= 0) and not env_secret:
        blockers.append("provider_secret_missing")
    provider_for_defaults = str(selected_provider or credentials.get("preferred_provider") or "")
    base_url = default_base_url_for_provider(provider_for_defaults, base_url)
    endpoint = default_endpoint_for_provider(provider_for_defaults, endpoint)
    model = default_model_for_provider(provider_for_defaults, model)
    if not str(base_url or "").strip():
        blockers.append("missing_base_url")
    if not str(endpoint or "").strip():
        blockers.append("missing_probe_endpoint")
    return {
        "schema_version": 1,
        "provider": credentials.get("preferred_provider"),
        "selected_provider": selected_provider,
        "ready": not blockers,
        "blockers": sorted(set(blockers)),
        "source": credentials.get("source"),
        "credential_ref": selected_entry,
        "probe": {
            "base_url": str(base_url or ""),
            "endpoint": str(endpoint or ""),
            "model": str(model or ""),
            "method": "GET",
            "mode": "plan-only",
            "sends_secret": False,
        },
        "credentials": credentials,
        "redacted": True,
    }


def resolve_model_provider_config(
    *,
    source: str | Path | None = None,
    preferred_provider: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    provider = normalize_provider(preferred_provider or env_get("VISUAL_AGENT_MODEL_PROVIDER") or DEFAULT_MODEL_PROVIDER)
    credentials = inspect_model_credentials(source=source, preferred_provider=provider)
    selected_provider = credentials.get("selected_provider") or provider
    credential_source = Path(str(credentials.get("source") or source or DEFAULT_MODEL_CREDENTIAL_FILE))
    secret = str(api_key or load_provider_secret(credential_source, str(selected_provider or provider)) or "")
    resolved_base_url = default_base_url_for_provider(str(selected_provider or provider), base_url)
    resolved_endpoint = default_endpoint_for_provider(str(selected_provider or provider), endpoint)
    resolved_model = default_model_for_provider(str(selected_provider or provider), model)
    blockers = []
    if not secret:
        blockers.append("missing_api_key")
    if not resolved_base_url:
        blockers.append("missing_base_url")
    if not resolved_model:
        blockers.append("missing_model")
    if not resolved_endpoint:
        blockers.append("missing_endpoint")
    return {
        "schema_version": 1,
        "provider": str(selected_provider or provider),
        "preferred_provider": provider,
        "source": str(credential_source),
        "credentials": credentials,
        "base_url": resolved_base_url,
        "endpoint": resolved_endpoint,
        "model": resolved_model,
        "_api_key": secret,
        "_auth_headers": build_auth_headers(str(selected_provider or provider), secret) if secret else {},
        "api_key_configured": bool(secret),
        "auth_headers_configured": bool(secret),
        "available": not blockers,
        "blockers": sorted(set(blockers)),
        "redacted": True,
    }


def run_model_api_probe(
    *,
    source: str | Path | None = None,
    preferred_provider: str | None = None,
    base_url: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
    prompt: str = "回复 OK",
    timeout_seconds: float = 15.0,
    max_completion_tokens: int = 64,
) -> dict[str, Any]:
    plan = build_model_api_probe_plan(
        source=source,
        preferred_provider=preferred_provider,
        base_url=base_url,
        endpoint=endpoint,
        model=model,
    )
    if not plan["ready"]:
        return {**plan, "status": "blocked", "executed": False}
    secret = load_provider_secret(Path(str(plan["source"])), str(plan["selected_provider"] or ""))
    if not secret:
        blocked = dict(plan)
        blocked["ready"] = False
        blocked["blockers"] = sorted(set(list(blocked.get("blockers") or []) + ["provider_secret_unreadable"]))
        return {**blocked, "status": "blocked", "executed": False}
    probe = plan["probe"]
    url = str(probe["base_url"]).rstrip("/") + "/" + str(probe["endpoint"]).lstrip("/")
    body = {
        "model": str(probe.get("model") or DEFAULT_MIMO_MODEL),
        "messages": [
            {"role": "system", "content": "You are a health-check endpoint. Reply with exactly OK."},
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": max(1, int(max_completion_tokens)),
        "temperature": 0,
        "stream": False,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **build_auth_headers(str(plan["selected_provider"] or ""), secret),
        },
        method="POST",
    )
    started_plan = {**plan, "probe": {**probe, "sends_secret": True, "mode": "execute"}}
    try:
        with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
            response_text = response.read().decode("utf-8", errors="replace")
            payload = json.loads(response_text)
            return {
                **started_plan,
                "status": "success",
                "executed": True,
                "http_status": int(getattr(response, "status", 0) or 0),
                "response": compact_probe_response(payload),
                "redacted": True,
            }
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        return {
            **started_plan,
            "status": "http_error",
            "executed": True,
            "http_status": int(exc.code),
            "error": redact_error_text(body_text),
            "redacted": True,
        }
    except Exception as exc:
        return {
            **started_plan,
            "status": "error",
            "executed": True,
            "error": {"type": exc.__class__.__name__, "message": redact_error_text(str(exc))},
            "redacted": True,
        }


def discover_model_credentials(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        provider = provider_from_line(line)
        secret_refs = secret_references(line)
        if provider is None and not secret_refs:
            continue
        provider = provider or "unknown"
        key = (provider, line_number)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "provider": provider,
                "line_number": line_number,
                "secret_count": len(secret_refs),
                "secret_refs": secret_refs,
                "summary": redact_credential_line(line),
            }
        )
    return entries


def env_credential_entry(provider: str) -> dict[str, Any]:
    return {
        "provider": normalize_provider(provider),
        "line_number": None,
        "secret_count": 1,
        "secret_refs": [{"type": "env_api_key", "fingerprint": "***"}],
        "summary": "environment variable: ***",
    }


def provider_from_line(line: str) -> str | None:
    normalized = line.lower()
    for provider, aliases in PROVIDER_ALIASES.items():
        if any(alias.lower() in normalized for alias in aliases):
            return provider
    return None


def normalize_provider(value: str) -> str:
    normalized = str(value or "").strip().lower()
    for provider, aliases in PROVIDER_ALIASES.items():
        if normalized == provider or normalized in {alias.lower() for alias in aliases}:
            return provider
    return normalized or DEFAULT_MODEL_PROVIDER


def secret_references(line: str) -> list[dict[str, str]]:
    refs = []
    for match in re.finditer(r"(sk-[A-Za-z0-9_-]{20,})", line):
        refs.append({"type": "api_key", "fingerprint": redact_secret(match.group(1))})
    assigned = re.search(
        r"(?:api[_-]?key|secret|token|password)\s*[:=]\s*([A-Za-z0-9_\-.]{32,})",
        line,
        re.IGNORECASE,
    )
    if assigned:
        refs.append({"type": "api_key", "fingerprint": redact_secret(assigned.group(1))})
    return refs


def load_provider_secret(path: Path, provider: str) -> str | None:
    normalized = normalize_provider(provider)
    env_secret = load_provider_secret_from_env(normalized)
    if env_secret:
        return env_secret
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if provider_from_line(line) != normalized:
            continue
        secret = extract_secret_from_line(line)
        if secret:
            return secret
    return None


def load_provider_secret_from_env(provider: str) -> str | None:
    normalized = normalize_provider(provider)
    checkpoint_name, legacy_name = provider_api_key_env_names(normalized)
    names = [
        checkpoint_name,
        legacy_name,
        f"{normalized.upper()}_API_KEY",
    ]
    if normalized == "openai":
        names.append("OPENAI_API_KEY")
    if normalized == "anthropic":
        names.append("ANTHROPIC_API_KEY")
    if normalized == "gemini":
        names.extend(["GEMINI_API_KEY", "GOOGLE_API_KEY"])
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def extract_secret_from_line(line: str) -> str | None:
    match = re.search(r"(sk-[A-Za-z0-9_-]{20,})", line)
    if match:
        return match.group(1)
    match = re.search(
        r"(?:api[_-]?key|secret|token|password)\s*[:=]\s*([A-Za-z0-9_\-.]{32,})",
        line,
        re.IGNORECASE,
    )
    if match:
        value = match.group(1).strip()
        if "://" not in value and not value.startswith("#"):
            return value
    return None


def redact_credential_line(line: str) -> str:
    redacted = line
    redacted = re.sub(r"sk-[A-Za-z0-9_-]{6,}", lambda item: redact_secret(item.group(0)), redacted)
    redacted = re.sub(
        r"(?i)(secret access key|api[_ -]?key|token|登录密码|密码)\s*[:=]\s*.+",
        lambda item: item.group(0).split(":", 1)[0].split("=", 1)[0] + ": ***",
        redacted,
    )
    redacted = re.sub(r"(AKLT[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+", r"\1***", redacted)
    return redacted


def redact_secret(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "***"
    if len(text) <= 8:
        return "***"
    return f"{text[:6]}...{text[-4:]}"


def model_credentials_to_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Model Credentials",
        "",
        f"- Source: `{result.get('source') or ''}`",
        f"- Source exists: `{bool(result.get('source_exists'))}`",
        f"- Preferred provider: `{result.get('preferred_provider') or ''}`",
        f"- Preferred available: `{bool(result.get('preferred_available'))}`",
        f"- Selected provider: `{result.get('selected_provider') or 'none'}`",
        "",
        "## Providers",
        "",
    ]
    providers = result.get("providers") if isinstance(result.get("providers"), list) else []
    if not providers:
        lines.append("No model credentials discovered.")
    else:
        lines.append("| provider | line | secrets | summary |")
        lines.append("| --- | --- | --- | --- |")
        for entry in providers:
            lines.append(
                "| "
                + " | ".join(
                    markdown_cell(value)
                    for value in (
                        entry.get("provider"),
                        entry.get("line_number"),
                        entry.get("secret_count"),
                        entry.get("summary"),
                    )
                )
                + " |"
            )
    if result.get("suggestion"):
        lines.extend(["", f"Hint: {result['suggestion']}"])
    lines.append("")
    return "\n".join(lines)


def model_api_probe_plan_to_markdown(plan: dict[str, Any]) -> str:
    probe = plan.get("probe") if isinstance(plan.get("probe"), dict) else {}
    lines = [
        "# Model API Probe Plan",
        "",
        f"- Provider: `{plan.get('provider') or ''}`",
        f"- Selected provider: `{plan.get('selected_provider') or 'none'}`",
        f"- Ready: `{bool(plan.get('ready'))}`",
        f"- Blockers: {', '.join(f'`{item}`' for item in plan.get('blockers', [])) or 'none'}",
        f"- Base URL: `{probe.get('base_url') or ''}`",
        f"- Endpoint: `{probe.get('endpoint') or ''}`",
        f"- Model: `{probe.get('model') or ''}`",
        f"- Mode: `{probe.get('mode') or 'plan-only'}`",
        f"- Sends secret: `{bool(probe.get('sends_secret'))}`",
        "",
    ]
    credential_ref = plan.get("credential_ref") if isinstance(plan.get("credential_ref"), dict) else {}
    if credential_ref:
        lines.extend(
            [
                "## Credential Reference",
                "",
                f"- Provider: `{credential_ref.get('provider') or ''}`",
                f"- Line: `{credential_ref.get('line_number') or ''}`",
                f"- Secrets: `{credential_ref.get('secret_count') or 0}`",
                f"- Summary: {credential_ref.get('summary') or ''}",
                "",
            ]
        )
    return "\n".join(lines)


def model_api_probe_result_to_markdown(result: dict[str, Any]) -> str:
    lines = [model_api_probe_plan_to_markdown(result).rstrip(), "", "## Probe Result", ""]
    lines.extend(
        [
            f"- Status: `{result.get('status') or 'unknown'}`",
            f"- Executed: `{bool(result.get('executed'))}`",
            f"- HTTP status: `{result.get('http_status') or ''}`",
        ]
    )
    response = result.get("response") if isinstance(result.get("response"), dict) else {}
    if response:
        lines.extend(
            [
                f"- Response model: `{response.get('model') or ''}`",
                f"- Finish reason: `{response.get('finish_reason') or ''}`",
                f"- Content preview: {response.get('content_preview') or ''}",
                f"- Usage: `{json.dumps(response.get('usage') or {}, ensure_ascii=False)}`",
            ]
        )
    error = result.get("error")
    if error:
        lines.append(f"- Error: `{json.dumps(error, ensure_ascii=False) if isinstance(error, dict) else error}`")
    lines.append("")
    return "\n".join(lines)


def default_base_url_for_provider(provider: str, value: str | None) -> str:
    if value:
        return str(value)
    return PROVIDER_DEFAULTS.get(normalize_provider(provider), {}).get("base_url", "")


def default_endpoint_for_provider(provider: str, value: str | None) -> str:
    if value:
        return str(value)
    return PROVIDER_DEFAULTS.get(normalize_provider(provider), {}).get("endpoint", "")


def default_model_for_provider(provider: str, value: str | None) -> str:
    if value:
        return str(value)
    return PROVIDER_DEFAULTS.get(normalize_provider(provider), {}).get("model", "")


def build_auth_headers(provider: str, secret: str) -> dict[str, str]:
    defaults = PROVIDER_DEFAULTS.get(normalize_provider(provider), {})
    if defaults.get("auth_style") == "api-key":
        return {"api-key": secret}
    if defaults.get("auth_style") == "x-api-key":
        return {"x-api-key": secret}
    return {"Authorization": f"Bearer {secret}"}


def compact_probe_response(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = str(message.get("content") or "")
    return {
        "id": str(payload.get("id") or ""),
        "object": str(payload.get("object") or ""),
        "model": str(payload.get("model") or ""),
        "finish_reason": str(first.get("finish_reason") or ""),
        "content_preview": content[:120],
        "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
    }


def redact_error_text(text: str) -> str:
    return redact_credential_line(str(text or ""))[:1000]


def markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()
