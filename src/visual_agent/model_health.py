"""Pacer Multi-Model Health Matrix & Concurrent Inspection Engine.

Provides non-blocking concurrent health probes, latency measurement,
and operational status reporting for all Pacer integrated AI engines:
1. OpenAI Codex Plus (CLI: codex.CMD)
2. xAI Grok (configured Sub2API endpoint)
3. Overseas VPS Gemini 4 (8-Account Jio Pool)
4. WorkBuddy Desktop Gateway (127.0.0.1:57756 / local DeepSeek)
5. Gemini Flash CLI (gemini.CMD)
"""

from __future__ import annotations

import concurrent.futures
import datetime
import os
import shutil
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any

from .gemini4_vps_client import is_gemini4_vps_available
from .gemini_bridge import find_gemini_executable, is_gemini_available
from .grok_bridge import GROK_USER_AGENT, is_grok_available
from .workbuddy_bridge import WorkBuddyGatewayClient
from .live_call_guard import live_http_call_blocked, live_socket_probe_blocked, model_cli_launch_blocked


@dataclass
class ModelHealthStatus:
    name: str
    status: str  # "online", "offline", "degraded"
    role: str
    latency_ms: float
    details: str
    error: str | None = None
    # Capability truthfulness: status must reflect only what was actually proven.
    installed: bool = False
    reachable: bool = False
    authenticated: bool | None = None  # None = not tested / unknown
    generation_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_codex_health(timeout: float = 2.0) -> ModelHealthStatus:
    """Check OpenAI Codex Plus CLI availability and probe latency."""
    t0 = time.perf_counter()
    codex_bin = shutil.which("codex")
    if not codex_bin and os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        npm_path = os.path.join(appdata, "npm", "codex.CMD")
        if os.path.exists(npm_path):
            codex_bin = npm_path

    if not codex_bin:
        return ModelHealthStatus(
            name="codex",
            status="offline",
            role="重型架构与高难编码 (L2/L3)",
            latency_ms=0.0,
            details="codex CLI 未安装或不在 PATH 中",
            error="Executable not found",
        )

    try:
        command = [codex_bin, "--version"]
        if model_cli_launch_blocked(command, subprocess.run):
            raise RuntimeError("Live Codex CLI probes are disabled during pytest.")
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        ver = proc.stdout.strip() or proc.stderr.strip() or "ready"

        # A non-zero exit code means the CLI itself refused to run the
        # command (authentication failure, expired login, missing config).
        # The binary being present proves nothing about usability, so this
        # must NOT be reported as merely "degraded / reachable".
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}")
            auth_hint = any(
                marker in detail.lower()
                for marker in (
                    "auth",
                    "login",
                    "log in",
                    "unauthorized",
                    "forbidden",
                    "credential",
                    "token",
                    "expired",
                    "401",
                    "403",
                )
            )
            return ModelHealthStatus(
                name="codex",
                status="offline",
                role="重型架构与高难编码 (L2/L3)",
                latency_ms=latency_ms,
                details=f"{codex_bin} (--version 退出码 {proc.returncode}: {detail})",
                error=f"codex --version exited with code {proc.returncode}",
                installed=True,
                reachable=False,
                authenticated=False if auth_hint else None,
                generation_verified=False,
            )

        # --version only proves the binary runs. It does NOT prove login,
        # quota, or generation. Report "degraded" (installed, unverified)
        # rather than "online" so routing/dashboards are not misled.
        return ModelHealthStatus(
            name="codex",
            status="degraded",
            role="重型架构与高难编码 (L2/L3)",
            latency_ms=latency_ms,
            details=f"{codex_bin} ({ver}) — installed; login/quota/generation unverified",
            installed=True,
            reachable=True,
            authenticated=None,
            generation_verified=False,
        )
    except subprocess.TimeoutExpired:
        return ModelHealthStatus(
            name="codex",
            status="degraded",
            role="重型架构与高难编码 (L2/L3)",
            latency_ms=round(timeout * 1000, 1),
            details=f"{codex_bin} (响应超时 >{timeout}s)",
            error="Timeout",
            installed=True,
            reachable=False,
        )
    except Exception as exc:
        return ModelHealthStatus(
            name="codex",
            status="offline",
            role="重型架构与高难编码 (L2/L3)",
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            details=str(codex_bin),
            error=str(exc),
            installed=True,
            reachable=False,
        )


