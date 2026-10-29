"""Session rotation proxy for the `pacer claude` passthrough.

Why this exists: real billing data (2026-08-09, ccusage over local JSONL)
showed 96% of quota burn came from a dozen interactive monster sessions with
a ~800:1 history-reread overhead, while every Pacer worker session was
negligible. The user keeps one window open per project and will not adopt a
manual /clear discipline, so the window and the billed session must be
decoupled: Pacer owns the window, and silently rotates the underlying Claude
Code session once it has consumed enough tokens, carrying a distilled
handoff into the fresh session.

Design constraints:
- Zero extra model calls: the handoff is distilled from the session JSONL
  offline, so rotation itself costs no quota.
- Rotation only at a turn boundary (last transcript record is a final
  assistant text and the file has been quiet), never mid-stream or while a
  tool is likely running.
- The next session must keep working silently; the handoff prompt says so.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_ROTATE_TOKENS = 10_000_000
"""Session-total tokens before rotation. From measured data: sessions under
10M tokens were 1.3% of the bill combined; everything above trended monster."""

DEFAULT_MAX_THINKING_TOKENS = "10000"
POLL_SECONDS = 10.0
IDLE_SECONDS = 10.0
ROTATE_ENV = "PACER_CLAUDE_ROTATE"
ROTATE_TOKENS_ENV = "PACER_CLAUDE_ROTATE_TOKENS"

AUTOCOMPACT_ENV = "PACER_AUTOCOMPACT"
"""Opt-in engine context cap: `PACER_AUTOCOMPACT=<tokens>` injects
`--autocompact <tokens>`.

Reversed 2026-08-10, same day it shipped as the default: a 100k window makes
the engine compact constantly, and each compaction is a minutes-long blocking
progress bar (measured 5m+ on 2.1.226). Quota is a soft constraint; waiting
is a hard pain. The no-wait lever is now chat-side rotation once the carried
context crosses DEFAULT_ROTATE_CONTEXT_TOKENS (claude_chat.py): same quota
ceiling, but the cost is one ~10s restart at a turn boundary instead of a
compaction stall mid-work.
"""

DEFAULT_ROTATE_CONTEXT_TOKENS = 150_000
"""Per-turn carried context (input + cache) that triggers chat rotation.

Billing follows the context carried each request, not session totals; the
measured burn data put 93% of spend in turns above 150k context, so that is
the cut line."""
ROTATE_CONTEXT_ENV = "PACER_CLAUDE_ROTATE_CONTEXT"

_CAVEAT_MARKERS = (
    "<local-command-caveat>",
    "<command-name>",
    "<local-command-stdout>",
    "<system-reminder>",
)
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def claude_projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def rotation_enabled(environ: dict[str, str] | None = None) -> bool:
    """Off unless asked for: killing a live session is the risky lever."""
    env = os.environ if environ is None else environ
    return str(env.get(ROTATE_ENV, "0")).strip().lower() in {"1", "on", "true", "yes"}


def autocompact_setting(environ: dict[str, str] | None = None) -> str:
    """Empty unless explicitly requested: blocking compaction is opt-in."""
    env = os.environ if environ is None else environ
    raw = str(env.get(AUTOCOMPACT_ENV, "")).strip()
    if not raw or raw.lower() in {"off", "0", "no", "false"}:
        return ""
    return raw


def argv_with_autocompact(argv: list[str], value: str) -> list[str]:
    """Add the engine's own context cap, never overriding an explicit choice."""
    if not value:
        return list(argv)
    for token in argv:
        if token == "--autocompact" or token.startswith("--autocompact="):
            return list(argv)
    return ["--autocompact", value, *argv]


