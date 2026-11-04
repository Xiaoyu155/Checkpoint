"""Per-task native Claude Code windows.

Design constraint checked 2026-08-13: Claude Code exposes no hook or flag
that lets an external process pick the model per turn, or restart the
session, while the user stays inside one native interactive TUI process
(verified against the hooks/settings docs -- no UserPromptSubmit override
for the model, no Stop-triggered session restart). So "auto-pick model" and
"auto-fresh context" cannot both live *inside* one native window without
Pacer building its own fake REPL -- which is what the previous design
(claude_chat.py) did, and it was rejected: it wasn't Claude's real interface.

This module gives up nothing else instead. Every window IS the real `claude`
TUI -- genuine stdio inheritance, no stream-json wrapper, no re-typed
markdown-formatting instructions. The automation happens *between* windows:
Pacer asks for the next task at its own prompt, classifies it, launches a
fresh native process with the right `--model`, and once the user exits that
window, distills a handoff from Claude's own on-disk session transcript (the
same JSONL Claude writes to ~/.claude/projects regardless of how it was
launched) and carries it into the next window's opening message.

Trade-off stated up front: "task done" is signalled by the user exiting the
native session (Ctrl+D / /exit / closing the window), not detected
automatically -- Claude Code doesn't expose a "turn finished" event Pacer
could react to from outside. And the model is chosen once, before the
window opens, from that task's first message; it does not change mid-task
the way switching by hand with /model inside the window would.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from .claude_rotation import (
    DEFAULT_MAX_THINKING_TOKENS,
    autocompact_setting,
    argv_with_autocompact,
    build_handoff,
    claude_projects_root,
    discover_session_jsonl,
    save_handoff,
)
from .model_router import route_task

MODEL_MAP_ENV = "PACER_TASK_MODEL_MAP"
DEFAULT_MODEL_MAP: dict[str, str] = {"cheap": "sonnet", "standard": "sonnet", "strong": "opus"}
_TIER_LABEL = {"cheap": "轻任务", "standard": "中任务", "strong": "重任务"}


def parse_task_model_map(raw: str, default: dict[str, str]) -> dict[str, str]:
    mapping = dict(default)
    for chunk in str(raw or "").split(","):
        if "=" not in chunk:
            continue
        tier, model = (part.strip() for part in chunk.split("=", 1))
        if tier.lower() in mapping and model:
            mapping[tier.lower()] = model
    return mapping


def pick_model(objective: str, model_map: dict[str, str], *, repeated_failure: bool = False) -> tuple[str, str]:
    """Classify one task and return (model, human-readable reason)."""
    route = route_task(objective=objective, repeated_failure=repeated_failure)
    model = model_map.get(route.tier, "")
    label = _TIER_LABEL.get(route.tier, route.tier)
    return model, f"{label} → {model or '引擎默认'}｜{route.reason}"


def task_handoff_prompt(handoff: str, raw_task: str) -> str:
    return (
        "上一个任务已经结束，这是一个新的原生会话。下面是上一个任务的交接摘要，"
        "供你了解背景，和这次任务无关的部分可以忽略，不用复述或问候。\n\n"
        + handoff
        + "\n\n---\n\n新任务：" + raw_task
    )


def _has_buffered_line(timeout: float = 0.05) -> bool:
    """A terminal paste of a multi-line task description must land as one
    message, not be split line-by-line into separate commands: input()
    reads one line at a time, so drain whatever else the paste already
    buffered right behind the first line before treating it as one turn."""
    if os.name == "nt":
        import msvcrt

        return msvcrt.kbhit()
    import select

    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    return bool(ready)


def read_task_input(prompt: str) -> str:
    lines = [input(prompt)]
    while _has_buffered_line():
        try:
            lines.append(input())
        except EOFError:
            break
    return "\n".join(lines).strip()


def run_pacer_task(argv: list[str], *, environ: dict[str, str] | None = None) -> int:
    import shutil

    executable = shutil.which("claude") or shutil.which("claude.cmd")
    if not executable:
        print("Pacer requires Claude Code CLI on PATH. Install it with: npm install -g @anthropic-ai/claude-code")
        return 127
    from .process_guard import ProcessGuard
    from .subprocess_window import prepare_subprocess_command

    env_source = os.environ if environ is None else environ
    cwd = os.getcwd()
    projects_root = claude_projects_root()
    base_flags = argv_with_autocompact(list(argv), autocompact_setting(env_source))
    if "--permission-mode" not in base_flags and not any(
        token.startswith("--permission-mode=") for token in base_flags
    ):
        # A window opens per task -- reconfirming permissions on every one
        # would fight the whole point of this command. Pass your own
        # --permission-mode to override.
        base_flags += ["--permission-mode", "bypassPermissions"]
    child_env = dict(env_source)
    child_env.setdefault("MAX_THINKING_TOKENS", DEFAULT_MAX_THINKING_TOKENS)
    model_map = parse_task_model_map(env_source.get(MODEL_MAP_ENV, ""), DEFAULT_MODEL_MAP)

    forced_model: str | None = None
    pending_handoff = ""
    used_transcripts: set[Path] = set()
    last_failed = False

    print(
        "[pacer task] 每个任务一个真正的 Claude 原生窗口：这一步没有 Pacer 拦截，"
        "Todo 面板、原生渲染都和你自己敲 claude 一样。默认 --permission-mode bypassPermissions"
        "（一个任务一个窗口，逐个确认权限太烦；想要正常确认自己传 --permission-mode 覆盖）。"
        "任务结束请退出这个窗口（Ctrl+D 或 /exit），Pacer 会读它自己的会话记录做交接，"
        "再问你下一个任务、按内容自动选模型开新窗口。"
    )
    print(f"[pacer task] 模型映射：{'  '.join(f'{_TIER_LABEL[t]}→{m}' for t, m in model_map.items())}（/model 可临时指定，/exit 退出）")

    while True:
        try:
            raw = read_task_input("\n下一个任务> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            continue
        if raw in {"/exit", "/quit", "exit", "quit"}:
            return 0
        if raw == "/model" or raw.startswith("/model "):
            arg = raw.split(None, 1)[1].strip().lower() if " " in raw else ""
            if not arg or arg == "auto":
                forced_model = None
                print("[pacer task] 已恢复自动按任务选模型。")
            else:
                forced_model = arg
                print(f"[pacer task] 下一个任务固定用模型 {arg}（/model auto 恢复自动）。")
            continue

        if forced_model:
            model, reason = forced_model, "手动指定（/model auto 恢复自动）"
        else:
            model, reason = pick_model(raw, model_map, repeated_failure=last_failed)
        print(f"[任务] {reason}")

        launch_argv = list(base_flags)
        if model:
            launch_argv += ["--model", model]
        prompt_text = task_handoff_prompt(pending_handoff, raw) if pending_handoff else raw
        launch_argv.append(prompt_text)
        pending_handoff = ""

        started = time.time()
        guard = ProcessGuard()
        guard.start()
        process = subprocess.Popen(prepare_subprocess_command([executable, *launch_argv]), env=child_env)
        guard.adopt(process.pid)
        try:
            while True:
                try:
                    process.wait()
                    break
                except KeyboardInterrupt:
                    # Ctrl+C is for the native child's own interrupt handling,
                    # not a reason to tear down the tree from out here.
                    continue
        finally:
            guard.close()

        found = discover_session_jsonl(
            projects_root=projects_root,
            cwd=cwd,
            started_after=started - 5,
            exclude=used_transcripts,
        )
        elapsed = time.time() - started
        if found is not None:
            used_transcripts.add(found)
            handoff = build_handoff(found)
            saved = save_handoff(handoff, cwd=cwd)
            pending_handoff = handoff
            location = f"，交接存档 {saved}" if saved else ""
            print(f"[pacer task] 任务窗口关闭（{elapsed:.0f}s），已读取交接{location}。")
        else:
            print(f"[pacer task] 任务窗口关闭（{elapsed:.0f}s），未找到会话记录，下一个任务不带交接。")
        last_failed = process.returncode not in (0, None)