def check_grok_health(timeout: float = 2.5) -> ModelHealthStatus:
    """Check xAI Grok (Sub2API) endpoint reachability and latency.

    Uses the SAME credential/base resolvers as the actual generation path
    (grok_bridge.get_grok_api_key / get_grok_api_base), so a probe that says
    "online" reflects the configuration the caller will actually use.
    """
    t0 = time.perf_counter()
    if not is_grok_available():
        return ModelHealthStatus(
            name="grok",
            status="offline",
            role="极速微补丁生成器 (Sub2API)",
            latency_ms=0.0,
            details="未配置 Grok / Sub2API 凭据",
            error="Missing API Key",
            installed=False,
            reachable=False,
        )

    from .grok_bridge import get_grok_api_base, get_grok_api_key

    endpoint = get_grok_api_base()
    api_key = get_grok_api_key()
    from urllib.parse import urlsplit
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ModelHealthStatus(name="grok", status="offline", role="Grok", latency_ms=0.0,
                                 details="Grok endpoint is not configured", error="Invalid API base URL",
                                 installed=False, reachable=False)
    try:
        import urllib.request

        req = urllib.request.Request(
            f"{endpoint}/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                # Identical to the header used by the real generation call,
                # so a passing probe reflects a call that will actually work.
                "User-Agent": GROK_USER_AGENT,
            },
        )
        if live_http_call_blocked():
            raise RuntimeError("Live provider health requests are disabled during pytest.")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = resp.getcode()
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        ok = status_code == 200
        if not ok:
            return ModelHealthStatus(
                name="grok",
                status="offline" if status_code in (401, 403) else "degraded",
                role="极速微补丁生成器 (Sub2API)",
                latency_ms=latency_ms,
                details=f"{endpoint} (HTTP {status_code})",
                error=f"HTTP {status_code}",
                installed=True,
                reachable=True,
                authenticated=False if status_code in (401, 403) else None,
                generation_verified=False,
            )
        return ModelHealthStatus(
            name="grok",
            status="degraded",
            role="极速微补丁生成器 (Sub2API)",
            latency_ms=latency_ms,
            details=f"{endpoint} (HTTP 200) — 端点可达；生成未验证",
            installed=True,
            reachable=True,
            authenticated=True,
            generation_verified=False,
        )
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        err_msg = str(exc)
        # urllib raises HTTPError for 4xx/5xx — read the real status code
        # instead of pattern-matching the message text.
        http_code = getattr(exc, "code", None)
        if http_code is None:
            http_code = next(
                (code for code in (401, 403, 404, 429, 500, 502, 503, 504) if str(code) in err_msg),
                None,
            )
        if http_code in (401, 403):
            return ModelHealthStatus(
                name="grok",
                status="offline",
                role="极速微补丁生成器 (Sub2API)",
                latency_ms=latency_ms,
                details=f"{endpoint} (HTTP {http_code})",
                error=str(http_code),
                installed=True,
                reachable=True,
                authenticated=False,
                generation_verified=False,
            )
        # Network / upstream failure — reachable config exists but the service
        # is not serving. Do not call this "online".
        return ModelHealthStatus(
            name="grok",
            status="degraded" if (http_code in (429, 500, 502, 503, 504) or "upstream" in err_msg.lower()) else "offline",
            role="极速微补丁生成器 (Sub2API)",
            latency_ms=latency_ms,
            details=endpoint,
            error=err_msg,
            installed=True,
            reachable=False,
            authenticated=None,
            generation_verified=False,
        )


