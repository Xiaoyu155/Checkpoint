"""Grok / Sub2API Fast Gateway Bridge.

Integrates with xAI Grok models via Sub2API (or direct OpenAI-compatible endpoint).
Supports streaming output to eliminate HTTP read timeouts on large code generations.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any
import urllib.error
import urllib.request
from .live_call_guard import live_http_call_blocked


DEFAULT_SUB2API_BASE = "https://direct.aiboxz.com/v1"
DEFAULT_GROK_KEY = ""
DEFAULT_GROK_MODEL = "grok-latest"

# Sub2API gateways sit behind Cloudflare, which rejects Python's default
# User-Agent with "error code: 1010". The health probe and the real
# generation call must present the SAME client identity, otherwise the
# probe reports "online" while every real request is refused.
GROK_USER_AGENT = "Pacer/1.0"


def _read_pacer_credentials() -> dict[str, Any]:
    """Read credentials from ~/.pacer/credentials.json or local .pacer/credentials.json."""
    candidates = [
        Path(os.path.expanduser("~/.pacer/credentials.json")),
        Path(".pacer/credentials.json"),
    ]
    for p in candidates:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
    return {}


@dataclass
class GrokResult:
    success: bool
    response: str
    raw_output: str
    parsed_json: dict[str, Any] | list[Any] | None = None
    error: str | None = None
    elapsed_seconds: float = 0.0


def get_grok_api_base() -> str:
    creds = _read_pacer_credentials()
    base = (
        os.environ.get("PACER_GROK_API_BASE")
        or os.environ.get("SUB2API_API_BASE")
        or creds.get("sub2api_base")
        or creds.get("grok_api_base")
        or DEFAULT_SUB2API_BASE
    )
    base = str(base).rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def get_grok_api_key() -> str:
    creds = _read_pacer_credentials()
    return str(
        os.environ.get("PACER_GROK_API_KEY")
        or os.environ.get("SUB2API_API_KEY")
        or os.environ.get("GROK_API_KEY")
        or creds.get("grok_api_key")
        or creds.get("sub2api_key")
        or DEFAULT_GROK_KEY
    ).strip()


def get_grok_model() -> str:
    creds = _read_pacer_credentials()
    return str(
        os.environ.get("PACER_GROK_MODEL")
        or creds.get("grok_model")
        or DEFAULT_GROK_MODEL
    ).strip()


def is_grok_available() -> bool:
    """Check if Grok API key is configured."""
    return bool(get_grok_api_key())


def invoke_grok(
    prompt: str,
    system_prompt: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: float = 60.0,
    retries: int = 1,
) -> GrokResult:
    """Invoke Grok via Sub2API OpenAI-compatible endpoint with streaming response."""
    t0 = time.time()
    api_key = get_grok_api_key()
    if not api_key:
        return GrokResult(
            success=False,
            response="",
            raw_output="",
            error="No Grok API key configured (set PACER_GROK_API_KEY)",
            elapsed_seconds=0.0,
        )

    chosen_model = model or get_grok_model()
    url = f"{get_grok_api_base()}/chat/completions"

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": chosen_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": GROK_USER_AGENT,
        },
    )

    collected_chunks: list[str] = []
    reasoning_chunks: list[str] = []
    try:
        if live_http_call_blocked():
            raise RuntimeError("Live model HTTP calls are disabled during pytest.")
        stream_error: str | None = None
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                if hasattr(resp, "fp") and hasattr(resp.fp, "raw") and hasattr(resp.fp.raw, "_sock") and resp.fp.raw._sock:
                    resp.fp.raw._sock.settimeout(timeout)
            except Exception:
                pass
            while True:
                line = resp.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="ignore").strip()
                if line_str.startswith("event: error"):
                    stream_error = "SSE event: error"
                if line_str.startswith("data: "):
                    data_part = line_str[6:].strip()
                    if data_part == "[DONE]":
                        break
                    try:
                        chunk_obj = json.loads(data_part)
                        if "error" in chunk_obj:
                            err_info = chunk_obj["error"]
                            msg = err_info.get("message") or err_info.get("code") if isinstance(err_info, dict) else str(err_info)
                            stream_error = f"Grok API Error: {msg}"
                            break
                        delta = chunk_obj.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            collected_chunks.append(content)
                        reasoning = delta.get("reasoning_content", "")
                        if reasoning:
                            reasoning_chunks.append(reasoning)
                    except Exception:
                        pass
        full_text = "".join(collected_chunks)
        elapsed = time.time() - t0
        if not full_text.strip() and reasoning_chunks:
            return GrokResult(
                success=False,
                response="",
                raw_output="",
                error="empty_content",
                elapsed_seconds=elapsed,
            )

        if stream_error:
            if retries > 0 and ("concurrency" in stream_error.lower() or "rate limit" in stream_error.lower()):
                time.sleep(1.2)
                return invoke_grok(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    retries=retries - 1,
                )
            return GrokResult(
                success=False,
                response="",
                raw_output=full_text,
                error=stream_error,
                elapsed_seconds=elapsed,
            )

        if not full_text.strip():
            if retries > 0:
                time.sleep(1.2)
                return invoke_grok(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    retries=retries - 1,
                )
            return GrokResult(
                success=False,
                response="",
                raw_output="",
                error="Grok returned empty response (rate limit, concurrency limit, or connection reset)",
                elapsed_seconds=elapsed,
            )

        # Try parsing JSON if content looks like JSON
        parsed_json = None
        trimmed = full_text.strip()
        if (trimmed.startswith("{") and trimmed.endswith("}")) or (trimmed.startswith("[") and trimmed.endswith("]")):
            try:
                parsed_json = json.loads(trimmed)
            except Exception:
                pass

        return GrokResult(
            success=True,
            response=full_text,
            raw_output=full_text,
            parsed_json=parsed_json,
            elapsed_seconds=elapsed,
        )
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore") if hasattr(e, "read") else str(e)
        return GrokResult(
            success=False,
            response="",
            raw_output=err_msg,
            error=f"HTTP {e.code}: {err_msg}",
            elapsed_seconds=time.time() - t0,
        )
    except Exception as e:
        return GrokResult(
            success=False,
            response="",
            raw_output="",
            error=str(e),
            elapsed_seconds=time.time() - t0,
        )
