"""Pacer 后端服务 — HTTP API + 数据层源码

零依赖的本地 Web 服务，提供看板 UI 和 REST API。
功能：任务管理、Worker 控制、工作空间切换、对话、通知。

依赖：仅 Python 标准库
"""

from __future__ import annotations

import json
import ipaddress
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


# ============================================================================
# 数据层 (data.py)
# ============================================================================

# ---------------------------------------------------------------------------
# TTL 缓存
# ---------------------------------------------------------------------------

_SENTINEL = object()


class _TTLCache:
    """线程安全的 TTL 缓存"""

    def __init__(self, ttl: float = 60.0):
        self._ttl = ttl
        self._ts: float = 0.0
        self._data: Any = _SENTINEL
        self._lock = threading.Lock()

    def is_valid(self) -> bool:
        with self._lock:
            return self._data is not _SENTINEL and (time.monotonic() - self._ts) < self._ttl

    def get(self) -> Any:
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
# Agent 缓存
# ---------------------------------------------------------------------------

_agents_cache = _TTLCache(ttl=60.0)


def get_agents_cached() -> list[dict[str, Any]]:
    """返回 agents_doctor() 结果，缓存 60 秒"""
    if _agents_cache.is_valid():
        return _agents_cache.get()
    from visual_agent.agent_capabilities import agents_doctor
    result = agents_doctor()
    _agents_cache.set(result)
    return result


# ---------------------------------------------------------------------------
# Dashboard 数据缓存
# ---------------------------------------------------------------------------

_data_cache = _TTLCache(ttl=2.0)


def build_dashboard_data_cached(root: Path) -> dict[str, Any]:
    """构建 dashboard 数据，2 秒 TTL 缓存"""
    if _data_cache.is_valid():
        return _data_cache.get()
    data = _build_dashboard_data_uncached(root)
    _data_cache.set(data)
    return data


def _build_dashboard_data_uncached(root: Path) -> dict[str, Any]:
    """原始 dashboard 数据组装"""
    from visual_agent.missions import list_missions
    from visual_agent.chief_plans_store import list_plans
    from visual_agent.chief_queue import list_mission_queue_items
    from visual_agent.programs import list_programs
    from visual_agent.workbench_board import attach_board_fields

    missions = attach_board_fields(root, list_missions(root))
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

    counts: dict[str, int] = {}
    for mission in missions:
        key = mission.get("status") or "unknown"
        counts[key] = counts.get(key, 0) + 1

    installed_agents = [str(a.get("agent")) for a in agents if isinstance(a, dict) and a.get("installed")]

    try:
        from visual_agent.agent_backends import resolve_backend_by_name
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
        "repo_root": str(root.parent),
        "status": _read_status(root),
        "value": _build_value(root, missions, plans),
        "agents": agents,
        "installed_agents": installed_agents,
        "missions": missions,
        "mission_counts": counts,
        "plans": plans,
        "queue": queue,
        "programs": programs,
        "launches": _launch_snapshot(),
        "worker": worker_status(),
    }


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


def _build_value(root: Path, missions: list[dict[str, Any]], plans: list[dict[str, Any]]) -> dict[str, Any]:
    from visual_agent.chief_plans_store import load_plan, load_worker_records
    from visual_agent.mimo_efficiency import compute_mimo_efficiency

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
# 共享状态
# ---------------------------------------------------------------------------

_LAUNCHES: dict[str, dict[str, Any]] = {}
_LAUNCH_LOCK = threading.Lock()

_WORKER_PROC: subprocess.Popen[bytes] | None = None
_WORKER_LOCK = threading.Lock()


def _launch_snapshot() -> list[dict[str, Any]]:
    with _LAUNCH_LOCK:
        return [dict(item) for item in _LAUNCHES.values()]


def worker_status() -> dict[str, Any]:
    global _WORKER_PROC
    with _WORKER_LOCK:
        if _WORKER_PROC is None:
            return {"running": False, "pid": None}
        if _WORKER_PROC.poll() is not None:
            _WORKER_PROC = None
            return {"running": False, "pid": None}
        return {"running": True, "pid": _WORKER_PROC.pid}


