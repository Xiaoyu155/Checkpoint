"""Gemini 4 Overseas VPS Gateway Client (Sub2API-Grade).

Connects to the 8-account Jio 18M Google Gemini 4 reverse proxy pool
on the remote VPS (192.255.206.89) via secure SSH channel / SOCKS5 proxy.

Responsibilities:
1. Wish Analysis & Architecture Blueprint Generation (许愿开发分析).
2. Deep Code & Acceptance Review (判别性审查与反假绿推演).
3. Pool Status & Health Monitoring.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from .live_call_guard import live_http_call_blocked

logger = logging.getLogger("visual_agent.gemini4_vps_client")

VPS_IP = "192.255.206.89"
VPS_PORT = 22
VPS_USER = "root"
KEY_PATH = r"C:/Users/xiaoyu/.ssh/vps-racknerd-192.255.206.89"
SOCKS_PROXY_HOST = "127.0.0.1"
SOCKS_PROXY_PORT = 10808
GATEWAY_URL = "http://127.0.0.1:8999"
GATEWAY_API_KEY = ""
DIRECT_GATEWAY_URLS = [
    "https://direct.aiboxz.com/gemini4",
    "https://vps.aiboxz.com/gemini4",
]


@dataclass
class Gemini4Response:
    success: bool
    content: str
    model: str = "gemini-4"
    tokens_used: int = 0
    error: Optional[str] = None


def is_gemini4_vps_available() -> bool:
    """Check if direct HTTPS gateway is configured or SSH key exists."""
    settings = gemini4_settings()
    return bool(settings["enabled"] and settings["base_url"] and settings["api_key"])


# --- Live account-pool state -------------------------------------------------
# A configured gateway is not a usable gateway: the reverse proxy fronts a pool
# of accounts, and an empty pool cannot serve a single request. The Phase 8
# spec requires the planner and reviewer to notice an empty pool up front and
# fall back immediately, instead of waiting for the request to fail.
_POOL_STATE: dict[str, Any] = {"at": 0.0, "value": None}
_POOL_STATE_TTL = 30.0


def _reset_pool_state_cache() -> None:
    """Drop the cached probe result (used by tests and after a config change)."""
    _POOL_STATE["at"] = 0.0
    _POOL_STATE["value"] = None


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _probe_pool(base: str, timeout: float) -> dict[str, Any] | None:
    """Read one gateway's /health payload, or None if it did not answer."""
    if live_http_call_blocked():
        return None
    try:
        req = urllib.request.Request(
            base + "/health",
            headers={"User-Agent": "Pacer-HealthCheck/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.getcode() != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict):
            return None
        return {
            "available_accounts": _as_int(data.get("available_accounts")),
            "pool_size": _as_int(data.get("pool_size")),
        }
    except Exception:
        return None


def gemini4_pool_state(timeout: float = 1.5) -> dict[str, Any]:
    """Probe the gateway's account pool.

    Returns a dict with:
        reachable         : the health endpoint answered
        available_accounts: accounts currently able to serve a request (None
                            if the gateway did not report the field)
        pool_size         : total accounts in the pool (None if unreported)
        checked_at        : monotonic timestamp of the probe

    Both gateway aliases are probed in parallel so the worst case is one
    timeout rather than one per URL, and the result is cached for
    ``_POOL_STATE_TTL`` seconds so a burst of planning/review calls does not
    hammer the gateway.
    """
    now = time.monotonic()
    cached = _POOL_STATE.get("value")
    if cached is not None and now - float(_POOL_STATE.get("at") or 0.0) < _POOL_STATE_TTL:
        return dict(cached)

    state: dict[str, Any] = {
        "reachable": False,
        "available_accounts": None,
        "pool_size": None,
        "checked_at": now,
    }
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=len(DIRECT_GATEWAY_URLS)) as pool:
            probed = list(pool.map(lambda base: _probe_pool(base, timeout), DIRECT_GATEWAY_URLS))
    except Exception:
        probed = []
    for result in probed:
        if result is not None:
            state["reachable"] = True
            state["available_accounts"] = result["available_accounts"]
            state["pool_size"] = result["pool_size"]
            break

    _POOL_STATE["at"] = time.monotonic()
    _POOL_STATE["value"] = dict(state)
    return state


def is_gemini4_vps_pool_healthy(timeout: float = 1.5) -> bool:
    """True only when the gateway is reachable AND has at least one account.

    An unreachable gateway or an empty pool both mean "no usable Gemini 4".
    """
    state = gemini4_pool_state(timeout=timeout)
    if not state.get("reachable"):
        return False
    active = state.get("available_accounts")
    if active is None:
        # The gateway answered but did not report the pool. Treat it as
        # unknown rather than healthy, but do not claim an empty pool either.
        return False
    return int(active) > 0


def gemini4_settings() -> dict[str, Any]:
    from .model_catalog import load_profiles
    row = load_profiles()["providers"]["gemini4"]
    return {**row, "base_url": os.environ.get("PACER_GEMINI4_BASE_URL") or row["base_url"],
            "api_key": os.environ.get("PACER_GEMINI4_API_KEY") or row.get("api_key") or GATEWAY_API_KEY,
            "model": os.environ.get("PACER_GEMINI4_MODEL") or row.get("model") or "gemini-4"}


def _invoke_via_direct_https(
    prompt: str,
    system_prompt: str,
    temperature: float,
    timeout_seconds: int,
) -> Optional[Gemini4Response]:
    """Attempt direct HTTPS invocation against Caddy reverse-proxy gateway."""
    if live_http_call_blocked():
        return Gemini4Response(False, "", error="Live model HTTP calls are disabled during pytest.")
    settings = gemini4_settings()
    if not settings["enabled"]:
        return Gemini4Response(False, "", error="Gemini 4 provider is disabled")
    payload = {
        "model": settings["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "stream": True,
    }
    data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    deadline = time.monotonic() + timeout_seconds
    urls = [settings["base_url"]] if settings["base_url"] else DIRECT_GATEWAY_URLS
    last_error = "Gemini 4 gateway did not return a response"
    for base_url in urls:
        base_url = base_url.rstrip("/")
        url = base_url + ("/chat/completions" if base_url.endswith("/v1") else "/v1/chat/completions")
        try:
            req = urllib.request.Request(
                url,
                data=data_bytes,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {settings['api_key']}",
                    "User-Agent": "Pacer-Gemini4-Client/1.0",
                },
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_error = "Gemini 4 gateway timed out before completing the request"
                break
            with urllib.request.urlopen(req, timeout=remaining) as resp:
                if resp.getcode() == 200:
                    content_type = str(resp.headers.get("Content-Type", "")).lower()
                    if "text/event-stream" in content_type:
                        parts, model, tokens, complete = [], settings["model"], 0, False
                        for raw_line in resp:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Gemini 4 stream timed out before completion")
                            line = raw_line.decode("utf-8").strip()
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                complete = True
                                break
                            if not data:
                                continue
                            event = json.loads(data)
                            if "error" in event:
                                detail = event["error"]
                                message = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
                                return Gemini4Response(False, "", error="Gemini 4 stream error: " + message.replace(settings["api_key"], "[redacted]"))
                            model = event.get("model", model)
                            tokens = event.get("usage", {}).get("total_tokens", tokens)
                            for choice in event.get("choices", []):
                                text = choice.get("delta", {}).get("content", "")
                                if text:
                                    parts.append(text)
                                if choice.get("finish_reason") == "stop":
                                    complete = True
                        content = "".join(parts)
                        if not complete or not content.strip():
                            return Gemini4Response(False, "", error="Gemini 4 stream ended without a complete non-empty response")
                        return Gemini4Response(True, content, model=model, tokens_used=tokens)
                    raw_data = json.loads(resp.read().decode("utf-8"))
                    if "error" in raw_data:
                        return Gemini4Response(
                            success=False,
                            content="",
                            error=str(raw_data["error"]),
                        )
                    choices = raw_data.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")
                        tokens = raw_data.get("usage", {}).get("total_tokens", 0)
                        return Gemini4Response(
                            success=bool(content.strip()),
                            content=content,
                            model=raw_data.get("model", "gemini-4"),
                            tokens_used=tokens,
                            error=None if content.strip() else "Gemini 4 returned an empty response",
                        )
                    last_error = "Gemini 4 gateway returned no choices"
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            try:
                body = json.loads(detail)
                detail = str(body.get("detail") or body.get("error") or exc.reason)
            except ValueError:
                detail = str(exc.reason)
            last_error = f"Gemini 4 gateway HTTP {exc.code}: {detail}"
        except Exception as exc:
            last_error = f"Gemini 4 gateway {type(exc).__name__}: {exc}"
            logger.debug("Direct HTTPS attempt on %s failed: %s, trying next...", url, exc)
            continue
    return Gemini4Response(False, "", error=last_error.replace(settings["api_key"], "[redacted]"))


def _ssh_fallback_is_mocked() -> bool:
    try:
        import paramiko
        import socks
        from unittest.mock import Mock
    except ImportError:
        return False
    return isinstance(socks.socksocket, Mock) and isinstance(paramiko.SSHClient, Mock)


def invoke_gemini4_vps(
    prompt: str,
    system_prompt: str = "You are Gemini 4 (4 Argon / Pro) architectural reviewer and wish analysis engine for Pacer.",
    temperature: float = 0.2,
    timeout_seconds: int = 120,
) -> Gemini4Response:
    """Invoke Gemini 4 on the overseas VPS gateway with HTTPS primary & SSH fallback."""
    # 1. Primary: Direct HTTPS through reverse proxy
    direct_res = _invoke_via_direct_https(
        prompt=prompt,
        system_prompt=system_prompt,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
    )
    if direct_res is not None and direct_res.success:
        return direct_res
    if live_http_call_blocked() and not _ssh_fallback_is_mocked():
        return direct_res or Gemini4Response(False, "", error="Live model calls are disabled during pytest.")
    if not gemini4_settings().get("allow_ssh_fallback", False):
        return direct_res or Gemini4Response(False, "", error="Configured Gemini 4 gateway did not return a valid response")

    # 2. Fallback: Secure SSH Tunnel over SOCKS5 proxy
    try:
        import socks
        import paramiko
    except ImportError as e:
        if direct_res is not None:
            return direct_res
        return Gemini4Response(
            success=False,
            content="",
            error=f"Direct HTTPS failed and missing SSH dependencies: {e}",
        )

    try:
        s = socks.socksocket()
        s.settimeout(min(timeout_seconds, 15))
        s.set_proxy(socks.SOCKS5, SOCKS_PROXY_HOST, SOCKS_PROXY_PORT)
        s.connect((VPS_IP, VPS_PORT))

        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        known = Path.home() / ".ssh" / "known_hosts"
        if known.is_file():
            ssh.load_host_keys(str(known))
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        ssh.connect(
            VPS_IP,
            port=VPS_PORT,
            username=VPS_USER,
            key_filename=KEY_PATH,
            sock=s,
            timeout=15,
        )

        payload = {
            "model": "gemini-4",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
        }

        # Safely execute curl on the remote VPS against internal gateway
        script = "import sys,json,urllib.request; d=json.load(sys.stdin); r=urllib.request.Request(d['url'],data=json.dumps(d['body']).encode(),headers=d['headers']); print(urllib.request.urlopen(r,timeout=d['timeout']).read().decode())"
        remote_cmd = "python3 -c " + shlex.quote(script)

        stdin, stdout, stderr = ssh.exec_command(remote_cmd, timeout=timeout_seconds)
        stdin.write(json.dumps({"url": GATEWAY_URL + "/v1/chat/completions", "body": payload,
                              "headers": {"Content-Type": "application/json", "Authorization": "Bearer " + gemini4_settings()["api_key"]},
                              "timeout": timeout_seconds}, ensure_ascii=False))
        stdin.flush()
        stdin.channel.shutdown_write()
        raw_output = stdout.read().decode("utf-8")
        ssh.close()

        if not raw_output.strip():
            err_msg = stderr.read().decode("utf-8")
            return Gemini4Response(success=False, content="", error=f"Empty VPS response: {err_msg}")

        data = json.loads(raw_output)
        if "error" in data:
            return Gemini4Response(
                success=False,
                content="",
                error=str(data["error"]),
            )

        choices = data.get("choices", [])
        if not choices:
            return Gemini4Response(
                success=False,
                content="",
                error=f"No choices in response: {raw_output[:200]}",
            )

        content = choices[0].get("message", {}).get("content", "")
        tokens = data.get("usage", {}).get("total_tokens", 0)
        return Gemini4Response(
            success=True,
            content=content,
            model=data.get("model", "gemini-4"),
            tokens_used=tokens,
        )

    except Exception as exc:
        logger.warning("Gemini 4 VPS SSH invocation failed: %s", exc)
        if direct_res is not None:
            return direct_res
        return Gemini4Response(success=False, content="", error=str(exc))