def check_gemini4_vps_health(timeout: float = 2.5) -> ModelHealthStatus:
    """Check Overseas VPS Gemini 4 Jio Account Pool connectivity via HTTPS / socket."""
    t0 = time.perf_counter()
    from .gemini4_vps_client import gemini4_settings
    from urllib.parse import urlsplit
    base = str(gemini4_settings().get("base_url") or "").rstrip("/")
    parsed = urlsplit(base)
    vps_urls = [base + "/health"] if parsed.scheme in {"http", "https"} and parsed.hostname else []
    for url in vps_urls:
        try:
            import json
            import urllib.request

            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Pacer-HealthCheck/1.0"},
            )
            if live_http_call_blocked():
                raise RuntimeError("Live provider health requests are disabled during pytest.")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.getcode() == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
                    active = data.get("available_accounts", 1)
                    pool_sz = data.get("pool_size", 8)
                    try:
                        active_n = int(active)
                    except (TypeError, ValueError):
                        active_n = 0
                    # An empty pool cannot serve generation. Do not paint it green.
                    if active_n <= 0:
                        return ModelHealthStatus(
                            name="gemini4_vps",
                            status="degraded",
                            role="架构设计、深度审查与许愿分析",
                            latency_ms=latency_ms,
                            details=f"{url} (反代可达但账号池为空: 0/{pool_sz})",
                            error="No available accounts in pool",
                            installed=True,
                            reachable=True,
                            authenticated=False,
                            generation_verified=False,
                        )
                    return ModelHealthStatus(
                        name="gemini4_vps",
                        status="online",
                        role="架构设计、深度审查与许愿分析",
                        latency_ms=latency_ms,
                        details=f"{url} (8-Jio 反代池就绪, 活跃:{active_n}/{pool_sz})",
                        installed=True,
                        reachable=True,
                        authenticated=True,
                        generation_verified=False,
                    )
        except Exception:
            continue

    return ModelHealthStatus(
        name="gemini4_vps", status="offline", role="架构设计、深度审查与许愿分析",
        latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        details=base, error="Gateway unavailable or not configured", installed=False, reachable=False,
    )


def check_workbuddy_health(timeout: float = 1.0) -> ModelHealthStatus:
    """Check WorkBuddy desktop local gateway.

    A reachable gateway does NOT prove the underlying model can generate.
    The gateway can be up while create_run returns AuthenticationRequiredError.
    Report "degraded" unless generation is actually verified.
    """
    t0 = time.perf_counter()
    try:
        client = WorkBuddyGatewayClient()
        is_up = client.is_available
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        if is_up and client.info:
            port = client.info.port or 57756
            pid = client.info.pid or "active"
            health = client.check_health(timeout_seconds=min(timeout, 3.0))
            status_data = health.get("status") or {}
            inner = status_data.get("data") if isinstance(status_data.get("data"), dict) else status_data
            gateway_ok = bool(health.get("available")) and str(inner.get("status", "")).lower() == "ok"
            if not gateway_ok:
                return ModelHealthStatus(
                    name="workbuddy",
                    status="offline",
                    role="本地离线保底 (DeepSeek V3 / Kimi)",
                    latency_ms=latency_ms,
                    details=f"http://127.0.0.1:{port} (网关状态异常: {health.get('error') or 'unhealthy'})",
                    error=str(health.get("error") or "gateway unhealthy"),
                    installed=True,
                    reachable=False,
                )
            busy = bool(inner.get("busy"))
            return ModelHealthStatus(
                name="workbuddy",
                status="degraded",
                role="本地离线保底 (DeepSeek V3 / Kimi)",
                latency_ms=latency_ms,
                details=(
                    f"http://127.0.0.1:{port} (PID: {pid}) — 网关可达"
                    f"{'，忙' if busy else ''}；生成/鉴权未验证"
                ),
                installed=True,
                reachable=True,
                authenticated=None,
                generation_verified=False,
            )
        return ModelHealthStatus(
            name="workbuddy",
            status="offline",
            role="本地离线保底 (DeepSeek V3 / Kimi)",
            latency_ms=latency_ms,
            details="127.0.0.1 未检测到 WorkBuddy 桌面客户端监听",
            error="Gateway not running",
            installed=False,
            reachable=False,
        )
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return ModelHealthStatus(
            name="workbuddy",
            status="offline",
            role="本地离线保底 (DeepSeek V3 / Kimi)",
            latency_ms=latency_ms,
            details="WorkBuddy 检测异常",
            error=str(exc),
            installed=False,
            reachable=False,
        )


