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
    argv_with_autocompact,
    autocompact_setting,
    rotate_threshold,
    rotation_prompt,
    save_handoff,
)

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_MAX_HISTORY = 12


@dataclass
class RateLimit:
    """Real subscription quota, straight from the engine's rate_limit_event."""

    kind: str = ""
    utilization: float = 0.0
    resets_at: int = 0
    status: str = ""

    def describe(self) -> str:
        label = {"seven_day": "周额度", "five_hour": "5小时额度"}.get(self.kind, self.kind or "额度")
        text = f"{label} 已用 {self.utilization * 100:.0f}%"
        if self.resets_at:
            remaining = self.resets_at - time.time()
            if remaining > 0:
                hours = remaining / 3600
                text += f"，{hours / 24:.1f} 天后重置" if hours >= 24 else f"，{hours:.1f} 小时后重置"
        return text


@dataclass
class TurnResult:
    session_id: str = ""
    text: str = ""
    tokens: int = 0
    touched: list[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    rate_limits: dict[str, RateLimit] = field(default_factory=dict)


@dataclass
class ChatState:
    session_id: str = ""
    session_tokens: int = 0
    rotations: int = 0
    history: list[dict[str, str]] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)
    pending_handoff: str = ""
    rate_limits: dict[str, RateLimit] = field(default_factory=dict)

    def record_turn(self, prompt: str, result: TurnResult) -> None:
        self.session_id = result.session_id or self.session_id
        self.session_tokens += result.tokens
        self.rate_limits.update(result.rate_limits)
        self.history.append({"user": prompt, "assistant": result.text})
        if len(self.history) > _MAX_HISTORY:
            self.history.pop(0)
        for item in result.touched:
            if item not in self.touched:
                self.touched.append(item)

    def reset_session(self) -> None:
        self.session_id = ""
        self.session_tokens = 0


def format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return str(count)


def fold_event(
    event: dict[str, Any],
    result: TurnResult,
    *,
    on_text: Callable[[str], None] | None = None,
) -> str | None:
    """Absorb one stream-json event; return a progress line to display, if any."""
    kind = event.get("type")
    if kind == "stream_event":
        inner = event.get("event") or {}
        if inner.get("type") == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta" and on_text:
                chunk = str(delta.get("text") or "")
                if chunk:
                    on_text(chunk)
        return None
    if kind == "rate_limit_event":
        info = event.get("rate_limit_info") or {}
        limit = RateLimit(
            kind=str(info.get("rateLimitType") or ""),
            utilization=float(info.get("utilization") or 0.0),
            resets_at=int(info.get("resetsAt") or 0),
            status=str(info.get("status") or ""),
        )
        if limit.kind:
            result.rate_limits[limit.kind] = limit
        return None
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


