"""Data aggregation and caching layer for the dashboard.

Provides TTL-based caching for expensive operations like agents_doctor()
and full dashboard data assembly, eliminating redundant subprocess calls
and disk I/O on every frontend poll.
"""

from __future__ import annotations

import json
import subprocess
import time
import threading
from pathlib import Path
from typing import Any

from ..agent_capabilities import agents_doctor
from ..chief_plans_store import list_plans, load_plan, load_verification, load_worker_records, plan_dir
from ..commercial_config import CommercialConfig, load_commercial_config
from ..chief_queue import list_mission_queue_items
from ..mission_progress import build_mission_progress
from ..missions import list_missions
from ..mimo_efficiency import compute_mimo_efficiency
from ..notifications import load_notification_config
from ..programs import list_programs
from ..pacer_support import build_pacer_support_snapshot
from ..subscription_quota import load_quota_snapshot, quota_status, quota_summary
from ..user_profile import load_user_profile
from ..workbench_board import attach_board_fields
from ..workbench_model_config import load_workbench_model_config


# ---------------------------------------------------------------------------
# Generic TTL cache
# ---------------------------------------------------------------------------

_SENTINEL = object()


class _TTLCache:
    """Thread-safe, TTL-based cache for any value."""

    def __init__(self, ttl: float = 60.0):
        self._ttl = ttl
        self._ts: float = 0.0
        self._data: Any = _SENTINEL
        self._lock = threading.Lock()

    def is_valid(self) -> bool:
        with self._lock:
            return self._data is not _SENTINEL and (time.monotonic() - self._ts) < self._ttl

    def get(self) -> Any:
        """Return cached data. Caller must check is_valid() first."""
        with self._lock:
            return None if self._data is _SENTINEL else self._data

    def set(self, data: Any) -> None:
        with self._lock:
            self._data = data
            self._ts = time.monotonic()

    def invalidate(self) -> None:
        with self._lock:
            self._data = _SENTINEL
            self._ts = 0.0


# ---------------------------------------------------------------------------
# Cached agents_doctor — eliminates subprocess calls on every poll
# ---------------------------------------------------------------------------

_agents_cache = _TTLCache(ttl=60.0)


def get_agents_cached() -> list[dict[str, Any]]:
    """Return agents_doctor() results, cached for 60 seconds."""
    if _agents_cache.is_valid():
        return _agents_cache.get()  # type: ignore[return-value]
    result = agents_doctor()
    _agents_cache.set(result)
    return result


# ---------------------------------------------------------------------------
# Cached backend token reads — eliminates repeated file I/O
# ---------------------------------------------------------------------------

_token_cache: dict[str, _TTLCache] = {}
_token_lock = threading.Lock()

TOKEN_TTL = 300.0  # 5 minutes


def get_backend_token_cached(name: str, *, load_fn: Any = None) -> str | None:
    """Return a backend token with 5-minute caching."""
    with _token_lock:
        if name not in _token_cache:
            _token_cache[name] = _TTLCache(ttl=TOKEN_TTL)
        cache = _token_cache[name]

    if cache.is_valid():
        return cache.get()  # type: ignore[return-value]

    token = load_fn(name) if load_fn else None
    cache.set(token)
    return token


# ---------------------------------------------------------------------------
# Cached dashboard data — eliminates redundant disk I/O on rapid polls
# ---------------------------------------------------------------------------

_data_caches: dict[str, _TTLCache] = {}
_data_cache_lock = threading.Lock()


def invalidate_dashboard_data_cache(root: Path | str | None = None) -> None:
    """Drop cached dashboard data for one workspace or for all workspaces."""
    with _data_cache_lock:
        if root is None:
            _data_caches.clear()
            return
        key = str(Path(root).expanduser().resolve())
        _data_caches.pop(key, None)


def build_dashboard_data_cached(root: Path) -> dict[str, Any]:
    """Build dashboard data with 2-second TTL caching."""
    cache_key = str(Path(root).expanduser().resolve())
    with _data_cache_lock:
        cache = _data_caches.setdefault(cache_key, _TTLCache(ttl=2.0))

    if cache.is_valid():
        return cache.get()  # type: ignore[return-value]

    data = _build_dashboard_data_uncached(root)
    cache.set(data)
    return data