def rotate_threshold(environ: dict[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    raw = str(env.get(ROTATE_TOKENS_ENV, "")).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_ROTATE_TOKENS
    return value if value > 0 else DEFAULT_ROTATE_TOKENS


def rotate_context_threshold(environ: dict[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    raw = str(env.get(ROTATE_CONTEXT_ENV, "")).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_ROTATE_CONTEXT_TOKENS
    return value if value > 0 else DEFAULT_ROTATE_CONTEXT_TOKENS


def is_interactive_launch(argv: list[str]) -> bool:
    """Rotation only makes sense for the interactive REPL.

    Print mode, resume/continue, and subcommands (`claude mcp ...`) manage
    their own lifecycle; wrapping them would break semantics, so those fall
    back to plain passthrough.
    """
    blockers = {"-p", "--print", "--resume", "-r", "--continue", "-c", "--help", "-h", "--version", "-v"}
    subcommands = {"mcp", "config", "doctor", "update", "install", "migrate-installer", "setup-token", "plugin", "agents"}
    for token in argv:
        if token in blockers:
            return False
        if not token.startswith("-") and token in subcommands:
            return False
    return True


def _message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return "\n".join(part for part in parts if part)
    return ""


def _is_noise_user_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return any(marker in stripped for marker in _CAVEAT_MARKERS)


@dataclass
class SessionMeter:
    """Incremental token accounting over one session's JSONL transcript."""

    path: Path
    total_tokens: int = 0
    last_context_tokens: int = 0
    last_model: str = ""
    last_record_final_text: bool = False
    last_growth: float = field(default_factory=time.time)
    _offset: int = 0
    _tail: str = ""

    def refresh(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size <= self._offset:
            return
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError:
            return
        self.last_growth = time.time()
        data = self._tail + chunk
        lines = data.split("\n")
        self._tail = lines.pop()  # possibly incomplete final line
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._absorb(record)

    def _absorb(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "assistant":
            message = record.get("message")
            if isinstance(message, dict):
                # A mid-session /model switch shows up here; the fresh session
                # must inherit it or the user's choice silently reverts.
                model = str(message.get("model") or "").strip()
                if model:
                    self.last_model = model
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                turn_input = (
                    int(usage.get("input_tokens") or 0)
                    + int(usage.get("cache_creation_input_tokens") or 0)
                    + int(usage.get("cache_read_input_tokens") or 0)
                )
                self.total_tokens += turn_input + int(usage.get("output_tokens") or 0)
                self.last_context_tokens = turn_input
            self.last_record_final_text = self._is_final_text(message)
        elif kind in {"user", "system"}:
            self.last_record_final_text = False

    @staticmethod
    def _is_final_text(message: Any) -> bool:
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        if not isinstance(content, list):
            return False
        has_text = False
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                return False
            if block_type == "text" and str(block.get("text") or "").strip():
                has_text = True
        return has_text

    def idle_for(self) -> float:
        return time.time() - self.last_growth

    def at_turn_boundary(self, *, idle_seconds: float = IDLE_SECONDS) -> bool:
        return self.last_record_final_text and self.idle_for() >= idle_seconds


def discover_session_jsonl(
    *,
    projects_root: Path,
    cwd: str,
    started_after: float,
    exclude: set[Path] | None = None,
) -> Path | None:
    """Find the transcript of the session we just launched.

    Directory-name munging of cwd is undocumented, so instead of guessing it
    we scan for fresh .jsonl files and confirm ownership by the ``cwd`` field
    inside the first records.
    """
    exclude = exclude or set()
    if not projects_root.is_dir():
        return None
    target = os.path.normcase(os.path.normpath(cwd))
    candidates: list[tuple[float, Path]] = []
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            if jsonl in exclude:
                continue
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            if mtime >= started_after:
                candidates.append((mtime, jsonl))
    for _, jsonl in sorted(candidates, reverse=True):
        if _transcript_cwd_matches(jsonl, target):
            return jsonl
    return None


def _transcript_cwd_matches(jsonl: Path, normalized_cwd: str) -> bool:
    try:
        with jsonl.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    return False
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = record.get("cwd")
                if cwd:
                    return os.path.normcase(os.path.normpath(str(cwd))) == normalized_cwd
    except OSError:
        return False
    return False


def build_handoff(jsonl_path: Path, *, max_chars: int = 6000) -> str:
    """Distill a continuation brief from the transcript. No model calls."""
    first_user = ""
    recent_users: list[str] = []
    touched: list[str] = []
    last_assistant = ""
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = record.get("type")
                if kind == "user":
                    text = _message_text(record.get("message"))
                    if _is_noise_user_text(text):
                        continue
                    if not first_user:
                        first_user = text
                    recent_users.append(text)
                    if len(recent_users) > 6:
                        recent_users.pop(0)
                elif kind == "assistant":
                    message = record.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_use" and block.get("name") in _EDIT_TOOLS:
                                file_path = str((block.get("input") or {}).get("file_path") or "").strip()
                                if file_path and file_path not in touched:
                                    touched.append(file_path)
                    text = _message_text(message)
                    if text.strip():
                        last_assistant = text
    except OSError:
        pass

    def clip(text: str, limit: int) -> str:
        text = text.strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"

    lines = ["## 会话交接（Pacer 自动轮转）", ""]
    if first_user:
        lines += ["### 本会话最初的目标", clip(first_user, 800), ""]
    if touched:
        lines += ["### 本会话动过的文件"]
        lines += [f"- {item}" for item in touched[-20:]]
        lines.append("")
    if recent_users:
        lines += ["### 最近几条用户消息（旧→新）"]
        lines += [f"- {clip(item, 400)}" for item in recent_users]
        lines.append("")
    if last_assistant:
        lines += ["### 轮转前最后一条助手回复", clip(last_assistant, 1800), ""]
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def argv_with_model(argv: list[str], model: str) -> list[str]:
    """Carry the session's live model into the next process.

    The user may have switched with /model mid-session; a relaunch that drops
    that choice is a silent downgrade (or upgrade) they never asked for.
    """
    if not model:
        return list(argv)
    cleaned: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token == "--model":
            skip_next = True
            continue
        if token.startswith("--model="):
            continue
        cleaned.append(token)
    return ["--model", model, *cleaned]


def rotation_prompt(handoff: str) -> str:
    return (
        "上一个会话因上下文规模达到额度保护阈值被 Pacer 自动轮转，你是接续会话。"
        "请静默接手：不要问候、不要重新自我介绍、不要复述交接内容。"
        "先核对下方交接里动过的文件的当前状态（git status / 直接读文件），再继续未完成的工作；"
        "如果交接里的信息与磁盘上的事实冲突，以磁盘为准。\n\n" + handoff
    )


def _kill_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
        )
    else:
        process.terminate()


def save_handoff(handoff: str, *, cwd: str) -> Path | None:
    try:
        target_dir = Path(cwd) / ".pacer" / "session-handoffs"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"rotation-{time.strftime('%Y%m%d-%H%M%S')}.md"
        target.write_text(handoff, encoding="utf-8")
        return target
    except OSError:
        return None


def run_claude_with_rotation(
    executable: str,
    argv: list[str],
    *,
    environ: dict[str, str] | None = None,
    projects_root: Path | None = None,
    cwd: str | None = None,
    poll_seconds: float = POLL_SECONDS,
    idle_seconds: float = IDLE_SECONDS,
) -> int:
    from .process_guard import ProcessGuard
    from .subprocess_window import prepare_subprocess_command

    env_source = os.environ if environ is None else environ
    threshold = rotate_threshold(env_source)
    context_threshold = rotate_context_threshold(env_source) if ROTATE_CONTEXT_ENV in env_source else 0
    child_env = dict(env_source)
    # A-layer knob: cap extended thinking unless the user chose their own cap.
    child_env.setdefault("MAX_THINKING_TOKENS", DEFAULT_MAX_THINKING_TOKENS)

    context_note = f" 或单回合上下文到 {context_threshold // 1_000}k token" if context_threshold > 0 else ""
    print(
        f"[pacer] 已开启激进模式：单会话到 {threshold // 1_000_000}M token{context_note} 会杀掉会话重开并交接"
        f"（会中断你正在做的事）。关闭方式 {ROTATE_ENV}=0，调会话阈值 {ROTATE_TOKENS_ENV}=<tokens>，调回合阈值 {ROTATE_CONTEXT_ENV}=<tokens>。"
    )

    cwd = cwd or os.getcwd()
    projects_root = projects_root or claude_projects_root()
    used_transcripts: set[Path] = set()
    pending_prompt: str | None = None
    live_model = ""
    rotations = 0

    guard = ProcessGuard()
    guard.start()
    try:
        while True:
            launch_argv = argv_with_model(argv, live_model)
            if pending_prompt:
                launch_argv.append(pending_prompt)
            command = prepare_subprocess_command([executable, *launch_argv])
            started = time.time()
            process = subprocess.Popen(command, env=child_env)
            guard.adopt(process.pid)

            meter: SessionMeter | None = None
            rotate_requested = False
            while True:
                returncode = process.poll()
                if returncode is not None:
                    break
                try:
                    time.sleep(poll_seconds)
                except KeyboardInterrupt:
                    # Ctrl+C reaching the wrapper must never take the session
                    # down with it (guard.close would kill the whole tree).
                    # Claude handles its own interrupts; just keep waiting.
                    try:
                        process.wait()
                    except KeyboardInterrupt:
                        pass
                    continue
                if meter is None:
                    found = discover_session_jsonl(
                        projects_root=projects_root,
                        cwd=cwd,
                        started_after=started - 5,
                        exclude=used_transcripts,
                    )
                    if found is not None:
                        meter = SessionMeter(path=found)
                        used_transcripts.add(found)
                if meter is None:
                    continue
                meter.refresh()
                should_rotate = (
                    meter.total_tokens >= threshold
                    or (context_threshold > 0 and meter.last_context_tokens >= context_threshold)
                )
                if should_rotate and meter.at_turn_boundary(idle_seconds=idle_seconds):
                    rotate_requested = True
                    _kill_tree(process)
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        pass
                    break

            if not rotate_requested:
                return int(process.returncode or 0)

            rotations += 1
            handoff = build_handoff(meter.path) if meter else ""
            saved = save_handoff(handoff, cwd=cwd) if handoff else None
            pending_prompt = rotation_prompt(handoff)
            live_model = (meter.last_model if meter else "") or live_model
            location = f"，交接存档 {saved}" if saved else ""
            model_note = f"，沿用模型 {live_model}" if live_model else ""
            print(
                f"\n[pacer] 第 {rotations} 次会话轮转：上一会话消耗约 "
                f"{(meter.total_tokens if meter else 0) / 1_000_000:.0f}M token，已达阈值。"
                f"正在带交接开启新会话{location}{model_note}……\n"
            )
            sys.stdout.flush()
    finally:
        guard.close()
