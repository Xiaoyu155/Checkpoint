from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .model_credentials import (
    build_auth_headers,
    default_base_url_for_provider,
    default_endpoint_for_provider,
    load_provider_secret_from_env,
)


OPENAI_COMPATIBLE_PROVIDERS = {"openai", "bugteam", "xiaomimimo", "qwen", "kimi", "deepseek", "volcengine"}
SUPPORTED_COMPLETION_PROVIDERS = OPENAI_COMPATIBLE_PROVIDERS | {"anthropic", "gemini"}


@dataclass(frozen=True)
class LLMBackend:
    provider: str
    model_id: str
    supported: bool = True
    notes: str = ""


def resolve_llm_backend(model_id: str | None) -> LLMBackend:
    value = str(model_id or "").strip()
    if not value:
        return LLMBackend(provider="anthropic", model_id="claude-haiku-4-5-20251001")
    if ":" in value:
        provider, _, model = value.partition(":")
        provider = _normalize_provider(provider)
        model = model.strip() or value
        supported = provider in SUPPORTED_COMPLETION_PROVIDERS
        notes = "" if supported else "LLM adapter is not implemented yet."
        return LLMBackend(provider=provider, model_id=model, supported=supported, notes=notes)
    lower = value.lower()
    if lower.startswith(("gpt-", "o1", "o3")) or "openai" in lower:
        return LLMBackend(provider="openai", model_id=value, supported=True)
    if "gemini" in lower:
        return LLMBackend(provider="gemini", model_id=value, supported=True)
    if lower.startswith("claude-") or "anthropic" in lower:
        return LLMBackend(provider="anthropic", model_id=value, supported=True)
    return LLMBackend(provider="anthropic", model_id=value, supported=True)


def llm_backend_manifest() -> list[dict[str, str | bool]]:
    return [
        {
            "provider": "anthropic",
            "supported": True,
            "note": "Current default backend.",
        },
        {
            "provider": "openai",
            "supported": True,
            "note": "OpenAI-compatible chat-completion backend.",
        },
        {
            "provider": "gemini",
            "supported": True,
            "note": "Gemini generateContent backend.",
        },
        {
            "provider": "xiaomimimo",
            "supported": True,
            "note": "OpenAI-compatible provider family member.",
        },
        {
            "provider": "bugteam",
            "supported": True,
            "note": "OpenAI-compatible provider family member.",
        },
        {
            "provider": "qwen",
            "supported": True,
            "note": "OpenAI-compatible provider family member.",
        },
        {
            "provider": "kimi",
            "supported": True,
            "note": "OpenAI-compatible provider family member.",
        },
        {
            "provider": "deepseek",
            "supported": True,
            "note": "OpenAI-compatible provider family member.",
        },
        {
            "provider": "volcengine",
            "supported": True,
            "note": "OpenAI-compatible provider family member.",
        },
    ]