class ClaudeSession:
    """One long-lived Claude process fed by stream-json on stdin.

    Measured: respawning per turn costs 7-11s of startup every time, which is
    what made the window feel dead. A persistent process answers follow-up
    turns in ~2s. Rotation still spawns a new process -- but only once per
    threshold crossing, not once per message.
    """

    def __init__(
        self,
        executable: str,
        base_flags: list[str],
        *,
        environ: dict[str, str] | None = None,
        resume_id: str = "",
    ) -> None:
        from .process_guard import ProcessGuard
        from .subprocess_window import prepare_subprocess_command

        child_env = dict(os.environ if environ is None else environ)
        child_env.setdefault("MAX_THINKING_TOKENS", DEFAULT_MAX_THINKING_TOKENS)
        argv = list(base_flags)
        if resume_id:
            argv += ["--resume", resume_id]
        argv += [
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        self._guard = ProcessGuard()
        self._guard.start()
        self.process = subprocess.Popen(
            prepare_subprocess_command([executable, *argv]),
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._guard.adopt(self.process.pid)
        self.session_id = resume_id

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def send(
        self,
        text: str,
        *,
        on_text: Callable[[str], None] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> TurnResult:
        result = TurnResult(session_id=self.session_id)
        if not self.alive or self.process.stdin is None or self.process.stdout is None:
            result.ok = False
            result.error = "session_dead"
            return result
        payload = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        try:
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            result.ok = False
            result.error = "session_dead"
            return result
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                progress = fold_event(event, result, on_text=on_text)
                if progress and on_progress:
                    on_progress(progress)
                if event.get("type") == "result":
                    break
            else:
                result.ok = False
                result.error = "session_ended"
        except KeyboardInterrupt:
            result.ok = False
            result.error = "interrupted"
        self.session_id = result.session_id or self.session_id
        return result

    def close(self) -> None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except (OSError, ValueError):
            pass
        if self.alive:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True)
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        self._guard.close()


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
    session = state.session_id or "（首条消息后建立）"
    lines = [
        f"当前会话 {session}",
        f"会话消耗 {format_tokens(state.session_tokens)} / {format_tokens(threshold)}（到阈值自动无损轮转）",
        f"本窗口已轮转 {state.rotations} 次",
    ]
    # Subscription quota is what actually runs out, so lead the eye to it.
    for kind in ("seven_day", "five_hour"):
        limit = state.rate_limits.get(kind)
        if limit:
            lines.append(limit.describe())
    if not state.rate_limits:
        lines.append("订阅额度：本窗口还没收到额度事件（发一条消息后显示）")
    return "\n".join(lines)


def receipt_text(state: ChatState, result: TurnResult, threshold: int, elapsed: float) -> str:
    parts = [
        f"本回合 {format_tokens(result.tokens)}",
        f"会话 {format_tokens(state.session_tokens)}/{format_tokens(threshold)}",
        f"{elapsed:.0f}s",
    ]
    weekly = state.rate_limits.get("seven_day")
    if weekly:
        parts.insert(2, f"周额度 {weekly.utilization * 100:.0f}%")
    return "—— " + " · ".join(parts)


def run_claude_chat(argv: list[str], *, environ: dict[str, str] | None = None) -> int:
    import shutil

    executable = shutil.which("claude") or shutil.which("claude.cmd")
    if not executable:
        print("Pacer requires Claude Code CLI on PATH. Install it with: npm install -g @anthropic-ai/claude-code")
        return 127
    env_source = os.environ if environ is None else environ
    threshold = rotate_threshold(env_source)
    base_flags = argv_with_autocompact(list(argv), autocompact_setting(env_source))
    if "--permission-mode" not in base_flags and not any(
        token.startswith("--permission-mode=") for token in base_flags
    ):
        base_flags += ["--permission-mode", "bypassPermissions"]

    state = ChatState()
    session: ClaudeSession | None = None
    print(
        f"[pacer chat] 一个项目一个窗口。上下文由引擎自动压缩控住；会话到 {format_tokens(threshold)}"
        f" token 再在回合之间无损轮转（{ROTATE_TOKENS_ENV} 可调）。"
        "每回合小票显示真实周额度。命令：/status /rotate /new /exit"
    )

    def open_session(resume_id: str = "") -> ClaudeSession:
        return ClaudeSession(executable, base_flags, environ=env_source, resume_id=resume_id)

    def rotate(reason: str, *, carry_handoff: bool) -> None:
        nonlocal session
        if carry_handoff and state.history:
            handoff = build_chat_handoff(state)
            saved = save_handoff(handoff, cwd=os.getcwd())
            state.pending_handoff = handoff
            state.rotations += 1
            suffix = f"，交接存档 {saved}" if saved else ""
        else:
            state.pending_handoff = ""
            state.history.clear()
            state.touched.clear()
            suffix = ""
        state.reset_session()
        if session is not None:
            session.close()
            session = None
        print(f"[pacer chat] {reason}{suffix}")

    try:
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
            if raw == "/rotate":
                rotate("已手动轮转，下一条消息带交接开启新会话", carry_handoff=True)
                continue
            if raw == "/new":
                rotate("已开启全新会话（不带交接）", carry_handoff=False)
                continue

            if state.session_tokens >= threshold and state.history:
                rotate(
                    f"会话达到 {format_tokens(threshold)} 阈值，已在回合之间无损轮转"
                    f"（第 {state.rotations + 1} 次），带交接继续",
                    carry_handoff=True,
                )

            prompt = raw
            if state.pending_handoff:
                prompt = rotation_prompt(state.pending_handoff) + "\n\n---\n\n用户的新消息：" + raw
                state.pending_handoff = ""

            if session is None or not session.alive:
                # Resume keeps the persistent process cheap to replace if the
                # engine exits on its own; a rotation deliberately has no id.
                session = open_session(state.session_id)

            started = time.time()
            streamed = False

            def emit_text(chunk: str) -> None:
                nonlocal streamed
                if not streamed:
                    print()
                    streamed = True
                sys.stdout.write(chunk)
                sys.stdout.flush()

            result = session.send(
                prompt,
                on_text=emit_text,
                on_progress=lambda line: print(line, flush=True),
            )
            if streamed:
                print()
            elif result.text:
                print(f"\n{result.text}")
            if result.error == "interrupted":
                print("[pacer chat] 已取消本回合，会话保留。")
                session.close()
                session = None
            elif result.error in {"session_dead", "session_ended"}:
                print("[pacer chat] 引擎退出，下一条消息会自动续上当前会话。")
                session = None
            elif not result.ok and result.error:
                print(f"[pacer chat] 本回合异常：{result.error}")
            state.record_turn(raw, result)
            print(receipt_text(state, result, threshold, time.time() - started))
    finally:
        if session is not None:
            session.close()
