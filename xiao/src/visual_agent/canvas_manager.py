"""Local State Canvas Manager for Pacer Stateless Micro-Agent Architecture.

Maintains `.pacer/canvas.json` in the workspace as the single external shared memory
across subagent invocations, replacing multi-turn conversation history accumulation.

Core capabilities:
1. Atomic cross-process read/write via `portalocker` and temp-file rename (`os.replace`).
2. Structured state slices:
   - `Goal`: Current objective, acceptance command, and execution status.
   - `Invariants`: Immutable project/task rules that subagents must not violate.
   - `FocusFiles`: Active file paths mapped to compact AST symbol summaries & line ranges.
   - `Scratchpad`: Latest verification error trace, failing line, attempt counter, and
     rejected hypotheses (preventing stateless micro-agents from repeating dead ends).
3. `build_stateless_prompt(task_instruction)`: Assembles a high-density, single-turn
   stateless prompt strictly capped within `max_prompt_tokens` (default: 1,500 tokens).
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator

try:
    import portalocker
except ImportError:  # pragma: no cover
    portalocker = None  # type: ignore[assignment]


DEFAULT_MAX_PROMPT_TOKENS = 1500
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def estimate_tokens(text: str) -> int:
    """Fast, conservative token estimator for mixed CJK + English/code text.

    Approximates ~0.65 tokens per CJK character and ~1 token per 3.6 ASCII/code characters.
    """
    if not text:
        return 0
    cjk_count = len(_CJK_RE.findall(text))
    non_cjk_count = max(0, len(text) - cjk_count)
    return int(cjk_count * 0.7 + (non_cjk_count + 3) / 3.5)


def truncate_to_token_budget(text: str, budget_tokens: int, *, tail_preserve_ratio: float = 0.0) -> str:
    """Truncate `text` so its estimated token count never exceeds `budget_tokens`."""
    if budget_tokens <= 0:
        return ""
    if estimate_tokens(text) <= budget_tokens:
        return text

    marker = "\n... [truncated for 1.5k token budget] ...\n"
    marker_tokens = estimate_tokens(marker)
    if marker_tokens >= budget_tokens:
        marker = "\n... [truncated]"
        marker_tokens = estimate_tokens(marker)

    effective_budget = max(0, budget_tokens - marker_tokens)

    if 0.0 < tail_preserve_ratio < 1.0 and effective_budget > 0:
        tail_budget = int(effective_budget * tail_preserve_ratio)
        head_budget = max(0, effective_budget - tail_budget)

        low, high = 0, len(text)
        best_head = 0
        while low <= high:
            mid = (low + high) // 2
            if estimate_tokens(text[:mid]) <= head_budget:
                best_head = mid
                low = mid + 1
            else:
                high = mid - 1
        head_text = text[:best_head].rstrip()

        low, high = 0, len(text) - best_head
        best_tail = 0
        while low <= high:
            mid = (low + high) // 2
            if estimate_tokens(text[-mid:] if mid > 0 else "") <= tail_budget:
                best_tail = mid
                low = mid + 1
            else:
                high = mid - 1
        tail_text = text[-best_tail:].lstrip() if best_tail > 0 else ""

        candidate = f"{head_text}{marker}{tail_text}" if (head_text or tail_text) else marker.strip()
    else:
        suffix = "\n... [truncated]"
        s_budget = max(0, budget_tokens - estimate_tokens(suffix))
        low, high = 0, len(text)
        best_idx = 0
        while low <= high:
            mid = (low + high) // 2
            if estimate_tokens(text[:mid]) <= s_budget:
                best_idx = mid
                low = mid + 1
            else:
                high = mid - 1
        candidate = text[:best_idx].rstrip() + suffix

    while candidate and estimate_tokens(candidate) > budget_tokens:
        candidate = candidate[:-1].rstrip()
    return candidate


@dataclass
class CanvasGoal:
    summary: str = ""
    acceptance_cmd: str = ""
    status: str = "pending"  # pending | in_progress | completed | failed
    updated_at: str = field(default_factory=_utc_now)


@dataclass
class FocusFileEntry:
    path: str
    symbols: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    target_snippet: str = ""
    updated_at: str = field(default_factory=_utc_now)


@dataclass
class CanvasScratchpad:
    latest_error: str = ""
    failing_line: str = ""
    attempt_count: int = 0
    last_failed_hypothesis: str = ""
    failed_hypotheses: list[str] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=_utc_now)


@dataclass
class CanvasState:
    version: str = "2.0"
    workspace_root: str = ""
    goal: CanvasGoal = field(default_factory=CanvasGoal)
    invariants: list[str] = field(default_factory=list)
    focus_files: dict[str, FocusFileEntry] = field(default_factory=dict)
    scratchpad: CanvasScratchpad = field(default_factory=CanvasScratchpad)
    updated_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "workspace_root": self.workspace_root,
            "goal": asdict(self.goal),
            "invariants": list(self.invariants),
            "focus_files": {k: asdict(v) for k, v in self.focus_files.items()},
            "scratchpad": asdict(self.scratchpad),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], workspace_root: str = "") -> CanvasState:
        goal_raw = data.get("goal") if isinstance(data.get("goal"), dict) else {}
        goal = CanvasGoal(
            summary=str(goal_raw.get("summary") or ""),
            acceptance_cmd=str(goal_raw.get("acceptance_cmd") or ""),
            status=str(goal_raw.get("status") or "pending"),
            updated_at=str(goal_raw.get("updated_at") or _utc_now()),
        )

        invariants_raw = data.get("invariants")
        invariants = [str(item) for item in invariants_raw] if isinstance(invariants_raw, list) else []

        focus_files: dict[str, FocusFileEntry] = {}
        focus_raw = data.get("focus_files")
        if isinstance(focus_raw, dict):
            for path_key, entry_val in focus_raw.items():
                if isinstance(entry_val, dict):
                    focus_files[str(path_key)] = FocusFileEntry(
                        path=str(entry_val.get("path") or path_key),
                        symbols=[str(s) for s in entry_val.get("symbols", []) if s],
                        imports=[str(i) for i in entry_val.get("imports", []) if i],
                        target_snippet=str(entry_val.get("target_snippet") or ""),
                        updated_at=str(entry_val.get("updated_at") or _utc_now()),
                    )
                elif isinstance(entry_val, list):
                    focus_files[str(path_key)] = FocusFileEntry(
                        path=str(path_key),
                        symbols=[str(s) for s in entry_val if s],
                    )

        scratch_raw = data.get("scratchpad") if isinstance(data.get("scratchpad"), dict) else {}
        scratchpad = CanvasScratchpad(
            latest_error=str(scratch_raw.get("latest_error") or ""),
            failing_line=str(scratch_raw.get("failing_line") or ""),
            attempt_count=int(scratch_raw.get("attempt_count") or 0),
            last_failed_hypothesis=str(scratch_raw.get("last_failed_hypothesis") or ""),
            failed_hypotheses=[str(h) for h in scratch_raw.get("failed_hypotheses", []) if h],
            completed_steps=[str(s) for s in scratch_raw.get("completed_steps", []) if s],
            updated_at=str(scratch_raw.get("updated_at") or _utc_now()),
        )

        return cls(
            version=str(data.get("version") or "2.0"),
            workspace_root=str(data.get("workspace_root") or workspace_root),
            goal=goal,
            invariants=invariants,
            focus_files=focus_files,
            scratchpad=scratchpad,
            updated_at=str(data.get("updated_at") or _utc_now()),
        )


class CanvasManager:
    """Manages `.pacer/canvas.json` with atomic locking and <=1,500-token prompt synthesis."""

    def __init__(
        self,
        workspace_root: str | Path = ".",
        *,
        canvas_rel_path: str = ".pacer/canvas.json",
        max_prompt_tokens: int = DEFAULT_MAX_PROMPT_TOKENS,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.canvas_path = (self.workspace_root / canvas_rel_path).resolve()
        self.lock_path = self.canvas_path.with_suffix(".lock")
        self.max_prompt_tokens = max(256, int(max_prompt_tokens))

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        self.canvas_path.parent.mkdir(parents=True, exist_ok=True)
        if portalocker is not None:
            with portalocker.Lock(str(self.lock_path), timeout=5.0):
                yield
        else:
            yield

    def _read_unlocked(self) -> CanvasState:
        if not self.canvas_path.is_file():
            return CanvasState(workspace_root=str(self.workspace_root))
        try:
            raw = json.loads(self.canvas_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return CanvasState.from_dict(raw, workspace_root=str(self.workspace_root))
        except Exception:
            pass
        return CanvasState(workspace_root=str(self.workspace_root))

    def _write_unlocked(self, state: CanvasState) -> None:
        self.canvas_path.parent.mkdir(parents=True, exist_ok=True)
        state.updated_at = _utc_now()
        tmp_path = self.canvas_path.with_suffix(f".tmp.{os.getpid()}")
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, self.canvas_path)

    def load(self) -> CanvasState:
        """Atomically read current CanvasState from `.pacer/canvas.json`."""
        with self._file_lock():
            return self._read_unlocked()

    def save(self, state: CanvasState) -> CanvasState:
        """Atomically persist CanvasState to `.pacer/canvas.json`."""
        with self._file_lock():
            self._write_unlocked(state)
            return state

    def reset(self) -> CanvasState:
        """Reset `.pacer/canvas.json` to a clean initial state."""
        state = CanvasState(workspace_root=str(self.workspace_root))
        return self.save(state)

    def update_goal(
        self,
        summary: str,
        *,
        acceptance_cmd: str | None = None,
        status: str = "in_progress",
        reset_scratchpad: bool = False,
    ) -> CanvasState:
        """Atomically update the current Goal (and optionally clear scratchpad for a new run)."""
        with self._file_lock():
            state = self._read_unlocked()
            state.goal.summary = str(summary).strip()
            if acceptance_cmd is not None:
                state.goal.acceptance_cmd = str(acceptance_cmd).strip()
            state.goal.status = status
            state.goal.updated_at = _utc_now()
            if reset_scratchpad:
                state.scratchpad = CanvasScratchpad()
            self._write_unlocked(state)
            return state

    def set_invariants(self, invariants: list[str]) -> CanvasState:
        """Replace the invariant list with deduplicated rules."""
        with self._file_lock():
            state = self._read_unlocked()
            deduped: list[str] = []
            for item in invariants:
                cleaned = str(item).strip()
                if cleaned and cleaned not in deduped:
                    deduped.append(cleaned)
            state.invariants = deduped
            self._write_unlocked(state)
            return state

    def add_invariants(self, *invariants: str) -> CanvasState:
        """Append one or more invariants without duplicating existing rules."""
        with self._file_lock():
            state = self._read_unlocked()
            for item in invariants:
                cleaned = str(item).strip()
                if cleaned and cleaned not in state.invariants:
                    state.invariants.append(cleaned)
            self._write_unlocked(state)
            return state

    def upsert_focus_file(
        self,
        rel_path: str,
        *,
        symbols: list[str] | None = None,
        imports: list[str] | None = None,
        target_snippet: str | None = None,
        max_focus_files: int = 6,
    ) -> CanvasState:
        """Update or insert AST symbols and focused code snippet for `rel_path`."""
        norm_path = str(rel_path).replace("\\", "/").lstrip("/")
        with self._file_lock():
            state = self._read_unlocked()
            existing = state.focus_files.get(norm_path) or FocusFileEntry(path=norm_path)
            if symbols is not None:
                existing.symbols = [str(s).strip() for s in symbols if str(s).strip()][:25]
            if imports is not None:
                existing.imports = [str(i).strip() for i in imports if str(i).strip()][:15]
            if target_snippet is not None:
                existing.target_snippet = str(target_snippet).strip()
            existing.updated_at = _utc_now()
            state.focus_files[norm_path] = existing

            if len(state.focus_files) > max_focus_files:
                ordered = sorted(
                    state.focus_files.items(),
                    key=lambda kv: kv[1].updated_at,
                    reverse=True,
                )
                state.focus_files = dict(ordered[:max_focus_files])

            self._write_unlocked(state)
            return state

    def update_scratchpad(
        self,
        *,
        latest_error: str | None = None,
        failing_line: str | None = None,
        attempt_count: int | None = None,
        last_failed_hypothesis: str | None = None,
        completed_step: str | None = None,
    ) -> CanvasState:
        """Atomically update execution scratchpad, error trace, and failed hypothesis log."""
        with self._file_lock():
            state = self._read_unlocked()
            sp = state.scratchpad
            if latest_error is not None:
                sp.latest_error = str(latest_error).strip()[-1200:]
            if failing_line is not None:
                sp.failing_line = str(failing_line).strip()
            if attempt_count is not None:
                sp.attempt_count = int(attempt_count)
            if last_failed_hypothesis is not None:
                hyp = str(last_failed_hypothesis).strip()
                sp.last_failed_hypothesis = hyp
                if hyp and hyp not in sp.failed_hypotheses:
                    sp.failed_hypotheses.append(hyp)
                    sp.failed_hypotheses = sp.failed_hypotheses[-5:]
            if completed_step is not None:
                step = str(completed_step).strip()
                if step and step not in sp.completed_steps:
                    sp.completed_steps.append(step)
                    sp.completed_steps = sp.completed_steps[-8:]
            sp.updated_at = _utc_now()
            self._write_unlocked(state)
            return state

    def build_stateless_prompt(
        self,
        task_instruction: str,
        *,
        max_tokens: int | None = None,
        require_search_replace: bool = True,
    ) -> str:
        """Assemble a single-turn, history-free prompt strictly within `max_tokens` (<= 1,500 tokens)."""
        budget = min(self.max_prompt_tokens, max_tokens or self.max_prompt_tokens)
        state = self.load()

        if require_search_replace:
            format_block = (
                "[OUTPUT FORMAT: SEARCH/REPLACE DIFF ONLY]\n"
                "Rely on the provided goal, task instructions, and indexed files below. "
                "Do NOT output full files unless creating a brand-new file. "
                "Emit minimal patches using this exact format:\n"
                "path/to/file.py\n"
                "```python\n"
                "<<<<<<< SEARCH\n"
                "<exact existing lines>\n"
                "=======\n"
                "<replacement lines>\n"
                ">>>>>>> REPLACE\n"
                "```\n"
                "If creating a new file, the SEARCH block may be empty."
            )
        else:
            format_block = "[EXECUTION MODE: STATELESS MICRO-AGENT]\nRespond concisely and directly."

        goal_lines: list[str] = ["[GOAL]"]
        if state.goal.summary:
            goal_lines.append(f"- Objective: {state.goal.summary}")
        if state.goal.acceptance_cmd:
            goal_lines.append(f"- Acceptance Gate: `{state.goal.acceptance_cmd}`")

        if state.invariants:
            goal_lines.append("\n[INVARIANTS (DO NOT VIOLATE)]")
            for inv in state.invariants[:6]:
                goal_lines.append(f"- {inv}")
        goal_block = truncate_to_token_budget("\n".join(goal_lines), int(budget * 0.18))

        task_block = truncate_to_token_budget(
            f"[CURRENT TASK]\n{task_instruction.strip()}",
            int(budget * 0.22),
        )

        sp = state.scratchpad
        scratch_lines: list[str] = []
        if sp.attempt_count > 0 or sp.latest_error or sp.failed_hypotheses or sp.completed_steps:
            scratch_lines.append("[SCRATCHPAD & EXECUTION STATE]")
            if sp.completed_steps:
                scratch_lines.append(f"- Completed Steps: {', '.join(sp.completed_steps[-4:])}")
            if sp.attempt_count > 0:
                scratch_lines.append(f"- Retry Attempt: #{sp.attempt_count}")
            if sp.failing_line:
                scratch_lines.append(f"- Failing Location: {sp.failing_line}")
            if sp.failed_hypotheses:
                scratch_lines.append("- Rejected Approaches (DO NOT REPEAT):")
                for hyp in sp.failed_hypotheses[-3:]:
                    scratch_lines.append(f"  * {hyp}")
            if sp.latest_error:
                scratch_lines.append(f"- Latest Verification Error:\n{sp.latest_error}")
        scratch_block = truncate_to_token_budget(
            "\n".join(scratch_lines),
            int(budget * 0.22),
            tail_preserve_ratio=0.65,
        )

        focus_lines: list[str] = []
        if state.focus_files:
            focus_lines.append("[FOCUS FILES & AST SYMBOLS]")
            for rel_path, entry in list(state.focus_files.items())[:4]:
                focus_lines.append(f"File: `{rel_path}`")
                if entry.imports:
                    focus_lines.append(f"  Imports: {', '.join(entry.imports[:8])}")
                if entry.symbols:
                    focus_lines.append("  AST Symbols: " + "; ".join(entry.symbols[:12]))
                if entry.target_snippet:
                    snippet_trimmed = truncate_to_token_budget(entry.target_snippet, 180)
                    focus_lines.append(f"  Focused Code Excerpt:\n{snippet_trimmed}")

        used_so_far = (
            estimate_tokens(format_block)
            + estimate_tokens(goal_block)
            + estimate_tokens(task_block)
            + estimate_tokens(scratch_block)
            + 20
        )
        remaining_for_focus = max(120, budget - used_so_far)
        focus_block = truncate_to_token_budget("\n".join(focus_lines), remaining_for_focus)

        sections = [
            s
            for s in (goal_block, scratch_block, focus_block, task_block, format_block)
            if s and s.strip()
        ]
        assembled = "\n\n".join(sections)

        if estimate_tokens(assembled) > budget:
            assembled = truncate_to_token_budget(assembled, budget, tail_preserve_ratio=0.35)
        return assembled