def run_llm_completion(
    *,
    backend: LLMBackend,
    system_prompt: str,
    prompt: str,
    max_tokens: int,
    api_key: str | None = None,
    base_url: str | None = None,
    endpoint: str | None = None,
    reasoning_effort: str | None = None,
    timeout_seconds: float = 30.0,
) -> str:
    provider = _normalize_provider(backend.provider)
    if not backend.supported:
        raise NotImplementedError(f"LLM backend '{provider}' is not implemented yet.")

    if provider == "anthropic" and api_key is None and base_url is None and endpoint is None:
        try:
            return _run_anthropic_sdk_completion(backend=backend, system_prompt=system_prompt, prompt=prompt, max_tokens=max_tokens)
        except ImportError:
            pass

    secret = str(api_key or load_provider_secret_from_env(provider) or "")
    if not secret:
        raise RuntimeError(f"Missing API key for LLM backend '{provider}'.")

    resolved_base_url = base_url or default_base_url_for_provider(provider, None)
    resolved_endpoint = endpoint or default_endpoint_for_provider(provider, None)
    if not resolved_base_url:
        raise RuntimeError(f"Missing base URL for LLM backend '{provider}'.")
    if not resolved_endpoint:
        raise RuntimeError(f"Missing endpoint for LLM backend '{provider}'.")

    request_url = resolved_base_url.rstrip("/") + "/" + resolved_endpoint.lstrip("/")
    payload = _completion_request_payload(
        provider,
        backend.model_id,
        system_prompt,
        prompt,
        max_tokens,
        reasoning_effort=reasoning_effort,
    )
    headers = {"Content-Type": "application/json", **build_auth_headers(provider, secret)}
    if provider == "anthropic":
        headers.setdefault("anthropic-version", "2023-06-01")

    request = urllib.request.Request(
        request_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
            response_payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(
            f"LLM backend '{provider}' returned HTTP {exc.code}: {body[:500] or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM backend '{provider}' request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LLM backend '{provider}' returned invalid JSON.") from exc

    return _extract_completion_text(response_payload, provider=provider)


def _run_anthropic_sdk_completion(
    *,
    backend: LLMBackend,
    system_prompt: str,
    prompt: str,
    max_tokens: int,
) -> str:
    import anthropic

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=backend.model_id,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}],
    )
    content = getattr(message, "content", [])
    text = _extract_text_from_node(content)
    if text:
        return text
    raise RuntimeError("Anthropic returned an empty response.")


def _completion_request_payload(
    provider: str,
    model_id: str,
    system_prompt: str,
    prompt: str,
    max_tokens: int,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    if provider == "gemini":
        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system_prompt.strip():
            payload["system_instruction"] = {"parts": [{"text": system_prompt}]}
        return payload
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    if system_prompt.strip():
        payload["messages"] = [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]
    effort = str(reasoning_effort or "").strip()
    if effort and provider in OPENAI_COMPATIBLE_PROVIDERS:
        payload["reasoning_effort"] = effort
    if provider == "anthropic":
        return {
            "model": model_id,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
        }
    return payload


def _extract_completion_text(payload: Any, *, provider: str) -> str:
    if not isinstance(payload, dict):
        raise RuntimeError(f"LLM backend '{provider}' returned an unexpected payload type.")
    if provider == "gemini":
        text = _extract_from_candidates(payload.get("candidates"))
        if text:
            return text
    if provider == "anthropic":
        text = _extract_text_from_node(payload.get("content"))
        if text:
            return text
    if isinstance(payload.get("output_text"), str) and str(payload.get("output_text")).strip():
        return str(payload["output_text"])
    if isinstance(payload.get("choices"), list):
        for choice in payload["choices"]:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict):
                text = _extract_text_from_node(message.get("content"))
                if text:
                    return text
            text = _extract_text_from_node(choice.get("content"))
            if text:
                return text
            text = str(choice.get("text") or "").strip()
            if text:
                return text
    text = _extract_text_from_node(payload.get("output"))
    if text:
        return text
    raise RuntimeError(f"LLM backend '{provider}' returned no text content.")


def _extract_from_candidates(candidates: Any) -> str:
    if not isinstance(candidates, list):
        return ""
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        text = _extract_text_from_node(candidate.get("content"))
        if text:
            return text
    return ""


def _extract_text_from_node(node: Any) -> str:
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, dict):
        for key in ("text", "output_text", "content"):
            text = _extract_text_from_node(node.get(key))
            if text:
                return text
        parts = node.get("parts")
        if isinstance(parts, list):
            text = _extract_text_from_node(parts)
            if text:
                return text
        if "value" in node:
            text = _extract_text_from_node(node.get("value"))
            if text:
                return text
        return ""
    if isinstance(node, list):
        for item in node:
            text = _extract_text_from_node(item)
            if text:
                return text
        return ""
    if node is None:
        return ""
    return str(node).strip()


def _normalize_provider(provider: str) -> str:
    value = provider.strip().lower()
    if value in {"claude", "anthropic"}:
        return "anthropic"
    if value in {"openai", "gpt"}:
        return "openai"
    if value in {"gemini", "google"}:
        return "gemini"
    return value or "anthropic"
