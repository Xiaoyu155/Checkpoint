"""Pacer Decoupled Context Profiler (上下文画像解耦引擎).

Completely isolates the repository and project context from conversational dialogue.
Eliminates full-context dumps into interactive chat windows (e.g. Gemini 3.8 Flash host)
by maintaining an out-of-band, on-disk context profile (< 1,500 tokens).

Key capabilities:
1. Fast (< 20ms) AST symbol extraction and Git state delta tracking.
2. Structured ContextProfile artifact stored in `.pacer/context_profile.json`.
3. Deterministic compaction to keep stateless subtask handoffs strictly within token budgets.
4. Automatic synchronization on workspace state modifications.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from .canvas_manager import estimate_tokens, truncate_to_token_budget
from .subprocess_window import hidden_subprocess_kwargs


def extract_latest_utterance(messages: list) -> str:
    """Keep only the latest user text. Earlier turns are not part of the request."""
    for message in reversed(list(messages or [])):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            text = ""
        text = text.strip()
        if text:
            return text
    return ""


@dataclass
class ContextProfile:
    repo_name: str
    workspace_root: str
    git_branch: str = "main"
    git_head: str = ""
    is_dirty: bool = False
    modified_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    core_modules: list[str] = field(default_factory=list)
    exported_symbols: dict[str, list[str]] = field(default_factory=dict)
    architecture_overview: str = ""
    active_invariants: list[str] = field(default_factory=list)
    active_wish: str = ""
    confirmed_decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    plan_items: list[str] = field(default_factory=list)
    current_contract: str = ""
    last_failure: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    estimated_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_compact_markdown(self, max_tokens: int = 1200) -> str:
        """Render a concise markdown representation bounded by `max_tokens`."""
        lines = [
            f"# Context Profile: {self.repo_name} ({self.git_branch}@{self.git_head[:7] if self.git_head else 'local'})",
            f"- Status: {'Dirty' if self.is_dirty else 'Clean'} | Modified: {len(self.modified_files)} | Untracked: {len(self.untracked_files)}",
        ]
        if self.architecture_overview:
            lines.append(f"- Architecture: {self.architecture_overview}")
        if self.active_wish:
            lines.append(f"- Active wish: {self.active_wish}")
        if self.open_questions:
            lines.append("- Open questions:")
            for question in self.open_questions[:4]:
                lines.append(f"  * {question}")
        if self.confirmed_decisions:
            lines.append("- Confirmed decisions:")
            for decision in self.confirmed_decisions[:5]:
                lines.append(f"  * {decision}")
        if self.plan_items:
            lines.append("- Plan:")
            for item in self.plan_items[:6]:
                lines.append(f"  * {item}")
        if self.current_contract:
            lines.append(f"- Current contract: {self.current_contract}")
        if self.last_failure:
            lines.append(f"- Last failure: {self.last_failure}")
        if self.active_invariants:
            lines.append("- Invariants:")
            for inv in self.active_invariants[:5]:
                lines.append(f"  * {inv}")
        if self.modified_files:
            lines.append(f"- Active Diff Target Files: {', '.join(self.modified_files[:6])}")
        if self.exported_symbols:
            lines.append("- Key Symbols & Contracts:")
            for mod, syms in list(self.exported_symbols.items())[:8]:
                sym_str = ", ".join(syms[:6])
                lines.append(f"  * `{mod}`: {sym_str}")

        content = "\n".join(lines)
        return truncate_to_token_budget(content, max_tokens)


class ContextProfileManager:
    """Manages generation, storage, and retrieval of out-of-band context profiles."""

    def __init__(self, workspace_root: str | Path):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.pacer_dir = self.workspace_root / ".pacer"
        self.profile_file = self.pacer_dir / "context_profile.json"
        self.markdown_file = self.pacer_dir / "context_profile.md"

    def extract_file_symbols(self, rel_path: str, max_symbols: int = 8) -> list[str]:
        """Extract top-level functions and classes from a Python file using standard `ast`."""
        full_path = self.workspace_root / rel_path
        if not full_path.is_file() or not full_path.name.endswith(".py"):
            return []
        try:
            tree = ast.parse(full_path.read_text(encoding="utf-8", errors="ignore"))
            symbols = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(f"def {node.name}()")
                elif isinstance(node, ast.ClassDef):
                    symbols.append(f"class {node.name}")
                if len(symbols) >= max_symbols:
                    break
            return symbols
        except Exception:
            return []

    def get_git_state(self) -> tuple[str, str, bool, list[str], list[str]]:
        """Query branch, HEAD sha, dirty status, modified files, and untracked files."""
        branch = "main"
        head = ""
        is_dirty = False
        modified: list[str] = []
        untracked: list[str] = []

        try:
            b_res = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                timeout=5,
                **hidden_subprocess_kwargs(),
            )
            if b_res.returncode == 0:
                branch = b_res.stdout.strip()

            h_res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                timeout=5,
                **hidden_subprocess_kwargs(),
            )
            if h_res.returncode == 0:
                head = h_res.stdout.strip()

            s_res = subprocess.run(
                ["git", "status", "--porcelain=v1", "-z", "--", "."],
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
                **hidden_subprocess_kwargs(),
            )
            prefix_res = subprocess.run(
                ["git", "rev-parse", "--show-prefix"],
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
                **hidden_subprocess_kwargs(),
            )
            if s_res.returncode == 0 and prefix_res.returncode == 0:
                prefix = prefix_res.stdout.rstrip("\r\n")
                entries = iter(s_res.stdout.split("\0"))
                for line in entries:
                    if len(line) < 4:
                        continue
                    status_code = line[:2]
                    file_path = line[3:]
                    if "R" in status_code or "C" in status_code:
                        next(entries, None)  # -z emits the old rename/copy path next.
                    if prefix and not file_path.startswith(prefix):
                        continue
                    file_path = file_path[len(prefix):]
                    if status_code.startswith("?"):
                        untracked.append(file_path)
                    else:
                        modified.append(file_path)
                is_dirty = bool(modified or untracked)
        except Exception:
            pass

        return branch, head, is_dirty, modified, untracked

    def build_profile(
        self,
        *,
        architecture_overview: str = "",
        invariants: list[str] | None = None,
        focus_files: list[str] | None = None,
    ) -> ContextProfile:
        """Build a comprehensive context profile without injecting multi-file dumps."""
        branch, head, is_dirty, modified, untracked = self.get_git_state()

        # Discover core module files
        candidate_files: list[str] = []
        if focus_files:
            candidate_files.extend(focus_files)
        candidate_files.extend(modified[:6])

        # If few files, scan top-level source files
        if len(candidate_files) < 4:
            for py_f in sorted(self.workspace_root.glob("src/**/*.py")):
                if len(candidate_files) >= 10:
                    break
                if any(p in py_f.parts for p in {".git", ".pacer", ".venv", "venv", "__pycache__"}):
                    continue
                try:
                    rel = py_f.relative_to(self.workspace_root).as_posix()
                    if rel not in candidate_files:
                        candidate_files.append(rel)
                except ValueError:
                    pass

        exported_symbols: dict[str, list[str]] = {}
        for rel_f in candidate_files[:10]:
            syms = self.extract_file_symbols(rel_f)
            if syms:
                exported_symbols[rel_f] = syms

        profile = ContextProfile(
            repo_name=self.workspace_root.name,
            workspace_root=str(self.workspace_root),
            git_branch=branch,
            git_head=head,
            is_dirty=is_dirty,
            modified_files=modified[:15],
            untracked_files=untracked[:15],
            core_modules=candidate_files[:10],
            exported_symbols=exported_symbols,
            architecture_overview=architecture_overview or "Pacer Stateless Subagent Multi-Model Architecture",
            active_invariants=invariants or [
                "Strict test-tamper defense (cannot modify test files unless explicitly requested)",
                "Witness-driven verification (no tautological assertions or false greens allowed)",
                "Stateless micro-agent loop strictly capped within 1,500 tokens",
            ],
        )

        md_repr = profile.to_compact_markdown()
        profile.estimated_tokens = estimate_tokens(md_repr)
        return profile

    def save_profile(self, profile: ContextProfile) -> Path:
        """Persist the ContextProfile to .pacer/context_profile.json and .md."""
        self.pacer_dir.mkdir(parents=True, exist_ok=True)
        data = profile.to_dict()
        self.profile_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self.markdown_file.write_text(profile.to_compact_markdown(), encoding="utf-8")
        return self.profile_file

    def load_profile(self) -> ContextProfile | None:
        """Load context profile from disk if available."""
        if not self.profile_file.is_file():
            return None
        try:
            data = json.loads(self.profile_file.read_text(encoding="utf-8"))
            known = {item.name for item in fields(ContextProfile)}
            return ContextProfile(**{key: value for key, value in data.items() if key in known})
        except Exception:
            return None

    def get_or_create_profile(self, **kwargs: Any) -> ContextProfile:
        """Refresh live Git state, symbols and task focus on each handoff."""
        previous = self.load_profile()
        profile = self.build_profile(**kwargs)
        if previous is not None:
            if not kwargs.get("architecture_overview"):
                profile.architecture_overview = previous.architecture_overview
            profile.active_wish = previous.active_wish
            profile.confirmed_decisions = list(previous.confirmed_decisions)
            profile.open_questions = list(previous.open_questions)
            profile.plan_items = list(previous.plan_items)
            profile.current_contract = previous.current_contract
            profile.last_failure = previous.last_failure
        self.save_profile(profile)
        return profile