# ============================================================================
# API 层 (api.py)
# ============================================================================

# ---------------------------------------------------------------------------
# 活动工作空间
# ---------------------------------------------------------------------------

_active_workspace: Path | None = None
_active_workspace_lock = threading.Lock()


def get_active_workspace(default: Path) -> Path:
    global _active_workspace
    with _active_workspace_lock:
        if _active_workspace is None:
            _active_workspace = default
        return _active_workspace


def set_active_workspace(path: str | Path) -> dict[str, Any]:
    global _active_workspace
    if not path:
        return {"ok": False, "error": "请输入路径"}
    p = Path(str(path)).expanduser().resolve()
    if p.name != ".agent-workspace":
        candidate = p / ".agent-workspace"
        if candidate.exists():
            p = candidate
    if not p.exists():
        return {"ok": False, "error": f"路径不存在: {p}"}
    if p.name != ".agent-workspace":
        return {"ok": False, "error": f"不是有效的工作空间目录: {p}"}
    with _active_workspace_lock:
        _active_workspace = p
    return {"ok": True, "workspace": str(p)}


def list_known_workspaces() -> list[str]:
    with _active_workspace_lock:
        current = _active_workspace
    found: list[Path] = []
    scan_roots: list[Path] = []
    if current:
        proj_dir = current.parent
        siblings_dir = proj_dir.parent
        scan_roots.append(proj_dir)
        if len(siblings_dir.parts) > 1:
            scan_roots.append(siblings_dir)
    scan_roots.append(Path.home())
    seen: set[str] = set()
    for root in scan_roots:
        try:
            for child in root.iterdir():
                ws = child / ".agent-workspace"
                if ws.is_dir() and str(ws) not in seen:
                    seen.add(str(ws))
                    found.append(ws)
        except (PermissionError, OSError):
            continue
        if len(found) >= 20:
            break
    return [str(p) for p in found[:20]]


# ---------------------------------------------------------------------------
# Launch 注册
# ---------------------------------------------------------------------------

def record_launch(launch_id: str, **fields: Any) -> None:
    with _LAUNCH_LOCK:
        entry = _LAUNCHES.setdefault(launch_id, {"launch_id": launch_id})
        entry.update(fields)


# ---------------------------------------------------------------------------
# Worker 进程管理
# ---------------------------------------------------------------------------

def start_worker(workspace_root: Path) -> dict[str, Any]:
    global _WORKER_PROC
    with _WORKER_LOCK:
        if _WORKER_PROC is not None and _WORKER_PROC.poll() is None:
            return {"ok": False, "error": "Worker 已在运行中", "pid": _WORKER_PROC.pid}
        cmd = [
            sys.executable, "-m", "visual_agent.cli",
            "mission", "worker", "--watch",
            "--workspace-root", str(workspace_root),
        ]
        try:
            _WORKER_PROC = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "pid": _WORKER_PROC.pid}


def stop_worker() -> dict[str, Any]:
    global _WORKER_PROC
    with _WORKER_LOCK:
        if _WORKER_PROC is None or _WORKER_PROC.poll() is not None:
            _WORKER_PROC = None
            return {"ok": True, "was_running": False}
        _WORKER_PROC.terminate()
        try:
            _WORKER_PROC.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _WORKER_PROC.kill()
        _WORKER_PROC = None
        return {"ok": True, "was_running": True}


# ---------------------------------------------------------------------------
# Mission 重试
# ---------------------------------------------------------------------------

def retry_mission(workspace_root: Path, mission_id: str) -> dict[str, Any]:
    from visual_agent.missions import load_mission
    mission = load_mission(workspace_root, mission_id) or {}
    if not mission:
        return {"ok": False, "error": "找不到该 mission"}
    status = str(mission.get("status") or "")
    if status == "verified":
        return {"ok": False, "error": "任务已验收，无需重试"}
    goal = str(mission.get("objective") or "").strip()
    if not goal:
        return {"ok": False, "error": "原任务没有记录目标文本"}
    agent = str(mission.get("agent") or "")
    test_command = str(mission.get("test_command") or "")
    return start_workbench_mission(
        workspace_root=workspace_root,
        repo_root=str(workspace_root.parent),
        goal=goal,
        test_command=test_command,
        agent=agent,
        execute=True,
    )


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------

