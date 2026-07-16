from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model_credentials import DEFAULT_MODEL_CREDENTIAL_FILE


DEFAULT_SUB2API_BASE_URL = "http://174.138.75.136:8080/v1"
DEFAULT_SUB2API_MODEL = "gpt-4o-mini"
WORKBENCH_BACKEND_NAME = "bugteam"


@dataclass(frozen=True)
class WorkbenchModelConfig:
    base_url: str = DEFAULT_SUB2API_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_SUB2API_MODEL
    reasoning_effort: str = ""
    monthly_budget_usd: float = 0.0
    per_mission_budget_usd: float = 0.0
    auto_switch_quota_percent: float = 80.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.api_key.strip() and self.model.strip())

    @property
    def budget_guard_configured(self) -> bool:
        return self.monthly_budget_usd > 0 or self.per_mission_budget_usd > 0


def default_credentials_path(root: str | Path | None = None) -> Path:
    return Path(root or Path.cwd()) / DEFAULT_MODEL_CREDENTIAL_FILE


def load_workbench_model_config(path: str | Path | None = None) -> WorkbenchModelConfig:
    source = Path(path or default_credentials_path())
    if not source.exists():
        return WorkbenchModelConfig()
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        if not _line_is_workbench_backend(line):
            continue
        return WorkbenchModelConfig(
            base_url=_option(line, "base_url") or DEFAULT_SUB2API_BASE_URL,
            api_key=_token(line),
            model=_option(line, "model") or DEFAULT_SUB2API_MODEL,
            reasoning_effort=_option(line, "reasoning_effort"),
            monthly_budget_usd=_float_option(line, "monthly_budget_usd", 0.0),
            per_mission_budget_usd=_float_option(line, "per_mission_budget_usd", 0.0),
            auto_switch_quota_percent=_float_option(line, "auto_switch_quota_percent", 80.0),
        )
    return WorkbenchModelConfig()


def save_workbench_model_config(config: WorkbenchModelConfig, path: str | Path | None = None) -> Path:
    if not config.base_url.strip():
        raise ValueError("base_url 不能为空")
    if not config.api_key.strip():
        raise ValueError("api_key 不能为空")
    if not config.model.strip():
        raise ValueError("model 不能为空")
    if config.monthly_budget_usd < 0:
        raise ValueError("monthly_budget_usd 不能为负数")
    if config.per_mission_budget_usd < 0:
        raise ValueError("per_mission_budget_usd 不能为负数")
    if not 0 <= config.auto_switch_quota_percent <= 100:
        raise ValueError("auto_switch_quota_percent 必须在 0 到 100 之间")

    source = Path(path or default_credentials_path())
    source.parent.mkdir(parents=True, exist_ok=True)
    lines = source.read_text(encoding="utf-8-sig").splitlines() if source.exists() else []
    replacement = _format_config_line(config)
    replaced = False
    updated: list[str] = []
    for line in lines:
        if _line_is_workbench_backend(line):
            if not replaced:
                updated.append(replacement)
                replaced = True
            continue
        updated.append(line)
    if not replaced:
        updated.append(replacement)
    source.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")
    return source


def redacted_config_summary(config: WorkbenchModelConfig) -> str:
    key = config.api_key.strip()
    if not key:
        redacted = "未配置"
    elif len(key) <= 8:
        redacted = "***"
    else:
        redacted = f"{key[:4]}...{key[-4:]}"
    effort = config.reasoning_effort.strip() or "默认"
    budget = (
        f"monthly=${config.monthly_budget_usd:.2f} mission=${config.per_mission_budget_usd:.2f} "
        f"switch_at={config.auto_switch_quota_percent:.0f}%"
    )
    return f"base_url={config.base_url.strip() or '-'} model={config.model.strip() or '-'} effort={effort} {budget} api_key={redacted}"


def probe_workbench_model_config(config: WorkbenchModelConfig, *, timeout_seconds: int = 12) -> dict[str, Any]:
    if not config.configured:
        return {"ok": False, "error": "请先填写 base_url、api_key 和 model"}
    url = config.base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {config.api_key.strip()}"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", 0) or 0)
            return {"ok": 200 <= status < 300, "status": status, "url": url}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "url": url, "error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - UI probe should surface concise diagnostics.
        return {"ok": False, "url": url, "error": str(exc)}


def _line_is_workbench_backend(line: str) -> bool:
    return line.strip().lower().startswith(f"{WORKBENCH_BACKEND_NAME} ")


def _token(line: str) -> str:
    match = re.search(r"\bapi_key\s*[:=]\s*([^\s,;]+)", line, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"((?:sk|tp)-[A-Za-z0-9_\-]{20,})", line)
    return match.group(1) if match else ""


def _option(line: str, option: str) -> str:
    match = re.search(rf"\b{re.escape(option)}\s*[:=]\s*([^\s,;]+)", line, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _float_option(line: str, option: str, default: float) -> float:
    raw = _option(line, option)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _format_config_line(config: WorkbenchModelConfig) -> str:
    line = (
        f"{WORKBENCH_BACKEND_NAME} "
        f"api_key={config.api_key.strip()} "
        f"base_url={config.base_url.rstrip('/')} "
        f"model={config.model.strip()}"
        + (f" reasoning_effort={config.reasoning_effort.strip()}" if config.reasoning_effort.strip() else "")
    )
    if config.monthly_budget_usd > 0:
        line += f" monthly_budget_usd={config.monthly_budget_usd:.4f}".rstrip("0").rstrip(".")
    if config.per_mission_budget_usd > 0:
        line += f" per_mission_budget_usd={config.per_mission_budget_usd:.4f}".rstrip("0").rstrip(".")
    if config.auto_switch_quota_percent != 80.0:
        line += f" auto_switch_quota_percent={config.auto_switch_quota_percent:.2f}".rstrip("0").rstrip(".")
    return line
