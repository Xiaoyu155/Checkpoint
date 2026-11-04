"""Local model configuration, observable inventory, and workflow selection."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import re
from typing import Any
from urllib.parse import urlsplit


def profiles_path() -> Path:
    return Path(os.environ.get("PACER_MODEL_PROFILES") or Path.home() / ".pacer/model-gateway/profiles.json")


def load_profiles() -> dict[str, Any]:
    defaults = {"workflow": "balanced", "providers": {"gemini4": {"enabled": True,
        "base_url": "https://direct.aiboxz.com/gemini4", "model": "gemini-4", "api_key": "",
        "allow_ssh_fallback": not profiles_path().exists()}}, "checks": {}}
    path = profiles_path()
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        defaults.update({k: v for k, v in loaded.items() if k in ("workflow", "checks")})
        defaults["providers"]["gemini4"].update(loaded.get("providers", {}).get("gemini4", {}))
    return defaults


def save_profiles(update: dict[str, Any]) -> dict[str, Any]:
    from .pacer_model import write_json
    current = load_profiles()
    mode = update.get("workflow", current["workflow"])
    if mode not in ("fast", "balanced", "gemini4"):
        raise ValueError("Unknown workflow")
    row = dict(current["providers"]["gemini4"])
    provided = update.get("providers", {}).get("gemini4", {})
    if not isinstance(provided, dict):
        raise ValueError("Gemini 4 configuration must be an object")
    for field in ("base_url", "model", "enabled"):
        if field in provided:
            row[field] = provided[field]
    if provided.get("api_key"):
        row["api_key"] = str(provided["api_key"]).strip()
    parsed = urlsplit(str(row["base_url"]).strip())
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("Remote model endpoints must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use a base URL without credentials, query or fragment")
    row["base_url"] = str(row["base_url"]).strip().rstrip("/")
    if not isinstance(row["model"], str) or not row["model"].strip():
        raise ValueError("Model ID is required")
    if not isinstance(row["enabled"], bool):
        raise ValueError("enabled must be a boolean")
    row["allow_ssh_fallback"] = False
    if row != current["providers"]["gemini4"]:
        current.setdefault("checks", {}).pop("gemini4", None)
    current["workflow"], current["providers"]["gemini4"] = mode, row
    write_json(profiles_path(), current)
    return public_profiles(current)


def public_profiles(config: dict[str, Any] | None = None) -> dict[str, Any]:
    import copy
    public = copy.deepcopy(config or load_profiles())
    for row in public["providers"].values():
        row["key_configured"] = bool(row.pop("api_key", ""))
    return public


def inventory() -> list[dict[str, Any]]:
    from .grok_bridge import is_grok_available, get_grok_model
    from .gemini_bridge import find_gemini_executable
    from .gemini4_vps_client import gemini4_settings
    rows = [
        {"id": "grok", "name": get_grok_model(), "status": "configured" if is_grok_available() else "unconfigured",
         "role": "快速实现、局部修改、普通对话"},
        {"id": "codex", "name": "Codex CLI", "status": "installed" if shutil.which("codex") else "unavailable",
         "role": "复杂实现、失败修复、跨模块重构"},
        {"id": "gemini4_vps", "name": gemini4_settings()["model"], "status": "configured" if gemini4_settings()["enabled"] else "disabled",
         "role": "网页 Gemini 4：复杂任务规划、独立审查、指定编码"},
        {"id": "gemini", "name": "Gemini CLI", "status": "installed" if find_gemini_executable() else "unavailable",
         "role": "Gemini 4 不可用时的规划与只读审查降级；禁止写码"},
        {"id": "workbuddy", "name": "WorkBuddy 桌面网关", "status": "not_probed",
         "role": "可选备用；模型名称与额度以客户端为准"},
        {"id": "local", "name": "本地 AST / pytest / 变异验证", "status": "available", "role": "索引与验收，不消耗模型生成额度"},
    ]
    path = Path.home() / ".codex/config.toml"
    if path.exists():
        try:
            import tomllib
            rows[1]["name"] = "Codex · " + str(tomllib.loads(path.read_text(encoding="utf-8")).get("model", "default"))
        except (ImportError, OSError, ValueError):
            pass
    return rows


def select_model(
    role: str,
    *,
    workbuddy_available: bool = True,
    grok_available: bool = True,
    repeated_failure: bool = False,
) -> str:
    """Pick a workflow post. Gemini 3.8 and Codex are not coding workers."""
    name = (role or "").lower()
    if name in {"planner", "reviewer", "diagnostician", "architect", "wish"}:
        return "gemini4_vps"
    if name == "coder":
        if workbuddy_available and not repeated_failure:
            return "workbuddy"
        if grok_available:
            return "grok"
        return "local"
    return "local"


def workflow(mode: str, complexity: str, prompt: str = "") -> dict[str, Any]:
    explicit = bool(re.search(r"(?:使用|调用|交给|让|用|\buse\b|\busing\b|\bvia\b)\s*(?:网页\s*)?gemini\s*-?\s*4", prompt, re.I))
    heavy = complexity == "L3" or mode == "gemini4" or explicit
    coder = "workbuddy"
    fallback = "grok"
    if heavy:
        return {"name": "Gemini 4 优先规划与审查", "coder": coder, "planner": "gemini4_vps",
                "planner_fallback": "gemini38", "reviewer": "gemini4_vps",
                "reviewer_fallback": "gemini38", "fallback": fallback}
    return {"name": "快速实现", "coder": coder, "planner": "local", "reviewer": "local", "fallback": fallback}