def _build_dashboard_data_uncached(root: Path) -> dict[str, Any]:
    """Original dashboard data assembly (uncached, called by cache layer)."""
    missions = attach_board_fields(root, list_missions(root))
    for mission in missions:
        if mission.get("board_column") == "pending_merge":
            mission["board_column"] = "in_review"
        mission["efficiency"] = _mission_efficiency(root, mission)
        mission["pacer_evidence"] = _mission_pacer_evidence(root, mission)
        mission["verification_mode"] = str(mission["pacer_evidence"].get("verification_mode") or mission.get("verification_mode") or "")
        for key in ("activity", "activity_label", "activity_command", "activity_elapsed_seconds"):
            mission[key] = mission["pacer_evidence"].get(key)
    plans = list_plans(root)

    try:
        raw_queue = list_mission_queue_items(root).get("entries", [])
    except Exception:
        raw_queue = []

    mission_map = {str(m.get("mission_id") or ""): m for m in missions if m.get("mission_id")}
    queue: list[dict[str, Any]] = []
    for item in raw_queue:
        mid = str(item.get("mission_id") or "")
        enriched = dict(item)
        m = mission_map.get(mid) or {}
        enriched["objective"] = str(m.get("objective") or m.get("goal") or "")
        queue.append(enriched)

    try:
        programs = list_programs(root)
    except Exception:
        programs = []

    agents = get_agents_cached()
    subscription_snapshot = load_quota_snapshot()
    subscription_summary = quota_summary(subscription_snapshot)
    commercial_config = load_commercial_config()
    status = _read_status(root)
    worker = worker_status(root)
    value = _build_value(root, missions, plans)
    pacer_support = build_pacer_support_snapshot(root)

    counts: dict[str, int] = {}
    for mission in missions:
        key = mission.get("status") or "unknown"
        counts[key] = counts.get(key, 0) + 1

    installed_agents = [str(a.get("agent")) for a in agents if isinstance(a, dict) and a.get("installed")]

    try:
        from ..agent_backends import resolve_backend_by_name
        bugteam_available = resolve_backend_by_name("bugteam") is not None
        mimo_available = resolve_backend_by_name("mimo") is not None
    except Exception:
        bugteam_available = False
        mimo_available = False

    return {
        "product": "Pacer",
        "orchestrator": "Pacer",
        "bugteam_available": bugteam_available,
        "mimo_available": mimo_available,
        "engine": "Checkpoint",
        "workspace_root": str(root),
        "repo_root": str(pacer_support.get("repo_root") or root.parent),
        "status": status,
        "value": value,
        "pacer_support": pacer_support,
        "subscription_quota": {
            "snapshot": subscription_snapshot,
            "status": quota_status(subscription_snapshot),
            "summary": subscription_summary,
        },
        "agents": agents,
        "installed_agents": installed_agents,
        "user_profile": load_user_profile().to_public_dict(),
        "commercial_config": commercial_config.to_dict(redact=True),
        "missions": missions,
        "mission_counts": counts,
        "plans": plans,
        "queue": queue,
        "programs": programs,
        "launches": _launch_snapshot(root),
        "work_traces": _build_work_traces(root, missions=missions, plans=plans, queue=queue, programs=programs),
        "worker": worker,
        "core_readiness": _build_core_readiness(
            root,
            status=status,
            agents=agents,
            installed_agents=installed_agents,
            missions=missions,
            queue=queue,
            programs=programs,
            subscription_snapshot=subscription_snapshot,
            subscription_summary=subscription_summary,
            value=value,
            worker=worker,
            pacer_support=pacer_support,
        ),
        "promotion_readiness": _build_promotion_readiness(
            root,
            status=status,
            agents=agents,
            installed_agents=installed_agents,
            missions=missions,
            subscription_snapshot=subscription_snapshot,
            subscription_summary=subscription_summary,
            commercial_config=commercial_config,
            worker=worker,
        ),
    }


