"""Multi-Model Autonomous Subagent Orchestrator for Pacer (Stateless Canvas + Diff Edition).

Upgrades the legacy multi-turn/full-file orchestrator to a Stateless Micro-Agent loop:
1. Local State Canvas (`.pacer/canvas.json`): Replaces multi-turn chat history with a
   bounded, structured state canvas capped at <= 1,500 tokens per prompt.
2. Sub-20ms Local AST Indexer (`local_indexer.py`): Replaces slow LLM Indexer calls
   with native Python `ast` symbol/import/snippet extraction.
3. Fast SEARCH/REPLACE Patch Engine (`diff_patcher.py`): Applies differential code blocks
   directly to disk with exact + whitespace-tolerant matching and path traversal guards.
4. Zero-Latency Local Rule Planner + Verification Deduplication: Skips redundant LLM
   planning calls and skips duplicate verification runs when no files changed between
   `coder` and `verifier`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from graphlib import CycleError, TopologicalSorter
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, Callable

from .acceptance_discrimination import classify_acceptance, probe_base_command
from .canvas_manager import CanvasManager, estimate_tokens
from .command_verification import (
    changed_acceptance_chain_files,
    changed_test_files,
    run_command_verification,
    tamper_repair_brief,
)
from .diff_patcher import apply_search_replace_text, parse_search_replace_blocks
from .gemini_bridge import invoke_gemini, is_gemini_available
from .grok_bridge import invoke_grok, is_grok_available
from .local_indexer import index_file_symbols, parse_error_location
from .model_credentials import run_model_api_probe
from .model_router import route_task
from .pacer_trace import record_pacer_trace
from .process_guard import guarded_run
from .repo_map import build_repo_map, render_repo_map
from .subprocess_window import prepare_subprocess_command
from .verification_profiles import (
    choose_verification_command,
    find_colocated_test_command,
    validate_safe_verification_command,
)
from .workbuddy_bridge import (
    WorkBuddyGatewayClient,
    extract_and_apply_code_blocks,
    invoke_workbuddy,
)


_GOAL_FILE_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+[/\\])*[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|json|yaml|yml|toml|md|sh)\b"
)


@dataclass
class SubagentTask:
    id: str
    title: str
    role: str  # planner | indexer | coder | verifier | reviewer
    backend: str  # gemini | workbuddy | codex | api | local
    instructions: str
    target_files: list[str] = field(default_factory=list)
    verification_command: str = ""
    dependencies: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | running | completed | failed | skipped | escalated
    output: str = ""
    error: str | None = None
    retries: int = 0
    max_retries: int = 2
    elapsed_seconds: float = 0.0
    acceptance_tier: str = ""
    prompt_tokens: int = 0
    patched_files: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SubagentPlan:
    goal: str
    tasks: list[SubagentTask]
    repo_root: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "repo_root": self.repo_root,
            "created_at": self.created_at,
            "tasks": [t.to_dict() for t in self.tasks],
        }

    def get_task(self, task_id: str) -> SubagentTask | None:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None


def _goal_allows_test_edits(goal: str) -> bool:
    """Check if the goal explicitly requests adding or modifying tests.

    Negative instructions (e.g. 'do not modify tests') or generic test mentions
    (e.g. 'make tests pass', 'run pytest') MUST NOT permit test tampering.
    """
    text = str(goal or "").lower()
    negative_markers = (
        "never modify test",
        "do not modify test",
        "don't modify test",
        "cannot modify test",
        "no test edit",
        "严禁修改测试",
        "不能修改测试",
        "不要修改测试",
        "禁止修改单测",
        "不能修改单测",
        "不要修改单测",
    )
    if any(neg in text for neg in negative_markers):
        return False

    positive_markers = (
        "add test",
        "write test",
        "create test",
        "update test",
        "add unit test",
        "write unit test",
        "new test",
        "新增单测",
        "编写单测",
        "添加单测",
        "编写测试",
        "添加测试",
        "新增测试",
        "附带完整单测",
        "补充单测",
        "写单测",
        "写测试",
    )
    return any(pos in text for pos in positive_markers)


def discover_target_files_for_goal(goal: str, repo_root: Path) -> list[str]:
    """Fast local discovery of target files mentioned in `goal` without calling an LLM."""
    root = repo_root.expanduser().resolve()
    explicit = [m.replace("\\", "/").lstrip("/") for m in _GOAL_FILE_RE.findall(str(goal or ""))]
    safe_explicit: list[str] = []
    for cand_str in explicit:
        try:
            cand_p = (root / cand_str).resolve()
            rel = cand_p.relative_to(root).as_posix()
            safe_explicit.append(rel)
        except (ValueError, RuntimeError):
            continue

    valid = [rel for rel in safe_explicit if (root / rel).is_file()]
    if valid:
        return list(dict.fromkeys(valid))[:4]
    if safe_explicit:
        # Even for new files not yet created on disk (e.g., examples/game_2048/core.py), keep safe paths!
        return list(dict.fromkeys(safe_explicit))[:4]

    # Fallback: match goal keywords against tracked Python/JS/TS files with fast in-place directory pruning
    tokens = {tok.lower() for tok in re.findall(r"[A-Za-z_]\w{2,}", str(goal or ""))}
    matched: list[str] = []
    fallback_py: list[str] = []
    exclude_dirs = {".git", ".pacer", ".agent-workspace", ".venv", "venv", "env", "node_modules", "__pycache__"}

    for root_dir, dirnames, filenames in os.walk(str(repo_root)):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs and not d.startswith(".")]
        for fname in sorted(filenames):
            if not fname.endswith((".py", ".ts", ".js")):
                continue
            fpath = Path(root_dir) / fname
            try:
                rel = fpath.relative_to(repo_root).as_posix()
            except ValueError:
                continue
            if rel.startswith(("tests/", "test/")):
                continue
            if tokens and any(tok in rel.lower() for tok in tokens):
                matched.append(rel)
                if len(matched) >= 3:
                    return matched
            elif len(fallback_py) < 3:
                fallback_py.append(rel)

    return matched if matched else fallback_py[:3]


def select_best_backend(
    role: str,
    instructions: str,
    target_files: list[str] | None = None,
    *,
    workbuddy_available: bool = False,
    gemini_available: bool = True,
    grok_available: bool | None = None,
    repeated_failure: bool = False,
) -> str:
    """Select the optimal execution backend according to AGENTS.md contract.

    - Gemini is a multimodal inspection & planning lane (not a default coding worker).
    - Grok is a high-speed streaming coding/reasoning bridge via Sub2API (default primary tier).
    - Codex is an in-depth fallback for repeated failures or specific heavy CLI workflows.
    """
    if grok_available is None:
        grok_available = is_grok_available()

    if role in ("indexer", "planner", "inspector"):
        return "gemini" if gemini_available else "local"

    if role == "verifier":
        return "local"

    if repeated_failure:
        return "codex"

    preferred = (os.environ.get("PACER_DEFAULT_BACKEND") or "").lower()
    if preferred in ("grok", "codex", "gemini", "workbuddy"):
        return preferred

    # 1. Primary fast & reasoning tier: Grok via Sub2API
    if grok_available:
        return "grok"

    decision = route_task(objective=instructions, changed_files=target_files)
    if decision.tier == "strong":
        return "codex"

    # Default executable coding worker fallback
    if shutil_which("codex"):
        return "codex"

    if gemini_available:
        return "gemini"

    if workbuddy_available:
        return "workbuddy"

    return "codex"


def generate_subagent_plan(
    goal: str,
    *,
    repo_root: str | Path = ".",
    workbuddy_available: bool = False,
    gemini_available: bool | None = None,
    fast_local_plan: bool = False,
) -> SubagentPlan:
    """Generate a DAG of subagents for the given goal using fast local rules or Gemini."""
    root = Path(repo_root).expanduser().resolve()
    if gemini_available is None:
        gemini_available = is_gemini_available()

    # Fast-path: 0ms local rule decomposition avoids 20s LLM planner call when fast_local_plan=True
    if fast_local_plan or not gemini_available:
        return _decompose_with_rules(
            goal=goal,
            repo_root=str(root),
            workbuddy_available=workbuddy_available,
            gemini_available=gemini_available,
        )

    try:
        repo_data = build_repo_map(repo_root=root)
        repo_summary = render_repo_map(repo_data, goal=goal, max_lines=45)
    except Exception:
        repo_summary = "Repository structure indexing skipped."

    plan = _decompose_with_gemini(
        goal=goal,
        repo_summary=repo_summary,
        repo_root=str(root),
        workbuddy_available=workbuddy_available,
    )
    if plan and plan.tasks:
        return plan

    return _decompose_with_rules(
        goal=goal,
        repo_root=str(root),
        workbuddy_available=workbuddy_available,
        gemini_available=gemini_available,
    )


def _decompose_with_gemini(
    goal: str,
    repo_summary: str,
    repo_root: str,
    workbuddy_available: bool,
) -> SubagentPlan | None:
    detected_verify_cmd = choose_verification_command(repo_root) or "python -m pytest -q"
    system_prompt = (
        "You are an expert autonomous software engineer and subagent orchestrator. "
        "Your task is to decompose a programming objective into an executable DAG of subagent tasks.\n"
        "Available subagent roles:\n"
        "- indexer: analyze relevant files and existing architecture\n"
        "- coder: implement code changes or new features\n"
        "- verifier: run automated tests, lint, or syntax verification\n"
        "- reviewer: summarize changes and verify acceptance criteria\n\n"
        "Respond ONLY with a valid JSON array of tasks with schema:\n"
        "[\n"
        "  {\n"
        '    "id": "task_1",\n'
        '    "title": "Short title",\n'
        '    "role": "indexer|coder|verifier|reviewer",\n'
        '    "instructions": "Specific actionable prompt for subagent",\n'
        '    "target_files": ["path/to/file.py"],\n'
        f'    "verification_command": "{detected_verify_cmd} (optional)",\n'
        '    "dependencies": []\n'
        "  }\n"
        "]"
    )

    user_prompt = (
        f"Objective:\n{goal}\n\n"
        f"Repository Context:\n{repo_summary}\n\n"
        "Generate a concise 2 to 5 step subagent execution plan in pure JSON."
    )

    res = invoke_gemini(
        user_prompt,
        system_prompt=system_prompt,
        cwd=repo_root,
        expect_json=True,
        timeout_seconds=45,
    )

    if not res.success or not isinstance(res.parsed_json, list):
        return None

    tasks: list[SubagentTask] = []
    for idx, item in enumerate(res.parsed_json):
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("id") or f"task_{len(tasks) + 1}")
        role = str(item.get("role") or "coder").lower()
        title = str(item.get("title") or task_id)
        instructions = str(item.get("instructions") or "")
        target_files = [str(f) for f in item.get("target_files", [])]
        verification_cmd = str(item.get("verification_command") or "")
        deps = [str(d) for d in item.get("dependencies", [])]

        backend = select_best_backend(
            role=role,
            instructions=instructions or title,
            target_files=target_files,
            workbuddy_available=workbuddy_available,
            gemini_available=True,
        )

        tasks.append(
            SubagentTask(
                id=task_id,
                title=title,
                role=role,
                backend=backend,
                instructions=instructions,
                target_files=target_files,
                verification_command=verification_cmd,
                dependencies=deps,
            )
        )

    return SubagentPlan(goal=goal, tasks=tasks, repo_root=repo_root)


def _decompose_with_rules(
    goal: str,
    repo_root: str,
    workbuddy_available: bool,
    gemini_available: bool,
) -> SubagentPlan:
    """Fast deterministic decomposition with local AST indexer and dynamic verification discovery."""
    root = Path(repo_root).expanduser().resolve()
    target_files = discover_target_files_for_goal(goal, root)
    coder_backend = select_best_backend(
        role="coder",
        instructions=goal,
        target_files=target_files,
        workbuddy_available=workbuddy_available,
        gemini_available=gemini_available,
    )
    colocated_cmd = find_colocated_test_command(target_files, repo_root=root)
    default_verify_cmd = colocated_cmd or choose_verification_command(repo_root) or "python -m pytest -q"

    t1 = SubagentTask(
        id="task_1",
        title="Local AST symbol & dependency indexing",
        role="indexer",
        backend="local",
        instructions=f"Analyze AST symbols and focus files for: {goal}",
        target_files=target_files,
        dependencies=[],
    )

    t2 = SubagentTask(
        id="task_2",
        title="Implement SEARCH/REPLACE patch",
        role="coder",
        backend=coder_backend,
        instructions=f"Implement changes required for: {goal}",
        target_files=target_files,
        verification_command=default_verify_cmd,
        dependencies=["task_1"],
    )

    t3 = SubagentTask(
        id="task_3",
        title="Run automated verification",
        role="verifier",
        backend="local",
        instructions=f"Verify implementation for: {goal}",
        verification_command=default_verify_cmd,
        dependencies=["task_2"],
    )

    return SubagentPlan(goal=goal, tasks=[t1, t2, t3], repo_root=repo_root)


def is_infra_or_quota_error(error_msg: str | None) -> bool:
    """Determine if an error is an infrastructure/quota/network issue rather than code logic error."""
    if not error_msg:
        return False
    msg = error_msg.lower()
    infra_markers = (
        "429",
        "rate limit",
        "ratelimit",
        "quota",
        "insufficient_quota",
        "quota_exhausted",
        "额度",
        "余额",
        "401",
        "403",
        "unauthorized",
        "forbidden",
        "invalid_api_key",
        "authentication",
        "connection refused",
        "connect timed out",
        "read timeout",
        "httperror",
        "connectionreseterror",
        "remotedisconnected",
        "socket error",
        "not found on path",
        "executable not found",
        "all workbuddy gateway accounts exhausted",
        "no grok api key configured",
        "gemini cli not available",
    )
    return any(marker in msg for marker in infra_markers)


class SubagentOrchestrator:
    """Executes a SubagentPlan using the Stateless Local Canvas + SEARCH/REPLACE diff patch loop."""

    def __init__(
        self,
        plan: SubagentPlan,
        *,
        on_progress: Callable[[SubagentTask, str], None] | None = None,
        workspace_root: str | Path | None = None,
        enforce_test_tamper_guard: bool = True,
        probe_base_discrimination: bool = False,
        use_stateless_canvas: bool = True,
        invariants: list[str] | None = None,
        session_source: str = "cli",
    ):
        self.plan = plan
        self.on_progress = on_progress
        self.workspace_root = Path(workspace_root or plan.repo_root).resolve()
        self.wb_client = WorkBuddyGatewayClient()
        self.enforce_test_tamper_guard = enforce_test_tamper_guard
        self.probe_base_discrimination = probe_base_discrimination
        self.use_stateless_canvas = use_stateless_canvas
        self.session_source = session_source
        self.canvas = CanvasManager(self.workspace_root)
        self._last_passed_verification: tuple[str, tuple[str, ...]] | None = None
        self._failover_path: list[str] = []
        self._effective_backend: str = ""

        # Initialize canvas goal & invariants
        verify_cmd = next((t.verification_command for t in self.plan.tasks if t.verification_command), "")
        self.canvas.update_goal(
            self.plan.goal,
            acceptance_cmd=verify_cmd,
            status="in_progress",
            reset_scratchpad=True,
        )
        if invariants:
            self.canvas.add_invariants(*invariants)

    def _notify(self, task: SubagentTask, message: str) -> None:
        if self.on_progress:
            self.on_progress(task, message)
        else:
            print(f"[{task.id} | {task.role.upper()} | {task.backend}] {message}", flush=True)

    def _topological_order(self) -> tuple[list[SubagentTask], set[str]]:
        """Sort tasks in valid topological order; return `(ordered_tasks, invalid_or_cyclic_ids)`."""
        task_map = {t.id: t for t in self.plan.tasks}
        graph: dict[str, set[str]] = {}
        invalid_ids: set[str] = set()

        for t in self.plan.tasks:
            missing_deps = [d for d in t.dependencies if d not in task_map]
            if missing_deps:
                invalid_ids.add(t.id)
                continue
            graph[t.id] = set(t.dependencies)

        sorter = TopologicalSorter(graph)
        ordered: list[SubagentTask] = []
        try:
            sorter.prepare()
            while sorter.is_active():
                ready_ids = sorter.get_ready()
                if not ready_ids:
                    break
                for tid in ready_ids:
                    ordered.append(task_map[tid])
                    sorter.done(tid)
        except CycleError:
            scheduled = {t.id for t in ordered}
            for t in self.plan.tasks:
                if t.id not in scheduled:
                    invalid_ids.add(t.id)

        scheduled_ids = {t.id for t in ordered}
        for t in self.plan.tasks:
            if t.id not in scheduled_ids:
                ordered.append(t)

        return ordered, invalid_ids

    def _inject_dependency_context(self, task: SubagentTask, task_map: dict[str, SubagentTask]) -> None:
        """Pass upstream dependency outputs into downstream task instructions and update Canvas."""
        if not task.dependencies:
            return
        upstream_notes: list[str] = []
        for dep_id in task.dependencies:
            dep_task = task_map.get(dep_id)
            if dep_task and dep_task.output and dep_task.output.strip():
                excerpt = dep_task.output.strip()[:1200]
                upstream_notes.append(f"--- Context from [{dep_task.id} ({dep_task.role})]: {dep_task.title} ---\n{excerpt}")
        if upstream_notes:
            joined = "\n\n".join(upstream_notes)
            if joined not in task.instructions:
                task.instructions = f"{task.instructions}\n\n[Upstream Subagent Context]:\n{joined}"

    def _populate_canvas_ast_focus(self, task: SubagentTask, *, failing_file: str = "", failing_line: int | None = None) -> list[str]:
        """Run sub-20ms local AST indexing on target files and sync symbol summaries into `.pacer/canvas.json`."""
        candidate_files = list(task.target_files)
        if failing_file and failing_file not in candidate_files:
            candidate_files.insert(0, failing_file)
        if not candidate_files:
            candidate_files = discover_target_files_for_goal(task.instructions or self.plan.goal, self.workspace_root)
            task.target_files = list(candidate_files)

        summaries: list[str] = []
        for rel_path in candidate_files[:4]:
            line_hint = failing_line if rel_path == failing_file else None
            idx_res = index_file_symbols(
                rel_path,
                workspace_root=self.workspace_root,
                target_line=line_hint,
                query=task.instructions or self.plan.goal,
            )
            if not idx_res.error:
                self.canvas.upsert_focus_file(
                    idx_res.file_path,
                    symbols=idx_res.compact_symbols(),
                    imports=idx_res.imports,
                    target_snippet=idx_res.focused_snippet,
                )
                summaries.append(idx_res.to_summary_text(include_snippet=False))
        return summaries

    def run(self) -> dict[str, Any]:
        """Execute all tasks in topological order using the Stateless Canvas + SEARCH/REPLACE loop."""
        t_start = time.time()
        completed_ids: set[str] = set()
        failed_ids: set[str] = set()
        task_map = {t.id: t for t in self.plan.tasks}

        ordered_tasks, invalid_ids = self._topological_order()

        for task in ordered_tasks:
            if task.id in invalid_ids:
                task.status = "skipped"
                failed_ids.add(task.id)
                self._notify(task, f"Skipped: unresolved or cyclic dependencies {task.dependencies}")
                continue

            if any(dep in failed_ids for dep in task.dependencies):
                task.status = "skipped"
                failed_ids.add(task.id)
                self._notify(task, f"Skipped because dependency failed: {task.dependencies}")
                continue

            unresolved = [dep for dep in task.dependencies if dep not in completed_ids]
            if unresolved:
                task.status = "skipped"
                failed_ids.add(task.id)
                self._notify(task, f"Skipped: unmet dependencies {unresolved}")
                continue

            self._inject_dependency_context(task, task_map)

            success = self._execute_task_with_retry(task)
            if success:
                completed_ids.add(task.id)
                self.canvas.update_scratchpad(completed_step=f"{task.id}:{task.title}")
            else:
                failed_ids.add(task.id)

        total_elapsed = time.time() - t_start
        all_passed = len(failed_ids) == 0 and len(completed_ids) == len(self.plan.tasks)
        self.canvas.update_goal(
            self.plan.goal,
            status="completed" if all_passed else "failed",
        )

        all_patched: list[str] = []
        for t in self.plan.tasks:
            for pf in t.patched_files:
                if pf not in all_patched:
                    all_patched.append(pf)

        primary_b = self.plan.tasks[1].backend if len(self.plan.tasks) > 1 else (self.plan.tasks[0].backend if self.plan.tasks else "unknown")
        effective_b = self._effective_backend or primary_b

        last_verif_cmd = ""
        last_verif_verdict = ""
        last_verif_out = ""
        for t in reversed(self.plan.tasks):
            if t.verification_command:
                last_verif_cmd = t.verification_command
                last_verif_verdict = "pass" if t.status == "completed" else ("fail" if t.status == "failed" else "skipped")
                last_verif_out = t.output or t.error or ""
                break

        record_pacer_trace(
            goal=self.plan.goal,
            workspace_root=self.workspace_root,
            primary_backend=primary_b,
            effective_backend=effective_b,
            failover_path=self._failover_path,
            status="success" if all_passed else "failed",
            elapsed_seconds=total_elapsed,
            patched_files=all_patched,
            verification_command=last_verif_cmd,
            verification_verdict=last_verif_verdict,
            verification_output=last_verif_out,
            error=None if all_passed else "; ".join(f"{t.id}: {t.error}" for t in self.plan.tasks if t.error),
            session_source=self.session_source,
        )

        return {
            "success": all_passed,
            "goal": self.plan.goal,
            "total_tasks": len(self.plan.tasks),
            "completed_tasks": len(completed_ids),
            "failed_tasks": len(failed_ids),
            "elapsed_seconds": total_elapsed,
            "effective_backend": effective_b,
            "failover_path": self._failover_path,
            "canvas": self.canvas.load().to_dict(),
            "plan": self.plan.to_dict(),
        }

    def _compute_focus_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        canvas = self.canvas.load()
        fps: list[tuple[str, int, int]] = []
        for rel_path in sorted(canvas.focus_files.keys()):
            p = (Path(self.workspace_root) / rel_path).resolve()
            if p.exists():
                try:
                    stat = p.stat()
                    fps.append((rel_path, int(stat.st_mtime_ns), int(stat.st_size)))
                except OSError:
                    fps.append((rel_path, -1, -1))
            else:
                fps.append((rel_path, -1, -1))
        return tuple(fps)

    def _execute_task_with_retry(self, task: SubagentTask) -> bool:
        """Stateless micro-agent execution loop:
        Read Canvas -> Local AST Index -> Build <=1500-token Stateless Prompt ->
        Dispatch -> Apply SEARCH/REPLACE Patch -> Run Verification -> Update Canvas.
        """
        task.status = "running"
        t0 = time.time()
        self._notify(task, f"Starting subtask: {task.title}")

        # Fast-path for verifier task: if upstream coder just verified the exact same command and no files changed, skip re-running
        current_focus_fp = self._compute_focus_fingerprint()
        if (
            task.role == "verifier"
            and task.backend == "local"
            and task.verification_command
            and self._last_passed_verification == (task.verification_command, current_focus_fp)
        ):
            task.output = f"Verification '{task.verification_command}' already passed on current file state (cached)."
            task.status = "completed"
            task.elapsed_seconds = time.time() - t0
            self._notify(task, f"Subtask completed in {task.elapsed_seconds:.1f}s (reused upstream verification receipt)")
            return True

        base_probe: dict[str, Any] | None = None
        if self.probe_base_discrimination and task.verification_command:
            try:
                base_probe = probe_base_command(
                    command=task.verification_command,
                    repo_root=self.workspace_root,
                    base_ref="HEAD",
                    timeout_seconds=120.0,
                )
            except Exception:
                base_probe = None

        base_instruction = task.instructions
        failing_file = ""
        failing_line: int | None = None

        while task.retries <= task.max_retries:
            # 1. Refresh local AST focus on the Canvas (< 20ms)
            self._populate_canvas_ast_focus(task, failing_file=failing_file, failing_line=failing_line)

            # 2. Assemble stateless single-turn prompt from Canvas (<= 1,500 tokens)
            if self.use_stateless_canvas and task.role == "coder":
                stateless_prompt = self.canvas.build_stateless_prompt(
                    base_instruction,
                    require_search_replace=True,
                )
                task.prompt_tokens = estimate_tokens(stateless_prompt)
            else:
                stateless_prompt = task.instructions
                task.prompt_tokens = estimate_tokens(stateless_prompt)

            setattr(task, "_stateless_prompt", stateless_prompt)

            # Snapshot target files state before dispatch to detect direct-write modifications
            pre_file_states = {}
            for tf in (task.target_files or []):
                fp = self.workspace_root / tf
                try:
                    stat = fp.stat()
                    pre_file_states[tf] = (stat.st_mtime_ns, stat.st_size)
                except OSError:
                    pre_file_states[tf] = None

            # 3. Dispatch single-turn prompt to backend
            run_output, error = self._dispatch_to_backend(task)
            task.output = run_output
            task.error = error

            # 4. If coder returned SEARCH/REPLACE blocks (or fenced file blocks), apply patch to disk
            if task.role == "coder" and run_output:
                default_target = task.target_files[0] if len(task.target_files) == 1 else ""
                sr_blocks = parse_search_replace_blocks(run_output, default_file=default_target)
                if sr_blocks:
                    allowed = set(task.target_files or [])
                    allowed.update(self.canvas.load().focus_files.keys())
                    safe_allowed = [
                        f for f in allowed
                        if not f.startswith((".git", ".agent-workspace", ".pacer", ".env"))
                    ]
                    patch_res = apply_search_replace_text(
                        run_output,
                        workspace_root=self.workspace_root,
                        default_file=default_target,
                        allowed_files=safe_allowed or None,
                    )
                    if patch_res.failed_count > 0 or not patch_res.success:
                        task.patched_files = []
                        setattr(task, "_patch_batch_error", "; ".join(patch_res.errors))
                    else:
                        task.patched_files = list(patch_res.modified_files)
                    for mod_file in task.patched_files:
                        self.canvas.upsert_focus_file(mod_file)
                else:
                    written = extract_and_apply_code_blocks(
                        run_output,
                        workspace_root=self.workspace_root,
                        allowed_files=task.target_files,
                    )
                    if written:
                        task.patched_files = written
                        for mod_file in written:
                            self.canvas.upsert_focus_file(mod_file)

            # Direct-writing backend (Codex) modifies files in-place: detect modifications against pre-dispatch state
            if task.role == "coder" and task.backend == "codex" and not task.patched_files:
                modified_targets = []
                for tf in (task.target_files or []):
                    fp = self.workspace_root / tf
                    try:
                        stat = fp.stat()
                        curr = (stat.st_mtime_ns, stat.st_size)
                    except OSError:
                        curr = None
                    if curr != pre_file_states.get(tf):
                        modified_targets.append(tf)
                if modified_targets:
                    task.patched_files = modified_targets
                    for mod_file in modified_targets:
                        self.canvas.upsert_focus_file(mod_file)

            # 5. Automated verification & Canvas feedback loop
            if task.verification_command:
                self._notify(task, f"Running verification: {task.verification_command}")
                verif_ok, verif_out = self._run_verification_command(task.verification_command)
                if not verif_ok:
                    self._last_passed_verification = None
                    task.retries += 1
                    task.history.append({
                        "attempt": task.retries,
                        "backend": task.backend,
                        "error": verif_out,
                    })

                    failing_file, failing_line = parse_error_location(verif_out)
                    failing_loc = f"{failing_file}:{failing_line}" if failing_file and failing_line else failing_file
                    self.canvas.update_scratchpad(
                        latest_error=verif_out[:600],
                        failing_line=failing_loc,
                        attempt_count=task.retries,
                        last_failed_hypothesis=f"Attempt #{task.retries} on {task.backend} failed verification.",
                    )

                    if task.retries <= task.max_retries:
                        # If a verifier task fails on local backend, local execution does not edit code.
                        # Route failure back to the upstream coder task to self-heal.
                        if task.role == "verifier" and task.backend == "local":
                            upstream_coder = None
                            for dep_id in reversed(task.dependencies):
                                dep_task = self.plan.get_task(dep_id)
                                if dep_task and dep_task.role == "coder":
                                    upstream_coder = dep_task
                                    break
                            if upstream_coder is not None:
                                self._notify(
                                    task,
                                    f"Verifier failed. Routing failure back to coder task '{upstream_coder.id}' for automated self-repair (attempt {task.retries}/{task.max_retries})...",
                                )
                                upstream_coder.instructions = (
                                    f"{upstream_coder.instructions}\n\n"
                                    f"[CRITICAL: Verifier '{task.id}' failed verification gate]:\n"
                                    f"{verif_out[:1500]}\n\n"
                                    "Please analyze and repair the code to pass verification."
                                )
                                upstream_coder.retries += 1
                                if upstream_coder.backend in ("gemini", "workbuddy"):
                                    upstream_coder.backend = "codex"
                                    upstream_coder.status = "escalated"
                                self._execute_task_with_retry(upstream_coder)
                                continue

                        if task.backend in ("gemini", "workbuddy"):
                            old_backend = task.backend
                            task.backend = "codex"
                            task.status = "escalated"
                            self._notify(
                                task,
                                f"Verification failed. Escalating from {old_backend} to Codex Plus (attempt {task.retries}/{task.max_retries}): {verif_out[:120]}...",
                            )
                        else:
                            self._notify(
                                task,
                                f"Verification failed. Retrying with {task.backend} (attempt {task.retries}/{task.max_retries}): {verif_out[:120]}...",
                            )

                        task.instructions = (
                            f"{base_instruction}\n\n"
                            f"[CRITICAL: Previous attempt failed verification]:\n"
                            f"{verif_out[:1500]}\n\n"
                            "Please analyze and repair the error."
                        )
                        continue
                    else:
                        task.status = "failed"
                        task.error = verif_out
                        self._notify(task, f"Subtask failed after {task.retries} attempts.")
                        task.elapsed_seconds = time.time() - t0
                        return False
                else:
                    # Verification command passed. Guard against false greens where search-replace blocks failed to apply
                    had_sr_blocks = bool(task.role == "coder" and run_output and parse_search_replace_blocks(run_output))
                    if had_sr_blocks and not task.patched_files:
                        self._last_passed_verification = None
                        task.retries += 1
                        patch_err = getattr(task, "_patch_batch_error", "")
                        err_msg = f"SEARCH/REPLACE blocks from {task.backend} failed to match or apply to target files: {patch_err}" if patch_err else f"SEARCH/REPLACE blocks from {task.backend} failed to match or apply to target files."
                        task.history.append({
                            "attempt": task.retries,
                            "backend": task.backend,
                            "error": err_msg,
                        })
                        self.canvas.update_scratchpad(
                            latest_error=err_msg,
                            attempt_count=task.retries,
                            last_failed_hypothesis=f"Attempt #{task.retries} on {task.backend} produced unmatchable SEARCH/REPLACE blocks.",
                        )
                        if task.retries <= task.max_retries:
                            if task.backend != "codex":
                                old_backend = task.backend
                                task.backend = "codex"
                                task.status = "escalated"
                                self._notify(task, f"Diff patch failed to apply ({patch_err or 'mismatch'}). Escalating from {old_backend} to Codex Plus (attempt {task.retries}/{task.max_retries})...")
                            else:
                                self._notify(task, f"Diff patch failed to apply ({patch_err or 'mismatch'}). Retrying (attempt {task.retries}/{task.max_retries})...")
                            task.instructions = (
                                f"{base_instruction}\n\n"
                                f"[CRITICAL: SEARCH/REPLACE blocks failed to apply: {patch_err or 'did not match target file'}]\n"
                                "Ensure SEARCH blocks match the exact existing file content or leave SEARCH empty if creating new files."
                            )
                            continue
                        else:
                            task.status = "failed"
                            task.error = err_msg
                            self._notify(task, f"Subtask failed after {task.retries} attempts (patch apply failure).")
                            task.elapsed_seconds = time.time() - t0
                            return False

                    self.canvas.update_scratchpad(latest_error="", failing_line="")
                    updated_focus_fp = self._compute_focus_fingerprint()
                    self._last_passed_verification = (task.verification_command, updated_focus_fp)
                    if base_probe is not None:
                        disc = classify_acceptance(
                            command_result={"command": task.verification_command, "verdict": "pass"},
                            base_probe=base_probe,
                        )
                        task.acceptance_tier = str(disc.get("tier") or "")
            else:
                # No verification command on this subtask: require applied changes for coder subtasks
                has_target_files = bool(task.target_files or (task.role == "coder" and run_output and parse_search_replace_blocks(run_output)))
                if task.role == "coder" and has_target_files and not task.patched_files:
                    self._last_passed_verification = None
                    task.retries += 1
                    err_msg = f"Coder task on {task.backend} did not modify any files (no valid changes applied to disk)."
                    task.history.append({
                        "attempt": task.retries,
                        "backend": task.backend,
                        "error": err_msg,
                    })
                    self.canvas.update_scratchpad(
                        latest_error=err_msg,
                        attempt_count=task.retries,
                        last_failed_hypothesis=f"Attempt #{task.retries} on {task.backend} produced no applied changes.",
                    )
                    if task.retries <= task.max_retries:
                        if task.backend != "codex":
                            old_backend = task.backend
                            task.backend = "codex"
                            task.status = "escalated"
                            self._notify(
                                task,
                                f"Coder produced no file modifications. Escalating from {old_backend} to Codex Plus (attempt {task.retries}/{task.max_retries})...",
                            )
                        else:
                            self._notify(
                                task,
                                f"Codex produced no file modifications. Retrying (attempt {task.retries}/{task.max_retries})...",
                            )
                        task.instructions = (
                            f"{base_instruction}\n\n"
                            "[CRITICAL: Previous attempt produced no valid file changes on disk.]\n"
                            "You must output code modifications or apply edits to target files."
                        )
                        continue
                    else:
                        task.status = "failed"
                        task.error = err_msg
                        self._notify(task, f"Subtask failed after {task.retries} attempts (no changes produced).")
                        task.elapsed_seconds = time.time() - t0
                        return False

            if error and not run_output:
                task.status = "failed"
                task.elapsed_seconds = time.time() - t0
                return False

            task.status = "completed"
            task.elapsed_seconds = time.time() - t0
            self._notify(task, f"Subtask completed in {task.elapsed_seconds:.1f}s")
            return True

        task.status = "failed"
        task.elapsed_seconds = time.time() - t0
        return False

    def _dispatch_to_backend(self, task: SubagentTask) -> tuple[str, str | None]:
        """Dispatch single-turn stateless prompt with zero-waiting multi-model failover."""
        return self._dispatch_with_failover(task)

    def _dispatch_with_failover(self, task: SubagentTask) -> tuple[str, str | None]:
        backend = task.backend.lower()
        effective_prompt = getattr(task, "_stateless_prompt", None) or task.instructions

        # Fast-path: execute local AST indexing in <20ms without LLM roundtrip
        if task.role == "indexer" and (backend == "local" or self.use_stateless_canvas):
            ast_summaries = self._populate_canvas_ast_focus(task)
            summary_text = (
                "\n\n".join(ast_summaries)
                if ast_summaries
                else f"Local AST index completed for '{task.title}'."
            )
            return summary_text, None

        if backend == "local":
            return f"Local subtask '{task.title}' evaluated successfully.", None

        # Build cascade order: start with task's current backend, followed by standard fallback cascade
        preferred_cascade = ["grok", "gemini", "workbuddy", "codex"]
        candidates = [backend]
        for b in preferred_cascade:
            if b not in candidates:
                candidates.append(b)

        last_error: str | None = None
        for idx, candidate_backend in enumerate(candidates):
            out, err = self._invoke_single_backend(candidate_backend, task, effective_prompt)
            if out:
                self._effective_backend = candidate_backend
                task.backend = candidate_backend
                return out, None

            last_error = err
            if is_infra_or_quota_error(err) and idx + 1 < len(candidates):
                next_backend = candidates[idx + 1]
                hop = f"{candidate_backend} -> {next_backend} ({err})"
                self._failover_path.append(hop)
                self._notify(
                    task,
                    f"Zero-waiting failover: {candidate_backend} unavailable ({err}). Auto-switching to {next_backend}...",
                )
                continue
            else:
                break

        return "", last_error

    def _invoke_single_backend(
        self,
        backend: str,
        task: SubagentTask,
        effective_prompt: str,
    ) -> tuple[str, str | None]:
        backend = backend.lower()
        if backend in ("grok", "gork"):
            if not is_grok_available():
                return "", "No Grok API key configured"
            grok_res = invoke_grok(
                effective_prompt,
                system_prompt="You are an expert coding subagent. Provide exact SEARCH/REPLACE diff blocks to update code.",
                timeout=90.0,
            )
            return (grok_res.response, grok_res.error) if grok_res.success else ("", grok_res.error)

        elif backend == "gemini":
            if not is_gemini_available():
                return "", "Gemini CLI not available on system"
            use_cli_yolo = (task.role == "coder") and (not self.use_stateless_canvas)
            result = invoke_gemini(
                effective_prompt,
                cwd=self.workspace_root,
                yolo=use_cli_yolo,
                output_format="text",
                timeout_seconds=90,
            )
            return (result.response, result.error) if result.success else ("", result.error)

        elif backend == "workbuddy":
            result = invoke_workbuddy(
                effective_prompt,
                cwd=self.workspace_root,
                target_files=task.target_files,
                apply_changes=(task.role == "coder"),
                timeout_seconds=90,
            )
            if result.success:
                return (result.response, None)
            return ("", result.error or "WorkBuddy gateway failed")

        elif backend == "codex":
            return self._run_codex_cli(task)

        elif backend == "api":
            try:
                probe = run_model_api_probe(prompt=effective_prompt, timeout_seconds=30)
                content = probe.get("response", {}).get("content_preview", "")
                return content, None
            except Exception as exc:
                return "", str(exc)

        elif backend == "local":
            return f"Local subtask '{task.title}' evaluated successfully.", None

        return "", f"Unknown backend: {backend}"

    def _run_codex_cli(self, task: SubagentTask) -> tuple[str, str | None]:
        """Run non-interactive codex command guarded by ProcessGuard."""
        codex_bin = shutil_which("codex")
        if not codex_bin:
            return "", "Codex executable not found on PATH."

        prompt_text = getattr(task, "_stateless_prompt", None) or task.instructions
        cmd = prepare_subprocess_command([
            codex_bin,
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--color",
            "never",
            prompt_text,
        ])
        try:
            proc = guarded_run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self.workspace_root),
                timeout=180,
            )
            if proc.returncode == 0:
                return (proc.stdout or "").strip(), None
            return (proc.stdout or "").strip(), (proc.stderr or "").strip() or f"Codex exited with code {proc.returncode}"
        except Exception as exc:
            return "", f"Codex execution failed: {exc}"

    def _run_verification_command(self, cmd: str) -> tuple[bool, str]:
        """Execute verification command using Pacer's command_verification gate + tamper guards."""
        is_safe, safety_reason = validate_safe_verification_command(cmd, repo_root=self.workspace_root)
        if not is_safe:
            return False, f"Verification command rejected by safety policy: {safety_reason}"

        if self.enforce_test_tamper_guard:
            # Acceptance chain files (pytest.ini, pyproject.toml, etc.) must NEVER be tampered with
            tampered_chain = changed_acceptance_chain_files(repo_root=self.workspace_root, command=cmd)
            if tampered_chain:
                return (
                    False,
                    f"Acceptance chain files modified ({', '.join(tampered_chain)}); revert them and fix product code instead.",
                )
            if not _goal_allows_test_edits(self.plan.goal):
                tampered_tests = changed_test_files(repo_root=self.workspace_root)
                if tampered_tests:
                    brief = tamper_repair_brief(tampered_tests)
                    return (False, str(brief.get("repair_prompt") or brief.get("message") or "Test tampering detected"))

        effective_cmd = cmd.strip()
        if effective_cmd.startswith("python ") and not shutil.which("python"):
            effective_cmd = f'"{sys.executable}" {effective_cmd[len("python "):]}'

        try:
            res = run_command_verification(
                command=effective_cmd,
                repo_root=self.workspace_root,
                timeout_seconds=120.0,
            )
            verdict_ok = res.get("verdict") == "pass"
            output = str(res.get("output_tail") or res.get("reason") or "").strip()
            if res.get("shell_hint"):
                output = f"{output}\n{res['shell_hint']}".strip()
            return (verdict_ok, output)
        except Exception as exc:
            return (False, f"Verification execution error: {exc}")


def shutil_which(cmd: str) -> str | None:
    return shutil.which(cmd) or shutil.which(f"{cmd}.cmd") or shutil.which(f"{cmd}.exe")