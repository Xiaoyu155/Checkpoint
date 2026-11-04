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
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterator

from .session_projection import SessionProjection

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
    version: str = "2.1"
    workspace_root: str = ""
    goal: CanvasGoal = field(default_factory=CanvasGoal)
    invariants: list[str] = field(default_factory=list)
    focus_files: dict[str, FocusFileEntry] = field(default_factory=dict)
    recent_focus_files: list[str] = field(default_factory=list)
    scratchpad: CanvasScratchpad = field(default_factory=CanvasScratchpad)
    updated_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "workspace_root": self.workspace_root,
            "goal": asdict(self.goal),
            "invariants": list(self.invariants),
            "focus_files": {k: asdict(v) for k, v in self.focus_files.items()},
            "recent_focus_files": list(self.recent_focus_files),
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

        recent_raw = data.get("recent_focus_files")
        recent_focus_files: list[str] = []
        if isinstance(recent_raw, list):
            for item in recent_raw:
                normalized = str(item or "").replace("\\", "/").strip().lstrip("/")
                path = Path(normalized)
                if (normalized and not path.is_absolute() and ".." not in path.parts
                        and not re.match(r"^[A-Za-z]:", normalized) and normalized not in recent_focus_files):
                    recent_focus_files.append(normalized)

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
            version=str(data.get("version") or "2.1"),
            workspace_root=str(data.get("workspace_root") or workspace_root),
            goal=goal,
            invariants=invariants,
            focus_files=focus_files,
            recent_focus_files=recent_focus_files[:12],
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
        self.session_projection = SessionProjection(self.workspace_root)

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
        except (OSError, ValueError, TypeError):
            return CanvasState(workspace_root=str(self.workspace_root))
        return CanvasState(workspace_root=str(self.workspace_root))

    def record_verified_lesson(
        self, category: str, rule_text: str, target_files: list[str]
    ) -> bool:
        """Store a compact, sanitized rule; return False for an existing rule.

        Target paths are deliberately excluded from persistent lessons. Secret-like
        assignments, URLs, paths, and common credential formats are redacted.
        """
        def sanitize(value: str) -> str:
            value = re.sub(
                r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|"
                r"authorization|credential)\b\s*[:=]\s*[^\s,;]+",
                "[redacted]", str(value),
            )
            value = re.sub(
                r"(?i)\b(?:bearer\s+\S+|sk-[A-Za-z0-9_-]+|"
                r"gh[pousr]_[A-Za-z0-9_]+|AKIA[A-Z0-9]{16})\b",
                "[redacted]", value,
            )
            value = re.sub(r"(?i)\bhttps?://[^\s]+", "[path]", value)
            value = re.sub(
                r"(?<!\w)(?:[A-Za-z]:[\\/]|~[\\/]|[\\/]{1,2})[^\s,;]+"
                r"|\b[\w.-]+(?:[\\/][\w.-]+)+",
                "[path]", value,
            )
            for path in target_files:
                if str(path):
                    value = value.replace(str(path), "[path]")
            return " ".join(value.replace("<!--", "").replace("-->", "").split())

        cleaned_category = sanitize(category)
        cleaned_rule = sanitize(rule_text)
        if not cleaned_category or not cleaned_rule:
            raise ValueError("category and rule_text must contain text")
        words = (cleaned_category + " " + cleaned_rule).split()[:50]
        content = " ".join(words)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        line = f"- <!-- {digest} --> {content}\n"
        lessons_path = self.workspace_root / ".pacer" / "lessons.md"
        with self._file_lock():
            existing = lessons_path.read_text(encoding="utf-8") if lessons_path.exists() else ""
            if re.search(rf"^-\s*<!-- {digest} -->", existing, re.MULTILINE):
                return False
            prefix = existing
            if prefix and not prefix.endswith("\n"):
                prefix += "\n"
            tmp_path = lessons_path.with_suffix(f".tmp.{os.getpid()}")
            tmp_path.write_text(prefix + line, encoding="utf-8")
            os.replace(tmp_path, lessons_path)
        return True

    def read_lessons(self) -> list[str]:
        """Read lesson text from the local Markdown store."""
        lessons_path = self.workspace_root / ".pacer" / "lessons.md"
        with self._file_lock():
            if not lessons_path.exists():
                return []
            text = lessons_path.read_text(encoding="utf-8")
        return re.findall(r"^- <!-- [0-9a-f]{64} --> (.+)$", text, re.MULTILINE)

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
        reset_focus: bool = False,
    ) -> CanvasState:
        """Atomically update the current Goal (and optionally clear scratchpad/focus for a new run)."""
        with self._file_lock():
            state = self._read_unlocked()
            state.goal.summary = str(summary).strip()
            if acceptance_cmd is not None:
                state.goal.acceptance_cmd = str(acceptance_cmd).strip()
            state.goal.status = status
            state.goal.updated_at = _utc_now()
            if reset_scratchpad:
                state.scratchpad = CanvasScratchpad()
            if reset_focus:
                state.focus_files.clear()
            self._write_unlocked(state)
            self.session_projection.append_event("task_contract", summary=state.goal.summary, acceptance_cmd=state.goal.acceptance_cmd, status=state.goal.status)
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
            self.session_projection.append_event("invariants", items=list(state.invariants))
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
            self.session_projection.append_event("invariants", items=list(state.invariants))
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

    def set_recent_focus_files(self, paths: list[str], *, limit: int = 12) -> CanvasState:
        """Persist the most recently patched files for the next stateless request."""
        normalized = list(dict.fromkeys(
            str(path).replace("\\", "/").strip().lstrip("/")
            for path in paths
            if str(path).strip()
        ))
        normalized = [
            path for path in normalized
            if not Path(path).is_absolute() and ".." not in Path(path).parts and not re.match(r"^[A-Za-z]:", path)
        ]
        with self._file_lock():
            state = self._read_unlocked()
            state.recent_focus_files = normalized[:max(0, int(limit))]
            self._write_unlocked(state)
        self.session_projection.append_event("recent_focus_files", paths=list(state.recent_focus_files))
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
        target_files: list[str] | None = None,
        acceptance_exam: list[dict[str, Any]] | None = None,
        context_profile: str = "",
    ) -> str:
        """Assemble a single-turn, history-free prompt strictly within `max_tokens` (<= 1,500 tokens)."""
        budget = min(self.max_prompt_tokens, max_tokens or self.max_prompt_tokens)
        if budget < 128:
            raise ValueError("Prompt budget must be at least 128 tokens to preserve focus and patch format")
        state = self.load()

        if require_search_replace:
            format_block = (
                "[OUTPUT FORMAT: SEARCH/REPLACE DIFF ONLY]\n"
                "Use minimal patches, not full files. For each file, emit `path/to/file.py` and this exact fenced form:\n"
                "```python\n<<<<<<< SEARCH\n<exact existing lines>\n=======\n<replacement lines>\n>>>>>>> REPLACE\n```\n"
                "An empty SEARCH creates a file. Never copy displayed line numbers; SEARCH must exactly match the source."
            )
        else:
            format_block = "[EXECUTION MODE: STATELESS MICRO-AGENT]\nRespond concisely and directly."

        goal_lines: list[str] = ["[GOAL]"]
        if state.goal.summary:
            goal_lines.append(f"- Objective: {state.goal.summary}")
        if state.goal.acceptance_cmd:
            goal_lines.append(f"- Acceptance Gate: `{state.goal.acceptance_cmd}`")

        goal_block = truncate_to_token_budget("\n".join(goal_lines), int(budget * 0.05))

        task_block = truncate_to_token_budget(
            f"[CURRENT TASK]\n{task_instruction.strip()}",
            int(budget * 0.12),
        )

        invariant_lines = ["[2. PROJECT INVARIANTS] [INVARIANTS (DO NOT VIOLATE)]"]
        invariant_lines.extend(f"- {inv}" for inv in state.invariants[:8])
        invariant_block = truncate_to_token_budget("\n".join(invariant_lines), int(budget * 0.08))

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
            int(budget * 0.07),
            tail_preserve_ratio=0.65,
        )

        focus_lines: list[str] = ["[3. TARGET FILE AST SIGNATURES] [FOCUS FILES & AST SYMBOLS]"]
        if state.focus_files or state.recent_focus_files:
            tf_set = {str(tf).replace("\\", "/").lstrip("/") for tf in (target_files or [])}
            entries = dict(state.focus_files)
            # Re-index recent files lazily when a new run reset the detailed focus map.
            for rel_path in state.recent_focus_files:
                if rel_path in entries:
                    continue
                try:
                    from .local_indexer import index_file_symbols

                    indexed = index_file_symbols(rel_path, workspace_root=self.workspace_root)
                    if not indexed.error:
                        entries[rel_path] = FocusFileEntry(
                            path=rel_path,
                            symbols=indexed.compact_symbols(),
                            imports=indexed.imports,
                            target_snippet=indexed.focused_snippet,
                        )
                except (OSError, ValueError):
                    continue
            # Explicit targets first, then the previous turn's patched files, then other context.
            recent_rank = {path: index for index, path in enumerate(state.recent_focus_files)}
            ordered_files = sorted(entries.items(), key=lambda kv: kv[1].updated_at, reverse=True)
            ordered_files.sort(key=lambda kv: (
                0 if kv[0] in tf_set else 1,
                recent_rank.get(kv[0], len(recent_rank)),
            ))
            for rel_path, entry in ordered_files[:4]:
                focus_lines.append(f"File: `{rel_path}`")
                if entry.imports:
                    focus_lines.append(f"  Imports: {', '.join(entry.imports[:8])}")
                if entry.symbols:
                    focus_lines.append("  AST Symbols: " + "; ".join(entry.symbols[:12]))
                if entry.target_snippet:
                    snippet_lines: list[str] = []
                    for code_line in entry.target_snippet.splitlines():
                        if estimate_tokens("\n".join((*snippet_lines, code_line))) > max(180, int(budget * 0.35)):
                            break
                        snippet_lines.append(code_line)
                    if snippet_lines:
                        focus_lines.append("  Focused Code Excerpt:\n" + "\n".join(snippet_lines))

        exam_lines: list[str] = []
        if acceptance_exam:
            exam_lines.append("[ACCEPTANCE EXAM / 验收考题]")
            exam_lines.append("Your code will be physically graded against these assertions before acceptance:")
            for i, ex in enumerate(acceptance_exam, 1):
                qid = ex.get("id") or f"exam_{i}"
                question = ex.get("question") or ""
                check_type = ex.get("check_type") or "contract"
                command = ex.get("assertion_cmd") or ""
                exam_lines.append(f"{i}. [{qid}] {question} (type: {check_type})")
                if command:
                    exam_lines.append(f"   command: {command}")
        exam_block = truncate_to_token_budget("\n".join(exam_lines), int(budget * 0.04))
        profile_block = truncate_to_token_budget(
            "[PROJECT CONTEXT PROFILE]\n" + context_profile.strip() if context_profile.strip() else "",
            int(budget * 0.05),
        )

        static_contract = truncate_to_token_budget(
            "[1. FIXED SYSTEM CONTRACT]\n"
            "You are a stateless Pacer micro-agent. Use only the logged goal, invariants, AST signatures, and dynamic task below. "
            "Do not rely on chat history or claim work that was not verified.",
            int(budget * 0.08),
        )
        if estimate_tokens(format_block) > int(budget * 0.22):
            format_block = (
                "[OUTPUT FORMAT: SEARCH/REPLACE ONLY]\n"
                "Use exact source lines between `<<<<<<< SEARCH` and `=======`, then replacement lines before `>>>>>>> REPLACE`."
            )
        if estimate_tokens(format_block) > int(budget * 0.45):
            format_block = "[OUTPUT FORMAT] Exact `<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` patch only."
        dynamic_format = format_block
        used_so_far = sum(estimate_tokens(part) for part in (
            static_contract, invariant_block, goal_block, task_block, scratch_block, exam_block, dynamic_format, profile_block,
        )) + 8
        remaining_for_focus = max(0, budget - used_so_far)
        has_focus_content = len(focus_lines) > 1
        focus_budget = max(int(budget * 0.25), remaining_for_focus) if has_focus_content else remaining_for_focus

        retained_focus: list[str] = []
        for line in "\n".join(focus_lines).splitlines():
            candidate = "\n".join((*retained_focus, line))
            if estimate_tokens(candidate) <= focus_budget:
                retained_focus.append(line)
                continue
            if line.startswith("  AST Symbols: "):
                prefix = "  AST Symbols: "
                symbols = line[len(prefix):].split("; ")
                partial = prefix
                for symbol in symbols:
                    proposed = partial + ("; " if partial != prefix else "") + symbol
                    if estimate_tokens("\n".join((*retained_focus, proposed))) > focus_budget:
                        break
                    partial = proposed
                if partial != prefix:
                    retained_focus.append(partial)
            break
        focus_block = "\n".join(retained_focus)

        dynamic_tail = "\n\n".join(
            section for section in (
                "[4. DYNAMIC TASK AND EXECUTION ERRORS]",
                goal_block,
                task_block,
                profile_block,
                dynamic_format,
                exam_block,
                scratch_block,
            ) if section and section.strip()
        )
        # Keep the cacheable prefix stable: contract -> invariants/profile -> AST signatures.
        assembled = "\n\n".join(
            section for section in (static_contract, invariant_block, focus_block, dynamic_tail)
            if section and section.strip()
        )

        if estimate_tokens(assembled) > budget:
            optional_blocks = ["scratch", "profile", "exam", "goal", "invariants", "task", "static"]
            blocks = {
                "scratch": scratch_block,
                "profile": profile_block,
                "exam": exam_block,
                "goal": goal_block,
                "invariants": invariant_block,
                "task": task_block,
                "static": static_contract,
            }
            for name in optional_blocks:
                while estimate_tokens(assembled) > budget and blocks[name]:
                    excess = estimate_tokens(assembled) - budget
                    current_tokens = estimate_tokens(blocks[name])
                    next_budget = max(0, current_tokens - max(1, excess))
                    blocks[name] = truncate_to_token_budget(blocks[name], next_budget, tail_preserve_ratio=0.5)
                    scratch_block = blocks["scratch"]
                    profile_block = blocks["profile"]
                    exam_block = blocks["exam"]
                    goal_block = blocks["goal"]
                    invariant_block = blocks["invariants"]
                    task_block = blocks["task"]
                    static_contract = blocks["static"]
                    dynamic_tail = "\n\n".join(
                        section for section in (
                            "[4. DYNAMIC TASK AND EXECUTION ERRORS]",
                            goal_block,
                            task_block,
                            profile_block,
                            dynamic_format,
                            exam_block,
                            scratch_block,
                        ) if section and section.strip()
                    )
                    assembled = "\n\n".join(
                        section for section in (static_contract, invariant_block, focus_block, dynamic_tail)
                        if section and section.strip()
                    )
                    if next_budget == 0:
                        break
        self.session_projection.append_event(
            "prompt_projection",
            prompt=assembled,
            task_instruction=task_instruction.strip(),
            target_files=[str(path).replace("\\", "/") for path in (target_files or [])],
        )
        return assembled
