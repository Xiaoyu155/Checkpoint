"""Pacer CMD session: plain chat + slash menu. No dashboard required."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .simple_task import run_simple_managed_task, simple_result_to_markdown


TaskRunner = Callable[..., dict[str, Any]]

MENU_TEXT = """\
【菜单】直接打 /命令 即可（不用面板）

  /帮助  /菜单     看这份说明
  /状态  /进度     最近任务做到哪了（人话）
  /托管            托管仪表（额度/是否可过夜/任务统计）
  /任务            最近几条任务列表
  /测试            这个项目用什么测试命令
  /验收            现在就跑一遍测试命令
  /provider        看底层 Codex 通道
  /provider subscription | relay <id> | inherit
  /退出            离开

其他任意句子 = 交代一件开发任务（白话就行）。
例子：给风险模块补一个单元测试，并让 pytest 过。
长时间托管（默认省额度）：pacer host run --goal \"...\" --execute
均衡：pacer host run --mode standard --hours 2 --execute
吃额度换效率：pacer host unleash --goal \"...\" --hours 3 --execute
"""


def run_interactive_agent(
    *,
    repo_root: str | Path = ".",
    workspace_root: str | Path = ".agent-workspace",
    task_runner: TaskRunner = run_simple_managed_task,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], Any] = print,
    test_command: str | None = None,
) -> int:
    repo = Path(repo_root).expanduser().resolve()
    workspace = Path(workspace_root).expanduser().resolve()
    if workspace.name != ".agent-workspace" and not str(workspace).endswith(".agent-workspace"):
        # allow either explicit workspace or default under repo
        if not workspace.exists():
            workspace = repo / ".agent-workspace"
    session_path = _session_path(workspace)
    last_goal = ""
    last_program_id = ""
    last_mission_id = ""
    codex_provider = "inherit"
    resolved_test_command = str(test_command or "").strip()

    output_func(f"Pacer · {repo}")
    output_func("跟我说要做什么就行。需要菜单时输入 /菜单")
    output_func("（不会用 Codex 也没关系；我会用人话告诉你进度。）")

    while True:
        try:
            raw = input_func("Pacer> ")
        except EOFError:
            output_func("")
            return 0
        except KeyboardInterrupt:
            output_func("\n好，先到这。有需要再叫我。")
            return 130

        message = str(raw or "").strip()
        if not message:
            continue

        # Slash menu
        if message.startswith("/") or message.lower() in {
            "help",
            "status",
            "menu",
            "exit",
            "quit",
            "帮助",
            "状态",
            "菜单",
            "退出",
        }:
            handled, last_mission_id, resolved_test_command, codex_provider, code = _handle_slash(
                message,
                repo=repo,
                workspace=workspace,
                last_program_id=last_program_id,
                last_mission_id=last_mission_id,
                test_command=resolved_test_command,
                codex_provider=codex_provider,
                session_path=session_path,
                output_func=output_func,
            )
            if code is not None:
                return code
            if handled:
                continue

        # Small talk / capability questions — don't start a mission
        conversational = _conversational_response(message, repo)
        if conversational:
            output_func(conversational)
            _append_session_event(
                session_path,
                {"type": "assistant_response", "message": message, "response": conversational},
            )
            continue

        # Natural language task
        effective_goal = _with_conversation_context(message, last_goal, last_program_id)
        _append_session_event(
            session_path,
            {"type": "user_task", "message": message, "effective_goal": effective_goal},
        )
        output_func("好，我先看看目标和这个项目怎么验收，再请编程助手动手。")
        try:
            payload = task_runner(
                effective_goal,
                repo_root=repo,
                workspace_root=workspace,
                codex_provider=codex_provider,
                progress_func=lambda line: output_func(_friend_progress_line(line)),
            )
        except KeyboardInterrupt:
            output_func("这轮先停。记录还在本地，你随时可以 /状态 看。")
            continue
        except Exception as exc:  # noqa: BLE001 — keep REPL alive
            output_func(f"这轮没顺顺利利跑完：{exc}")
            output_func("你可以换个说法再试，或先 /测试 看验收命令对不对。")
            continue

        if payload.get("status") != "needs_input":
            last_goal = message
            last_program_id = str(payload.get("program_id") or last_program_id)
            last_mission_id = str(payload.get("mission_id") or last_mission_id)
        if not resolved_test_command:
            resolved_test_command = str(payload.get("test_command") or "")

        _append_session_event(
            session_path,
            {
                "type": "task_result",
                "message": message,
                "program_id": last_program_id,
                "status": payload.get("status"),
            },
        )
        output_func(_friend_result_text(payload, goal=message))


def _handle_slash(
    message: str,
    *,
    repo: Path,
    workspace: Path,
    last_program_id: str,
    last_mission_id: str,
    test_command: str,
    codex_provider: str,
    session_path: Path,
    output_func: Callable[[str], Any],
) -> tuple[bool, str, str, str, int | None]:
    """Returns (handled, mission_id, test_command, provider, exit_code|None)."""
    raw = message.strip()
    # allow "帮助" without slash
    if not raw.startswith("/"):
        raw = "/" + raw
    parts = raw.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    aliases = {
        "/help": "/帮助",
        "/menu": "/菜单",
        "/status": "/状态",
        "/progress": "/进度",
        "/host": "/托管",
        "/tasks": "/任务",
        "/test": "/测试",
        "/verify": "/验收",
        "/exit": "/退出",
        "/quit": "/退出",
    }
    cmd = aliases.get(cmd, cmd)

    if cmd in {"/退出"}:
        output_func("好，先到这。")
        return True, last_mission_id, test_command, codex_provider, 0

    if cmd in {"/帮助", "/菜单"}:
        output_func(MENU_TEXT)
        return True, last_mission_id, test_command, codex_provider, None

    if cmd in {"/状态", "/进度"}:
        output_func(_human_status(workspace, last_program_id, last_mission_id))
        return True, last_mission_id, test_command, codex_provider, None

    if cmd == "/托管":
        try:
            from .pacer_host import build_host_dashboard, host_dashboard_to_markdown

            dash = build_host_dashboard(
                workspace_root=workspace,
                repo_root=repo,
                agent="codex",
                run_pytest=False,
                auto_resume=False,
            )
            output_func(host_dashboard_to_markdown(dash))
        except Exception as exc:  # noqa: BLE001
            output_func(f"托管仪表暂时读不到：{exc}")
        return True, last_mission_id, test_command, codex_provider, None

    if cmd == "/任务":
        output_func(_list_missions_text(workspace))
        return True, last_mission_id, test_command, codex_provider, None

    if cmd == "/测试":
        detected = _detect_test_command(repo, test_command)
        if detected:
            output_func(f"这个项目我会用这条命令当验收：\n  {detected}\n（你也可以在任务里说明别的命令。）")
        else:
            output_func("我还没认出测试命令。你可以说：用 pytest 验收，或告诉我 npm test 之类。")
        return True, last_mission_id, detected or test_command, codex_provider, None

    if cmd == "/验收":
        detected = _detect_test_command(repo, test_command)
        if not detected:
            output_func("还没有验收命令。先 /测试，或直接告诉我用什么命令检查。")
            return True, last_mission_id, test_command, codex_provider, None
        output_func(f"正在跑：{detected}")
        code, tail = _run_shell(detected, cwd=repo)
        if code == 0:
            output_func("验收通过了。")
        else:
            output_func(f"验收没过（退出码 {code}）。末尾输出：\n{tail}")
        return True, last_mission_id, detected, codex_provider, None

    if cmd == "/provider":
        if not arg:
            output_func(_provider_text(codex_provider))
            return True, last_mission_id, test_command, codex_provider, None
        selected, error = _parse_provider_command("/provider " + arg)
        if error:
            output_func(error)
        else:
            codex_provider = selected
            output_func(_provider_text(codex_provider))
            _append_session_event(session_path, {"type": "provider_selected", "provider": codex_provider})
        return True, last_mission_id, test_command, codex_provider, None

    # unknown slash — show menu, don't start a mission
    if cmd.startswith("/"):
        output_func(f"还不认识「{cmd}」。\n{MENU_TEXT}")
        return True, last_mission_id, test_command, codex_provider, None

    return False, last_mission_id, test_command, codex_provider, None


def _friend_progress_line(line: str) -> str:
    text = str(line or "").strip()
    if not text:
        return text
    # Keep system lines but soften a few common ones.
    if "已启动" in text or "Program" in text:
        return "已经开工了，编程助手在隔离环境里忙，主项目先不动。"
    return text


def _friend_result_text(payload: dict[str, Any], *, goal: str) -> str:
    from .pacer_voice import user_story, user_markdown_section

    status = str(payload.get("status") or "")
    # Map simple_task statuses onto stop_reason-ish tags
    reason_map = {
        "verified": "verified",
        "done": "verified",
        "needs_input": "coverage_gap",
        "blocked": "worker_error",
        "failed": "verification_failed",
        "timeout": "budget_exhausted",
    }
    stop_reason = reason_map.get(status, status or "unknown")
    story = user_story(
        stop_reason=stop_reason,
        status=status,
        goal=goal,
        message_fallback=str(payload.get("message") or ""),
    )
    lines = user_markdown_section(story)
    # Keep a compact technical appendix for support, not the main story
    detail = simple_result_to_markdown(payload)
    if detail and len(detail) < 2500:
        lines.extend(["", "——", "", "<details><summary>技术摘要（可忽略）</summary>", "", detail, "", "</details>"])
    return "\n".join(lines)


def _human_status(workspace: Path, last_program_id: str, last_mission_id: str) -> str:
    from .pacer_voice import user_story

    # Prefer mission progress if present
    mission_id = last_mission_id
    missions_root = workspace / "missions"
    if not mission_id and missions_root.is_dir():
        dirs = sorted(
            [p for p in missions_root.iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if dirs:
            mission_id = dirs[0].name
    if mission_id:
        progress_path = missions_root / mission_id / "progress.json"
        mission_path = missions_root / mission_id / "mission.json"
        progress: dict[str, Any] = {}
        mission: dict[str, Any] = {}
        if progress_path.exists():
            try:
                progress = json.loads(progress_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                progress = {}
        if mission_path.exists():
            try:
                mission = json.loads(mission_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                mission = {}
        story = user_story(
            stop_reason=str(progress.get("stop_reason") or mission.get("stop_reason") or ""),
            status=str(progress.get("status") or mission.get("status") or ""),
            stage=str(progress.get("stage") or ""),
            goal=str(mission.get("objective") or ""),
            product_change_count=progress.get("changed_product_file_count"),
            verification_command=str(mission.get("test_command") or progress.get("verification_command") or ""),
            worktree=str(progress.get("worktree") or ""),
        )
        journey: dict[str, Any] = {}
        try:
            from .mission_journey import build_mission_journey

            journey = build_mission_journey(
                workspace_root=workspace,
                mission_id=mission_id,
                mission=mission,
                progress=progress,
            )
        except Exception:  # noqa: BLE001 - keep the local conversation available.
            journey = {}
        lines = [
            f"**{story['headline']}**",
            story["what_happened"],
        ]
        if journey:
            lines.extend(["", f"闭环：{journey.get('summary') or journey.get('status')}"])
        lines.extend(
            [
                "",
                "你可以：" + "；".join(story.get("choices") or []),
                f"（任务号 {mission_id}，可忽略）",
            ]
        )
        return "\n".join(lines)

    # Program fallback
    text = _status_text(workspace, last_program_id)
    if "还没有" in text:
        return "现在没有正在跟的任务。你直接说想做什么就行。"
    return "最近有一条任务记录：\n" + text


def _list_missions_text(workspace: Path) -> str:
    root = workspace / "missions"
    if not root.is_dir():
        return "还没有任务记录。说一句想做什么，就会开始。"
    dirs = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    if not dirs:
        return "还没有任务记录。"
    lines = ["最近任务："]
    for path in dirs:
        objective = ""
        status = ""
        mission_file = path / "mission.json"
        if mission_file.exists():
            try:
                data = json.loads(mission_file.read_text(encoding="utf-8-sig"))
                objective = str(data.get("objective") or "")[:60]
                status = str(data.get("status") or "")
            except (OSError, json.JSONDecodeError):
                pass
        lines.append(f"- {path.name}  [{status or '?'}]  {objective}")
    lines.append("想看人话进度：/状态")
    return "\n".join(lines)


def _detect_test_command(repo: Path, current: str) -> str:
    if str(current or "").strip():
        from .verification_profiles import resolve_test_command

        resolved, _meta = resolve_test_command(current, repo_root=repo)
        return str(resolved or current)
    from .verification_profiles import choose_verification_command, resolve_test_command

    auto = choose_verification_command(repo)
    if not auto:
        return ""
    resolved, _meta = resolve_test_command(auto, repo_root=repo)
    return str(resolved or auto)


def _run_shell(command: str, *, cwd: Path) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    tail = ((completed.stdout or "") + (completed.stderr or ""))[-1200:]
    return int(completed.returncode), tail


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
    if lower in {"你好", "在吗", "hi", "hello"}:
        return "在的。直接说你想改项目里的什么，或输入 /菜单。"
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
        "可以。你用日常说话的方式告诉我要做什么就行，不必会用 Codex。\n"
        f"当前项目：{repo}\n"
        "最好顺便说：怎样算做完（例如测试要过）。也可以先 /测试 看我会用什么验收。"
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
    return "", "用法：/provider subscription  或  /provider relay <id>  或  /provider inherit"


def _provider_text(provider: str) -> str:
    if provider == "openai":
        return "现在走：Codex 登录订阅。"
    if provider == "inherit":
        return "现在走：沿用你本机 Codex 当前配置（订阅或中转）。"
    return f"现在走：Codex 中转 {provider}。"


def _session_path(workspace: Path) -> Path:
    now = datetime.now(timezone.utc)
    directory = workspace / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"agent-{now.strftime('%Y%m%d-%H%M%S')}.jsonl"


def _append_session_event(path: Path, payload: dict[str, Any]) -> None:
    entry = {"recorded_at": datetime.now(timezone.utc).isoformat(), **payload}
    path.parent.mkdir(parents=True, exist_ok=True)
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