def check_gemini_cli_health(timeout: float = 2.0) -> ModelHealthStatus:
    """Check Gemini Flash CLI availability and executable."""
    t0 = time.perf_counter()
    gemini_bin = find_gemini_executable()
    if not gemini_bin:
        return ModelHealthStatus(
            name="gemini_cli",
            status="offline",
            role="终端会话协调与极速推断 (Flash)",
            latency_ms=0.0,
            details="gemini CLI 未安装",
            error="Executable not found",
        )

    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    # Finding the executable only proves it is installed, not that it can
    # authenticate or generate. Report "degraded" (installed, unverified).
    return ModelHealthStatus(
        name="gemini_cli",
        status="degraded",
        role="终端会话协调与极速推断 (Flash)",
        latency_ms=latency_ms,
        details=f"{gemini_bin} — installed; auth/generation unverified",
        installed=True,
        reachable=True,
        authenticated=None,
        generation_verified=False,
    )


def check_all_models_concurrently(timeout: float = 2.5) -> dict[str, Any]:
    """Concurrently check all 5 model backends within the timeout window.

    The `timeout` is propagated to every probe AND enforced as an overall
    deadline, so a single slow probe cannot make the aggregate exceed the
    caller's budget.
    """
    t_start = time.perf_counter()
    probes = {
        "codex": check_codex_health,
        "grok": check_grok_health,
        "gemini4_vps": check_gemini4_vps_health,
        "workbuddy": check_workbuddy_health,
        "gemini_cli": check_gemini_cli_health,
    }

    results: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fn, timeout): name
            for name, fn in probes.items()
        }
        deadline = t_start + timeout
        try:
            for fut in concurrent.futures.as_completed(
                futures, timeout=max(0.1, deadline - time.perf_counter())
            ):
                name = futures[fut]
                try:
                    status = fut.result()
                    results[name] = status.to_dict()
                except Exception as exc:
                    results[name] = ModelHealthStatus(
                        name=name,
                        status="offline",
                        role="Unknown",
                        latency_ms=0.0,
                        details=f"Probe failed: {exc}",
                        error=str(exc),
                    ).to_dict()
        except concurrent.futures.TimeoutError:
            # Deadline reached; probes still running are reported below.
            pass

    # Any probe that outlived the deadline is reported as a timeout rather
    # than silently dropped, so the caller can see the backend is not healthy.
    for fut, name in futures.items():
        if name in results:
            continue
        results[name] = ModelHealthStatus(
            name=name,
            status="degraded",
            role="Unknown",
            latency_ms=round(timeout * 1000, 1),
            details=f"Probe exceeded the {timeout}s inspection deadline",
            error="Timeout",
            installed=True,
            reachable=False,
            generation_verified=False,
        ).to_dict()

    total_latency_ms = round((time.perf_counter() - t_start) * 1000, 1)
    online_count = sum(1 for m in results.values() if m.get("status") == "online")
    degraded_count = sum(1 for m in results.values() if m.get("status") == "degraded")
    offline_count = sum(1 for m in results.values() if m.get("status") == "offline")
    generation_ready_count = sum(1 for m in results.values() if m.get("generation_verified"))

    return {
        "status": "ok",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "total_probed": len(results),
        "online_count": online_count,
        "degraded_count": degraded_count,
        "offline_count": offline_count,
        "generation_ready_count": generation_ready_count,
        "total_latency_ms": total_latency_ms,
        "models": results,
    }
