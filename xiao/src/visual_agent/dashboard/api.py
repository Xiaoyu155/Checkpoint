"""API endpoint handlers for the dashboard.

Each function handles one /api/* endpoint and returns a dict for JSON serialization.
The HTTP layer in server.py delegates here.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data import (
    _LAUNCHES,
    _LAUNCH_LOCK,
    build_dashboard_data_cached as build_dashboard_data_cached,
    invalidate_dashboard_data_cache,
    worker_status,
)
from ..chief_plans_store import load_plan, load_worker_records, plan_dir
from ..commercial_config import commercial_config_from_payload, load_commercial_config, save_commercial_config as save_commercial_config_file
from ..goal_intake import intake_dialogue_lines, refine_goal
from ..mission_contract import normalize_requirement_contract
from ..mission_progress import build_mission_progress
from ..missions import list_missions, load_mission, save_mission
from ..programs import list_programs, load_program
from ..pacer_support import build_pacer_support_snapshot
from ..pacer_pillars import assess_pillar
from ..subprocess_window import hidden_subprocess_kwargs
from ..user_profile import load_user_profile, profile_from_payload, save_user_profile
from ..workbench_board import attach_board_fields, mission_review_payload
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
_workspace_operation_lock = threading.Lock()


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
    if not p.is_dir():
        return {"ok": False, "error": f"路径不存在: {p}"}
    if p.name != ".agent-workspace":
        return {"ok": False, "error": f"不是有效的工作空间目录（应以 .agent-workspace 结尾）: {p}"}
    with _workspace_operation_lock:
        state = worker_status()
        if state.get("running"):
            worker_root = str(state.get("workspace_root") or "")
            with _active_workspace_lock:
                current = _active_workspace
            effective_worker_root = Path(worker_root).resolve() if worker_root else current
            if effective_worker_root is None or effective_worker_root != p:
                return {
                    "ok": False,
                    "error": "当前项目的 Worker 仍在运行，请先停止 Worker 再切换项目。",
                    "worker_workspace": str(effective_worker_root or ""),
                }
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
    workspace_path = Path(workspace_root).expanduser().resolve()
    with _workspace_operation_lock:
        with _active_workspace_lock:
            current = _active_workspace
        if current is not None and current != workspace_path:
            return {"ok": False, "error": "项目已切换，请刷新页面后再启动 Worker。"}
        with _data_mod._WORKER_LOCK:
            if _data_mod._WORKER_PROC is not None and _data_mod._WORKER_PROC.poll() is None:
                return {
                    "ok": False,
                    "error": "Worker 已在运行中",
                    "pid": _data_mod._WORKER_PROC.pid,
                    "workspace_root": str(_data_mod._WORKER_WORKSPACE or ""),
                }
            cmd = [
                sys.executable,
                "-m",
                "visual_agent.cli",
                "mission",
                "worker",
                "--watch",
                "--workspace-root",
                str(workspace_path),
            ]
            try:
                _data_mod._WORKER_PROC = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    **hidden_subprocess_kwargs(detached=True),
                )
                _data_mod._WORKER_WORKSPACE = workspace_path
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "pid": _data_mod._WORKER_PROC.pid, "workspace_root": str(workspace_path)}


def stop_worker() -> dict[str, Any]:
    import visual_agent.dashboard.data as _data_mod
    with _workspace_operation_lock:
        with _data_mod._WORKER_LOCK:
            if _data_mod._WORKER_PROC is None or _data_mod._WORKER_PROC.poll() is not None:
                _data_mod._WORKER_PROC = None
                _data_mod._WORKER_WORKSPACE = None
                return {"ok": True, "was_running": False}
            _data_mod._WORKER_PROC.terminate()
            try:
                _data_mod._WORKER_PROC.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _data_mod._WORKER_PROC.kill()
            _data_mod._WORKER_PROC = None
            _data_mod._WORKER_WORKSPACE = None
            return {"ok": True, "was_running": True}


# ---------------------------------------------------------------------------
# Mission retry
# ---------------------------------------------------------------------------

_retry_lock = threading.Lock()


def retry_mission(workspace_root: Path, mission_id: str) -> dict[str, Any]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    with _retry_lock:
        mission = load_mission(workspace_path, mission_id) or {}
        if not mission:
            return {"ok": False, "error": "找不到该 mission"}
        status = str(mission.get("status") or "")
        if status not in {"stopped", "failed"}:
            return {"ok": False, "error": f"只有已停止或失败的任务才能重试，当前状态：{status or '未知'}"}
        goal = str(mission.get("objective") or "").strip()
        if not goal:
            return {"ok": False, "error": "原任务没有记录目标文本"}
        repo_root = str(mission.get("repo_root") or workspace_path.parent)
        agent = str(mission.get("agent") or "")
        test_command = str(mission.get("test_command") or "")
        merge_policy = str(mission.get("merge_policy") or "manual")
        stop_reason = str(mission.get("stop_reason") or "")
        mission["status"] = "retrying"
        mission["stop_reason"] = ""
        save_mission(workspace_path, mission)

    try:
        result = start_workbench_mission(
            workspace_root=workspace_path,
            repo_root=repo_root,
            goal=goal,
            test_command=test_command,
            agent=agent,
            execute=True,
            merge_policy=merge_policy,
            intake=mission.get("requirement_contract") if isinstance(mission.get("requirement_contract"), dict) else None,
        )
    except Exception:
        with _retry_lock:
            mission["status"] = status
            mission["stop_reason"] = stop_reason
            save_mission(workspace_path, mission)
        raise

    with _retry_lock:
        if result.get("ok"):
            mission["status"] = "retried"
            mission["retry_launch_id"] = str(result.get("launch_id") or "")
            mission["retried_at"] = datetime.now(timezone.utc).isoformat()
        else:
            mission["status"] = status
            mission["stop_reason"] = stop_reason
        save_mission(workspace_path, mission)
        invalidate_dashboard_data_cache(workspace_path)
    return result


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
    effective_use_model = use_model
    intake_policy = "local_rules"
    if llm:
        kwargs = {
            "model_id": f"{llm['provider']}:{llm['model']}",
            "api_key": llm["api_key"],
            "base_url": llm["base_url"],
            "endpoint": "/chat/completions",
            "max_tokens": 800,
        }
        intake_policy = "selected_agent_model"
    elif use_model and agent:
        # Product rule: if the user selected a coding agent, do not silently
        # substitute another cheap backend for requirement intake.
        cli_result = _refine_goal_with_selected_agent_cli(
            goal,
            answers=answers,
            agent=agent,
            repo_root=str(payload.get("repo_root") or ""),
            test_command=str(payload.get("test_command") or ""),
        )
        if cli_result.get("ok"):
            result = dict(cli_result["result"])
            result["answers"] = answers
            result["agent"] = agent
            result["intake_policy"] = "selected_agent_cli"
            result["dialogue_lines"] = intake_dialogue_lines(result, answers=answers)
            result["ok"] = True
            return result
        effective_use_model = False
        intake_policy = "selected_agent_unavailable"
    elif use_model:
        intake_policy = "auto_backend"
    result = refine_goal(goal, answers=answers, enable_model=effective_use_model, **kwargs)
    if intake_policy == "selected_agent_unavailable":
        result["model_unavailable"] = True
        result["model_error"] = f"selected_agent_unavailable_for_intake:{agent}"
    result["answers"] = answers
    result["agent"] = agent
    result["intake_policy"] = intake_policy
    dialogue_lines = intake_dialogue_lines(result, answers=answers)
    if intake_policy == "selected_agent_unavailable":
        dialogue_lines.append(f"当前选择的编码 Agent `{agent}` 暂不能用于目标收口；本轮只用本地规则整理问题，未切换到其他模型。")
    result["dialogue_lines"] = dialogue_lines
    result["ok"] = True
    return result


def _refine_goal_with_selected_agent_cli(
    goal: str,
    *,
    answers: list[str],
    agent: str,
    repo_root: str = "",
    test_command: str = "",
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    from ..agent_capabilities import canonical_agent_name
    from ..chief_engineer import assess_goal_clarity

    canonical = canonical_agent_name(agent)
    command = _selected_agent_intake_command(canonical)
    if command is None:
        return {"ok": False, "reason": "selected_agent_cli_unsupported", "agent": agent}
    prompt = _selected_agent_intake_prompt(
        goal,
        answers=answers,
        agent=agent,
        repo_root=repo_root,
        test_command=test_command,
    )
    argv = list(command["argv"])
    stdin_text = command.get("stdin_text")
    if stdin_text == "<prompt>":
        stdin_text = prompt
    else:
        argv = [prompt if item == "<prompt>" else item for item in argv]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            input=stdin_text,
            text=True,
            timeout=timeout_seconds,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "selected_agent_intake_timeout", "agent": agent}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "reason": "selected_agent_intake_launch_error", "agent": agent, "error": str(exc)}
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        return {
            "ok": False,
            "reason": "selected_agent_intake_failed",
            "agent": agent,
            "exit_code": completed.returncode,
            "output_tail": output[-500:],
        }
    try:
        parsed = _parse_selected_agent_intake_json(output)
    except ValueError as exc:
        return {
            "ok": False,
            "reason": "selected_agent_intake_invalid_json",
            "agent": agent,
            "error": str(exc),
            "output_tail": output[-500:],
        }
    clarity = assess_goal_clarity(goal, answers=answers)
    questions = [str(q).strip() for q in (parsed.get("clarifying_questions") or []) if str(q).strip()][:3]
    suggested = str(parsed.get("suggested_goal") or "").strip() or goal
    return {
        "ok": True,
        "result": {
            "source": "selected_agent_cli",
            "model_id": f"{canonical}:cli",
            "input_goal": goal,
            "already_clear": bool(clarity["ok"]) and not questions,
            "clarifying_questions": questions,
            "suggested_goal": suggested,
            "acceptance_hint": str(parsed.get("acceptance_hint") or "").strip(),
            "clarity": clarity,
        },
    }


def _selected_agent_intake_command(canonical: str) -> dict[str, Any] | None:
    if canonical == "codex":
        found = shutil.which("codex")
        if not found:
            return None
        argv = [found, "exec", "--sandbox", "read-only", "-"]
        if found.lower().endswith((".cmd", ".bat")):
            argv = ["cmd", "/c", found, "exec", "--sandbox", "read-only", "-"]
        return {"argv": argv, "stdin_text": "<prompt>"}
    if canonical == "claude-code":
        found = shutil.which("claude")
        if not found:
            return None
        argv = [found, "-p", "<prompt>", "--permission-mode", "plan"]
        if found.lower().endswith((".cmd", ".bat")):
            argv = ["cmd", "/c", found, "-p", "<prompt>", "--permission-mode", "plan"]
        return {"argv": argv}
    if canonical == "gemini":
        found = shutil.which("gemini")
        if not found:
            return None
        argv = [found, "-p", "<prompt>"]
        if found.lower().endswith((".cmd", ".bat")):
            argv = ["cmd", "/c", found, "-p", "<prompt>"]
        return {"argv": argv}
    return None


def _selected_agent_intake_prompt(
    goal: str,
    *,
    answers: list[str],
    agent: str,
    repo_root: str,
    test_command: str,
) -> str:
    lines = [
        "You are the selected coding agent inside Pacer, but this turn is intake only.",
        "Do not edit files, do not run commands, and do not change the workspace.",
        "Your job is to clarify the user's development request enough for an autonomous coding mission.",
        "Return STRICT JSON only, no Markdown, with keys:",
        '- "clarifying_questions": array of at most 3 short questions; empty if clear.',
        '- "suggested_goal": one precise, self-contained rewrite in the user language.',
        '- "acceptance_hint": one short sentence describing how Pacer should verify completion.',
        "",
        f"Selected agent: {agent}",
        f"Project directory: {repo_root or '(not specified)'}",
        f"Known verification command: {test_command or '(not specified)'}",
        "",
        "Rough goal:",
        goal,
    ]
    if answers:
        lines.extend(["", "User already answered:"])
        lines.extend(f"- {item}" for item in answers if str(item).strip())
    return "\n".join(lines)


def _parse_selected_agent_intake_json(output: str) -> dict[str, Any]:
    raw = str(output or "").strip()
    if not raw:
        raise ValueError("empty agent output")
    candidates = [raw]
    try:
        outer = json.loads(raw)
        if isinstance(outer, dict):
            if _looks_like_intake_payload(outer):
                return outer
            for key in ("result", "reply", "text", "content", "message", "output"):
                value = outer.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
    except ValueError:
        pass
    for candidate in candidates:
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not match:
            continue
        try:
            parsed = json.loads(match.group(0))
        except ValueError:
            continue
        if isinstance(parsed, dict) and _looks_like_intake_payload(parsed):
            return parsed
    raise ValueError("agent output did not contain an intake JSON object")


def _looks_like_intake_payload(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in ("clarifying_questions", "suggested_goal", "acceptance_hint"))


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
    invalidate_dashboard_data_cache()
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
# Local user profile
# ---------------------------------------------------------------------------

def get_user_profile() -> dict[str, Any]:
    return {"ok": True, **load_user_profile().to_public_dict()}


def save_profile_config(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        profile = profile_from_payload(payload)
        path = save_user_profile(profile)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"保存失败：{exc}"}
    invalidate_dashboard_data_cache()
    return {
        "ok": True,
        "message": "本地邮箱身份已保存",
        "path": str(path),
        **profile.to_public_dict(),
    }


# ---------------------------------------------------------------------------
# Commercial auth and billing configuration
# ---------------------------------------------------------------------------

def get_commercial_config() -> dict[str, Any]:
    return {"ok": True, **load_commercial_config().to_dict(redact=True)}


def save_commercial_config(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        existing = load_commercial_config()
        config = commercial_config_from_payload(payload, existing=existing)
        path = save_commercial_config_file(config, existing=existing)
    except OSError as exc:
        return {"ok": False, "error": f"保存失败：{exc}"}
    invalidate_dashboard_data_cache()
    return {
        "ok": True,
        "message": "登录与付费配置已保存",
        "path": str(path),
        **config.to_dict(redact=True),
    }


# ---------------------------------------------------------------------------
# Workbench model API configuration
# ---------------------------------------------------------------------------

def get_model_config() -> dict[str, Any]:
    config = load_workbench_model_config()
    return {
        "ok": True,
        "enabled": config.configured,
        "base_url": config.base_url,
        "api_key": "****" if config.api_key else "",
        "api_key_configured": bool(config.api_key),
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "monthly_budget_usd": config.monthly_budget_usd,
        "per_mission_budget_usd": config.per_mission_budget_usd,
        "auto_switch_quota_percent": config.auto_switch_quota_percent,
        "budget_guard_configured": config.budget_guard_configured,
        "configured": config.configured,
        "summary": redacted_config_summary(config),
    }


def save_model_config(payload: dict[str, Any]) -> dict[str, Any]:
    enabled = bool(payload.get("enabled", True))
    config = _model_config_from_payload(payload)
    if not enabled:
        return {"ok": False, "error": "关闭优先使用 sub2api 后不会删除已有配置；如需停用，请在 Agent 下拉框选择 codex 或 claude-code。"}
    try:
        path = save_workbench_model_config(config)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    invalidate_dashboard_data_cache()
    return {
        "ok": True,
        "message": "总工作台模型接口已保存，新的任务会优先显示 bugteam（低成本）。",
        "path": str(path),
        "summary": redacted_config_summary(config),
    }


def test_model_config(payload: dict[str, Any]) -> dict[str, Any]:
    config = _model_config_from_payload(payload)
    result = probe_workbench_model_config(config)
    return {"ok": bool(result.get("ok")), **result}


def _model_config_from_payload(payload: dict[str, Any]) -> WorkbenchModelConfig:
    existing = load_workbench_model_config()
    api_key = str(payload.get("api_key") or "").strip()
    if api_key == "****":
        api_key = existing.api_key
    return WorkbenchModelConfig(
        base_url=str(payload.get("base_url") or "").strip(),
        api_key=api_key,
        model=str(payload.get("model") or "").strip(),
        reasoning_effort=str(payload.get("reasoning_effort") or "").strip(),
        monthly_budget_usd=_float_payload(payload, "monthly_budget_usd", 0.0),
        per_mission_budget_usd=_float_payload(payload, "per_mission_budget_usd", 0.0),
        auto_switch_quota_percent=_float_payload(payload, "auto_switch_quota_percent", 80.0),
    )


def _float_payload(payload: dict[str, Any], key: str, default: float) -> float:
    raw = payload.get(key)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


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

    worker = worker_status(workspace_root)

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "product": "Pacer",
        "platform": f"{platform.system()} {platform.release()}",
        "python": sys.version.split()[0],
        "workspace_root": str(workspace_root),
        "workspace_exists": workspace_root.exists(),
        "agents": agents,
        "worker_running": bool(worker.get("running") and worker.get("active_for_workspace")),
        "worker": worker,
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


def build_five_pillars_data(workspace_root: str | Path) -> dict[str, Any]:
    """Build the five-pillar view from durable Program and dispatch records."""
    root = Path(workspace_root).expanduser().resolve()
    support = build_pacer_support_snapshot(root)
    memory = support.get("memory") if isinstance(support.get("memory"), dict) else {}
    commands = support.get("commands") if isinstance(support.get("commands"), dict) else {}
    if int(memory.get("total_outcomes") or 0) or int(commands.get("total_runs") or 0):
        return _native_five_pillars_data(root, support)
    candidates: list[dict[str, Any]] = []
    for summary in list_programs(root):
        program_id = str(summary.get("program_id") or "")
        program = load_program(root, program_id) if program_id else None
        tasks = program.get("tasks") if isinstance(program, dict) and isinstance(program.get("tasks"), list) else []
        if (
            isinstance(program, dict)
            and str(program.get("status") or "") == "completed"
            and len(tasks) >= 2
            and all(str(task.get("status") or "") == "verified" for task in tasks if isinstance(task, dict))
        ):
            candidates.append(program)
    candidates.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
    if not candidates:
        return {
            "ok": True,
            "workspace_root": str(root),
            "program": None,
            "missions": [],
            "pillars": [],
            "support": support,
        }

    program = candidates[0]
    task_rows = [task for task in program.get("tasks") or [] if isinstance(task, dict)][:2]
    missions = [_five_pillars_mission(root, task) for task in task_rows]
    closed_loop = ((program.get("autonomy_policy") or {}).get("closed_loop") or {})
    providers = _unique_text(row.get("provider") for row in missions)
    models = _unique_text(row.get("model") for row in missions)
    verdicts = _unique_text(row.get("verification_verdict") for row in missions)
    commands = _unique_text(row.get("verification_command") for row in missions)
    upstream_id = str(task_rows[0].get("mission_id") or "") if task_rows else ""
    recalled_ids = [
        memory_id
        for row in missions[1:]
        for memory_id in row.get("memory_ids") or []
        if memory_id == f"mission:{upstream_id}"
    ]
    summary = {
        "program_id": str(program.get("program_id") or ""),
        "status": str(program.get("status") or ""),
        "objective": str(program.get("objective") or ""),
        "source_plan": str(program.get("source_plan") or ""),
        "source_plan_sha256": str(program.get("source_plan_sha256") or closed_loop.get("source_plan_sha256") or ""),
        "provider": ", ".join(providers),
        "model": ", ".join(models),
        "worker_count": sum(int(row.get("worker_count") or 0) for row in missions),
        "repair_count": sum(int(row.get("repair_count") or 0) for row in missions),
        "verification_verdict": ", ".join(verdicts),
        "verification_command": " | ".join(commands),
        "upstream_memory_id": recalled_ids[0] if recalled_ids else "",
        "task_sequence": [
            {
                "task_id": str(task.get("task_id") or ""),
                "mission_id": str(task.get("mission_id") or ""),
                "status": str(task.get("status") or ""),
            }
            for task in task_rows
        ],
    }
    all_verified = len(task_rows) >= 2 and all(str(task.get("status") or "") == "verified" for task in task_rows)
    all_passed = bool(verdicts) and all(value == "pass" for value in verdicts)
    pillars = [
        _pillar("routing", "Codex 路由", bool(providers and models), f"{summary['provider']} / {summary['model']}", "中转 provider 与模型来自真实 dispatch 审计"),
        _pillar("memory", "本地记忆", bool(recalled_ids), summary["upstream_memory_id"] or "未召回直接上游", "阶段 2实际可见的上游 mission memory ID"),
        _pillar("managed", "托管开发", all_verified, f"{len(task_rows)} 个任务已验收", "依赖任务按顺序自动创建、执行并收口"),
        _pillar("acceptance", "强验收", all_passed, summary["verification_verdict"] or "无验收结论", summary["verification_command"] or "无验收命令"),
        _pillar("dogfood", "路线锁定", bool(summary["source_plan_sha256"]), summary["source_plan_sha256"] or "缺少路线 SHA", summary["source_plan"] or "缺少路线文件"),
    ]
    return {
        "ok": True,
        "workspace_root": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "program": summary,
        "missions": missions,
        "pillars": pillars,
        "support": support,
    }


def _native_five_pillars_data(root: Path, support: dict[str, Any]) -> dict[str, Any]:
    account = support.get("account") if isinstance(support.get("account"), dict) else {}
    runtime = support.get("runtime") if isinstance(support.get("runtime"), dict) else {}
    memory = support.get("memory") if isinstance(support.get("memory"), dict) else {}
    commands = support.get("commands") if isinstance(support.get("commands"), dict) else {}
    latest = memory.get("latest") if isinstance(memory.get("latest"), dict) else {}
    recent = support.get("recent_outcomes") if isinstance(support.get("recent_outcomes"), list) else []
    batch_run_id = str(latest.get("batch_run_id") or "")
    raw_verified_run_ids = commands.get("verified_run_ids")
    verified_run_id_rows = raw_verified_run_ids if isinstance(raw_verified_run_ids, list) else []
    verified_run_ids = {
        str(run_id)
        for run_id in verified_run_id_rows
        if str(run_id)
    }
    verification_errors = (
        latest.get("verification_errors")
        if isinstance(latest.get("verification_errors"), list)
        else []
    )
    verified_outcome = (
        str(latest.get("status") or "") == "completed"
        and str(latest.get("evidence_level") or "") == "verified_batch"
        and bool(batch_run_id)
        and batch_run_id in verified_run_ids
    )
    if verified_outcome:
        verification_evidence = f"最新 completed outcome 已绑定通过批次 {batch_run_id}"
    elif str(latest.get("status") or "") != "completed":
        verification_evidence = f"最新 outcome 状态为 {latest.get('status') or 'unknown'}，仅 completed 可通过"
    elif verification_errors:
        verification_evidence = (
            f"绑定批次 {batch_run_id or 'unknown'} 未通过可信验收校验："
            f"{verification_errors[0]}"
        )
    elif str(latest.get("evidence_level") or "") != "verified_batch":
        verification_evidence = (
            f"绑定批次 {batch_run_id} 没有可信 verified_batch 证据"
            if batch_run_id
            else "最新 completed outcome 没有 verified_batch 证据"
        )
    elif not batch_run_id:
        verification_evidence = "最新 completed outcome 没有绑定 batch_run_id"
    else:
        verification_evidence = f"绑定批次 {batch_run_id} 不存在、未执行或未通过"
    profile = support.get("profile") if isinstance(support.get("profile"), dict) else {}
    program = {
        "program_id": batch_run_id or "native-pacer",
        "status": str(latest.get("status") or "observed"),
        "objective": str(latest.get("goal") or "Pacer native development evidence"),
        "provider": str(runtime.get("provider") or "inherited"),
        "model": str(runtime.get("model") or "Codex default"),
        "worker_count": int(commands.get("total_runs") or 0),
        "repair_count": int(memory.get("failed_or_blocked") or 0),
        "verification_verdict": "passed" if verified_outcome else "evidence incomplete",
        "verification_command": str(latest.get("verification") or ""),
        "upstream_memory_id": f"{memory.get('total_outcomes') or 0} local outcomes",
        "source_plan": "Pacer native outcome ledger",
        "source_plan_sha256": "",
        "task_sequence": [
            {
                "task_id": f"outcome-{index + 1:03d}",
                "mission_id": str(item.get("batch_run_id") or "local-memory"),
                "status": str(item.get("status") or ""),
            }
            for index, item in enumerate(recent[:5])
            if isinstance(item, dict)
        ],
    }
    authenticated = bool(account.get("authenticated"))
    total_outcomes = int(memory.get("total_outcomes") or 0)
    verified_runs = int(commands.get("verified_runs") or 0)
    total_runs = int(commands.get("total_runs") or 0)
    completed = int(memory.get("completed") or 0)
    launches = support.get("launches") if isinstance(support.get("launches"), dict) else {}
    active_launch = launches.get("active") if isinstance(launches.get("active"), dict) else {}
    liveness = active_launch.get("liveness") if isinstance(active_launch.get("liveness"), dict) else {}
    program["lifecycle_status"] = str(active_launch.get("status") or program["status"])
    program["liveness_state"] = str(liveness.get("state") or "unknown")
    mechanical = active_launch.get("pillars") if isinstance(active_launch.get("pillars"), dict) else {}
    use_mechanical = all(isinstance(mechanical.get(name), dict) for name in ("routing", "memory", "managed", "acceptance", "dogfood"))

    def mechanical_active(name: str, fallback: bool) -> bool:
        active = bool(assess_pillar(name, mechanical[name]).get("passed")) if use_mechanical else fallback
        if name in {"managed", "acceptance", "dogfood"}:
            return active and verified_outcome and (fallback if name == "dogfood" else True)
        return active

    def mechanical_evidence(name: str, fallback: str) -> str:
        if not use_mechanical:
            return "legacy evidence: " + fallback
        item = mechanical[name]
        state = str(item.get("state") or "unknown")
        assessment = assess_pillar(name, item)
        assessment_status = str(assessment.get("status") or "indeterminate")
        reason_codes = ",".join(str(code) for code in assessment.get("reason_codes") or [])
        if name in {"managed", "acceptance", "dogfood"} and not verified_outcome:
            return (
                f"launch={active_launch.get('launch_id') or 'unknown'}; state={state}; "
                f"{verification_evidence}"
            )
        suffix = f"; reasons={reason_codes}" if reason_codes else ""
        return (
            f"launch={active_launch.get('launch_id') or 'unknown'}; state={state}; "
            f"assessment={assessment_status}{suffix}"
        )

    pillars = [
        _pillar(
            "routing",
            "Codex 运行身份",
            mechanical_active("routing", authenticated),
            f"{runtime.get('provider') or 'inherited'} / {runtime.get('model') or 'default'}",
            mechanical_evidence("routing", f"认证类型：{account.get('auth_method') or 'unknown'}；不会读取或展示凭证"),
        ),
        _pillar(
            "memory",
            "本地记忆",
            mechanical_active("memory", total_outcomes > 0),
            f"{total_outcomes} 条 outcome",
            mechanical_evidence("memory", str(memory.get("path") or "没有 native history")),
        ),
        _pillar(
            "managed",
            "托管开发",
            mechanical_active("managed", verified_outcome),
            f"{completed} 个完成记录 / {total_runs} 个命令批次",
            (
                f"批量执行累计 {float(commands.get('elapsed_seconds') or 0.0):.1f} 秒；"
                f"{mechanical_evidence('managed', verification_evidence)}"
            ),
        ),
        _pillar(
            "acceptance",
            "真实验收",
            mechanical_active("acceptance", verified_outcome),
            f"{verified_runs}/{total_runs} 批次通过可信验收",
            mechanical_evidence("acceptance", verification_evidence),
        ),
        _pillar(
            "dogfood",
            "开发纪律",
            mechanical_active("dogfood", verified_outcome and bool(latest.get("goal")) and bool(latest.get("summary"))),
            "有验收交接" if verified_outcome else "缺少机械路线证据",
            mechanical_evidence("dogfood", "已保存目标、结果和验收批次" if verified_outcome else verification_evidence),
        ),
    ]
    return {
        "ok": True,
        "mode": "native",
        "workspace_root": str(root),
        "generated_at": support.get("generated_at"),
        "program": program,
        "missions": recent,
        "pillars": pillars,
        "support": support,
        "active_launch": {
            "launch_id": str(active_launch.get("launch_id") or ""),
            "lifecycle_status": str(active_launch.get("status") or ""),
            "liveness": liveness,
            "task_trigger": active_launch.get("task_trigger")
            if isinstance(active_launch.get("task_trigger"), dict)
            else {},
            "trigger_diagnosis": active_launch.get("trigger_diagnosis")
            if isinstance(active_launch.get("trigger_diagnosis"), dict)
            else {},
            "task_lifecycle": active_launch.get("task_lifecycle")
            if isinstance(active_launch.get("task_lifecycle"), dict)
            else {},
            "task_generation": int(active_launch.get("task_generation") or 0),
        },
        "account_summary": {
            "codex_authenticated": authenticated,
            "auth_method": str(account.get("auth_method") or "none"),
            "profile_configured": bool(profile.get("configured")),
            "profile_email": str(profile.get("email") or ""),
        },
    }


def _five_pillars_mission(root: Path, task: dict[str, Any]) -> dict[str, Any]:
    mission_id = str(task.get("mission_id") or "")
    mission = load_mission(root, mission_id) or {}
    plan_id = str(mission.get("plan_id") or "")
    workers = load_worker_records(root, plan_id) if plan_id else []
    verification = _load_verification_payload(root, plan_id) if plan_id else {}
    command_result = verification.get("command_verification") if isinstance(verification.get("command_verification"), dict) else {}
    dispatches = _read_jsonl(plan_dir(root, plan_id) / "dispatches.jsonl") if plan_id else []
    dispatch = dispatches[-1] if dispatches else {}
    memory_usage = dispatch.get("project_memory_usage") if isinstance(dispatch.get("project_memory_usage"), dict) else {}
    return {
        "task_id": str(task.get("task_id") or ""),
        "mission_id": mission_id,
        "status": str(mission.get("status") or task.get("status") or ""),
        "plan_id": plan_id,
        "provider": str(dispatch.get("resolved_provider") or (workers[-1].get("resolved_provider") if workers else "")),
        "model": str(dispatch.get("resolved_model") or (workers[-1].get("resolved_model") if workers else "")),
        "worker_count": int(dispatch.get("worker_attempts") or len(workers)),
        "repair_count": int(dispatch.get("repair_rounds") or 0),
        "verification_verdict": str(verification.get("verdict") or command_result.get("verdict") or ""),
        "verification_command": str(command_result.get("command") or mission.get("test_command") or task.get("test_command") or ""),
        "memory_ids": list(memory_usage.get("dispatch_memory_ids") or memory_usage.get("injected_memory_ids") or []),
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _unique_text(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _pillar(pillar_id: str, title: str, passed: bool, metric: str, evidence: str) -> dict[str, str]:
    return {"id": pillar_id, "title": title, "status": "passed" if passed else "attention", "metric": metric, "evidence": evidence}


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
    progress = build_mission_progress(
        workspace_root=root,
        mission=mission,
        rounds=rounds,
        worker_records=worker_records,
        verification=verification,
    ) if mission else {}
    pacer_evidence = _build_pacer_evidence(root, mission, worker_records=worker_records, verification=verification)
    if progress:
        pacer_evidence["progress"] = progress
        pacer_evidence["stage"] = progress.get("stage", "")
        pacer_evidence["changed_product_file_count"] = progress.get("changed_product_file_count", 0)
        for key in ("activity", "activity_label", "activity_command", "activity_elapsed_seconds"):
            pacer_evidence[key] = progress.get(key)
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
        "progress": progress,
        "pacer_evidence": pacer_evidence,
        "activity": pacer_evidence.get("activity", ""),
        "activity_label": pacer_evidence.get("activity_label", ""),
        "activity_command": pacer_evidence.get("activity_command", ""),
        "activity_elapsed_seconds": pacer_evidence.get("activity_elapsed_seconds"),
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
    plan_id = str(mission.get("plan_id") or "").strip()
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
    plan = load_plan(workspace_root, plan_id) if plan_id else None
    plan = plan if isinstance(plan, dict) else {}
    verification_mode = str(plan.get("verification_mode") or "").strip()
    if not verification_mode:
        verification_mode = "command" if command else "workflow"
    return {
        "worker_status": str(latest_worker.get("status") or ""),
        "worker_command": str(latest_worker.get("command") or ""),
        "worker_exit_code": latest_worker.get("exit_code"),
        "worker_seconds": latest_worker.get("elapsed_seconds"),
        "worker_records": len(worker_records),
        "agent": str(latest_worker.get("agent") or mission.get("agent") or ""),
        "backend": str(backend.get("name") or ""),
        "model": str(latest_worker.get("resolved_model") or backend.get("model") or ""),
        "reasoning_effort": str(latest_worker.get("resolved_reasoning_effort") or ""),
        "dispatch_mode": str(latest_worker.get("dispatch_mode") or mission.get("dispatch_mode") or "tracked"),
        "worktree": str(latest_worker.get("cwd") or ""),
        "log_path": log_path,
        "log_tail": _read_tail(Path(log_path), max_chars=1600) if log_path else "",
        "verification_verdict": str(verification.get("verdict") or ""),
        "verification_command": command,
        "verification_mode": verification_mode,
        "verification_path": str(plan_dir(workspace_root, plan_id) / "verification.json") if plan_id else "",
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
    dispatch_mode: str = "tracked",
    execute: bool,
    merge_policy: str = "manual",
    allow_dirty: bool = False,
    spec: dict[str, Any] | None = None,
    intake: dict[str, Any] | None = None,
    answers: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    goal = str(goal or "").strip()
    if not goal:
        return {"ok": False, "error": "请填写目标（goal）。"}
    dispatch_mode = str(dispatch_mode or "tracked").strip().lower()
    if dispatch_mode not in {"tracked", "delegated"}:
        return {"ok": False, "error": "派工模式必须是 tracked 或 delegated。"}
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
    requirement_contract = normalize_requirement_contract(
        intake,
        goal=goal,
        answers=answers or (),
        repo_root=repo_root or ".",
        test_command=resolved_test_command or requested_test_command,
        agent=agent,
    )
    validator = SpecValidator()
    normalized_spec = validator.validate(spec) if spec is not None else validator.derive_request_spec(
        goal=goal,
        repo_root=repo_root or ".",
        test_command=resolved_test_command or requested_test_command,
        agent=agent,
        execute=bool(execute),
    )
    if requirement_contract:
        normalized_spec["requirement_contract"] = requirement_contract
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
            "dispatch_mode": str(dispatch_mode or "tracked"),
            "merge_policy": str(merge_policy or "manual"),
            "requirement_contract": requirement_contract,
        },
    )
    record_launch(
        launch_id,
        workspace_root=str(Path(workspace_root).expanduser().resolve()),
        goal=goal,
        execute=bool(execute),
        test_command=resolved_test_command or "",
        verification_profile=verification_profile or {},
        requirement_contract=requirement_contract,
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
                requirement_contract=requirement_contract,
                dispatch_mode=str(dispatch_mode or "tracked"),
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
                    saved_mission["dispatch_mode"] = str(dispatch_mode or "tracked")
                    if requirement_contract:
                        saved_mission["requirement_contract"] = requirement_contract
                    save_mission(workspace_root, saved_mission)
                pipeline.attach_mission(pipeline_state, mission_id)
            background = None
            if effective_execute and mission_id and status in {"preview", "created"}:
                from ..chief_background import start_background_chief_run
                background = start_background_chief_run(
                    workspace_root=workspace_root,
                    mission_id=mission_id,
                    agents=((agent,) if agent else ()),
                    run_profile="supervised",
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
        "dispatch_mode": str(dispatch_mode or "tracked"),
        "test_command": resolved_test_command or "",
        "verification_profile": verification_profile or {},
        "manual_verification": manual_verification,
        "review_plan": review_plan,
        "allow_dirty": bool(allow_dirty),
        "requirement_contract": requirement_contract,
        "state_path": str(pipeline.path),
        "spec": normalized_spec,
    }