def run_chat(payload: dict[str, Any]) -> dict[str, Any]:
    import shutil

    message = str(payload.get("message") or "").strip()
    if not message:
        return {"ok": False, "error": "消息不能为空"}
    agent = str(payload.get("agent") or "claude-code").strip()
    history: list[dict[str, Any]] = list(payload.get("history") or [])

    context_turns = history[-17:-1]
    prompt_parts: list[str] = []
    for h in context_turns:
        role = str(h.get("role") or "")
        content = str(h.get("content") or "").strip()
        if role == "user":
            prompt_parts.append(f"用户: {content}")
        elif role == "assistant":
            prompt_parts.append(f"助手: {content}")
    prompt_parts.append(f"用户: {message}")
    full_prompt = "\n\n".join(prompt_parts)

    from visual_agent.agent_capabilities import AGENT_ALIASES
    canonical = AGENT_ALIASES.get(agent.lower().replace(" ", "-"), agent)
    if canonical == "codex":
        return {"ok": False, "error": "Codex 是任务型 Agent，不支持对话模式。请切换到 claude-code 或 gemini。"}
    if canonical == "mimo":
        try:
            from visual_agent.agent_backends import canonical_backend_name, resolve_backend_by_name
            from visual_agent.llm_providers import LLMBackend, run_llm_completion

            backend_name = canonical_backend_name(agent)
            backend = resolve_backend_by_name(backend_name)
        except Exception:
            backend = None
        if not backend:
            return {"ok": False, "error": "低成本后端未配置"}
        base_url = str((backend.get("env") or {}).get("ANTHROPIC_BASE_URL") or "").rstrip("/")
        if base_url.endswith("/anthropic"):
            base_url = base_url[: -len("/anthropic")] + "/v1"
        try:
            reply = run_llm_completion(
                backend=LLMBackend(provider=str(backend.get("provider") or "openai"), model_id=str(backend.get("model") or "gpt-4o-mini")),
                system_prompt="你是 Pacer 工作台里的简洁研发助手。",
                prompt=full_prompt,
                max_tokens=1200,
                api_key=str((backend.get("env") or {}).get("ANTHROPIC_API_KEY") or ""),
                base_url=base_url,
                endpoint="/chat/completions",
                timeout_seconds=90,
            )
            return {"ok": True, "reply": reply or "（AI 返回了空响应）"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"低成本后端调用失败：{exc}"}
    exe_map = {"claude-code": "claude", "gemini": "gemini", "mimo": "claude"}
    exe = exe_map.get(canonical, "claude")
    found = shutil.which(exe)
    if not found:
        return {"ok": False, "error": f"找不到 {exe} 命令，请确认已安装并在 PATH 中"}

    argv = [found, "-p", full_prompt]
    stdin_text: str | None = None
    if found.lower().endswith((".cmd", ".bat")):
        # Windows batch shims parse arguments through cmd.exe. Never place the
        # user-controlled prompt on that command line; pass it through stdin so
        # shell metacharacters remain data.
        argv = [found, "-p"]
        stdin_text = full_prompt

    try:
        completed = subprocess.run(
            argv, capture_output=True, input=stdin_text, text=True, timeout=90,
            check=False, encoding="utf-8", errors="replace",
        )
        reply = (completed.stdout or completed.stderr or "").strip()
        if not reply:
            reply = "（AI 返回了空响应）"
        return {"ok": True, "reply": reply}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "AI 响应超时（90 秒）"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"调用失败：{exc}"}


# ---------------------------------------------------------------------------
# 通知配置
# ---------------------------------------------------------------------------

def _pacer_config_path() -> Path:
    return Path.home() / ".pacer" / "notifications.json"


