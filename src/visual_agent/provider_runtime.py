"""Reloadable provider tiers. Adapted from the requested Opus repair review.

Reading configuration never creates files or probes remote paid services.
Cooldowns belong to this server process, rather than one task's orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit
from .live_call_guard import live_socket_probe_blocked

_LOCK = threading.RLock()
_COOLDOWNS: dict[str, float] = {}


def config_dir() -> Path:
    return Path(os.environ.get("PACER_GATEWAY_CONFIG", str(Path.home() / ".pacer/model-gateway/config.json"))).expanduser().parent


def providers_path() -> Path:
    return Path(os.environ.get("PACER_PROVIDERS_FILE", str(config_dir() / "providers.json"))).expanduser()


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("configuration must be a JSON object")
        return data
    except (OSError, ValueError) as exc:
        # Do not echo invalid JSON: it might include credentials.
        raise ValueError(f"Cannot read provider configuration ({path.name}, {type(exc).__name__})") from None


def template() -> dict:
    return {
        "_help": "Enable the desired slots explicitly. Use api_key_env, or a local api_key. Status never prints keys. JSON does not support comments.",
        "tier1_free": {
            "antigravity_gemini38": {"enabled": True, "cli_fallback": True, "base_url_from_gateway_config": "upstream_url", "model": "gemini-3.8-flash-low", "protocol": "anthropic"},
            "workbuddy": {"enabled": True},
        },
        "tier2_domestic": [
            {"name": "deepseek", "enabled": False, "provider": "deepseek", "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"},
            {"name": "qwen", "enabled": False, "provider": "qwen", "model": "YOUR_MODEL", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key_env": "QWEN_API_KEY"},
        ],
        "tier3_premium_escalation": [
            {"name": "sub2api_primary", "enabled": False, "provider": "anthropic", "model": "claude-haiku-4-5-20251001", "base_url_env": "PACER_SUB2API_BASE_URL", "api_key_env": "PACER_PRIMARY_KEY"},
            {"name": "sub2api_backup", "enabled": False, "provider": "anthropic", "model": "claude-haiku-4-5-20251001", "base_url_env": "PACER_SUB2API_BASE_URL", "api_key_env": "PACER_BACKUP_KEY"},
        ],
        "tier4_architect_reviewer": {"gemini4": {"enabled": False, "provider": "gemini", "model": "gemini-4", "base_url_env": "PACER_GEMINI4_BASE_URL", "api_key_env": "PACER_GEMINI4_API_KEY"}},
    }


def ensure_template(directory: Path | None = None) -> Path:
    path = directory / "providers.json" if directory is not None else providers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Exclusive creation prevents concurrent starts from overwriting user edits.
        with path.open("x", encoding="utf-8") as stream:
            json.dump(template(), stream, ensure_ascii=False, indent=2)
        path.chmod(0o600)
    except FileExistsError:
        pass
    return path


def load_providers() -> dict:
    data = {**template(), **_read_json(providers_path())}
    for key in ("tier1_free", "tier4_architect", "tier4_architect_reviewer"):
        if key in data and (not isinstance(data[key], dict) or any(not isinstance(row, dict) for row in data[key].values())):
            raise ValueError(f"Invalid provider section: {key}")
    for key in ("tier2_domestic", "tier3_premium", "tier3_premium_escalation"):
        if key in data and (not isinstance(data[key], list) or any(not isinstance(row, dict) for row in data[key])):
            raise ValueError(f"Invalid provider section: {key}")
    return data


def tcp_reachable(url: str) -> bool:
    if live_socket_probe_blocked():
        return False
    try:
        parsed = urlsplit(url)
        with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)), timeout=0.12):
            return True
    except (OSError, ValueError):
        return False


def cooldown_remaining(node_id: str) -> int:
    with _LOCK:
        return max(0, math.ceil(_COOLDOWNS.get(node_id, 0) - time.monotonic()))


def note_failure(node_id: str, error: str) -> None:
    text = error.lower()
    seconds = 600 if any(x in text for x in ("401", "403", "unauthorized", "forbidden", "authentication", "invalid_api_key")) else 300 if any(x in text for x in ("429", "quota")) else 60
    with _LOCK:
        _COOLDOWNS[node_id] = max(_COOLDOWNS.get(node_id, 0), time.monotonic() + seconds)


def clear_failure(node_id: str) -> None:
    with _LOCK:
        _COOLDOWNS.pop(node_id, None)


@dataclass
class ProviderNode:
    node_id: str
    tier: int
    name: str
    provider: str
    model: str
    base_url: str
    endpoint: str
    enabled: bool
    api_key: str = field(default="", repr=False)
    blockers: list[str] = field(default_factory=list)
    reachable: bool = False
    cli_available: bool = False
    cli_fallback: bool = False

    def is_available(self) -> bool:
        return (
            self.enabled
            and not self.blockers
            and bool(self.api_key)
            and not cooldown_remaining(self.node_id)
            and (self.tier > 1 or self.reachable or (self.cli_available and self.cli_fallback))
        )

    def completion_config(self) -> dict:
        return dict(provider=self.provider, model=self.model, base_url=self.base_url, endpoint=self.endpoint,
                    _api_key=self.api_key, available=self.is_available(), blockers=self.blockers, node_id=self.node_id)


def _stable_node_id(provider: str, model: str, base_url: str, key: str, name: str = "") -> str:
    # Distinguish primary and backup keys on the exact same provider/model/URL.
    digest = hashlib.sha256(key.encode()).hexdigest()
    return hashlib.sha256(f"{provider}:{model}@{base_url}#{digest}:{name}".encode()).hexdigest()[:16]


def antigravity_node(data: dict | None = None, *, probe: bool = True) -> ProviderNode:
    from .gemini_bridge import find_gemini_executable
    data = data if data is not None else load_providers()
    row = data.get("tier1_free", {}).get("antigravity_gemini38", {})
    gateway = _read_json(Path(os.environ.get("PACER_GATEWAY_CONFIG", str(config_dir() / "config.json"))))
    url = os.environ.get("PACER_ANTIGRAVITY_URL") or row.get("base_url") or os.environ.get("PACER_FAST_API_BASE") or gateway.get("upstream_url") or "http://127.0.0.1:8090"
    key = os.environ.get("PACER_ANTIGRAVITY_KEY") or os.environ.get("PACER_FAST_API_KEY") or row.get("api_key") or os.environ.get(row.get("api_key_env", "")) or gateway.get("upstream_api_key") or "sk-local"
    blockers = []
    try:
        parsed = urlsplit(url)
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
        if not loopback or parsed.scheme not in {"http", "https"} or parsed.username or parsed.query or parsed.fragment:
            blockers.append("invalid_nonloopback_upstream")
        if parsed.port in {8091, int(gateway.get("port") or 8091)}:
            blockers.append("self_recursion_rejected")
    except ValueError:
        blockers.append("invalid_upstream_url")
    enabled = row.get("enabled", True) is True
    reachable = enabled and not blockers and probe and tcp_reachable(url)
    cli = enabled and not blockers and bool(find_gemini_executable())
    model = os.environ.get("PACER_GEMINI38_FALLBACK_MODEL") or row.get("model") or "gemini-3.8-flash-low"
    if not model.lower().startswith(("gemini-3.8", "gemini-3-8")):
        blockers.append("invalid_gemini38_model")
    protocol = row.get("protocol", "anthropic")
    cli_fallback = row.get("cli_fallback", True) is True
    return ProviderNode("gemini38", 1, "antigravity_gemini38", protocol, model, str(url), "/messages" if protocol == "anthropic" else "/chat/completions", enabled, str(key), blockers, reachable, cli, cli_fallback)


def _api_node(entry: dict, tier: int) -> ProviderNode:
    from .model_credentials import default_base_url_for_provider, default_endpoint_for_provider, load_provider_secret_from_env
    provider = str(entry.get("provider", ""))
    model = str(entry.get("model", ""))
    base = entry.get("base_url") or os.environ.get(entry.get("base_url_env", "")) or default_base_url_for_provider(provider, None)
    key = entry.get("api_key") or os.environ.get(entry.get("api_key_env", "")) or load_provider_secret_from_env(provider) or ""
    endpoint = entry.get("endpoint") or default_endpoint_for_provider(provider, None)
    from .llm_providers import OPENAI_COMPATIBLE_PROVIDERS
    if not endpoint and provider in OPENAI_COMPATIBLE_PROVIDERS:
        endpoint = "/chat/completions"
    if provider == "gemini":
        endpoint = entry.get("endpoint") or f"/models/{model}:generateContent"
    name = str(entry.get("name") or provider)
    blockers = ["missing_" + label for label, value in (("api_key", key), ("base_url", base), ("model", model), ("endpoint", endpoint)) if not value]
    from .llm_providers import resolve_llm_backend
    if not resolve_llm_backend(f"{provider}:{model}").supported:
        blockers.append("unsupported_provider")
    return ProviderNode(_stable_node_id(provider, model, str(base), str(key), name), tier, name, provider, model, str(base or ""), str(endpoint or ""), entry.get("enabled", False) is True, str(key), blockers)


def api_nodes(slot: str, data: dict | None = None) -> list[ProviderNode]:
    data = data if data is not None else load_providers()
    tier = 2 if slot == "domestic_api" else 3
    env_name = "PACER_DOMESTIC_API_POOL" if tier == 2 else "PACER_PREMIUM_API_POOL"
    raw = os.environ.get(env_name) or (os.environ.get("PACER_API_POOL") or os.environ.get("PACER_API_MODEL") if tier == 3 else None)
    if raw:
        from .llm_providers import resolve_api_pool
        configs = resolve_api_pool(entries=json.loads(raw) if raw.lstrip().startswith("[") else raw.split(","))
        return [_api_node({**c, "api_key": c.get("_api_key"), "enabled": True}, tier) for c in configs]
    entries = data.get("tier2_domestic", []) if tier == 2 else data.get("tier3_premium", data.get("tier3_premium_escalation", []))
    return [_api_node(e, tier) for e in entries]


def _is_vps_gateway(base_url: str) -> bool:
    """True when a resolved base URL points at the overseas Gemini 4 pool."""
    parsed = urlsplit(str(base_url or ""))
    host = (parsed.hostname or "").lower()
    return bool(parsed.scheme in {"http", "https"} and host and host != "generativelanguage.googleapis.com")


def architect_node(data: dict | None = None, *, probe: bool = True) -> ProviderNode:
    data = data if data is not None else load_providers()
    entry = dict(data.get("tier4_architect", data.get("tier4_architect_reviewer", {})).get("gemini4", {}))
    raw = _read_json(providers_path())
    explicit = raw.get("tier4_architect", raw.get("tier4_architect_reviewer", {})).get("gemini4", {})
    if os.environ.get("PACER_GEMINI4_API_KEY") and explicit.get("enabled") is not False:
        entry.update(enabled=True, api_key=os.environ["PACER_GEMINI4_API_KEY"],
                     base_url=os.environ.get("PACER_GEMINI4_BASE_URL") or entry.get("base_url"),
                     model=os.environ.get("PACER_GEMINI4_MODEL") or entry.get("model", "gemini-4"))
    node = _api_node(entry, 4)
    if probe and node.enabled and _is_vps_gateway(node.base_url):
        # The gateway fronts a pool of accounts. A configured gateway with an
        # empty pool (or an unreachable one) cannot serve a request, so the
        # planner and reviewer must fall back immediately instead of waiting
        # for the call to fail. This is the Phase 8 "zero-second" rule.
        from .gemini4_vps_client import is_gemini4_vps_pool_healthy
        if not is_gemini4_vps_pool_healthy():
            node.blockers.append("empty_account_pool")
    return node


class RoutingEntry(tuple):
    """(backend_name, family, timeout_seconds, cooldown_seconds, priority_reason)."""
    __slots__ = ()
    backend_name = property(lambda self: self[0])
    family = property(lambda self: self[1])
    timeout_seconds = property(lambda self: self[2])
    cooldown_seconds = property(lambda self: self[3])
    priority_reason = property(lambda self: self[4])


_ROUTE_TIMEOUTS = {"grok": 25.0, "gork": 25.0, "gemini38": 90.0, "gemini": 90.0,
                   "workbuddy": 90.0, "codex": 60.0, "domestic_api": 90.0, "premium_api": 90.0}
_DEFAULT_ROUTES = ("gemini38", "workbuddy", "domestic_api", "premium_api")


def resolve_coder_routes(*, preferred: str = "", cascade: str | None = None,
                         cooldowns: dict[str, float] | None = None) -> list[RoutingEntry]:
    """Resolve order once; an explicit cascade is authoritative, including an empty one."""
    from .task_review import backend_model_family
    if cascade is None:
        names = list(_DEFAULT_ROUTES)
        preferred = str(preferred or "").strip().lower()
        if preferred and preferred in names:
            names = [preferred, *(name for name in names if name != preferred)]
        reason = "caller_preference_then_default" if preferred in _DEFAULT_ROUTES else "default"
    else:
        names = [name.strip().lower() for name in cascade.split(",") if name.strip()]
        reason = "explicit_cascade"
    now = time.monotonic()
    try:
        codex_timeout = float(os.environ.get("PACER_CODEX_TIMEOUT_SECONDS", "60"))
        if not math.isfinite(codex_timeout) or codex_timeout <= 0:
            codex_timeout = 60.0
    except ValueError:
        codex_timeout = 60.0
    result = []
    for name in dict.fromkeys(names):
        remaining = max(0.0, float((cooldowns or {}).get(name, 0.0)) - now,
                        float(cooldown_remaining("backend:" + name)))
        timeout = codex_timeout if name == "codex" else _ROUTE_TIMEOUTS.get(name, 90.0)
        result.append(RoutingEntry((name, backend_model_family(name), timeout, remaining, reason)))
    return result


def cost_candidates(retries: int = 0, complexity: str = "L2") -> list[str]:
    return [entry.backend_name for entry in resolve_coder_routes(cascade=os.environ.get("PACER_CODER_CASCADE"))]


def status_rows() -> list[dict]:
    from .workbuddy_bridge import get_gateway_info
    data = load_providers()
    free = antigravity_node(data)
    wb_enabled = data.get("tier1_free", {}).get("workbuddy", {}).get("enabled", True) is True
    info = get_gateway_info() if wb_enabled else None
    wb_reachable = bool(info and tcp_reachable(f"http://127.0.0.1:{info.port}"))
    rows = []
    for node in [free, *api_nodes("domestic_api", data), *api_nodes("premium_api", data), architect_node(data)]:
        remaining = cooldown_remaining(node.node_id)
        state = (
            "disabled" if not node.enabled else
            "cooling" if remaining else
            "unavailable_empty_pool" if "empty_account_pool" in node.blockers else
            "unconfigured" if node.blockers else
            "tcp_reachable_unverified" if node.reachable else
            "cli_configured_unverified" if node.cli_available else
            "unreachable" if node.tier == 1 else
            "configured_unverified"
        )
        rows.append(dict(tier=node.tier, name=node.name, provider=node.provider, model=node.model, status=state, cooldown_seconds=remaining, blockers=node.blockers))
    rows.insert(1, dict(tier=1, name="workbuddy", provider="workbuddy", model="", status="disabled" if not wb_enabled else "cooling" if cooldown_remaining("backend:workbuddy") else "tcp_reachable_unverified" if wb_reachable else "unreachable", cooldown_seconds=cooldown_remaining("backend:workbuddy"), blockers=[]))
    return rows


def format_status_table() -> str:
    rows = status_rows()
    lines = ["Tier | Channel | Model | State | Cooldown seconds | Blockers", "--- | --- | --- | --- | --- | ---"]
    lines.extend(
        f"{r['tier']} | {r['name']} | {r['model']} | {r['status']} | {r['cooldown_seconds']} | {','.join(r['blockers'])}"
        for r in rows
    )
    return "\n".join(lines)


def invoke_architect(prompt: str, *, system_prompt: str = "", timeout_seconds: int = 30):
    from types import SimpleNamespace
    from .llm_providers import resolve_llm_backend, run_llm_completion
    node = architect_node()
    if not node.is_available():
        return SimpleNamespace(success=False, content="", error="Gemini 4 unconfigured, disabled, or cooling down")
    try:
        text = run_llm_completion(backend=resolve_llm_backend(f"{node.provider}:{node.model}"),
                                  system_prompt=system_prompt, prompt=prompt, max_tokens=4096,
                                  api_key=node.api_key, base_url=node.base_url,
                                  endpoint=node.endpoint, timeout_seconds=timeout_seconds)
        if not text.strip():
            raise ValueError("Empty architect response")
        return SimpleNamespace(success=True, content=text, error=None)
    except Exception as exc:
        from .security import redact_secret_text
        error = redact_secret_text(str(exc).replace(node.api_key, "[REDACTED]"))
        note_failure(node.node_id, error)
        return SimpleNamespace(success=False, content="", error=error)