def _build_core_readiness(
    root: Path,
    *,
    status: dict[str, Any],
    agents: list[dict[str, Any]],
    installed_agents: list[str],
    missions: list[dict[str, Any]],
    queue: list[dict[str, Any]],
    programs: list[dict[str, Any]],
    subscription_snapshot: dict[str, Any] | None,
    subscription_summary: dict[str, Any],
    value: dict[str, Any],
    worker: dict[str, Any],
    pacer_support: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(check_id: str, label: str, state: str, detail: str, action: str = "") -> None:
        checks.append(
            {
                "id": check_id,
                "label": label,
                "status": state,
                "detail": detail,
                "action": action,
            }
        )

    if root.exists() and (root / "missions").exists():
        add("workspace", "本地工作空间", "success", str(root))
    else:
        add("workspace", "本地工作空间", "failed", "工作空间未初始化", "初始化 .agent-workspace。")

    if installed_agents:
        add("agents", "编码 Agent", "success", "已发现 " + "、".join(installed_agents[:4]))
        add("intake", "需求收口", "success", "可用本地规则和已安装 Agent 把自然语言收口成任务合同")
    else:
        known = [str(item.get("agent")) for item in agents if isinstance(item, dict) and item.get("agent")]
        detail = "未发现可用编码 Agent"
        if known:
            detail += "；已知配置：" + "、".join(known[:4])
        add("agents", "编码 Agent", "failed", detail, "安装并登录 Codex、Claude Code 或 Gemini。")
        add("intake", "需求收口", "warning", "只能使用本地规则追问，无法调用用户已订阅的编码 Agent", "先让至少一个编码 Agent 可用。")

    codex_account = pacer_support.get("account") if isinstance(pacer_support.get("account"), dict) else {}
    if codex_account.get("authenticated"):
        method = str(codex_account.get("auth_method") or "codex_login")
        add("codex_account", "Codex 运行身份", "success", f"已认证，方式：{method}")
    elif codex_account.get("installed"):
        add("codex_account", "Codex 运行身份", "warning", "已安装但未确认登录", "运行 pacer account login，然后用 pacer account status 复核。")
    else:
        add("codex_account", "Codex 运行身份", "failed", "未发现 Codex CLI", "安装 Codex CLI 并登录。")

    q_len = len(queue)
    if worker.get("running") and q_len:
        add("worker", "托管 Worker", "success", f"Worker 正在运行，队列中 {q_len} 个任务")
    elif worker.get("running"):
        add("worker", "托管 Worker", "success", f"Worker 正在运行并待命，pid={worker.get('pid')}")
    elif q_len:
        add("worker", "托管 Worker", "warning", f"队列中有 {q_len} 个任务，但 Worker 未运行", "点击启动 Worker。")
    else:
        add("worker", "托管 Worker", "warning", "Worker 未运行；可以创建任务，但不会自动消费队列", "推广演示或托管开发前启动 Worker。")

    verified = [m for m in missions if str(m.get("status") or "") == "verified"]
    evidenced = [
        m for m in missions
        if isinstance(m.get("pacer_evidence"), dict)
        and (m["pacer_evidence"].get("worker_status") or m["pacer_evidence"].get("verification_verdict"))
    ]
    if verified and evidenced:
        add("closed_loop", "开发-验收闭环", "success", f"{len(verified)} 个已验收任务，{len(evidenced)} 个带 Worker/验收证据")
    elif verified:
        add("closed_loop", "开发-验收闭环", "warning", f"{len(verified)} 个已验收任务，但证据链还不完整", "再跑一次带 Worker 记录和验收命令的真实样例。")
    else:
        add("closed_loop", "开发-验收闭环", "warning", "还没有可证明闭环的已验收任务", "准备一个小任务，从收口、开发、验收跑完整。")

    if str(status.get("state") or "").upper() == "PASSING":
        add("acceptance", "本地验收状态", "success", "当前状态文件显示 PASSING")
    else:
        add("acceptance", "本地验收状态", "warning", f"当前状态：{status.get('state') or 'unknown'}", "跑 dashboard/browser 测试并更新状态。")

    windows = list(subscription_summary.get("windows") or [])
    max_used = float(subscription_summary.get("max_used_percentage") or 0.0)
    provider_count = int(subscription_summary.get("provider_count") or 0)
    snapshot_age = _freshest_quota_age_minutes(subscription_snapshot)
    if not windows:
        add("quota", "订阅额度感知", "warning", "还没有 Codex / Claude 额度窗口数据", "配置 Claude statusLine 或 Codex 状态命令。")
    elif snapshot_age is not None and snapshot_age > 360:
        add("quota", "订阅额度感知", "warning", f"已采集 {provider_count} 个来源，但最新数据约 {snapshot_age / 60.0:.1f} 小时前", "刷新额度快照。")
    else:
        add("quota", "订阅额度感知", "success", f"已采集 {provider_count} 个来源，最高使用 {max_used:.0f}%")

    model_config = load_workbench_model_config()
    if model_config.configured and model_config.budget_guard_configured:
        add("relay_budget", "额度耗尽兜底", "success", f"中转站和预算护栏已配置，{model_config.auto_switch_quota_percent:.0f}% 后可切换")
    elif model_config.configured:
        add("relay_budget", "额度耗尽兜底", "warning", "中转站可用，但缺少月预算或单任务上限", "设置月预算和单任务上限。")
    else:
        add("relay_budget", "额度耗尽兜底", "warning", "未配置中转站，订阅额度耗尽后不能自动续跑", "配置 OpenAI-compatible 中转站。")

    me = value.get("mimo_efficiency") if isinstance(value.get("mimo_efficiency"), dict) else {}
    saved_usd = float(me.get("saved_usd") or 0.0)
    saved_minutes = float(me.get("saved_minutes") or 0.0)
    saved_quota = float(me.get("saved_quota_percent") or 0.0)
    if saved_usd > 0 or saved_minutes > 0 or saved_quota > 0:
        add("value_metrics", "节省可见", "success", f"已记录节省 ${saved_usd:.2f} / {saved_minutes:.1f} 分钟 / 套餐额度 {saved_quota:.1f}%")
    else:
        add("value_metrics", "节省可见", "warning", "暂无真实节省样本；指标位置已就绪", "跑 1-2 个带 Worker 记录的真实任务。")

    if programs:
        add("multi_project", "多项目托管", "success", f"已发现 {len(programs)} 个项目托管计划")
    else:
        add("multi_project", "多项目托管", "warning", "当前没有项目托管计划样本", "用开发计划导入或创建多项目队列样本。")

    weights = {"success": 1.0, "warning": 0.5, "failed": 0.0}
    score = round(sum(weights.get(str(item["status"]), 0.0) for item in checks) / max(len(checks), 1) * 100)
    failed = sum(1 for item in checks if item["status"] == "failed")
    warnings = sum(1 for item in checks if item["status"] == "warning")
    if failed:
        level = "blocked"
        headline = "核心闭环有阻断项"
    elif score >= 85:
        level = "usable"
        headline = "核心托管开发可用"
    else:
        level = "needs_evidence"
        headline = "核心闭环还需要样本或配置"
    actions: list[str] = []
    seen: set[str] = set()
    for item in checks:
        action = str(item.get("action") or "").strip()
        if action and action not in seen:
            seen.add(action)
            actions.append(action)
    return {
        "schema_version": 1,
        "level": level,
        "score": score,
        "headline": headline,
        "failed": failed,
        "warnings": warnings,
        "checks": checks,
        "operator_actions": actions,
    }


def _build_promotion_readiness(
    root: Path,
    *,
    status: dict[str, Any],
    agents: list[dict[str, Any]],
    installed_agents: list[str],
    missions: list[dict[str, Any]],
    subscription_snapshot: dict[str, Any] | None,
    subscription_summary: dict[str, Any],
    commercial_config: CommercialConfig,
    worker: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(check_id: str, label: str, state: str, detail: str, user_required: str = "") -> None:
        checks.append(
            {
                "id": check_id,
                "label": label,
                "status": state,
                "detail": detail,
                "user_required": user_required,
            }
        )

    profile = load_user_profile()
    if profile.configured:
        add("profile", "邮箱身份", "success", f"已保存 {profile.email}")
    else:
        add("profile", "邮箱身份", "warning", "尚未保存本地联系邮箱", "你的常用邮箱，用于登录身份和通知收件人。")

    notification = load_notification_config()
    if notification is not None:
        add("notification", "邮件通知", "success", f"已配置通知到 {notification.recipient}")
    else:
        add("notification", "邮件通知", "warning", "未配置 SMTP，任务完成/额度耗尽时不能主动提醒", "SMTP 服务器、端口、用户名、授权码、发件人和收件人。")

    if commercial_config.auth_provider == "supabase" and commercial_config.supabase_url and commercial_config.supabase_anon_key:
        add("auth_supabase", "Supabase Auth", "success", f"已配置 {commercial_config.supabase_url}")
    else:
        add("auth_supabase", "Supabase Auth", "warning", "未配置 Supabase 项目登录参数", "Supabase Project URL 和 anon public key。")

    if commercial_config.google_oauth_configured:
        detail = "已确认 Google OAuth 通过 Supabase 登录"
        if commercial_config.google_client_id:
            detail += f"，client={commercial_config.google_client_id}"
        add("google_oauth", "Google OAuth", "success", detail)
    else:
        add("google_oauth", "Google OAuth", "warning", "未确认 Supabase 的 Google 登录 Provider", "在 Supabase Auth 里开启 Google Provider，并填入 Google OAuth Client ID/Secret。")

    if commercial_config.billing_configured:
        add("stripe_billing", "Stripe Billing", "success", f"已配置订阅价格 {commercial_config.stripe_price_id}")
    else:
        add("stripe_billing", "Stripe Billing", "warning", "未配置 Stripe 订阅收费参数", "Stripe publishable key、secret key、webhook signing secret、recurring Price ID。")

    if commercial_config.portal_configured:
        add("stripe_portal", "Stripe Customer Portal", "success", "已配置客户自助账单入口")
    else:
        add("stripe_portal", "Stripe Customer Portal", "warning", "未配置客户自助账单入口", "Stripe Customer Portal 配置或可打开的 portal 链接。")

    if commercial_config.usage_meter_configured:
        add("stripe_usage_meter", "Stripe Usage Meter", "success", f"已预留计量事件：{commercial_config.stripe_usage_meter_event}")
    else:
        add("stripe_usage_meter", "Stripe Usage Meter", "warning", "未配置用量计量事件，当前可先按订阅收费")

    model_config = load_workbench_model_config()
    if model_config.configured:
        add("relay", "中转站接口", "success", f"已配置 {model_config.model} @ {model_config.base_url}")
        if model_config.budget_guard_configured:
            add(
                "relay_budget",
                "中转站预算护栏",
                "success",
                f"月预算 ${model_config.monthly_budget_usd:.2f} / 单任务 ${model_config.per_mission_budget_usd:.2f} / {model_config.auto_switch_quota_percent:.0f}% 额度自动切换",
            )
        else:
            add("relay_budget", "中转站预算护栏", "warning", "中转站可用，但还没有配置月预算或单任务上限", "可接受的月预算和单任务最高花费。")
    else:
        add("relay", "中转站接口", "warning", "未配置可续跑的 OpenAI-compatible 中转站", "中转站 Base URL、API Key、模型名。")
        add("relay_budget", "中转站预算护栏", "warning", "未配置付费续跑预算", "月预算、单任务上限、额度阈值。")

    windows = list(subscription_summary.get("windows") or [])
    max_used = float(subscription_summary.get("max_used_percentage") or 0.0)
    provider_count = int(subscription_summary.get("provider_count") or 0)
    snapshot_age = _freshest_quota_age_minutes(subscription_snapshot)
    if not windows:
        add(
            "quota",
            "订阅额度采集",
            "warning",
            "还没有 Codex / Claude 额度窗口数据",
            "Claude statusLine 配置，或 PACER_CODEX_STATUS_COMMAND 命令。"
        )
    elif snapshot_age is not None and snapshot_age > 360:
        add(
            "quota",
            "订阅额度采集",
            "warning",
            f"已采集 {provider_count} 个来源，但最新数据约 {snapshot_age / 60.0:.1f} 小时前",
            "刷新 Codex/Claude 额度快照。"
        )
    elif max_used >= 95:
        add("quota", "订阅额度采集", "warning", f"额度已用 {max_used:.0f}%，应优先走中转站或暂停低优先级任务")
    else:
        add("quota", "订阅额度采集", "success", f"已采集 {provider_count} 个来源，最高使用 {max_used:.0f}%")

    if installed_agents:
        add("agents", "编码 Agent", "success", "已发现 " + "、".join(installed_agents[:4]))
    else:
        add("agents", "编码 Agent", "failed", "没有发现可用编码 Agent", "至少安装并登录 Codex、Claude Code 或 Gemini 其中一个。")

    if worker.get("running"):
        add("worker", "托管 Worker", "success", f"Worker 正在运行，pid={worker.get('pid')}")
    else:
        add("worker", "托管 Worker", "warning", "Worker 未运行，无法自动消费托管队列", "推广演示前在工作台启动 Worker。")

    verified = [m for m in missions if str(m.get("status") or "") == "verified"]
    evidenced = [
        m for m in missions
        if isinstance(m.get("pacer_evidence"), dict)
        and (m["pacer_evidence"].get("worker_status") or m["pacer_evidence"].get("verification_verdict"))
    ]
    if verified and evidenced:
        add("evidence", "验收证据", "success", f"{len(verified)} 个已验收任务，{len(evidenced)} 个带 Worker/验收证据")
    elif verified:
        add("evidence", "验收证据", "warning", f"{len(verified)} 个已验收任务，但证据字段还不完整")
    else:
        add("evidence", "验收证据", "warning", "还没有已验收任务作为推广样例", "准备 1-2 个真实成功案例。")

    if str(status.get("state") or "").upper() == "PASSING":
        add("acceptance", "本地验收状态", "success", "当前状态文件显示 PASSING")
    else:
        add("acceptance", "本地验收状态", "warning", f"当前状态：{status.get('state') or 'unknown'}", "推广前跑一次 dashboard/browser 测试并更新状态。")

    if root.exists() and (root / "missions").exists():
        add("workspace", "本地工作空间", "success", str(root))
    else:
        add("workspace", "本地工作空间", "failed", "工作空间不完整", "初始化 .agent-workspace。")

    weights = {"success": 1.0, "warning": 0.5, "failed": 0.0}
    score = round(sum(weights.get(str(item["status"]), 0.0) for item in checks) / max(len(checks), 1) * 100)
    failed = sum(1 for item in checks if item["status"] == "failed")
    warnings = sum(1 for item in checks if item["status"] == "warning")
    if failed:
        level = "blocked"
        headline = "推广前有阻断项"
    elif score >= 85 and warnings <= 2:
        level = "ready"
        headline = "可以小范围推广"
    else:
        level = "needs_config"
        headline = "还需要补配置"
    user_required = []
    seen: set[str] = set()
    for item in checks:
        required = str(item.get("user_required") or "").strip()
        if required and required not in seen:
            seen.add(required)
            user_required.append(required)
    return {
        "schema_version": 1,
        "level": level,
        "score": score,
        "headline": headline,
        "failed": failed,
        "warnings": warnings,
        "checks": checks,
        "user_required": user_required,
    }


def _freshest_quota_age_minutes(snapshot: dict[str, Any] | None) -> float | None:
    if not isinstance(snapshot, dict):
        return None
    ages: list[float] = []
    top = snapshot.get("age_minutes")
    if isinstance(top, (int, float)):
        ages.append(float(top))
    for provider in dict(snapshot.get("providers") or {}).values():
        if isinstance(provider, dict) and isinstance(provider.get("age_minutes"), (int, float)):
            ages.append(float(provider["age_minutes"]))
    return min(ages) if ages else None

def _read_status(root: Path) -> dict[str, Any]:
    candidates = [
        root.parent / ".visual-agent-status.md",
        root / ".visual-agent-status.md",
    ]
    for path in candidates:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="ignore")
            state = "unknown"
            for line in text.splitlines():
                if line.lower().startswith("## status:"):
                    state = line.split(":", 1)[1].strip()
                    break
            return {"state": state, "path": str(path), "raw": text[:2000]}
    return {"state": "none", "path": "", "raw": ""}


def _mission_efficiency(root: Path, mission: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(mission.get("plan_id") or "")
    records = load_worker_records(root, plan_id) if plan_id else []
    metrics = compute_mimo_efficiency(records or [])
    metrics["worker_runs"] = len([record for record in records or [] if isinstance(record, dict)])
    actual_worker_seconds = 0.0
    actual_cost_usd = 0.0
    actual_cost_available = False
    for record in records or []:
        if not isinstance(record, dict):
            continue
        actual_worker_seconds += float(record.get("elapsed_seconds") or 0.0)
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        if usage.get("cost_usd") is not None:
            actual_cost_available = True
            actual_cost_usd += float(usage.get("cost_usd") or 0.0)
    metrics["actual_worker_seconds"] = round(actual_worker_seconds, 3)
    metrics["actual_worker_minutes"] = round(actual_worker_seconds / 60.0, 2)
    metrics["actual_cost_usd"] = round(actual_cost_usd, 4)
    metrics["actual_cost_available"] = actual_cost_available
    metrics["actual_cost_label"] = f"${actual_cost_usd:.4f}" if actual_cost_available else "未回传"
    metrics["actual_task_seconds"] = _mission_elapsed_seconds(root, mission)
    metrics["actual_task_minutes"] = round(float(metrics["actual_task_seconds"] or 0.0) / 60.0, 2)
    return metrics


def _mission_pacer_evidence(root: Path, mission: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(mission.get("plan_id") or "").strip()
    mission_id = str(mission.get("mission_id") or "").strip()
    records = load_worker_records(root, plan_id) if plan_id else []
    verification = load_verification(root, plan_id) if plan_id else None
    verification = verification if isinstance(verification, dict) else {}
    plan = load_plan(root, plan_id) if plan_id else None
    plan = plan if isinstance(plan, dict) else {}
    latest_worker = records[-1] if records else {}
    backend = latest_worker.get("backend") if isinstance(latest_worker.get("backend"), dict) else {}
    log_path = str(latest_worker.get("log_path") or "").strip()
    report_path = root / "missions" / mission_id / "final_report.md" if mission_id else Path()
    command = ""
    if isinstance(verification.get("command_verification"), dict):
        command = str(verification["command_verification"].get("command") or "").strip()
    if not command:
        command = str(verification.get("command") or "").strip()
    verification_mode = str(plan.get("verification_mode") or "").strip()
    if not verification_mode:
        verification_mode = "command" if command else "workflow"
    progress = build_mission_progress(
        workspace_root=root,
        mission=mission,
        worker_records=records,
        verification=verification,
    )
    return {
        "worker_status": str(latest_worker.get("status") or ""),
        "worker_command": str(latest_worker.get("command") or ""),
        "worker_exit_code": latest_worker.get("exit_code"),
        "worker_seconds": latest_worker.get("elapsed_seconds"),
        "worker_records": len(records),
        "agent": str(latest_worker.get("agent") or mission.get("agent") or ""),
        "backend": str(backend.get("name") or ""),
        "model": str(backend.get("model") or ""),
        "worktree": str(latest_worker.get("cwd") or ""),
        "log_path": log_path,
        "log_tail": _read_worker_log_tail(Path(log_path)) if log_path else "",
        "verification_verdict": str(verification.get("verdict") or ""),
        "verification_command": command,
        "verification_mode": verification_mode,
        "verification_path": str(plan_dir(root, plan_id) / "verification.json") if plan_id else "",
        "merge_state": str(mission.get("merge_state") or ""),
        "final_report": str(report_path) if report_path else "",
        "stage": progress.get("stage", ""),
        "activity": progress.get("activity", ""),
        "activity_label": progress.get("activity_label", ""),
        "activity_command": progress.get("activity_command", ""),
        "activity_elapsed_seconds": progress.get("activity_elapsed_seconds"),
        "progress": progress,
        "changed_product_file_count": progress.get("changed_product_file_count", 0),
    }


def _read_worker_log_tail(path: Path, *, max_chars: int = 1600) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_chars * 3))
            data = handle.read()
        return data.decode("utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""


def _build_work_traces(
    root: Path,
    *,
    missions: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    queue: list[dict[str, Any]],
    programs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compact, UI-ready ledger of everything the workbench has touched."""
    traces: list[dict[str, Any]] = []

    def add(
        *,
        kind: str,
        status: str,
        title: str,
        detail: str = "",
        path: str = "",
        timestamp: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        traces.append(
            {
                "kind": kind,
                "status": status,
                "title": title[:180],
                "detail": detail[:800],
                "path": path,
                "timestamp": timestamp,
                "meta": meta or {},
            }
        )

    for launch in sorted(_launch_snapshot(root), key=lambda item: str(item.get("started_at") or ""), reverse=True)[:10]:
        launch_id = str(launch.get("launch_id") or "")
        state = str(launch.get("state") or "starting")
        goal = str(launch.get("goal") or "未命名任务")
        add(
            kind="launch",
            status=state,
            title=f"launch {launch_id}: {goal}",
            detail=str(launch.get("error") or launch.get("stop_reason") or launch.get("status") or ""),
            timestamp=str(launch.get("started_at") or ""),
            meta={
                "launch_id": launch_id,
                "mission_id": launch.get("mission_id") or "",
                "execute": bool(launch.get("execute")),
                "test_command": launch.get("test_command") or "",
                "state_path": launch.get("state_path") or "",
            },
        )

    for mission in sorted(missions, key=_mission_sort_key, reverse=True)[:20]:
        mission_id = str(mission.get("mission_id") or "")
        status = str(mission.get("status") or "unknown")
        stop_reason = str(mission.get("stop_reason") or "")
        ev = mission.get("pacer_evidence") if isinstance(mission.get("pacer_evidence"), dict) else {}
        summary = _mission_trace_summary(root, mission)
        detail_parts = [
            f"状态={status}",
            f"停止原因={stop_reason}" if stop_reason else "",
            f"验收={ev.get('verification_verdict')}" if ev.get("verification_verdict") else "",
            f"后端={ev.get('backend') or ev.get('agent')}" if (ev.get("backend") or ev.get("agent")) else "",
        ]
        add(
            kind="mission",
            status=status,
            title=f"mission {mission_id}: {mission.get('objective') or '未命名任务'}",
            detail=" | ".join(part for part in detail_parts if part),
            path=str(summary.get("report_path") or ev.get("log_path") or ""),
            timestamp=str(mission.get("updated_at") or mission.get("created_at") or ""),
            meta={
                **summary,
                "mission_id": mission_id,
                "plan_id": mission.get("plan_id") or "",
                "agent": mission.get("agent") or ev.get("agent") or "",
                "backend": ev.get("backend") or "",
                "model": ev.get("model") or "",
                "verification_command": ev.get("verification_command") or "",
                "log_path": ev.get("log_path") or "",
                "worktree": ev.get("worktree") or "",
            },
        )

    plan_ids_with_missions = {str(m.get("plan_id") or "") for m in missions if m.get("plan_id")}
    for plan in sorted(
        plans,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or item.get("saved_at") or ""),
        reverse=True,
    )[:12]:
        plan_id = str(plan.get("plan_id") or "")
        if not plan_id or plan_id in plan_ids_with_missions:
            continue
        records = load_worker_records(root, plan_id) or []
        latest = records[-1] if records else {}
        backend = latest.get("backend") if isinstance(latest.get("backend"), dict) else {}
        timestamp = str(
            latest.get("recorded_at")
            or plan.get("updated_at")
            or plan.get("created_at")
            or plan.get("saved_at")
            or ""
        )
        add(
            kind="plan",
            status=str(latest.get("status") or plan.get("status") or "planned"),
            title=f"plan {plan_id}: {plan.get('objective') or '未命名计划'}",
            detail=str(latest.get("command") or ""),
            path=str(latest.get("log_path") or plan_dir(root, plan_id)),
            timestamp=timestamp,
            meta={
                "plan_id": plan_id,
                "worker_records": len(records),
                "agent": latest.get("agent") or "",
                "backend": backend.get("name") or "",
                "model": backend.get("model") or "",
            },
        )

    for item in queue[:8]:
        add(
            kind="queue",
            status=str(item.get("status") or "queued"),
            title=f"queue {item.get('mission_id') or ''}: {item.get('objective') or '队列任务'}",
            detail=str(item.get("stop_reason") or ""),
            meta={"mission_id": item.get("mission_id") or "", "agent": item.get("agent") or ""},
        )

    for program in programs[:6]:
        add(
            kind="program",
            status=str(program.get("status") or "program"),
            title=f"program {program.get('program_id') or ''}: {program.get('objective') or '项目托管'}",
            detail=f"{program.get('task_count') or 0} 个任务",
            path=str(program.get("source_file") or ""),
            meta={"program_id": program.get("program_id") or "", "task_count": program.get("task_count") or 0},
        )

    for line in _dashboard_error_tail()[-8:]:
        add(kind="log", status="error", title="dashboard log", detail=line)

    return traces[:60]


def _mission_sort_key(mission: dict[str, Any]) -> str:
    return str(mission.get("updated_at") or mission.get("created_at") or mission.get("mission_id") or "")


def _mission_trace_summary(root: Path, mission: dict[str, Any]) -> dict[str, Any]:
    mission_id = str(mission.get("mission_id") or "").strip()
    if not mission_id:
        return {"rounds": 0, "latest_round": "", "report_exists": False, "report_path": ""}
    mission_dir = root / "missions" / mission_id
    rounds_path = mission_dir / "rounds.jsonl"
    rounds = 0
    latest_round = ""
    if rounds_path.exists():
        try:
            for line in rounds_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                rounds += 1
                try:
                    item = json.loads(line)
                    latest_round = f"{item.get('type') or 'round'}:{item.get('status') or ''}".rstrip(":")
                except json.JSONDecodeError:
                    latest_round = "unparseable"
        except OSError:
            pass
    report_path = mission_dir / "final_report.md"
    return {
        "rounds": rounds,
        "latest_round": latest_round,
        "report_exists": report_path.exists(),
        "report_path": str(report_path) if report_path.exists() else "",
    }


def _dashboard_error_tail(max_lines: int = 40) -> list[str]:
    path = Path.home() / ".pacer" / "logs" / "dashboard.log"
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    except OSError:
        return []


def _mission_elapsed_seconds(root: Path, mission: dict[str, Any]) -> float:
    mission_id = str(mission.get("mission_id") or "")
    if mission_id:
        background_path = root / "missions" / mission_id / "background.json"
        if background_path.exists():
            try:
                payload = json.loads(background_path.read_text(encoding="utf-8"))
                started = _parse_iso_timestamp(str(payload.get("started_at") or ""))
                completed = _parse_iso_timestamp(str(payload.get("completed_at") or ""))
                if started and completed and completed >= started:
                    return round(completed - started, 3)
            except (OSError, ValueError, TypeError):
                pass
    created = _parse_iso_timestamp(str(mission.get("created_at") or ""))
    updated = _parse_iso_timestamp(str(mission.get("updated_at") or ""))
    if created and updated and updated >= created:
        return round(updated - created, 3)
    return 0.0


def _parse_iso_timestamp(value: str) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime

        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _build_value(root: Path, missions: list[dict[str, Any]], plans: list[dict[str, Any]]) -> dict[str, Any]:
    verified = sum(1 for m in missions if str(m.get("status") or "") == "verified")
    tier_counts = {"cheap": 0, "standard": 0, "strong": 0}
    spent_usd = 0.0
    saved_usd = 0.0
    input_tokens = 0
    output_tokens = 0
    real_runs = 0
    all_worker_records: list[dict[str, Any] | None] = []

    for summary in plans:
        plan_id = summary.get("plan_id")
        if not plan_id:
            continue
        payload = load_plan(root, plan_id) or {}
        for track in payload.get("worker_tracks") or []:
            tier = str((track or {}).get("tier") or "")
            if tier in tier_counts:
                tier_counts[tier] += 1
        records = load_worker_records(root, plan_id) or []
        all_worker_records.extend(records)
        for record in records:
            usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
            cost = float(usage.get("cost_usd") or 0.0)
            if usage.get("cost_usd") is not None:
                real_runs += 1
                if usage.get("cost_is_savings"):
                    saved_usd += cost
                else:
                    spent_usd += cost
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)

    routed_tasks = tier_counts["cheap"] + tier_counts["standard"]
    return {
        "missions_total": len(missions),
        "verified": verified,
        "tier_counts": tier_counts,
        "downgraded_tasks": routed_tasks,
        "spent_usd": round(spent_usd, 4),
        "saved_usd": round(saved_usd, 4),
        "real_worker_runs": real_runs,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "mimo_efficiency": compute_mimo_efficiency(all_worker_records),
    }


# ---------------------------------------------------------------------------
# Launch and worker state — shared with server.py
# ---------------------------------------------------------------------------

_LAUNCHES: dict[str, dict[str, Any]] = {}
_LAUNCH_LOCK = threading.Lock()

_WORKER_PROC: subprocess.Popen[bytes] | None = None
_WORKER_WORKSPACE: Path | None = None
_WORKER_LOCK = threading.Lock()


def _launch_snapshot(workspace_root: str | Path | None = None) -> list[dict[str, Any]]:
    expected = str(Path(workspace_root).expanduser().resolve()) if workspace_root is not None else ""
    with _LAUNCH_LOCK:
        return [
            dict(item)
            for item in _LAUNCHES.values()
            if not expected or str(item.get("workspace_root") or "") == expected
        ]


def worker_status(workspace_root: str | Path | None = None) -> dict[str, Any]:
    global _WORKER_PROC, _WORKER_WORKSPACE
    expected = Path(workspace_root).expanduser().resolve() if workspace_root is not None else None
    with _WORKER_LOCK:
        if _WORKER_PROC is None:
            return {"running": False, "pid": None, "workspace_root": "", "active_for_workspace": False}
        if _WORKER_PROC.poll() is not None:
            _WORKER_PROC = None
            _WORKER_WORKSPACE = None
            return {"running": False, "pid": None, "workspace_root": "", "active_for_workspace": False}
        worker_root = _WORKER_WORKSPACE.resolve() if _WORKER_WORKSPACE is not None else None
        return {
            "running": True,
            "pid": _WORKER_PROC.pid,
            "workspace_root": str(worker_root or ""),
            "active_for_workspace": expected is None or worker_root == expected,
        }