def get_notifications_config() -> dict[str, Any]:
    path = _pacer_config_path()
    if not path.exists():
        return {"configured": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"configured": False, "error": "配置文件损坏"}
    safe = dict(data)
    if "password" in safe and safe["password"]:
        safe["password"] = "****"
    safe["configured"] = bool(safe.get("smtp_host") and safe.get("recipient"))
    return safe


def save_notifications_config(payload: dict[str, Any]) -> dict[str, Any]:
    smtp_host = str(payload.get("smtp_host") or "").strip()
    recipient = str(payload.get("recipient") or "").strip()
    if not smtp_host:
        return {"ok": False, "error": "SMTP 服务器不能为空"}
    if not recipient:
        return {"ok": False, "error": "收件人邮箱不能为空"}
    existing: dict = {}
    path = _pacer_config_path()
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
    password = str(payload.get("password") or "").strip()
    if password == "****":
        password = str(existing.get("password") or "")
    config = {
        "schema_version": 1,
        "smtp_host": smtp_host,
        "smtp_port": int(payload.get("smtp_port") or 587),
        "use_tls": bool(payload.get("use_tls", True)),
        "username": str(payload.get("username") or "").strip(),
        "password": password,
        "sender": str(payload.get("sender") or payload.get("username") or "").strip(),
        "recipient": recipient,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "message": "配置已保存"}


def test_notification() -> dict[str, Any]:
    try:
        from visual_agent.notifications import build_event_notification, send_email_notification, load_notification_config
        cfg = load_notification_config(_pacer_config_path())
        if cfg is None:
            return {"ok": False, "error": "未配置 SMTP"}
        notif = build_event_notification("mission_verified", {
            "project": "Pacer",
            "objective": "测试邮件通知配置",
            "status": "verified",
            "stop_reason": "verified",
            "message": "这是一封 Pacer 测试邮件。",
        })
        result = send_email_notification(notif, config=cfg, dry_run=False)
        return {"ok": result.get("status") == "sent", "result": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 诊断
# ---------------------------------------------------------------------------

def build_diagnostic_bundle(workspace_root: Path) -> dict[str, Any]:
    import platform
    from visual_agent.missions import list_missions

    try:
        agents = get_agents_cached()
    except Exception as exc:
        agents = [{"error": str(exc)}]

    missions_summary: list[dict[str, Any]] = []
    try:
        for m in list_missions(workspace_root)[:10]:
            missions_summary.append({
                "mission_id": m.get("mission_id"),
                "status": m.get("status"),
                "stop_reason": m.get("stop_reason"),
                "agent": m.get("agent"),
                "created_at": m.get("created_at"),
                "objective": str(m.get("objective") or "")[:120],
            })
    except Exception as exc:
        missions_summary = [{"error": str(exc)}]

    with _WORKER_LOCK:
        worker_running = _WORKER_PROC is not None and _WORKER_PROC.poll() is None

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "product": "Pacer",
        "platform": f"{platform.system()} {platform.release()}",
        "python": sys.version.split()[0],
        "workspace_root": str(workspace_root),
        "workspace_exists": workspace_root.exists(),
        "agents": agents,
        "worker_running": worker_running,
        "missions_recent": missions_summary,
        "error_log_tail": _error_log_tail(),
    }


def _error_log_path() -> Path:
    log_dir = Path.home() / ".pacer" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "dashboard.log"


def _error_log_tail(max_lines: int = 200) -> list[str]:
    path = _error_log_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-max_lines:]
    except OSError:
        return []


def log_error(source: str, message: str, detail: str = "") -> None:
    try:
        line = f"{datetime.now(timezone.utc).isoformat()} [{source}] {message}"
        if detail:
            line += f" | {detail}"
        path = _error_log_path()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line.replace("\n", " ⏎ ")[:4000] + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Mission 详情
# ---------------------------------------------------------------------------

