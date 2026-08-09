"""C-layer: Pacer-owned chat window driving Claude Code headless.

The B-layer rotation proxy (claude_rotation.py) wraps the native TUI and has
to *infer* turn boundaries from transcript idle time, then kill the process.
Here Pacer owns the input loop, so every turn boundary is exact: rotation is
just "start the next turn without --resume, prepending a distilled handoff".
No process killing, no JSONL sniffing, no lost draft input.

One window, forever: the user talks to the same REPL; underneath, each turn
runs `claude -p --output-format stream-json` either resuming the active
session or opening a fresh one when the token threshold is crossed.

Trade-off stated up front: headless mode has no native permission prompts,
so this window is for bypassPermissions-style workflows (which is exactly
how the target user launches Claude today).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .claude_rotation import (
    DEFAULT_MAX_THINKING_TOKENS,
    ROTATE_TOKENS_ENV,
    rotate_threshold,
    rotation_prompt,
    save_handoff,
)

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_MAX_HISTORY = 12


@dataclass
class TurnResult:
    session_id: str = ""
    text: str = ""
    tokens: int = 0
    touched: list[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""


@dataclass
class ChatState:
    session_id: str = ""
    session_tokens: int = 0
    rotations: int = 0
    history: list[dict[str, str]] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)
    pending_handoff: str = ""

    def record_turn(self, prompt: str, result: TurnResult) -> None:
        self.session_id = result.session_id or self.session_id
        self.session_tokens += result.tokens
        self.history.append({"user": prompt, "assistant": result.text})
        if len(self.history) > _MAX_HISTORY:
            self.history.pop(0)
        for item in result.touched:
            if item not in self.touched:
                self.touched.append(item)

    def reset_session(self) -> None:
        self.session_id = ""
        self.session_tokens = 0


def headless_argv(base_flags: list[str], *, resume_id: str, prompt: str) -> list[str]:
    argv = list(base_flags)
    if resume_id:
        argv += ["--resume", resume_id]
    # stream-json in print mode requires --verbose.
    argv += ["-p", "--output-format", "stream-json", "--verbose", prompt]
    return argv


def fold_event(event: dict[str, Any], result: TurnResult) -> str | None:
    """Absorb one stream-json event; return a progress line to display, if any."""
    kind = event.get("type")
    if kind == "assistant":
        message = event.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            return None
        progress = None
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    result.text = text
            elif block.get("type") == "tool_use":
                name = str(block.get("name") or "")
                tool_input = block.get("input") or {}
                target = str(
                    tool_input.get("file_path")
                    or tool_input.get("command")
                    or tool_input.get("pattern")
                    or ""
                )
                if name in _EDIT_TOOLS and tool_input.get("file_path"):
                    path = str(tool_input["file_path"])
                    if path not in result.touched:
                        result.touched.append(path)
                progress = f"  · {name} {target}".rstrip()[:120]
        return progress
    if kind == "result":
        result.session_id = str(event.get("session_id") or "")
        usage = event.get("usage")
        if isinstance(usage, dict):
            result.tokens = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("output_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
            )
        if event.get("is_error") or str(event.get("subtype") or "") not in {"", "success"}:
            result.ok = str(event.get("subtype") or "") == "success" and not event.get("is_error")
            result.error = str(event.get("result") or event.get("subtype") or "")
        final = str(event.get("result") or "").strip()
        if final:
            result.text = final
    return None


def run_turn(
    executable: str,
    base_flags: list[str],
    *,
    prompt: str,
    resume_id: str,
    environ: dict[str, str] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> TurnResult:
    from .process_guard import ProcessGuard
    from .subprocess_window import prepare_subprocess_command

    child_env = dict(os.environ if environ is None else environ)
    child_env.setdefault("MAX_THINKING_TOKENS", DEFAULT_MAX_THINKING_TOKENS)
    command = prepare_subprocess_command(
        [executable, *headless_argv(base_flags, resume_id=resume_id, prompt=prompt)]
    )
    result = TurnResult()
    guard = ProcessGuard()
    guard.start()
    try:
        process = subprocess.Popen(
            command,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        guard.adopt(process.pid)
        assert process.stdout is not None
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                progress = fold_event(event, result)
                if progress and on_progress:
                    on_progress(progress)
        except KeyboardInterrupt:
            # User cancelled the turn; kill it but keep the window alive.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            else:
                process.terminate()
            result.ok = False
            result.error = "interrupted"
        stderr_text = ""
        try:
            stderr_text = (process.stderr.read() or "").strip() if process.stderr else ""
        except (OSError, ValueError):
            pass
        returncode = process.wait()
        if returncode != 0 and result.ok and not result.text:
            result.ok = False
            result.error = stderr_text or f"claude exited with code {returncode}"
    finally:
        guard.close()
    return result


def build_chat_handoff(state: ChatState, *, max_chars: int = 6000) -> str:
    def clip(text: str, limit: int) -> str:
        text = text.strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"

    lines = ["## 会话交接（Pacer chat 精确轮转）", ""]
    if state.history:
        lines += ["### 本会话最初的目标", clip(state.history[0]["user"], 800), ""]
    if state.touched:
        lines += ["### 本会话动过的文件"]
        lines += [f"- {item}" for item in state.touched[-20:]]
        lines.append("")
    if state.history:
        lines += ["### 最近的对话（旧→新）"]
        for exchange in state.history[-5:]:
            lines.append(f"- 用户：{clip(exchange['user'], 300)}")
            if exchange.get("assistant"):
                lines.append(f"  助手：{clip(exchange['assistant'], 300)}")
        lines.append("")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def status_text(state: ChatState, threshold: int) -> str:
    used = state.session_tokens / 1_000_000
    cap = threshold / 1_000_000
    session = state.session_id or "（首条消息后建立）"
    return (
        f"当前会话 {session}\n"
        f"会话消耗 {used:.1f}M / {cap:.0f}M token（到阈值自动无损轮转）\n"
        f"本窗口已轮转 {state.rotations} 次"
    )


def run_claude_chat(argv: list[str], *, environ: dict[str, str] | None = None) -> int:
    import shutil

    executable = shutil.which("claude") or shutil.which("claude.cmd")
    if not executable:
        print("Pacer requires Claude Code CLI on PATH. Install it with: npm install -g @anthropic-ai/claude-code")
        return 127
    env_source = os.environ if environ is None else environ
    threshold = rotate_threshold(env_source)
    base_flags = list(argv)
    if "--permission-mode" not in base_flags and not any(
        token.startswith("--permission-mode=") for token in base_flags
    ):
        base_flags += ["--permission-mode", "bypassPermissions"]

    state = ChatState()
    print(
        f"[pacer chat] 一个项目一个窗口。会话到 {threshold // 1_000_000}M token 在回合之间无损轮转"
        f"（{ROTATE_TOKENS_ENV} 可调）。命令：/status /rotate /new /exit"
    )
    while True:
        try:
            raw = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            continue
        if raw in {"/exit", "/quit", "exit", "quit"}:
            return 0
        if raw == "/status":
            print(status_text(state, threshold))
            continue
        if raw in {"/rotate", "/new"}:
            if raw == "/rotate" and state.history:
                pending = build_chat_handoff(state)
                save_handoff(pending, cwd=os.getcwd())
                state.rotations += 1
                state.reset_session()
                state.pending_handoff = pending
                print("[pacer chat] 已手动轮转，下一条消息将带交接开启新会话。")
            else:
                state.reset_session()
                state.history.clear()
                state.touched.clear()
                print("[pacer chat] 已开启全新会话（不带交接）。")
            continue

        prompt = raw
        resume_id = state.session_id
        if state.session_tokens >= threshold and state.history:
            handoff = build_chat_handoff(state)
            save_handoff(handoff, cwd=os.getcwd())
            state.rotations += 1
            state.reset_session()
            resume_id = ""
            prompt = rotation_prompt(handoff) + "\n\n---\n\n用户的新消息：" + raw
            print(
                f"[pacer chat] 会话达到阈值，已在回合之间无损轮转（第 {state.rotations} 次），带交接继续。"
            )
        if state.pending_handoff and not resume_id:
            prompt = rotation_prompt(state.pending_handoff) + "\n\n---\n\n用户的新消息：" + raw
            state.pending_handoff = ""

        started = time.time()
        result = run_turn(
            executable,
            base_flags,
            prompt=prompt,
            resume_id=resume_id,
            environ=env_source,
            on_progress=lambda line: print(line, flush=True),
        )
        if result.text:
            print(f"\n{result.text}")
        if not result.ok and result.error and result.error != "interrupted":
            print(f"[pacer chat] 本回合异常：{result.error}")
        elif result.error == "interrupted":
            print("[pacer chat] 已取消本回合。")
        state.record_turn(raw, result)
        elapsed = time.time() - started
        print(
            f"\n—— {result.tokens / 1_000_000:.1f}M token · 本会话累计 "
            f"{state.session_tokens / 1_000_000:.1f}M / {threshold // 1_000_000}M · {elapsed:.0f}s"
        )
