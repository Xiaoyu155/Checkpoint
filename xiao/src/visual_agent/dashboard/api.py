"""API endpoint handlers for the dashboard.

Each function handles one /api/* endpoint and returns a dict for JSON serialization.
The HTTP layer in server.py delegates here.
"""

from __future__ import annotations

import json
import sys
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data import (
    _LAUNCHES,
    _LAUNCH_LOCK,
    _WORKER_LOCK,
    _WORKER_PROC,
    _launch_snapshot,
    build_dashboard_data_cached,
    worker_status,
)
from ..chief_plans_store import list_plans, load_plan, load_worker_records, plan_dir
from ..chief_queue import list_mission_queue_items
from ..goal_intake import intake_dialogue_lines, refine_goal
from ..missions import list_missions, load_mission, save_mission
from ..programs import list_programs
from ..workbench_board import attach_board_fields, archive_mission_now, merge_mission_now, mission_review_payload
from ..workbench_model_config import (
    WorkbenchModelConfig,
    load_workbench_model_config,
    probe_workbench_model_config,
    redacted_config_summary,
    save_workbench_model_config,
)


# ---------------------------------------------------------------------------
# Active workspace state
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
        return {"ok": False, "error": f"不是有效的工作空间目录（应以 .agent-workspace 结尾）: {p}"}
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
# Launch registry
# ---------------------------------------------------------------------------

def record_launch(launch_id: str, **fields: Any) -> None:
    with _LAUNCH_LOCK:
        entry = _LAUNCHES.setdefault(launch_id, {"launch_id": launch_id})
        entry.update(fields)


# ---------------------------------------------------------------------------
# Worker process management
# ---------------------------------------------------------------------------

def start_worker(workspace_root: Path) -> dict[str, Any]:
    import visual_agent.dashboard.data as _data_mod
    with _data_mod._WORKER_LOCK:
        if _data_mod._WORKER_PROC is not None and _data_mod._WORKER_PROC.poll() is None:
            return {"ok": False, "error": "Worker 已在运行中", "pid": _data_mod._WORKER_PROC.pid}
        cmd = [
            sys.executable,
            "-m",
            "visual_agent.cli",
            "mission",
            "worker",
            "--watch",
            "--workspace-root",
            str(workspace_root),
        ]
        try:
            _data_mod._WORKER_PROC = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "pid": _data_mod._WORKER_PROC.pid}


def stop_worker() -> dict[str, Any]:
    import visual_agent.dashboard.data as _data_mod
    with _data_mod._WORKER_LOCK:
        if _data_mod._WORKER_PROC is None or _data_mod._WORKER_PROC.poll() is not None:
            _data_mod._WORKER_PROC = None
            return {"ok": True, "was_running": False}
        _data_mod._WORKER_PROC.terminate()
        try:
            _data_mod._WORKER_PROC.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _data_mod._WORKER_PROC.kill()
        _data_mod._WORKER_PROC = None
        return {"ok": True, "was_running": True}


# ---------------------------------------------------------------------------
# Mission retry
# ---------------------------------------------------------------------------

def retry_mission(workspace_root: Path, mission_id: str) -> dict[str, Any]:
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
# Chat
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

    from ..agent_capabilities import AGENT_ALIASES
    canonical = AGENT_ALIASES.get(agent.lower().replace(" ", "-"), agent)
    if canonical == "codex":
        return {"ok": False, "error": "Codex 是任务型 Agent，不支持对话模式。请切换到 claude-code 或 gemini。"}

    llm = _resolve_agent_llm(agent)
    if llm:
        try:
            from ..llm_providers import LLMBackend, run_llm_completion

            reply = run_llm_completion(
                backend=LLMBackend(provider=llm["provider"], model_id=llm["model"]),
                system_prompt=(
                    "你是 Pacer 工作台里的需求架构师和研发调度助手。"
                    "先用很少的问题把目标、项目目录、验收方式和风险问清楚；"
                    "当用户确认“没问题/按默认继续/可以”时，明确告诉用户可以派发给编码模型执行。"
                ),
                prompt=full_prompt,
                max_tokens=1200,
                api_key=llm["api_key"],
                base_url=llm["base_url"],
                endpoint="/chat/completions",
                reasoning_effort=llm.get("reasoning_effort") or None,
                timeout_seconds=90,
            )
            return {"ok": True, "reply": reply or "（AI 返回了空响应）", "backend": llm["name"], "model": llm["model"]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"低成本后端调用失败：{exc}"}

    if canonical == "mimo":
        return {"ok": False, "error": "低成本后端未配置：请设置 CHECKPOINT_BUGTEAM_API_KEY、CHECKPOINT_BUGTEAM_BASE_URL，或在 model_api_keys.txt 里配置后端令牌"}

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
            argv,
            capture_output=True,
            input=stdin_text,
            text=True,
            timeout=90,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        reply = (completed.stdout or completed.stderr or "").strip()
        if not reply:
            reply = "（AI 返回了空响应）"
        return {"ok": True, "reply": reply}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "AI 响应超时（90 秒），请稍后重试"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"调用失败：{exc}"}


def refine_goal_intake(payload: dict[str, Any]) -> dict[str, Any]:
    goal = str(payload.get("goal") or payload.get("message") or "").strip()
    if not goal:
        return {"ok": False, "error": "目标不能为空"}
    answers = [str(item).strip() for item in (payload.get("answers") or []) if str(item).strip()]
    use_model = bool(payload.get("use_model", True))
    agent = str(payload.get("agent") or "").strip()
    llm = _resolve_agent_llm(agent) if use_model and agent else None
    kwargs: dict[str, Any] = {}
    if llm:
        kwargs = {
            "model_id": f"{llm['provider']}:{llm['model']}",
            "api_key": llm["api_key"],
            "base_url": llm["base_url"],
            "endpoint": "/chat/completions",
            "max_tokens": 800,
        }
    result = refine_goal(goal, answers=answers, enable_model=use_model, **kwargs)
    result["answers"] = answers
    result["dialogue_lines"] = intake_dialogue_lines(result, answers=answers)
    result["ok"] = True
    return result


def _resolve_agent_llm(agent: str) -> dict[str, str] | None:
    if not str(agent or "").strip():
        return None
    try:
        from ..agent_backends import resolve_backend_by_name

        backend = resolve_backend_by_name(agent)
    except Exception:
        backend = None
    if not backend:
        return None
    env = backend.get("env") if isinstance(backend.get("env"), dict) else {}
    base_url = str(env.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    if base_url.endswith("/anthropic"):
        base_url = base_url[: -len("/anthropic")] + "/v1"
    api_key = str(env.get("ANTHROPIC_API_KEY") or "")
    model = str(backend.get("model") or "gpt-4o-mini")
    provider = str(backend.get("provider") or "openai")
    if not base_url or not api_key:
        return None
    return {
        "name": str(backend.get("name") or agent),
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
        "reasoning_effort": str(backend.get("reasoning_effort") or ""),
    }


# ---------------------------------------------------------------------------
# Notification config
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
        from ..notifications import build_event_notification, send_email_notification, load_notification_config
        cfg = load_notification_config(_pacer_config_path())
        if cfg is None:
            return {"ok": False, "error": "未配置 SMTP，请先填写并保存设置"}
        notif = build_event_notification("mission_verified", {
            "project": "Pacer",
            "objective": "测试邮件通知配置",
            "status": "verified",
            "stop_reason": "verified",
            "message": "这是一封 Pacer 测试邮件，说明邮件通知配置正确。",
        })
        result = send_email_notification(notif, config=cfg, dry_run=False)
        return {"ok": result.get("status") == "sent", "result": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Workbench model API configuration
# ---------------------------------------------------------------------------

def get_model_config() -> dict[str, Any]:
    config = load_workbench_model_config()
    return {
        "ok": True,
        "enabled": config.configured,
        "base_url": config.base_url,
        "api_key": config.api_key,
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "configured": config.configured,
        "summary": redacted_config_summary(config),
    }


def save_model_config(payload: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(payload.get("enabled", True))
    config = WorkbenchModelConfig(
        base_url=str(payload.get("base_url") or "").strip(),
        api_key=str(payload.get("api_key") or "").strip(),
        model=str(payload.get("model") or "").strip(),
        reasoning_effort=str(payload.get("reasoning_effort") or "").strip(),
    )
    if not enabled:
        return {"ok": False, "error": "关闭优先使用 sub2api 后不会删除已有配置；如需停用，请在 Agent 下拉框选择 codex 或 claude-code。"}
    try:
        path = save_workbench_model_config(config)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "message": "总工作台模型接口已保存，新的任务会优先显示 bugteam（低成本）。",
        "path": str(path),
        "summary": redacted_config_summary(config),
    }


def test_model_config(payload: dict[str, Any]) -> dict[str, Any]:
    config = WorkbenchModelConfig(
        base_url=str(payload.get("base_url") or "").strip(),
        api_key=str(payload.get("api_key") or "").strip(),
        model=str(payload.get("model") or "").strip(),
        reasoning_effort=str(payload.get("reasoning_effort") or "").strip(),
    )
    result = probe_workbench_model_config(config)
    return {"ok": bool(result.get("ok")), **result}


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def build_diagnostic_bundle(workspace_root: Path) -> dict[str, Any]:
    import platform
    from .data import get_agents_cached

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
    import os
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
# Mission detail
# ---------------------------------------------------------------------------

def build_mission_detail(workspace_root: str | Path, mission_id: str) -> dict[str, Any]:
    root = Path(workspace_root).expanduser().resolve()
    mission = load_mission(root, mission_id) or {}
    plan_id = str(mission.get("plan_id") or "").strip()
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
    live_logs = _mission_live_logs(root, mission)
    worker_records = load_worker_records(root, plan_id) if plan_id else []
    verification = _load_verification_payload(root, plan_id) if plan_id else {}
    pacer_evidence = _build_pacer_evidence(root, mission, worker_records=worker_records, verification=verification)
    return {
        "mission": mission,
        "rounds": rounds,
        "final_report": final_report,
        "review": review,
        "can_merge": bool(decorated.get("can_merge")),
        "merge_state": str(decorated.get("merge_state") or ""),
        "live_logs": live_logs,
        "worker_records": worker_records,
        "verification": verification,
        "pacer_evidence": pacer_evidence,
    }


def _read_tail(path: Path, *, max_chars: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    return text[-max_chars:]


def _mission_live_logs(workspace_root: Path, mission: dict[str, Any]) -> dict[str, Any]:
    plan_id = str(mission.get("plan_id") or "").strip()
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
        entries.append({
            "path": key,
            "name": path.name,
            "modified": modified,
            "tail": tail,
        })
    entries.sort(key=lambda item: float(item.get("modified") or 0.0), reverse=True)
    latest = entries[0] if entries else {}
    return {
        "count": len(entries),
        "latest_path": latest.get("path", ""),
        "latest_key": f"{latest.get('path', '')}:{latest.get('modified', '')}:{len(str(latest.get('tail') or ''))}" if latest else "",
        "latest_tail": latest.get("tail", ""),
        "entries": entries[:6],
    }


def _load_verification_payload(workspace_root: Path, plan_id: str) -> dict[str, Any]:
    from ..chief_plans_store import load_verification

    payload = load_verification(workspace_root, plan_id)
    return payload if isinstance(payload, dict) else {}


def _build_pacer_evidence(
    workspace_root: Path,
    mission: dict[str, Any],
    *,
    worker_records: list[dict[str, Any]],
    verification: dict[str, Any],
) -> dict[str, Any]:
    mission_id = str(mission.get("mission_id") or "").strip()
    latest_worker = worker_records[-1] if worker_records else {}
    backend = latest_worker.get("backend") if isinstance(latest_worker.get("backend"), dict) else {}
    log_path = str(latest_worker.get("log_path") or "").strip()
    report_path = workspace_root / "missions" / mission_id / "final_report.md" if mission_id else Path()
    command = ""
    if isinstance(verification.get("command_verification"), dict):
        command = str(verification["command_verification"].get("command") or "").strip()
    if not command:
        command = str(verification.get("command") or "").strip()
    return {
        "worker_status": str(latest_worker.get("status") or ""),
        "worker_command": str(latest_worker.get("command") or ""),
        "worker_exit_code": latest_worker.get("exit_code"),
        "worker_seconds": latest_worker.get("elapsed_seconds"),
        "worker_records": len(worker_records),
        "agent": str(latest_worker.get("agent") or mission.get("agent") or ""),
        "backend": str(backend.get("name") or ""),
        "model": str(backend.get("model") or ""),
        "worktree": str(latest_worker.get("cwd") or ""),
        "log_path": log_path,
        "log_tail": _read_tail(Path(log_path), max_chars=1600) if log_path else "",
        "verification_verdict": str(verification.get("verdict") or ""),
        "verification_command": command,
        "verification_path": str(plan_dir(workspace_root, str(mission.get("plan_id") or "")) / "verification.json") if mission.get("plan_id") else "",
        "merge_state": str(mission.get("merge_state") or ""),
        "final_report": str(report_path) if report_path else "",
    }


# ---------------------------------------------------------------------------
# Mission start (workbench form)
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
    from ..verification_profiles import resolve_test_command
    from ..mission_pipeline import MissionPipeline, SpecValidator, mission_result_to_pipeline_state
    from ..mission_intake import is_manual_verification_goal, is_review_plan_goal

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
        state_path=str(pipeline.path),
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    def _run() -> None:
        from ..chief_run import run_chief_mission
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
                saved_mission = load_mission(workspace_root, mission_id) or {}
                if saved_mission:
                    saved_mission["test_command"] = resolved_test_command or requested_test_command or ""
                    saved_mission["agent"] = str(agent or "")
                    saved_mission["merge_policy"] = str(merge_policy or "manual")
                    save_mission(workspace_root, saved_mission)
                pipeline.attach_mission(pipeline_state, mission_id)
            background = None
            if effective_execute and mission_id and status in {"preview", "created"}:
                from ..chief_background import start_background_chief_run
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
        "allow_dirty": bool(allow_dirty),
        "state_path": str(pipeline.path),
        "spec": normalized_spec,
    }