def build_mission_detail(workspace_root: str | Path, mission_id: str) -> dict[str, Any]:
    from visual_agent.missions import load_mission
    from visual_agent.workbench_board import attach_board_fields, mission_review_payload

    root = Path(workspace_root).expanduser().resolve()
    mission = load_mission(root, mission_id) or {}
    rounds_path = root / "missions" / mission_id / "rounds.jsonl"
    rounds: list[dict[str, Any]] = []
    if rounds_path.exists():
        for line in rounds_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rounds.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    report_path = root / "missions" / mission_id / "final_report.md"
    final_report = report_path.read_text(encoding="utf-8", errors="ignore") if report_path.exists() else ""
    review = mission_review_payload(root, mission) if mission else {}
    decorated = attach_board_fields(root, [mission])[0] if mission else {}
    live_logs = _mission_live_logs(root, mission, plan_id=str(mission.get("plan_id") or ""))
    return {
        "mission": mission,
        "rounds": rounds,
        "final_report": final_report,
        "review": review,
        "can_merge": bool(decorated.get("can_merge")),
        "merge_state": str(decorated.get("merge_state") or ""),
        "live_logs": live_logs,
    }


def _read_tail(path: Path, *, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    return text[-max_chars:]


def _mission_live_logs(workspace_root: Path, mission: dict[str, Any], *, plan_id: str) -> dict[str, Any]:
    from visual_agent.chief_plans_store import plan_dir

    mission_id = str(mission.get("mission_id") or "").strip()
    entries: list[dict[str, Any]] = []
    candidates: list[Path] = []
    if plan_id:
        candidates.extend(sorted((plan_dir(workspace_root, plan_id) / "logs").glob("*.log")))
    if mission_id:
        candidates.extend(sorted((workspace_root / "missions" / mission_id / "logs").glob("*.log")))
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        tail = _read_tail(path)
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        entries.append({"path": key, "name": path.name, "modified": modified, "tail": tail})
    entries.sort(key=lambda item: float(item.get("modified") or 0.0), reverse=True)
    latest = entries[0] if entries else {}
    return {
        "count": len(entries),
        "latest_path": latest.get("path", ""),
        "latest_key": f"{latest.get('path', '')}:{latest.get('modified', '')}:{len(str(latest.get('tail') or ''))}" if latest else "",
        "latest_tail": latest.get("tail", ""),
        "entries": entries[:6],
    }


# ---------------------------------------------------------------------------
# Mission 启动（工作台表单）
# ---------------------------------------------------------------------------

def start_workbench_mission(
    *,
    workspace_root: str | Path,
    repo_root: str,
    goal: str,
    test_command: str,
    agent: str,
    execute: bool,
    merge_policy: str = "manual",
    allow_dirty: bool = False,
    spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    goal = str(goal or "").strip()
    if not goal:
        return {"ok": False, "error": "请填写目标（goal）。"}
    from visual_agent.verification_profiles import resolve_test_command
    from visual_agent.mission_pipeline import MissionPipeline, SpecValidator, mission_result_to_pipeline_state
    from visual_agent.mission_intake import is_manual_verification_goal, is_review_plan_goal

    requested_test_command = str(test_command or "").strip()
    resolved_test_command, verification_profile = resolve_test_command(
        requested_test_command or "auto",
        repo_root=repo_root or ".",
    )
    manual_verification = is_manual_verification_goal(goal) and not requested_test_command
    review_plan = is_review_plan_goal(goal) and not requested_test_command
    if manual_verification and not resolved_test_command:
        verification_profile = {
            "source": "manual",
            "status": "manual_required",
            "reason": "现场/真机验收任务需要人工验收方案，不强制自动化测试命令。",
        }
    if review_plan and not resolved_test_command:
        verification_profile = {
            "source": "report",
            "status": "report_required",
            "reason": "审查/开发计划任务以报告文件作为验收，不强制自动化测试命令。",
        }
    if bool(execute) and not resolved_test_command and not manual_verification and not review_plan:
        return {
            "ok": False,
            "error": (
                "执行模式需要真实验收命令。未自动识别到 pytest/npm/go/cargo 等项目测试命令，"
                "请先在任务对话里确认验收方案，或填写 python -m pytest -q / npm test。"
            ),
            "verification_profile": verification_profile or {"source": "auto", "status": "not_found", "profiles": []},
        }
    validator = SpecValidator()
    normalized_spec = validator.validate(spec) if spec is not None else validator.derive_request_spec(
        goal=goal,
        repo_root=repo_root or ".",
        test_command=resolved_test_command or requested_test_command,
        agent=agent,
        execute=bool(execute),
    )
    launch_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    pipeline = MissionPipeline(workspace_root, launch_id=launch_id)
    pipeline_state = pipeline.begin(
        spec=normalized_spec,
        execute=bool(execute),
        request={
            "goal": goal,
            "repo_root": str(repo_root or "."),
            "test_command": resolved_test_command or "",
            "agent": str(agent or ""),
            "merge_policy": str(merge_policy or "manual"),
        },
    )
    record_launch(
        launch_id,
        goal=goal,
        execute=bool(execute),
        test_command=resolved_test_command or "",
        verification_profile=verification_profile or {},
        state="starting",
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    def _run() -> None:
        from visual_agent.chief_run import run_chief_mission
        try:
            effective_execute = bool(execute) and not manual_verification
            pipeline.transition(
                pipeline_state,
                "EXECUTING" if effective_execute else "REVIEW",
                "chief_run_start",
                execute=effective_execute,
            )
            result = run_chief_mission(
                goal=goal,
                workspace_root=workspace_root,
                repo_root=repo_root or ".",
                test_command=(resolved_test_command or None),
                agents=((agent,) if agent else ()),
                execute=False if (effective_execute or manual_verification) else bool(execute),
                dry_run=True if (effective_execute or manual_verification) else not bool(execute),
                allow_coverage_gap=manual_verification or review_plan,
                allow_dirty=bool(allow_dirty),
                merge=False,
            )
            mission = result.get("mission") if isinstance(result, dict) else {}
            status = str((result or {}).get("status") or "")
            stop_reason = str((result or {}).get("stop_reason") or "")
            mission_id = str((mission or {}).get("mission_id") or "")
            if mission_id:
                pipeline.attach_mission(pipeline_state, mission_id)
            background = None
            if effective_execute and mission_id and status in {"preview", "created"}:
                from visual_agent.chief_background import start_background_chief_run
                background = start_background_chief_run(
                    workspace_root=workspace_root,
                    mission_id=mission_id,
                    test_command=(resolved_test_command or None),
                    allow_dirty=bool(allow_dirty),
                    allow_coverage_gap=manual_verification or review_plan,
                    merge=str(merge_policy or "manual").lower() == "auto",
                )
                status = str(background.get("status") or status)
                stop_reason = str(background.get("stop_reason") or stop_reason)
            pipeline.transition(
                pipeline_state,
                mission_result_to_pipeline_state(status, stop_reason),
                "chief_run_finished",
                status=status,
                stop_reason=stop_reason,
            )
            record_launch(
                launch_id,
                state="done",
                status=status,
                stop_reason=stop_reason,
                mission_id=mission_id,
                background_pid=(background or {}).get("background", {}).get("pid") if isinstance(background, dict) else None,
            )
        except Exception as exc:
            pipeline.transition(pipeline_state, "FAILED", "chief_run_error", error=str(exc)[:400])
            record_launch(launch_id, state="error", error=str(exc)[:400])
            log_error("mission", f"launch {launch_id} failed", repr(exc))

    threading.Thread(target=_run, daemon=True).start()
    return {
        "ok": True,
        "launch_id": launch_id,
        "execute": bool(execute),
        "merge_policy": str(merge_policy or "manual"),
        "test_command": resolved_test_command or "",
        "verification_profile": verification_profile or {},
        "manual_verification": manual_verification,
        "review_plan": review_plan,
        "state_path": str(pipeline.path),
        "spec": normalized_spec,
    }


# ============================================================================
# HTTP 服务层 (server.py)
# ============================================================================

# 静态文件目录
_STATIC_DIR = Path(__file__).parent / "src" / "visual_agent" / "dashboard" / "static"


class _DashboardHandler(BaseHTTPRequestHandler):
    server: "_DashboardServer"

    def log_message(self, *args: Any) -> None:
        return

    def _send(self, body: bytes, content_type: str, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        origin = self._allowed_cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        path = self.path.split("?", 1)[0]
        reason = self._api_request_block_reason(path, require_json=False) if path.startswith("/api/") else ""
        if reason:
            self._send_json({"ok": False, "error": reason}, status=403)
            return
        self.send_response(204)
        origin = self._allowed_cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")
        self.end_headers()

    def _send_json(self, data: Any, *, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8-sig")
        self._send(body, "application/json; charset=utf-8", status=status)

    def _serve_static(self, file_path: Path, content_type: str) -> None:
        if not file_path.exists():
            self.send_response(404)
            self.end_headers()
            return
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        try:
            self._do_get_inner()
        except Exception as exc:
            log_error("backend", f"GET {self.path} failed", repr(exc))
            try:
                self._send_json({"ok": False, "error": f"服务器内部错误：{exc}"})
            except OSError:
                pass

    def _do_get_inner(self) -> None:
        path = self.path.split("?", 1)[0]
        root = get_active_workspace(self.server.workspace_root)

        # 静态文件
        if path == "/" or path == "/index.html":
            self._serve_static(_STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self._serve_static(_STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
            return
        if path == "/style.css":
            self._serve_static(_STATIC_DIR / "style.css", "text/css; charset=utf-8")
            return

        # API 端点
        if path.startswith("/api/"):
            reason = self._api_request_block_reason(path, require_json=False)
            if reason:
                self._send_json({"ok": False, "error": reason}, status=403)
                return
        if path == "/api/data":
            self._send_json(build_dashboard_data_cached(root))
            return
        if path == "/api/diagnostic":
            self._send_json(build_diagnostic_bundle(root))
            return
        if path == "/api/mission":
            from urllib.parse import parse_qs, urlparse
            mission_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            self._send_json(build_mission_detail(root, mission_id))
            return
        if path == "/api/workspace":
            self._send_json({"workspace": str(root), "ok": True})
            return
        if path == "/api/workspaces":
            self._send_json({"workspaces": list_known_workspaces(), "current": str(root)})
            return
        if path == "/api/notifications/config":
            self._send_json(get_notifications_config())
            return

        self.send_response(404)
        self.end_headers()

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            parsed = json.loads(raw.decode("utf-8") or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, OSError):
            return {}

    def do_POST(self) -> None:
        try:
            self._do_post_inner()
        except Exception as exc:
            log_error("backend", f"POST {self.path} failed", repr(exc))
            try:
                self._send_json({"ok": False, "error": f"服务器内部错误：{exc}"})
            except OSError:
                pass

    def _do_post_inner(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            reason = self._api_request_block_reason(path, require_json=True)
            if reason:
                status = 415 if reason == "API requests must use Content-Type: application/json." else 403
                self._send_json({"ok": False, "error": reason}, status=status)
                return
        payload = self._read_body()
        root = get_active_workspace(self.server.workspace_root)

        if path == "/api/mission/start":
            from visual_agent.mission_pipeline import SpecValidationError, SpecValidator
            try:
                spec = SpecValidator().validate(payload.get("spec"))
            except SpecValidationError as exc:
                self._send_json(exc.to_response(), status=400)
                return
            result = start_workbench_mission(
                workspace_root=root,
                repo_root=str(payload.get("repo_root") or str(root.parent)),
                goal=str(payload.get("goal") or ""),
                test_command=str(payload.get("test_command") or ""),
                agent=str(payload.get("agent") or ""),
                execute=bool(payload.get("execute")),
                merge_policy=str(payload.get("merge_policy") or "manual"),
                spec=spec,
            )
            self._send_json(result)
        elif path == "/api/mission/merge":
            from visual_agent.workbench_board import merge_mission_now
            result = merge_mission_now(root, str(payload.get("mission_id") or ""))
            self._send_json(result)
        elif path == "/api/mission/retry":
            self._send_json(retry_mission(root, str(payload.get("mission_id") or "")))
        elif path == "/api/worker/start":
            self._send_json(start_worker(root))
        elif path == "/api/worker/stop":
            self._send_json(stop_worker())
        elif path == "/api/workspace/switch":
            self._send_json(set_active_workspace(str(payload.get("path") or "")))
        elif path == "/api/chat":
            self._send_json(run_chat(payload))
        elif path == "/api/notifications/config":
            self._send_json(save_notifications_config(payload))
        elif path == "/api/notifications/test":
            self._send_json(test_notification())
        elif path == "/api/client-error":
            log_error(
                "frontend",
                str(payload.get("message") or "unknown"),
                f"{payload.get('source') or ''}:{payload.get('line') or ''} {str(payload.get('stack') or '')[:1500]}",
            )
            self._send_json({"ok": True})
        else:
            self.send_response(404)
            self.end_headers()

    def _api_request_block_reason(self, path: str, *, require_json: bool) -> str:
        if not self._host_is_loopback():
            return "Blocked request Host; Pacer API only accepts loopback hosts."
        fetch_site = str(self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site == "cross-site":
            return "Blocked cross-site browser request."
        origin = str(self.headers.get("Origin") or "").strip()
        if origin and not self._origin_is_allowed(origin):
            return "Blocked request Origin."
        referer = str(self.headers.get("Referer") or "").strip()
        if referer and not self._origin_is_allowed(referer):
            return "Blocked request Referer."
        if require_json and not self._content_type_is_json():
            return "API requests must use Content-Type: application/json."
        return ""

    def _content_type_is_json(self) -> bool:
        value = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        return value == "application/json"

    def _host_is_loopback(self) -> bool:
        host = str(self.headers.get("Host") or "").strip()
        if not host:
            return True
        return _is_loopback_hostname(_hostname_from_netloc(host))

    def _origin_is_allowed(self, value: str) -> bool:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            return False
        if not _is_loopback_hostname(str(parsed.hostname or "")):
            return False
        expected_port = int(self.server.server_address[1])
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port == expected_port:
            return True
        return value.rstrip("/") in _extra_allowed_origins()

    def _allowed_cors_origin(self) -> str:
        origin = str(self.headers.get("Origin") or "").strip()
        return origin if origin and self._origin_is_allowed(origin) else ""


class _DashboardServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], workspace_root: Path):
        super().__init__(address, _DashboardHandler)
        self.workspace_root = workspace_root
        from visual_agent.dashboard.api import set_active_workspace
        set_active_workspace(workspace_root)


def _bind_dashboard_server(host: str, port: int, root: Path) -> _DashboardServer:
    candidates = [port, 8787, 8080, 8899, 9797, 0]
    seen: set[int] = set()
    last_error: Exception | None = None
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return _DashboardServer((host, candidate), root)
        except (PermissionError, OSError) as exc:
            last_error = exc
            continue
    raise RuntimeError(f"Could not bind the dashboard to any port on {host}: {last_error}")


def _hostname_from_netloc(value: str) -> str:
    parsed = urlparse(f"//{value}")
    return str(parsed.hostname or value.split(":", 1)[0]).strip("[]").lower()


def _is_loopback_hostname(host: str) -> bool:
    value = str(host or "").strip("[]").lower()
    if value in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _extra_allowed_origins() -> set[str]:
    raw = os.environ.get("PACER_DASHBOARD_ALLOWED_ORIGINS", "")
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


def serve_dashboard(
    *,
    workspace_root: str | Path,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = True,
) -> None:
    """启动 Pacer 看板服务"""
    root = Path(workspace_root).expanduser().resolve()
    server = _bind_dashboard_server(host, port, root)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    if actual_port != port:
        print(f"Port {port} was unavailable; using {actual_port} instead.")
    print(f"Pacer dashboard: {url}")
    print(f"Workspace: {root}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Pacer 看板服务")
    parser.add_argument("--workspace-root", required=True, help="工作空间根目录")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8787, help="监听端口")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    serve_dashboard(
        workspace_root=args.workspace_root,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )
