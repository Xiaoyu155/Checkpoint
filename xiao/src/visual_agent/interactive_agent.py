from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .simple_task import run_simple_managed_task, simple_result_to_markdown


TaskRunner = Callable[..., dict[str, Any]]


def run_interactive_agent(
    *,
    repo_root: str | Path = ".",
    workspace_root: str | Path = ".agent-workspace",
    task_runner: TaskRunner = run_simple_managed_task,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], Any] = print,
) -> int:
    repo = Path(repo_root).expanduser().resolve()
    workspace = Path(workspace_root).expanduser().resolve()
    session_path = _session_path(workspace)
    last_goal = ""
    last_program_id = ""
    codex_provider = "inherit"
    output_func(f"Pacer Agent · {repo}")
    output_func("直接描述开发任务；输入 /帮助 查看命令。")
    while True:
        try:
            raw = input_func("Pacer> ")
        except EOFError:
            output_func("")
            return 0
        except KeyboardInterrupt:
            output_func("\n已退出 Pacer Agent。")
            return 130
        message = str(raw or "").strip()
        if not message:
            continue
        normalized = message.lower()
        if normalized in {"/退出", "/exit", "exit", "quit", "退出"}:
            output_func("已退出 Pacer Agent。")
            return 0
        if normalized in {"/帮助", "/help", "help"}:
            output_func(
                "/状态  查看最近任务\n"
                "/provider  查看底层通道\n"
                "/provider subscription  使用 Codex 登录订阅\n"
                "/provider relay <id>  使用 Codex 中已配置的中转 provider\n"
                "/provider inherit  沿用 Codex 当前配置\n"
                "/退出  结束会话\n"
                "其他内容会作为新的托管开发任务。"
            )
            continue
        if normalized in {"/状态", "/status", "status", "状态"}:
            output_func(_status_text(workspace, last_program_id))
            continue
        if normalized == "/provider":
            output_func(_provider_text(codex_provider))
            continue
        if normalized.startswith("/provider "):
            selected, error = _parse_provider_command(message)
            if error:
                output_func(error)
            else:
                codex_provider = selected
                output_func(_provider_text(codex_provider))
                _append_session_event(session_path, {"type": "provider_selected", "provider": codex_provider})
            continue

        conversational = _conversational_response(message, repo)
        if conversational:
            output_func(conversational)
            _append_session_event(
                session_path,
                {"type": "assistant_response", "message": message, "response": conversational},
            )
            continue

        effective_goal = _with_conversation_context(message, last_goal, last_program_id)
        _append_session_event(session_path, {"type": "user_task", "message": message, "effective_goal": effective_goal})
        output_func("正在检查目标、项目和验收条件。")
        try:
            payload = task_runner(
                effective_goal,
                repo_root=repo,
                workspace_root=workspace,
                codex_provider=codex_provider,
                progress_func=output_func,
            )
        except KeyboardInterrupt:
            output_func("任务已中断；运行记录仍保留在本地工作空间。")
            continue
        if payload.get("status") != "needs_input":
            last_goal = message
            last_program_id = str(payload.get("program_id") or "")
        _append_session_event(
            session_path,
            {"type": "task_result", "message": message, "program_id": last_program_id, "status": payload.get("status")},
        )
        output_func(simple_result_to_markdown(payload))


def _with_conversation_context(message: str, last_goal: str, last_program_id: str) -> str:
    continuation = message.lower().startswith(("继续", "接着", "再", "重试", "修一下", "然后", "continue", "retry"))
    if not continuation or not last_goal:
        return message
    context = f"上一任务：{last_goal}。"
    if last_program_id:
        context += f" 上一 Program：{last_program_id}。"
    return f"{message} {context}请结合本地记忆中的上一任务证据继续开发。"


def _conversational_response(message: str, repo: Path) -> str:
    text = str(message or "").strip()
    lower = text.lower()
    asks_capability = (
        (text.endswith(("吗", "吗？", "么", "么？", "?", "？")) or lower.startswith(("can ", "could ", "are you able")))
        and any(
            token in lower
            for token in ("可以", "能不能", "能否", "会不会", "开发", "优化", "修改", "页面", "can ", "could ")
        )
    )
    if not asks_capability:
        return ""
    return (
        "可以。我会先确认具体项目、页面和完成标准，再创建托管任务，不会把询问句直接当成开发命令。\n"
        f"当前目录：{repo}\n"
        "请告诉我：项目路径或名称、要优化的页面/板块，以及你希望最终看到的效果。"
    )


def _parse_provider_command(message: str) -> tuple[str, str]:
    parts = message.split()
    if len(parts) == 2 and parts[1].lower() == "subscription":
        return "openai", ""
    if len(parts) == 2 and parts[1].lower() == "inherit":
        return "inherit", ""
    if len(parts) == 3 and parts[1].lower() == "relay":
        provider_id = parts[2].strip()
        if provider_id and all(char.isalnum() or char in "_.-" for char in provider_id):
            return provider_id, ""
    return "", "用法：/provider subscription | /provider relay <provider-id> | /provider inherit"


def _provider_text(provider: str) -> str:
    if provider == "openai":
        return "底层通道：Codex 用户订阅（openai provider）"
    if provider == "inherit":
        return "底层通道：沿用 Codex 当前配置（订阅或中转）"
    return f"底层通道：Codex 中转 provider {provider}"


def _session_path(workspace: Path) -> Path:
    now = datetime.now(timezone.utc)
    directory = workspace / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"agent-{now.strftime('%Y%m%d-%H%M%S')}.jsonl"


def _append_session_event(path: Path, payload: dict[str, Any]) -> None:
    entry = {"recorded_at": datetime.now(timezone.utc).isoformat(), **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _status_text(workspace: Path, last_program_id: str) -> str:
    from .programs import list_programs, load_program

    program_id = last_program_id
    if not program_id:
        programs = list_programs(workspace)
        program_id = str(programs[0].get("program_id") or "") if programs else ""
    if not program_id:
        return "当前会话还没有开发任务。"
    program = load_program(workspace, program_id)
    if not program:
        return f"Program {program_id} 的记录不存在。"
    tasks = [item for item in program.get("tasks") or [] if isinstance(item, dict)]
    task_text = ", ".join(f"{item.get('task_id')}={item.get('status')}" for item in tasks)
    return f"Program {program_id}: {program.get('status')}\n{task_text}"
