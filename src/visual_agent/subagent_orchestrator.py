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

import ast
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from graphlib import CycleError, TopologicalSorter
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .acceptance_discrimination import (
    classify_acceptance,
    probe_workspace_command,
)
from .anti_slop import detect_slop_in_files, format_slop_critique
from .canvas_manager import CanvasManager, estimate_tokens
from .command_verification import (
    changed_acceptance_chain_files,
    changed_test_files,
    changed_snapshot_files,
    snapshot_verification_files,
    is_test_path,
    run_command_verification,
    tamper_repair_brief,
)
from .context_profile import ContextProfile, ContextProfileManager
from .dag_scheduler import TaskComplexity, classify_task_complexity, topological_waves
from .diff_patcher import apply_search_replace_text, parse_search_replace_blocks
from .gemini_bridge import invoke_gemini, is_gemini_available, invoke_gemini38, is_gemini38_available
from .grok_bridge import invoke_grok, is_grok_available
from .local_indexer import index_file_symbols, parse_error_location
from .llm_providers import resolve_api_pool, resolve_llm_backend, run_llm_completion
from .model_credentials import resolve_model_provider_config
from .model_router import route_task
from .pacer_trace import record_pacer_trace
from .process_guard import guarded_run
from .managed_checkpoint import (
    clear_managed_checkpoint,
    is_checkpoint_resumable,
    load_managed_checkpoint,
    save_managed_checkpoint,
)
from .session_projection import SessionProjection
from .repo_map import build_repo_map, render_repo_map
from .subprocess_window import hidden_subprocess_kwargs, prepare_subprocess_command, resolve_codex_executable
from .tamper_guard import TamperFatalError, TamperGuard
from .tiered_router import RoutingDecision, RoutingPath, TieredRouter
from .verification_profiles import (
    choose_verification_command,
    find_colocated_test_command,
    parse_verification_command_argv,
    validate_safe_verification_command,
    validate_safe_verification_argv,
)
from .witness_verifier import WitnessVerifier
from .workbuddy_bridge import (
    WorkBuddyGatewayClient,
    extract_and_apply_code_blocks,
    get_gateway_info,
    invoke_workbuddy,
)


logger = logging.getLogger(__name__)


_GOAL_FILE_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+[/\\])*[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|json|yaml|yml|toml|md|sh|html|htm|css|sql)\b"
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
    # Execution timeline (filled by `annotate_plan_timeline` / `ExecutionPacer`)
    eta_seconds: float = 0.0
    wave_index: int = -1
    started_at: float | None = None
    finished_at: float | None = None
    heartbeats: int = 0
    # Exam-Driven Task Handoff & Acceptance Assertions (带考题的任务分发与验收)
    acceptance_exam: list[dict[str, Any]] = field(default_factory=list)
    acceptance_test: dict[str, str] = field(default_factory=dict)
    exam_results: list[dict[str, Any]] = field(default_factory=list)
    dogfood_evidence: dict[str, Any] = field(default_factory=dict)
    verification_argv: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SubagentPlan:
    goal: str
    tasks: list[SubagentTask]
    repo_root: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    complexity: str = "standard"
    eta_seconds: float = 0.0
    wave_count: int = 0
    waves: list[list[str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "repo_root": self.repo_root,
            "created_at": self.created_at,
            "complexity": self.complexity,
            "eta_seconds": self.eta_seconds,
            "eta_human": format_duration(self.eta_seconds),
            "wave_count": self.wave_count,
            "waves": [list(w) for w in self.waves],
            "tasks": [t.to_dict() for t in self.tasks],
        }

    def get_task(self, task_id: str) -> SubagentTask | None:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None


def build_default_acceptance_exam(
    task_title: str,
    instructions: str,
    target_files: list[str],
    repo_root: str | Path,
    verification_cmd: str = "",
) -> list[dict[str, Any]]:
    """Generate deterministic acceptance assertions (考题) for subagent task handoff.

    Eliminates false green by mandating physical AST syntax check, symbol contract verification,
    and non-trivial execution assertions.
    """
    exam: list[dict[str, Any]] = []

    # 1. Syntax & AST exam for target python files
    py_targets = [f for f in target_files if f.endswith(".py")]
    if py_targets:
        exam.append({
            "id": "exam_syntax_ast",
            "question": f"All target Python files ({', '.join(py_targets[:3])}) must parse cleanly via AST without syntax errors.",
            "check_type": "syntax_ast",
            "target_files": py_targets,
        })

    # 2. File creation/modification and symbol contract exam
    if target_files:
        exam.append({
            "id": "exam_contract",
            "question": f"Target files ({', '.join(target_files[:3])}) must exist on disk and export valid symbols/definitions.",
            "check_type": "contract",
            "target_files": target_files,
        })

    # 3. Execution verification exam if verification_cmd is provided
    if verification_cmd:
        exam.append({
            "id": "exam_execution",
            "question": f"Verification command `{verification_cmd}` must physically run and exit with code 0.",
            "check_type": "execution",
            "assertion_cmd": verification_cmd,
            "expected_exit_code": 0,
        })

    return exam


def verify_acceptance_exam(
    task: SubagentTask,
    repo_root: str | Path,
) -> tuple[bool, list[dict[str, Any]], str]:
    """Physically grade acceptance assertions attached to a subagent task.

    Returns:
        (all_passed, results_list, critique_message)
    """
    root = Path(repo_root).resolve()
    results: list[dict[str, Any]] = []
    failures: list[str] = []

    if not task.acceptance_exam:
        return True, [], ""

    for ex in task.acceptance_exam:
        qid = str(ex.get("id") or "exam_check")
        q_text = str(ex.get("question") or "")
        c_type = str(ex.get("check_type") or "contract")
        targets = ex.get("target_files") or task.target_files or []

        if c_type == "syntax_ast":
            syntax_ok = True
            err_detail = ""
            for tf in targets:
                if not tf.endswith(".py"):
                    continue
                fp = root / tf
                if not fp.is_file():
                    syntax_ok = False
                    err_detail = f"File '{tf}' does not exist on disk"
                    break
                try:
                    ast.parse(fp.read_text(encoding="utf-8", errors="ignore"))
                except SyntaxError as se:
                    syntax_ok = False
                    err_detail = f"SyntaxError in {tf}:{se.lineno}: {se.msg}"
                    break
                except Exception as exc:
                    syntax_ok = False
                    err_detail = f"AST parse error in {tf}: {exc}"
                    break

            results.append({
                "id": qid,
                "question": q_text,
                "check_type": c_type,
                "passed": syntax_ok,
                "score": 100 if syntax_ok else 0,
                "details": err_detail or "All target files parsed cleanly without syntax errors",
            })
            if not syntax_ok:
                failures.append(f"[{qid}] Syntax/AST verification failed: {err_detail}")

        elif c_type == "contract":
            contract_ok = True
            err_detail = ""
            for tf in targets:
                fp = root / tf
                if not fp.exists():
                    contract_ok = False
                    err_detail = f"Target file '{tf}' was not created or modified on disk"
                    break
                if tf.endswith(".py") and fp.is_file():
                    try:
                        tree = ast.parse(fp.read_text(encoding="utf-8", errors="ignore"))
                        has_defs = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for n in tree.body)
                        # Comments never appear in the AST. An empty body means
                        # the file is blank or comment-only — no executable code,
                        # so it cannot satisfy a contract.
                        if not tree.body:
                            contract_ok = False
                            err_detail = f"Target file '{tf}' has no executable statements (blank or comment-only)"
                            break
                        if not has_defs and fp.stat().st_size == 0:
                            contract_ok = False
                            err_detail = f"Target file '{tf}' is completely empty (zero-byte stub)"
                            break
                    except SyntaxError as se:
                        contract_ok = False
                        err_detail = f"Target file '{tf}' does not parse: {se.msg}"
                        break
                    except Exception:
                        pass

            results.append({
                "id": qid,
                "question": q_text,
                "check_type": c_type,
                "passed": contract_ok,
                "score": 100 if contract_ok else 0,
                "details": err_detail or "Target files contract verified on disk",
            })
            if not contract_ok:
                failures.append(f"[{qid}] Contract verification failed: {err_detail}")

        elif c_type in ("execution", "exit_code"):
            # An exam's own assertion_cmd must win over the task-wide
            # verification_command; otherwise the specific check never runs.
            cmd = ex.get("assertion_cmd") or task.verification_command
            expected_code = ex.get("expected_exit_code", 0)
            if not cmd:
                results.append({
                    "id": qid,
                    "question": q_text,
                    "check_type": c_type,
                    "passed": False,
                    "score": 0,
                    "details": "No assertion_cmd or verification_command provided; cannot verify",
                })
                failures.append(f"[{qid}] Execution check has no command to run; refusing to pass unverified")
                continue

            # If the execution assertion is the task's main verification command, delegate to Step 5
            # to leverage the full Canvas feedback loop, line-number location parsing, and self-repair loop.
            if task.verification_command and cmd == task.verification_command:
                results.append({
                    "id": qid,
                    "question": q_text,
                    "check_type": c_type,
                    "passed": True,
                    "score": 100,
                    "details": f"Delegated to automated verification step: `{cmd}`",
                })
                continue

            try:
                child_env = os.environ.copy()
                child_env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
                res = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    timeout=ex.get("timeout_seconds", 60),
                    env=child_env,
                    # Acceptance checks are background probes; a console flash per
                    # assertion would be pure noise on Windows.
                    **hidden_subprocess_kwargs(),
                )
                exec_ok = (res.returncode == expected_code)
                out = (res.stdout + "\n" + res.stderr).strip()
                results.append({
                    "id": qid,
                    "question": q_text,
                    "check_type": c_type,
                    "passed": exec_ok,
                    "score": 100 if exec_ok else 0,
                    "details": f"Exit code {res.returncode} (expected {expected_code}). Output: {out[:300]}",
                })
                if not exec_ok:
                    failures.append(f"[{qid}] Execution command `{cmd}` failed with exit code {res.returncode}:\n{out[:400]}")
            except subprocess.TimeoutExpired:
                results.append({
                    "id": qid,
                    "question": q_text,
                    "check_type": c_type,
                    "passed": False,
                    "score": 0,
                    "details": f"Command `{cmd}` timed out",
                })
                failures.append(f"[{qid}] Execution command `{cmd}` timed out")
            except Exception as exc:
                results.append({
                    "id": qid,
                    "question": q_text,
                    "check_type": c_type,
                    "passed": False,
                    "score": 0,
                    "details": str(exc),
                })
                failures.append(f"[{qid}] Failed to run command `{cmd}`: {exc}")
        else:
            # Unknown check_type must not silently pass — that is a false-green
            # hole. Reject so the coder/plan gets fixed instead of rubber-stamped.
            results.append({
                "id": qid,
                "question": q_text,
                "check_type": c_type,
                "passed": False,
                "score": 0,
                "details": f"Unknown check_type '{c_type}'; refusing to accept an unexecuted check",
            })
            failures.append(f"[{qid}] Unknown check_type '{c_type}' — no verification was performed")

    all_passed = len(failures) == 0
    critique = ""
    if not all_passed:
        critique = (
            "[CRITICAL: Acceptance Exam (验收考题) Failed]\n"
            "Your changes failed the physical acceptance exam:\n"
            + "\n".join(f"- {f}" for f in failures)
            + "\nPlease address these specific defects and ensure your solution satisfies all criteria."
        )
    return all_passed, results, critique


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

    test_section = re.search(
        r"(?is)(?:^|[\r\n。！？!?；;.])\s*(?:tests?(?:\s+files?)?|测试(?:文件|用例)?)\s*[:：]\s*"
        r"(.*?)(?=\n\s*(?:source|源码|acceptance|验收|目标|要求)\s*[:：]|\Z)",
        str(goal or ""),
    )
    if test_section and re.search(
        r"修改|改为|更新|删除|添加|新增|修正|替换|保留|"
        r"\b(?:modify|change|update|remove|delete|add|replace|rewrite)\b",
        test_section.group(1),
        flags=re.IGNORECASE,
    ):
        return True

    positive_markers = (
        "add test",
        "write test",
        "create test",
        "update test",
        "add unit test",
        "write unit test",
        "new test",
        "fix test",
        "modify test",
        "correct test",
        "repair test",
        "新增单测",
        "编写单测",
        "添加单测",
        "更新单测",
        "修复单测",
        "修正单测",
        "修正刚才那组单测",
        "修改单测",
        "调整单测",
        "编写测试",
        "添加测试",
        "新增测试",
        "更新测试",
        "修复测试",
        "修改测试",
        "修正测试",
        "调整测试",
        "附带完整单测",
        "补充单测",
        "写单测",
        "写测试",
    )
    return any(pos in text for pos in positive_markers) or bool(
        re.search(r"\b(?:add|write|create|update)\b[^\n.]{0,50}\b(?:tests?|unit tests?)\b", text)
    )


def discover_target_files_for_goal(goal: str, repo_root: Path) -> list[str]:
    """Fast local discovery of target files mentioned in `goal` without calling an LLM."""
    root = repo_root.expanduser().resolve()
    explicit = [m.replace("\\", "/").lstrip("/") for m in _GOAL_FILE_RE.findall(str(goal or ""))]
    safe_explicit: list[str] = []
    valid: list[str] = []
    for cand_str in explicit:
        try:
            cand_p = (root / cand_str).resolve()
            if cand_p.is_file():
                valid.append(cand_p.relative_to(root).as_posix())
            else:
                # Search for filename match in repository
                cand_name = Path(cand_str).name
                matches = [
                    p.relative_to(root).as_posix()
                    for p in root.rglob(cand_name)
                    if p.is_file() and not any(part in {".git", ".pacer", ".agent-workspace", ".venv", "venv", "node_modules", "__pycache__", "backups", "releases"} or "audit" in part.lower() or "snapshot" in part.lower() or "backup" in part.lower() for part in p.parts)
                ]
                if matches:
                    valid.extend(matches)
                else:
                    rel = cand_p.relative_to(root).as_posix()
                    safe_explicit.append(rel)
        except (ValueError, RuntimeError):
            continue

    if valid:
        return list(dict.fromkeys(valid))[:4]
    if safe_explicit:
        # Even for new files not yet created on disk (e.g., examples/game_2048/core.py), keep safe paths!
        return list(dict.fromkeys(safe_explicit))[:4]

    # Match specific names and path segments, then rank candidates. Generic request
    # words such as "Pacer" must not select the first unrelated files in the repo.
    stop_words = {
        "pacer", "automatic", "task", "tasks", "path", "all", "python", "git",
        "shell", "model", "models", "cli", "call", "calls", "find", "identify",
        "missing", "windows", "hidden", "window", "parameter", "parameters",
        "cause", "causing", "flash", "flashing", "focus", "only", "analyze",
        "analysis", "audit", "inspect", "review", "modify", "file", "files",
        "test", "tests", "source", "code", "non", "interactive", "process",
        "subprocess", "cmd", "and", "the", "with", "from", "into", "for",
        "that", "which", "this", "these",
    }
    tokens = {
        tok.lower()
        for tok in re.findall(r"[A-Za-z_]\w{3,}", str(goal or ""))
    } - stop_words
    matched: list[tuple[int, str]] = []
    exclude_dirs = {".git", ".pacer", ".agent-workspace", ".venv", "venv", "env", "node_modules", "__pycache__", "backups", "releases"}

    for root_dir, dirnames, filenames in os.walk(str(repo_root)):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs and not d.startswith(".") and "audit" not in d.lower() and "snapshot" not in d.lower()]
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
            segments = {part.lower() for part in re.split(r"[\\/._-]", rel) if part}
            score = sum(1 for token in tokens if token in segments or token in rel.lower())
            if score:
                matched.append((score, rel))

    matched.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    return [rel for _, rel in matched[:4]]


def baseline_is_red(probe: dict[str, Any] | None) -> bool:
    """True only when the exam already failed before this edit."""
    if not isinstance(probe, dict):
        return False
    if probe.get("failed_on_base") is True:
        return True
    return str(probe.get("status") or "") == "failed_on_base"


def capture_workspace_snapshot(workspace_root: Path, relative_paths: list[str]) -> dict[str, str | None]:
    snapshot: dict[str, str | None] = {}
    for rel in relative_paths:
        path = workspace_root / rel
        if path.is_file():
            snapshot[rel] = path.read_text(encoding="utf-8", errors="replace")
        else:
            snapshot[rel] = None
    return snapshot


def restore_workspace_snapshot(
    workspace_root: Path,
    snapshot: dict[str, str | None],
    extra_paths: list[str] | None = None,
) -> None:
    """Put a failed attempt back, including files the attempt created."""
    for rel, content in snapshot.items():
        path = workspace_root / rel
        if content is None:
            if path.is_file():
                path.unlink()
        else:
            if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == content:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    for rel in extra_paths or []:
        if rel in snapshot:
            continue
        path = workspace_root / rel
        if path.is_file():
            path.unlink()


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

    if role in ("architect", "wish", "reviewer"):
        from .gemini4_vps_client import is_gemini4_vps_available
        if is_gemini4_vps_available():
            return "gemini4_vps"
        return "local"

    if role in ("indexer", "planner", "inspector", "verifier"):
        return "local"

    preferred = (os.environ.get("PACER_DEFAULT_BACKEND") or "").lower()
    if preferred in ("grok", "workbuddy"):
        return preferred

    if repeated_failure and grok_available:
        return "grok"
    if workbuddy_available:
        return "workbuddy"
    if grok_available:
        return "grok"
    return "local"


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
        return annotate_plan_timeline(
            _decompose_with_rules(
                goal=goal,
                repo_root=str(root),
                workbuddy_available=workbuddy_available,
                gemini_available=gemini_available,
            )
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
        return annotate_plan_timeline(plan)

    return annotate_plan_timeline(
        _decompose_with_rules(
            goal=goal,
            repo_root=str(root),
            workbuddy_available=workbuddy_available,
            gemini_available=gemini_available,
        )
    )


def _decompose_with_gemini(
    goal: str,
    repo_summary: str,
    repo_root: str,
    workbuddy_available: bool,
) -> SubagentPlan | None:
    detected_verify_cmd = ""
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

        exam_items = item.get("acceptance_exam")
        if isinstance(exam_items, list) and exam_items:
            task_exam = exam_items
        else:
            task_exam = build_default_acceptance_exam(
                task_title=title,
                instructions=instructions,
                target_files=target_files,
                repo_root=repo_root,
                verification_cmd=verification_cmd,
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
                acceptance_exam=task_exam,
            )
        )

    complexity = classify_task_complexity(goal).value
    return SubagentPlan(goal=goal, tasks=tasks, repo_root=repo_root, complexity=complexity)


def _resolve_explicit_goal_paths(text: str, root: Path) -> list[str]:
    """Return safe, workspace-relative paths explicitly named in a task fragment."""
    paths: list[str] = []
    for match in _GOAL_FILE_RE.findall(str(text or "")):
        raw = str(match).replace("\\", "/").lstrip("/")
        rel = Path(raw)
        if rel.is_absolute() or ".." in rel.parts:
            continue
        candidate = (root / rel).resolve()
        try:
            normalized = candidate.relative_to(root).as_posix()
        except (ValueError, RuntimeError):
            continue
        paths.append(normalized)
    return list(dict.fromkeys(paths))


def _numbered_goal_items(goal: str) -> list[str]:
    """Split a numbered requirement list while retaining each item's full instruction."""
    markers = list(re.finditer(r"(?<!\S)(\d{1,2})[.)、]\s+", str(goal or "")))
    if len(markers) < 2:
        return []
    return [
        str(goal)[marker.end():markers[index + 1].start() if index + 1 < len(markers) else len(goal)].strip(" \t\r\n;；")
        for index, marker in enumerate(markers)
    ]


def _stage_goal_items(goal: str) -> list[tuple[str, str]]:
    markers = list(re.finditer(r"(?i)(?<!\w)(?:阶段|stage|phase)\s*([A-Z]|\d+)\s*[:：]", str(goal or "")))
    if len(markers) < 2:
        return []
    text = str(goal)
    return [
        (marker.group(1), text[marker.end():markers[index + 1].start() if index + 1 < len(markers) else len(text)].strip())
        for index, marker in enumerate(markers)
    ]


def _available_parallel_coder_backends(workbuddy_available: bool, gemini_available: bool) -> list[str]:
    backends: list[str] = []
    if gemini_available or is_gemini38_available():
        backends.append("gemini38")
    # Keep the established Gemini/WorkBuddy split when WorkBuddy is known to
    # be available. In the local two-family setup (Gemini + Codex), Codex is
    # the second parallel writer and the normal Gemini failover target.
    if not workbuddy_available and (resolve_codex_executable() or shutil_which("codex")):
        backends.append("codex")
    if workbuddy_available:
        backends.append("workbuddy")
    try:
        from .provider_runtime import api_nodes

        if any(node.is_available() for node in api_nodes("domestic_api")):
            backends.append("domestic_api")
        if any(node.is_available() for node in api_nodes("premium_api")):
            backends.append("premium_api")
    except (OSError, ValueError, KeyError):
        pass
    return list(dict.fromkeys(backends)) or ["local"]


def _decompose_numbered_file_items(
    goal: str,
    root: Path,
    *,
    workbuddy_available: bool,
    gemini_available: bool,
) -> list[SubagentTask] | None:
    items = _numbered_goal_items(goal)
    parsed = [(item, _resolve_explicit_goal_paths(item, root)) for item in items]
    parsed = [(item, paths) for item, paths in parsed if paths]
    if len(parsed) < 2:
        paths = [p for p in _resolve_explicit_goal_paths(goal, root) if p.endswith('.py') and not is_test_path(p)]
        if len(paths) < 2:
            return None
        # Retain the full contract while constraining each worker to its module.
        parsed = [(goal, [p]) for p in paths]

    backends = _available_parallel_coder_backends(workbuddy_available, gemini_available)
    module_tasks: list[SubagentTask] = []
    list_paths: set[str] = set()
    for index, (instruction, paths) in enumerate(parsed, 1):
        source_paths = [path for path in paths if not is_test_path(path)]
        test_paths = [path for path in paths if is_test_path(path)]
        if not test_paths and re.search(r"单测|测试|pytest|\btests?\b", goal, re.I):
            test_paths = [f"tests/test_{Path(source_paths[0]).stem}.py"] if source_paths else []
        list_paths.update(paths)
        if not source_paths:
            continue
        verification_argv = [sys.executable, "-m", "pytest", *test_paths, "-q"] if test_paths else []
        module_tasks.append(SubagentTask(
            id=f"coder_{index}",
            title=f"Implement independent module {index}",
            role="coder",
            backend=backends[(index - 1) % len(backends)],
            instructions=(
                f"Implement only the independent requirement below. Keep its changes within "
                f"{', '.join(source_paths + test_paths)}. Create and run the named tests.\n\n{instruction}"
            ),
            target_files=source_paths + test_paths,
            verification_command=subprocess.list2cmdline(verification_argv) if verification_argv else "",
            verification_argv=verification_argv,
            dependencies=[],
        ))

    if len(module_tasks) < 2:
        return None

    all_named_paths = _resolve_explicit_goal_paths(goal, root)
    shared_test_paths = [
        path for path in all_named_paths
        if is_test_path(path) and path not in list_paths
    ]
    embedded_test_paths = [
        path for _instruction, paths in parsed for path in paths if is_test_path(path)
    ]
    test_paths = list(dict.fromkeys(shared_test_paths + embedded_test_paths))
    tasks: list[SubagentTask] = list(module_tasks)
    verify_command = ""
    verification_argv: list[str] = []
    if test_paths:
        verification_argv = [sys.executable, "-m", "pytest", *test_paths, "-q"]
        verify_command = subprocess.list2cmdline(verification_argv)
        test_task = SubagentTask(
            id="coder_tests",
            title="Add shared acceptance tests for parallel modules",
            role="coder",
            backend=backends[len(module_tasks) % len(backends)],
            instructions=(
                f"After all independent modules are implemented, create or update "
                f"{', '.join(test_paths)} with substantive tests covering every numbered requirement."
            ),
            target_files=test_paths,
            verification_command=verify_command,
            verification_argv=verification_argv,
            dependencies=[task.id for task in module_tasks],
        )
        tasks.append(test_task)
    else:
        verify_command = find_colocated_test_command(
            [path for task in module_tasks for path in task.target_files], repo_root=root
        ) or ""

    if verify_command and not test_paths:
        tasks.append(SubagentTask(
            id="verify_parallel_modules",
            title="Verify all independent modules",
            role="verifier",
            backend="local",
            instructions="Run the shared verification command after all independent module tasks complete.",
            verification_command=verify_command,
            dependencies=[task.id for task in module_tasks],
        ))
    elif verify_command and test_paths:
        # The test-writing task runs the physical verification gate itself.
        tasks[-1].verification_command = verify_command
    return tasks


def _decompose_stage_items(goal: str, root: Path, backends: list[str]) -> list[SubagentTask] | None:
    stages = _stage_goal_items(goal)
    parsed = [(label, text, _resolve_explicit_goal_paths(text, root)) for label, text in stages]
    parsed = [(label, text, paths) for label, text, paths in parsed if paths]
    if len(parsed) < 2:
        return None

    tasks: list[SubagentTask] = []
    previous_ids: list[str] = []
    last_test_paths: list[str] = []
    for index, (label, instruction, paths) in enumerate(parsed, 1):
        task_id = f"phase_{label.lower()}_{index}"
        last_test_paths = [path for path in paths if is_test_path(path)] or last_test_paths
        tasks.append(SubagentTask(
            id=task_id,
            title=f"Implement phase {label}",
            role="coder",
            backend=backends[(index - 1) % len(backends)],
            instructions=f"Complete phase {label} only after its listed dependencies.\n\n{instruction}",
            target_files=paths,
            dependencies=list(previous_ids),
        ))
        previous_ids = [task_id]

    all_paths = _resolve_explicit_goal_paths(goal, root)
    if not last_test_paths:
        last_test_paths = [path for path in all_paths if is_test_path(path)]
    verification_argv = [sys.executable, "-m", "pytest", *last_test_paths, "-q"] if last_test_paths else []
    verify_command = (
        subprocess.list2cmdline(verification_argv)
        if last_test_paths else find_colocated_test_command(all_paths, repo_root=root) or ""
    )
    if verify_command:
        tasks.append(SubagentTask(
            id="verify_phases",
            title="Verify completed milestone pipeline",
            role="verifier",
            backend="local",
            instructions="Run the end-to-end verification after the final phase.",
            verification_command=verify_command,
            verification_argv=verification_argv,
            dependencies=list(previous_ids),
        ))
    return tasks


def _decompose_with_rules(
    goal: str,
    repo_root: str,
    workbuddy_available: bool,
    gemini_available: bool,
) -> SubagentPlan:
    """Fast deterministic decomposition with local AST indexer and dynamic verification discovery."""
    root = Path(repo_root).expanduser().resolve()
    target_files = discover_target_files_for_goal(goal, root)
    explicit_paths = _resolve_explicit_goal_paths(goal, root)
    if explicit_paths:
        target_files = list(dict.fromkeys(target_files + explicit_paths))
    # Runtime Pacer state mentioned in a feature request is not an implementation
    # file to be created in the repository during the coding task.
    target_files = [p for p in target_files if not p.replace('\\', '/').startswith('.pacer/')]
    complexity = classify_task_complexity(goal, target_files)
    lower_goal = str(goal or "").lower()
    compact_goal = re.sub(r"\s+", "", lower_goal)
    read_only_markers = (
        "只读", "只分析", "仅分析", "只审计", "仅审计", "只检查", "仅检查",
        "不修改", "不改文件", "无需修改", "do not modify", "don't modify",
        "without modifying", "read-only", "read only", "audit only",
        "inspect only", "analysis only", "only analyze",
    )
    verification_only_markers = (
        "只验证", "仅验证", "只运行测试", "只跑测试", "验证专用",
        "verification-only", "verify only", "no code edits",
    )
    mutation_markers = (
        "修复", "实现", "添加", "新增", "更新", "重构", "增加", "删除",
        "fix code", "implement", "add code", "update code", "refactor",
    )
    verification_only_requested = any(
        marker in lower_goal or marker in compact_goal for marker in verification_only_markers
    )
    read_only_requested = verification_only_requested or (
        any(marker in lower_goal or marker in compact_goal for marker in read_only_markers)
        and not any(marker in lower_goal for marker in mutation_markers)
    )
    coder_backend = "local" if read_only_requested else select_best_backend(
        role="coder",
        instructions=goal,
        target_files=target_files,
        workbuddy_available=workbuddy_available,
        gemini_available=gemini_available,
    )
    default_verify_cmd = find_colocated_test_command(target_files, repo_root=root) or ""
    default_verify_argv: list[str] = []
    explicit_tests = [p for p in explicit_paths if is_test_path(p) and p.endswith('.py')]
    if not explicit_tests and re.search(r"单测|测试|pytest|\btests?\b", goal, re.I):
        sources = [p for p in target_files if p.endswith('.py') and not is_test_path(p)]
        if sources:
            explicit_tests = [f"tests/test_{Path(sources[0]).stem}.py"]
    if explicit_tests:
        target_files = list(dict.fromkeys(target_files + explicit_tests))
        default_verify_argv = [sys.executable, "-m", "pytest", *explicit_tests, "-q"]
        default_verify_cmd = subprocess.list2cmdline(default_verify_argv)

    if read_only_requested:
        tasks = [
            SubagentTask(
                id="task_1",
                title="Local read-only inspection",
                role="indexer",
                backend="local",
                instructions=f"Inspect AST symbols and dependencies for: {goal}",
                target_files=target_files,
                dependencies=[],
            ),
            SubagentTask(
                id="task_2",
                title="Read-only audit completion",
                role="verifier",
                backend="local",
                instructions=f"Complete the read-only audit for: {goal}",
                target_files=target_files,
                verification_command=default_verify_cmd,
                dependencies=["task_1"],
            ),
        ]
        ret_complexity = complexity.value
    elif (stage_tasks := _decompose_stage_items(
        goal,
        root,
        _available_parallel_coder_backends(workbuddy_available, gemini_available),
    )) is not None:
        tasks = stage_tasks
        ret_complexity = TaskComplexity.L3.value
    elif (parallel_tasks := _decompose_numbered_file_items(
        goal,
        root,
        workbuddy_available=workbuddy_available,
        gemini_available=gemini_available,
    )) is not None:
        tasks = parallel_tasks
        ret_complexity = TaskComplexity.L2.value
    elif complexity == TaskComplexity.L1:
        tasks = [
            SubagentTask(
                id="task_1",
                title="Fast micro-patch",
                role="coder",
                backend=coder_backend,
                instructions=f"Implement changes required for: {goal}",
                target_files=target_files,
                verification_command=default_verify_cmd,
                dependencies=[],
            ),
            SubagentTask(
                id="task_2",
                title="Run automated verification",
                role="verifier",
                backend="local",
                instructions=f"Verify implementation for: {goal}",
                verification_command=default_verify_cmd,
                dependencies=["task_1"],
            ),
        ]
        ret_complexity = TaskComplexity.L1.value
    elif complexity == TaskComplexity.L3:
        tasks = [
            SubagentTask(
                id="task_1",
                title="Role 0 Architect: Architecture contract & symbol indexing",
                role="indexer",
                backend="local",
                instructions=f"Index architectural symbols and contract stubs for: {goal}",
                target_files=target_files,
                dependencies=[],
            ),
            SubagentTask(
                id="task_2",
                title="Role 2 Coder: Implement contract-driven modules",
                role="coder",
                backend=coder_backend,
                instructions=f"Implement modules adhering to architectural contract for: {goal}",
                target_files=target_files,
                verification_command=default_verify_cmd,
                dependencies=["task_1"],
            ),
            SubagentTask(
                id="task_3",
                title="Role 4 Verifier Gate: Run automated verification",
                role="verifier",
                backend="local",
                instructions=f"Verify complete system integration for: {goal}",
                verification_command=default_verify_cmd,
                dependencies=["task_2"],
            ),
        ]
        ret_complexity = TaskComplexity.L3.value
    else:
        tasks = [
            SubagentTask(
                id="task_1",
                title="Local AST symbol & dependency indexing",
                role="indexer",
                backend="local",
                instructions=f"Analyze AST symbols and focus files for: {goal}",
                target_files=target_files,
                dependencies=[],
            ),
            SubagentTask(
                id="task_2",
                title="Implement SEARCH/REPLACE patch",
                role="coder",
                backend=coder_backend,
                instructions=f"Implement changes required for: {goal}",
                target_files=target_files,
                verification_command=default_verify_cmd,
                dependencies=["task_1"],
            ),
            SubagentTask(
                id="task_3",
                title="Run automated verification",
                role="verifier",
                backend="local",
                instructions=f"Verify implementation for: {goal}",
                verification_command=default_verify_cmd,
                dependencies=["task_2"],
            ),
        ]
        ret_complexity = TaskComplexity.L2.value

    # Attach exam assertions to all subtasks for exam-driven handoff & verification
    for t in tasks:
        if default_verify_argv and t.verification_command == default_verify_cmd:
            t.verification_argv = list(default_verify_argv)
        if not t.acceptance_exam:
            t.acceptance_exam = build_default_acceptance_exam(
                task_title=t.title,
                instructions=t.instructions,
                target_files=t.target_files,
                repo_root=root,
                verification_cmd=t.verification_command,
            )

    return SubagentPlan(goal=goal, tasks=tasks, repo_root=repo_root, complexity=ret_complexity)


def _is_safe_smoke_command(cmd: str) -> bool:
    """Check if a verification command is safe to run inside a dogfood venv.

    Only allows pytest/unittest/python commands. Rejects anything that
    could modify the workspace or execute arbitrary shell operations.
    """
    if not cmd:
        return False
    cmd_lower = cmd.strip().lower()
    allowed_prefixes = ("python -m pytest", "python -m unittest", "pytest")
    if any(cmd_lower.startswith(p) for p in allowed_prefixes):
        # Reject if it contains shell operators or redirects
        dangerous = ("&&", "||", ";", "|", ">", "<", "`", "$", "rm ", "del ", "rmdir")
        if any(d in cmd for d in dangerous):
            return False
        return True
    return False


def _is_transient_transport_error(error_msg: str | None) -> bool:
    """True for transport-layer failures that are worth one retry.

    These happen before a request completes, so no model tokens are consumed.
    Auth/quota/5xx are NOT transient — they must not be retried. Note that a 502
    is often wrapped in URLError, so 5xx must be excluded explicitly.
    """
    if not error_msg:
        return False
    msg = error_msg.lower()
    if any(k in msg for k in ("401", "403", "unauthorized", "forbidden", "invalid_api_key", "quota", "429")):
        return False
    # Upstream/gateway 5xx means the provider is down right now. Retrying
    # immediately just burns latency; the circuit breaker should handle it.
    if any(k in msg for k in ("502", "503", "504", "bad gateway", "service unavailable", "gateway timeout")):
        return False
    transient_markers = (
        "ssl",
        "handshake",
        "connection reset",
        "connectionreseterror",
        "remotedisconnected",
        "timed out",
        "timeout",
        "urlerror",
        "temporary failure",
        "network is unreachable",
        "connection aborted",
    )
    return any(marker in msg for marker in transient_markers)


def _capture_file_fingerprints(workspace_root: Path, rel_paths: list[str]) -> dict[str, dict[str, Any]]:
    """Capture content hashes as well as size and mtime for resume verification."""
    fingerprints: dict[str, dict[str, Any]] = {}
    for rel in rel_paths or []:
        fp = Path(workspace_root) / rel
        try:
            stat = fp.stat()
            fingerprints[rel] = {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns), "sha256": hashlib.sha256(fp.read_bytes()).hexdigest()}
        except OSError:
            fingerprints[rel] = {"size": -1, "mtime_ns": -1, "sha256": ""}
    return fingerprints


def _task_conflict_footprint(task: SubagentTask) -> set[str] | None:
    """Files a same-wave task may write, including its acceptance test and command targets."""
    paths = [str(path) for path in (task.target_files or []) if str(path).strip()]
    test_path = str((task.acceptance_test or {}).get("path") or "") if isinstance(task.acceptance_test, dict) else ""
    if test_path:
        paths.append(test_path)
    paths.extend(_GOAL_FILE_RE.findall(str(task.verification_command or "")))
    normalized = {
        Path(path.replace("\\", "/")).as_posix().casefold()
        for path in paths
        if path.strip()
    }
    # A task without declared file scope may have workspace-wide side effects; keep it serial.
    return normalized or None


def _conflict_free_wave_batches(wave: list[SubagentTask]) -> list[list[SubagentTask]]:
    """Greedily group tasks whose declared file footprints do not overlap."""
    batches: list[list[SubagentTask]] = []
    batch_files: list[set[str]] = []
    for task in wave:
        footprint = _task_conflict_footprint(task)
        placed = False
        if footprint is not None:
            for index, occupied in enumerate(batch_files):
                if occupied.isdisjoint(footprint):
                    batches[index].append(task)
                    occupied.update(footprint)
                    placed = True
                    break
        if not placed:
            batches.append([task])
            batch_files.append(set(footprint or {f"<unknown:{task.id}>"}))
    return batches


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
        "timed out",
        "timeout",
        "operation timed out",
        "httperror",
        "connectionreseterror",
        "remotedisconnected",
        "socket error",
        "not found on path",
        "executable not found",
        "all workbuddy gateway accounts exhausted",
        "no grok api key configured",
        "gemini cli not available",
        "concurrency",
        "limit exceeded",
        "busy",
        "capacity",
        "server error",
        "overloaded",
        "too many requests",
        # Upstream / gateway errors — these mean the model provider is down,
        # not that the code logic is wrong. Must trigger failover.
        "502",
        "503",
        "504",
        "upstream",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "internal server error",
        "missing api key", "missing base url", "missing endpoint",
        "missing_api_key", "missing_base_url", "request failed:", "529",
    )
    return any(marker in msg for marker in infra_markers)


# ==============================================================================
# Execution Timeline: per-task ETA, wave concurrency and real-time heartbeat
# ==============================================================================
#
# The estimates below are *deterministic heuristics*, not fabricated timings: each
# factor maps to work the orchestrator actually pays for at run time (backend
# latency, files the AST indexer must walk, prompt size, verification subprocess).
# `ExecutionPacer` then recalibrates the remaining estimates from observed
# durations, so the ETA converges instead of staying frozen at its first guess.

_ROLE_EFFORT_SECONDS: dict[str, float] = {
    "planner": 4.0,
    "indexer": 3.0,
    "architect": 6.0,
    "coder": 18.0,
    "verifier": 10.0,
    "reviewer": 8.0,
}
_DEFAULT_ROLE_EFFORT = 8.0
_DEFAULT_BACKEND_FACTOR = 1.0
_BACKEND_LATENCY_FACTOR: dict[str, float] = {
    "local": 0.35,      # in-process, no network
    "api": 0.90,
    "gemini": 1.00,
    "gemini38": 1.10,
    "workbuddy": 1.15,
    "grok": 1.20,
    "codex": 2.40,      # spawns a CLI subprocess per attempt
}
_COMPLEXITY_FACTOR: dict[str, float] = {"L1": 0.60, "L2": 1.00, "L3": 1.60}

# Fixed costs the orchestrator really incurs: per-file AST indexing, prompt
# assembly, verification subprocess startup, and the inter-wave guard sweep.
_PER_FILE_INDEX_SECONDS = 1.5
_PROMPT_CHARS_PER_SECOND = 250.0
_VERIFICATION_OVERHEAD_SECONDS = 5.0
_RETRY_EXPECTATION = 0.40  # historical share of tasks that need one repair attempt
_WAVE_BARRIER_SECONDS = 1.5


def estimate_task_seconds(task: "SubagentTask", *, complexity: str = "standard") -> float:
    """Deterministic effort estimate for one subagent task.

    role effort x backend latency x plan complexity, plus per-file indexing,
    prompt-size cost, verification startup and expected retry overhead.
    """
    base = _ROLE_EFFORT_SECONDS.get(str(task.role).lower(), _DEFAULT_ROLE_EFFORT)
    base *= _BACKEND_LATENCY_FACTOR.get(str(task.backend).lower(), _DEFAULT_BACKEND_FACTOR)
    base *= _COMPLEXITY_FACTOR.get(str(complexity).upper(), 1.0)

    base += _PER_FILE_INDEX_SECONDS * max(0, len(task.target_files or []) - 1)
    base += min(6.0, len(task.instructions or "") / _PROMPT_CHARS_PER_SECOND)
    if task.verification_command:
        base += _VERIFICATION_OVERHEAD_SECONDS
    if task.max_retries > 0:
        base *= 1.0 + _RETRY_EXPECTATION * 0.6

    return max(1.0, round(base, 1))


def format_duration(seconds: float) -> str:
    """Render a duration as `mm:ss` (`h:mm:ss` past one hour)."""
    total = max(0, int(round(float(seconds or 0.0))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def render_progress_bar(fraction: float, width: int = 24) -> str:
    """ASCII progress bar; between 0 and 1 it always shows at least one filled cell."""
    clamped = min(1.0, max(0.0, float(fraction or 0.0)))
    width = max(4, int(width))
    filled = int(round(clamped * width))
    if 0.0 < clamped < 1.0:
        filled = min(width - 1, max(1, filled))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def partition_waves(tasks: list["SubagentTask"]) -> list[list["SubagentTask"]]:
    """Topological waves; degrades to a deterministic linear chain on an invalid DAG."""
    try:
        return topological_waves(tasks)
    except (ValueError, CycleError):
        return [[t] for t in sorted(tasks, key=lambda x: x.id)]


def annotate_plan_timeline(plan: "SubagentPlan") -> "SubagentPlan":
    """Attach ETA + wave membership to every task and to the plan itself (idempotent)."""
    waves = partition_waves(plan.tasks)
    complexity = getattr(plan, "complexity", "standard")
    total = 0.0
    for wave_idx, wave in enumerate(waves):
        wave_eta = 0.0
        for task in wave:
            task.eta_seconds = estimate_task_seconds(task, complexity=complexity)
            task.wave_index = wave_idx
            wave_eta = max(wave_eta, task.eta_seconds)
        total += wave_eta + _WAVE_BARRIER_SECONDS
    plan.waves = [[t.id for t in wave] for wave in waves]
    plan.wave_count = len(waves)
    plan.eta_seconds = round(total, 1)
    return plan


class ExecutionPacer:
    """Live execution timeline: wave tracking, remaining-time ETA and heartbeat lines.

    `remaining_seconds()` is recomputed from scratch on every call (no accumulated
    drift): unfinished waves contribute `max(task ETA)` because tasks inside a wave
    run concurrently, and the estimate is scaled by a calibration factor learned
    from the tasks that already finished.
    """

    _MIN_CALIBRATION = 0.25
    _MAX_CALIBRATION = 4.0

    def __init__(
        self,
        plan: "SubagentPlan",
        waves: list[list["SubagentTask"]],
        *,
        heartbeat_interval: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.plan = plan
        self.waves = waves
        self.heartbeat_interval = float(heartbeat_interval)
        self._clock = clock
        self.task_by_id = {t.id: t for t in plan.tasks}
        self.wave_of: dict[str, int] = {}
        self.status: dict[str, str] = {}
        for wave_idx, wave in enumerate(waves):
            for task in wave:
                self.wave_of[task.id] = wave_idx
                self.status[task.id] = "pending"
                if not getattr(task, "eta_seconds", 0.0):
                    task.eta_seconds = estimate_task_seconds(
                        task, complexity=getattr(plan, "complexity", "standard")
                    )
        self.started_at = self._clock()
        self.finished_at: float | None = None
        self.task_started: dict[str, float] = {}
        self.task_actual: dict[str, float] = {}
        self.current_task_id: str = ""
        self.calibration = 1.0
        self.heartbeats_emitted = 0

    # ---------------------------------------------------------------- helpers
    def plan_eta_seconds(self) -> float:
        """Static plan ETA (waves sequential, tasks inside a wave parallel)."""
        total = 0.0
        for wave in self.waves:
            total += max((t.eta_seconds for t in wave), default=0.0) + _WAVE_BARRIER_SECONDS
        return round(total, 1)

    def _elapsed_of(self, task: "SubagentTask", now: float) -> float:
        started = self.task_started.get(task.id)
        return max(0.0, now - started) if started is not None else 0.0

    def _task_progress(self, task: "SubagentTask", now: float) -> float:
        if self.status.get(task.id) in ("completed", "failed", "skipped"):
            return 1.0
        eta = max(0.1, task.eta_seconds * self.calibration)
        # Cap at 95%: a running task must never look finished before it reports back.
        return min(0.95, self._elapsed_of(task, now) / eta)

    # ------------------------------------------------------------------ clock
    def remaining_seconds(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        current_wave = self.wave_of.get(self.current_task_id, -1)
        remaining = 0.0
        for wave_idx, wave in enumerate(self.waves):
            if wave_idx < current_wave:
                continue
            if all(self.status.get(t.id) in ("completed", "failed", "skipped") for t in wave):
                continue
            wave_remaining = 0.0
            for task in wave:
                if self.status.get(task.id) in ("completed", "failed", "skipped"):
                    continue
                budget = max(0.0, task.eta_seconds * self.calibration - self._elapsed_of(task, now))
                wave_remaining = max(wave_remaining, budget)
            remaining += wave_remaining + _WAVE_BARRIER_SECONDS
        return max(0.0, round(remaining, 1))

    def progress_fraction(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        if not self.task_by_id:
            return 1.0
        done = sum(1 for s in self.status.values() if s in ("completed", "failed", "skipped"))
        running = sum(
            self._task_progress(self.task_by_id[tid], now)
            for tid, status in self.status.items()
            if status == "running"
        )
        return min(1.0, (done + running) / len(self.task_by_id))

    def eta_clock_time(self, now: float | None = None) -> str:
        """Local wall-clock time at which the plan is expected to finish."""
        wall = datetime.now(timezone.utc).timestamp() + self.remaining_seconds(now)
        return datetime.fromtimestamp(wall, tz=timezone.utc).astimezone().strftime("%H:%M:%S")

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = self._clock() if now is None else now
        fraction = self.progress_fraction(now)
        return {
            "wave_index": self.wave_of.get(self.current_task_id, -1),
            "wave_count": len(self.waves),
            "current_task": self.current_task_id,
            "completed_tasks": sum(1 for s in self.status.values() if s in ("completed", "failed", "skipped")),
            "total_tasks": len(self.task_by_id),
            "progress": round(fraction, 4),
            "progress_bar": render_progress_bar(fraction),
            "elapsed_seconds": round(now - self.started_at, 1),
            "remaining_seconds": self.remaining_seconds(now),
            "eta_clock": self.eta_clock_time(now),
            "calibration": round(self.calibration, 3),
            "heartbeats": self.heartbeats_emitted,
        }

    # ------------------------------------------------------------- lifecycle
    def start_task(self, task: "SubagentTask", now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self.current_task_id = task.id
        self.task_started[task.id] = now
        self.status[task.id] = "running"

    def finish_task(self, task: "SubagentTask", status: str = "completed", now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self.status[task.id] = status
        elapsed = self._elapsed_of(task, now)
        self.task_actual[task.id] = round(elapsed, 2)
        if elapsed > 0.2 and task.eta_seconds > 0:
            ratio = min(self._MAX_CALIBRATION, max(self._MIN_CALIBRATION, elapsed / task.eta_seconds))
            # EWMA: trust the measurement, keep the previous belief as a prior.
            self.calibration = round(0.7 * self.calibration + 0.3 * ratio, 3)
        if self.current_task_id == task.id:
            self.current_task_id = ""

    def mark_skipped(self, task: "SubagentTask", now: float | None = None) -> None:
        self.status[task.id] = "skipped"

    def complete(self, now: float | None = None) -> None:
        self.finished_at = self._clock() if now is None else now

    # ------------------------------------------------------------ rendering
    def heartbeat_line(self, task: "SubagentTask", now: float | None = None) -> str:
        now = self._clock() if now is None else now
        self.heartbeats_emitted += 1
        fraction = self.progress_fraction(now)
        return (
            f"心跳 #{self.heartbeats_emitted} {render_progress_bar(fraction)} {fraction * 100:5.1f}% "
            f"| 波次 {self.wave_of.get(task.id, 0) + 1}/{len(self.waves)} "
            f"| {task.id} {task.role.upper()} 已用 {format_duration(self._elapsed_of(task, now))}"
            f"/预估 {format_duration(task.eta_seconds * self.calibration)} "
            f"| 剩余 {format_duration(self.remaining_seconds(now))} "
            f"| 预计完成 {self.eta_clock_time(now)} "
            f"| 当前岗位: {task.backend} | 剩余托管步数: {max(0, task.max_retries - task.retries)} "
            f"| 配额剩余时间: 按订阅窗口"
        )

    def wave_header(self, wave_idx: int) -> str:
        wave = self.waves[wave_idx]
        wave_eta = max((t.eta_seconds for t in wave), default=0.0) * self.calibration
        parallel = "并行" if len(wave) > 1 else "串行"
        return (
            f"━━ 波次 {wave_idx + 1}/{len(self.waves)} · {parallel} {len(wave)} 项 "
            f"· 本波预估 {format_duration(wave_eta)} "
            f"· 累计剩余 {format_duration(self.remaining_seconds())} ━━"
        )

    def gantt_lines(self, width: int = 22) -> list[str]:
        """Wave timeline preview: one row per wave, bar length proportional to ETA."""
        etas = [max((t.eta_seconds for t in wave), default=0.0) for wave in self.waves]
        total = sum(etas) or 1.0
        lines: list[str] = []
        for wave_idx, wave in enumerate(self.waves):
            cells = max(1, int(round(width * etas[wave_idx] / total)))
            bar = "█" * cells + "·" * (width - cells)
            ids = ", ".join(t.id for t in wave)
            lines.append(f"  Wave {wave_idx + 1} |{bar}| {format_duration(etas[wave_idx]):>6}  {ids}")
        return lines

    def summary(self) -> dict[str, Any]:
        now = self.finished_at if self.finished_at is not None else self._clock()
        planned = self.plan_eta_seconds()
        actual = round(now - self.started_at, 1)
        error_pct = round((actual - planned) / planned * 100, 1) if planned else 0.0
        return {
            "planned_eta_seconds": planned,
            "actual_seconds": actual,
            "eta_error_pct": error_pct,
            "calibration": round(self.calibration, 3),
            "wave_count": len(self.waves),
            "waves": [[t.id for t in wave] for wave in self.waves],
            "heartbeats": self.heartbeats_emitted,
            "per_task_actual_seconds": dict(self.task_actual),
        }


class _HeartbeatTicker:
    """Daemon ticker emitting one heartbeat line every `interval` seconds.

    Runs only while a task is in flight, so an idle orchestrator never writes to the
    terminal. `stop()` is idempotent and joins the thread, which keeps stdout
    ordering deterministic for callers that print immediately after a task returns.
    """

    def __init__(self, interval: float, callback: Callable[[], None]):
        self.interval = max(0.05, float(interval))
        self.callback = callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_HeartbeatTicker":
        self._thread = threading.Thread(target=self._loop, name="pacer-heartbeat", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.callback()
            except Exception as exc:  # a broken output sink must not kill the run
                print(f"[heartbeat] 心跳输出失败（已忽略）: {exc}", file=sys.stderr, flush=True)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.interval + 1.0)
        self._thread = None


class SubagentOrchestrator:
    """Executes a SubagentPlan using the Stateless Local Canvas + SEARCH/REPLACE diff patch loop."""

    def __init__(
        self,
        plan: SubagentPlan,
        *,
        on_progress: Callable[[SubagentTask, str], None] | None = None,
        workspace_root: str | Path | None = None,
        enforce_test_tamper_guard: bool | None = None,
        probe_base_discrimination: bool | None = None,
        require_red_baseline: bool | None = None,
        green_incremental: bool = False,
        use_stateless_canvas: bool = True,
        invariants: list[str] | None = None,
        session_source: str = "cli",
        enforce_anti_slop: bool = True,
        enforce_witness_verifier: bool = True,
        heartbeat_interval: float = 1.5,
        show_timeline: bool = True,
        resume: bool = False,
        allow_gemini4_diagnosis: bool = False,
        defer_trace_recording: bool = False,
    ):
        self._run_finalized = False
        self._write_lock = threading.RLock()
        self._checkpoint_lock = threading.RLock()
        self._canvas_prompt_lock = threading.RLock()
        self._pacer_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._verification_context = threading.local()
        self._api_node_cooldowns: dict[str, float] = {}
        self._api_node_attempts: list[dict[str, Any]] = []
        self._verification_receipts: list[dict[str, Any]] = []
        self.plan = plan
        self.on_progress = on_progress
        self.heartbeat_interval = float(heartbeat_interval)
        self.show_timeline = show_timeline
        self.resume = resume
        self.require_red_baseline = True if require_red_baseline is None else bool(require_red_baseline)
        self.green_incremental = bool(green_incremental)
        self.allow_gemini4_diagnosis = allow_gemini4_diagnosis
        self.defer_trace_recording = defer_trace_recording
        self.deferred_trace_payload: dict[str, Any] | None = None
        self._gemini4_diagnoses = 0
        self._failure_signatures: dict[str, int] = {}
        self.gemini4_session_ids: list[str] = []
        self._checkpoint_id = f"chk_{int(time.time())}"
        self.pacer: ExecutionPacer | None = None
        self.timeline_events: list[dict[str, Any]] = []
        self.workspace_root = Path(workspace_root or plan.repo_root).resolve()
        self._materialize_acceptance_tests()
        self.wb_client = WorkBuddyGatewayClient()
        self.use_stateless_canvas = use_stateless_canvas
        self.session_source = session_source
        self.canvas = CanvasManager(self.workspace_root)
        self._last_passed_verification: tuple[str, tuple[str, ...]] | None = None
        self._failover_path: list[str] = []
        self._effective_backend: str = ""
        self._prompt_dispatches: list[dict[str, Any]] = []
        # Circuit breaker: backend -> monotonic timestamp until which it is skipped.
        # A backend that returned an infra/quota error is demoted for a cooldown
        # window so dead providers do not burn latency on every single task.
        self._backend_cooldowns: dict[str, float] = {}

        # Guard and metrics tracking
        self.guard = TamperGuard(self.workspace_root)
        self.guard_tampered_files: list[str] = []
        self.mistakes_caught = 0
        self.self_heals_succeeded = 0
        self.slop_detected: list[dict[str, Any]] = []
        self.tamper_violations_blocked = 0
        self.enforce_anti_slop = enforce_anti_slop
        self.enforce_witness_verifier = enforce_witness_verifier
        self.witness_verifier = WitnessVerifier(self.workspace_root)
        self.witness_certificates: list[dict[str, Any]] = []
        self.false_greens_caught = 0

        # Tiered routing
        router = TieredRouter(self.workspace_root)
        target_files = []
        for t in self.plan.tasks:
            target_files.extend(t.target_files or [])
        self.routing_decision = router.route(self.plan.goal, list(dict.fromkeys(target_files)))

        if enforce_test_tamper_guard is None:
            self.enforce_test_tamper_guard = self.routing_decision.enforce_tamper_guard
        else:
            self.enforce_test_tamper_guard = enforce_test_tamper_guard

        if probe_base_discrimination is None:
            self.probe_base_discrimination = (
                self.routing_decision.probe_base_first or require_red_baseline is True
            )
        else:
            self.probe_base_discrimination = bool(probe_base_discrimination) or require_red_baseline is True

        # Context Profile Management (CPM): decouple repository context from chat prompt
        self.context_profile_manager = ContextProfileManager(self.workspace_root)
        self.context_profile = self.context_profile_manager.get_or_create_profile(
            invariants=invariants,
            focus_files=target_files,
        )

        # Initialize canvas goal & invariants
        verify_cmd = next((t.verification_command for t in self.plan.tasks if t.verification_command), "")
        self.canvas.update_goal(
            self.plan.goal,
            acceptance_cmd=verify_cmd,
            status="in_progress",
            reset_scratchpad=True,
            reset_focus=True,
        )
        if invariants:
            self.canvas.add_invariants(*invariants)
        # Compare worker changes to the bytes at task start, not to Git HEAD.
        # Existing user edits are legitimate input; caches are not test contracts.
        self._test_baseline = snapshot_verification_files(repo_root=self.workspace_root) if self.enforce_test_tamper_guard else {}
        self._allowed_test_targets = {
            Path(rel).as_posix() for task in self.plan.tasks if task.role == "coder"
            for rel in task.target_files if is_test_path(rel) and (
                _goal_allows_test_edits(self.plan.goal + "\n" + task.title + "\n" + task.instructions)
            )
        }
        self._chain_baselines = {
            command: snapshot_verification_files(repo_root=self.workspace_root, command=command, include_tests=False)
            for command in {t.verification_command for t in self.plan.tasks if t.verification_command}
        } if self.enforce_test_tamper_guard else {}

    def _materialize_acceptance_tests(self) -> None:
        """Create planner-authored tests before snapshots so the base probe runs them first."""
        pending: list[tuple[Path, str]] = []
        target_files = {
            Path(rel).as_posix()
            for task in self.plan.tasks
            for rel in (task.target_files or [])
        }
        witness = WitnessVerifier(self.workspace_root)
        for task in self.plan.tasks:
            spec = task.acceptance_test if isinstance(task.acceptance_test, dict) else {}
            relative = str(spec.get("path") or "").replace("\\", "/").strip()
            source = str(spec.get("source") or "")
            if not relative and not source:
                continue
            rel_path = Path(relative)
            if not relative or rel_path.is_absolute() or ".." in rel_path.parts:
                raise ValueError("acceptance_test path must be a safe workspace-relative path")
            normalized = rel_path.as_posix()
            if normalized in target_files:
                raise ValueError(f"acceptance_test must stay outside worker target_files: {normalized}")
            if not is_test_path(normalized) or rel_path.suffix.lower() != ".py":
                raise ValueError("acceptance_test must be a Python test file")
            target = (self.workspace_root / rel_path).resolve()
            try:
                target.relative_to(self.workspace_root)
            except ValueError as exc:
                raise ValueError("acceptance_test path escapes the workspace") from exc
            cursor = self.workspace_root
            for part in rel_path.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ValueError(f"acceptance_test path cannot traverse a symlink: {normalized}")
            if not source.strip() or len(source.encode("utf-8")) > 32_000:
                raise ValueError("acceptance_test source must be non-empty and at most 32 KB")
            command = str(task.verification_command or "").lower().replace("\\", "/")
            module = rel_path.with_suffix("").as_posix().replace("/", ".").lower()
            discovery_ref = (
                rel_path.name.lower() in command
                and (rel_path.parent == Path(".") or rel_path.parent.as_posix().lower() in command)
            )
            if normalized.lower() not in command and module not in command and not discovery_ref:
                raise ValueError(f"verification command does not run the acceptance_test: {normalized}")
            try:
                ast.parse(source)
            except SyntaxError as exc:
                raise ValueError(f"acceptance_test source has invalid Python syntax: {exc}") from exc
            substantive, detail, _violations = witness.verify_assertion_substance(source)
            if not substantive:
                raise ValueError(f"acceptance_test failed assertion-substance validation: {detail}")
            if target.exists():
                try:
                    existing = target.read_text(encoding="utf-8")
                except OSError as exc:
                    raise ValueError(f"cannot read existing acceptance_test: {normalized}") from exc
                if existing != source:
                    raise ValueError(f"refusing to overwrite an existing acceptance_test: {normalized}")
                continue
            pending.append((target, source))

        for target, source in pending:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8", newline="\n")

    def _notify_task_completion(self, summary: dict[str, Any]) -> None:
        """Non-blocking completion notification (email, webhook, or artifact dispatch)."""
        try:
            notif_dir = self.workspace_root / ".pacer" / "notifications"
            notif_dir.mkdir(parents=True, exist_ok=True)
            ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            notif_file = notif_dir / f"completion_{ts_str}.json"
            notif_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        except Exception:
            pass

    def _gemini4_request(self, latest_utterance: str) -> str:
        """One new Gemini 4 session. Payload is the portrait plus the latest sentence."""
        session_id = uuid.uuid4().hex
        self.gemini4_session_ids.append(session_id)
        profile = ""
        if self.context_profile is not None and hasattr(self.context_profile, "to_compact_markdown"):
            profile = self.context_profile.to_compact_markdown()
        payload = {
            "profile_summary": profile[:1500],
            "latest_utterance": str(latest_utterance or "")[:1500],
            "session_id": session_id,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _diagnose_with_gemini4(self, task: SubagentTask, error: str) -> str:
        """Ask Gemini 4 once per run after repeated exam failures."""
        if not self.allow_gemini4_diagnosis or self._gemini4_diagnoses >= 1 or task.retries < 2:
            return ""
        from .provider_runtime import invoke_architect as invoke_gemini4_vps, architect_node
        if not architect_node().is_available():
            return ""
        self._gemini4_diagnoses += 1
        system_prompt = "You are Pacer's diagnosis reviewer. Reply in Chinese, under 120 words."
        prompt = self._gemini4_request(error)
        self._record_model_request("gemini4_vps", system_prompt, prompt)
        response = invoke_gemini4_vps(
            prompt,
            system_prompt=system_prompt,
            timeout_seconds=45,
        )
        if not response.success or not response.content.strip():
            return ""
        note = response.content.strip()[:800]
        self._notify(task, "Gemini 4 已给出一次诊断，本次运行不再重复调用。")
        return note

    def _notify(self, task: SubagentTask, message: str) -> None:
        if self.on_progress:
            self.on_progress(task, message)
        else:
            print(f"[{task.id} | {task.role.upper()} | {task.backend}] {message}", flush=True)

    def _emit_timeline(self, task: SubagentTask | None, text: str) -> None:
        """Emit an ETA / wave / heartbeat line through the same sink as task progress."""
        self.timeline_events.append({"ts": round(time.time(), 3), "task_id": getattr(task, "id", ""), "message": text})
        if not self.show_timeline:
            return
        if self.on_progress and task is not None:
            self.on_progress(task, text)
        else:
            print(text, flush=True)

    def _emit_plan_timeline(self) -> None:
        """Announce the plan-wide ETA and the wave gantt before the first task starts."""
        if self.pacer is None:
            return
        self._emit_timeline(
            None,
            f"[Pacer ETA] 子任务 {len(self.plan.tasks)} 项 · 波次 {len(self.pacer.waves)} "
            f"· 预估总耗时 {format_duration(self.pacer.plan_eta_seconds())} "
            f"· 预计完成 {self.pacer.eta_clock_time()}",
        )
        for line in self.pacer.gantt_lines():
            self._emit_timeline(None, line)

    def _run_task_with_heartbeat(self, task: SubagentTask) -> bool:
        """Execute one task while a daemon ticker reports elapsed vs ETA progress."""
        if self.pacer is None or self.heartbeat_interval <= 0:
            return self._execute_task_with_retry(task)

        def beat() -> None:
            with self._pacer_lock:
                task.heartbeats += 1
                message = self.pacer.heartbeat_line(task)
            self._emit_timeline(task, message)

        with _HeartbeatTicker(self.heartbeat_interval, beat):
            return self._execute_task_with_retry(task)

    def _task_progress_line(self, task: SubagentTask, success: bool) -> str:
        if self.pacer is None:
            return f"任务{'完成' if success else '失败'}: {task.id}"
        with self._pacer_lock:
            snap = self.pacer.snapshot()
        return (
            f"{snap['progress_bar']} {snap['progress'] * 100:5.1f}% "
            f"| {task.id} {'完成' if success else '失败'} 实际 {format_duration(task.elapsed_seconds)} "
            f"(预估 {format_duration(task.eta_seconds)}) "
            f"| 剩余 {format_duration(snap['remaining_seconds'])} "
            f"| 预计完成 {snap['eta_clock']}"
        )

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
        recent_files = self.canvas.load().recent_focus_files
        if not candidate_files:
            discovery_text = (task.instructions or self.plan.goal).split("[Recovery Note]", 1)[0]
            candidate_files = discover_target_files_for_goal(discovery_text, self.workspace_root)
            if not candidate_files:
                # A follow-up without an explicit path continues from the last verified edit.
                candidate_files = list(recent_files[:4])
            task.target_files = list(candidate_files)
        if failing_file and failing_file not in candidate_files:
            candidate_files.insert(0, failing_file)
        for recent_path in recent_files:
            if recent_path not in candidate_files:
                candidate_files.append(recent_path)

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
        try:
            return self._run()
        finally:
            with self._write_lock:
                self._run_finalized = True
            if self.enforce_test_tamper_guard:
                self.guard.unlock_all()

    def _apply_task_patch(self, task: SubagentTask, output: str):
        with self._write_lock:
            if self._run_finalized or task.status in {"failed", "completed", "skipped", "rejected"}:
                raise RuntimeError("Late write blocked: orchestrator run is already finalized")
            if self.enforce_test_tamper_guard:
                from .diff_patcher import parse_search_replace_blocks, resolve_safe_workspace_path, _apply_single_block_to_content

                staged: dict[Path, str] = {}
                for block in parse_search_replace_blocks(output, default_file=task.target_files[0] if len(task.target_files) == 1 else ""):
                    path = resolve_safe_workspace_path(block.file_path, self.workspace_root, allowed_files=task.target_files)
                    original = staged.get(path)
                    if original is None:
                        original = path.read_text(encoding="utf-8") if path.is_file() else ""
                    candidate, _ = _apply_single_block_to_content(original, block.search_text, block.replace_text)
                    if candidate is None:
                        raise RuntimeError("Worker patch did not match the source")
                    staged[path] = candidate
                for path, candidate in staged.items():
                    self.guard.validate_candidate(path.relative_to(self.workspace_root).as_posix(), candidate)
            return apply_search_replace_text(
                output, workspace_root=self.workspace_root,
                default_file=task.target_files[0] if len(task.target_files) == 1 else "",
                allowed_files=task.target_files,
            )

    def _run(self) -> dict[str, Any]:
        """Execute all tasks in topological waves using the Stateless Canvas + SEARCH/REPLACE loop."""
        t_start = time.time()
        if self.enforce_test_tamper_guard and not self.guard.acceptance_frozen:
            self.guard.freeze_acceptance_suite()
            if self.guard.tamper_history:
                record = self.guard.tamper_history[-1]
                files = list(record.get("tampered_files") or [])
                self.guard_tampered_files.extend(item for item in files if item not in self.guard_tampered_files)
                self.tamper_violations_blocked += len(files)
                self.mistakes_caught += len(files)
        completed_ids: set[str] = set()
        failed_ids: set[str] = set()
        task_map = {t.id: t for t in self.plan.tasks}

        # Auto-lock contracts and test files if TamperGuard is enforced and not yet locked
        if self.enforce_test_tamper_guard:
            planned_targets = set()
            for t in self.plan.tasks:
                for tf in (t.target_files or []):
                    try:
                        planned_targets.add(Path(tf).as_posix())
                    except Exception:
                        pass

            planned_coder_targets = set()
            for t in self.plan.tasks:
                if t.role == "coder":
                    for tf in (t.target_files or []):
                        try:
                            planned_coder_targets.add(Path(tf).as_posix())
                        except Exception:
                            pass

            files_to_lock: list[str] = []
            files_to_lock.extend(rel for rel in self._test_baseline if rel not in planned_targets)
            for t in self.plan.tasks:
                if t.role != "coder":
                    for tf in (t.target_files or []):
                        rel = Path(tf).as_posix()
                        if rel not in planned_coder_targets:
                            fp = self.workspace_root / tf
                            if fp.is_file():
                                files_to_lock.append(tf)
            for pattern in ("contracts.py", "contracts/**/*.py", "test_*.py", "tests/**/*.py"):
                for p in self.workspace_root.glob(pattern):
                    if p.is_file() and not any(part in {".git", ".pacer", ".agent-workspace", ".venv", "venv", "node_modules", "__pycache__"} for part in p.parts):
                        try:
                            rel = p.relative_to(self.workspace_root).as_posix()
                            if rel not in planned_targets:
                                files_to_lock.append(rel)
                        except ValueError:
                            pass
            if files_to_lock:
                self.guard.lock_files(list(dict.fromkeys(files_to_lock)))

        # Partition into topological waves
        try:
            waves = topological_waves(self.plan.tasks)
        except Exception as exc:
            ordered_tasks, invalid_ids = self._topological_order()
            for inv_id in invalid_ids:
                failed_ids.add(inv_id)
                for t in self.plan.tasks:
                    if t.id == inv_id:
                        t.status = "failed"
                        self._notify(t, f"Failed before execution: invalid dependencies or cycle ({exc})")
            waves = [[t] for t in ordered_tasks]

        # Build the live timeline: per-task ETA, wave boundaries, heartbeat clock.
        annotate_plan_timeline(self.plan)
        self.pacer = ExecutionPacer(self.plan, waves, heartbeat_interval=self.heartbeat_interval)
        self._emit_plan_timeline()

        recovery_note = ""
        interruption_reason = ""
        if self.resume:
            chk = load_managed_checkpoint(self.workspace_root, expected_goal=self.plan.goal)
            if chk and is_checkpoint_resumable(chk):
                # Verify task count matches — different plan size means different plan
                if chk.plan_task_count and chk.plan_task_count != len(self.plan.tasks):
                    self._emit_timeline(None, f"[RESUME] 检查点任务数 ({chk.plan_task_count}) 与当前计划 ({len(self.plan.tasks)}) 不匹配，丢弃检查点")
                else:
                    interruption_reason = str(chk.interruption_reason or "").strip()
                    if not interruption_reason:
                        failed_state = next((
                            str(chk.task_states.get(task_id, {}).get("output") or "").strip()
                            for task_id in chk.failed_task_ids
                            if str(chk.task_states.get(task_id, {}).get("output") or "").strip()
                        ), "")
                        interruption_reason = failed_state or "The previous run ended before all planned steps completed."
                    resumed_ids: list[str] = []
                    rejected_ids: list[str] = []
                    for task_id in chk.completed_task_ids:
                        t = task_map.get(task_id)
                        if not t:
                            rejected_ids.append(f"{task_id}: task not in current plan")
                            continue
                        task_state = chk.task_states.get(task_id, {})
                        saved_files = task_state.get("patched_files", [])

                        files_to_verify = list(dict.fromkeys(list(saved_files) + (list(t.target_files) if t.role == "coder" else [])))
                        files_intact = not (t.role == "coder" and not files_to_verify)
                        for pf in files_to_verify:
                            fp = (self.workspace_root / pf).resolve()
                            saved_state = task_state.get("file_fingerprints", {}).get(pf, {})
                            try:
                                intact = (fp.is_relative_to(self.workspace_root.resolve()) and fp.is_file()
                                          and bool(saved_state.get("sha256"))
                                          and hashlib.sha256(fp.read_bytes()).hexdigest() == saved_state["sha256"])
                            except OSError:
                                intact = False
                            if not intact:
                                files_intact = False
                                rejected_ids.append(f"{task_id}: file {pf} missing, changed, or lacking a saved SHA-256")
                                break

                        if files_intact:
                            completed_ids.add(task_id)
                            t.status = "completed"
                            if saved_files:
                                t.patched_files = saved_files
                            if task_state.get("output"):
                                t.output = task_state["output"]
                            resumed_ids.append(task_id)
                        else:
                            logger.warning(f"[RESUME] Task {task_id} failed fingerprint verification, will re-execute")

                    completed_notes: list[str] = []
                    file_hashes: dict[str, str] = {}
                    for task_id in resumed_ids:
                        state = chk.task_states.get(task_id, {})
                        completed_notes.append(f"- {task_id}: {state.get('status', 'completed')}")
                        for rel_path, fingerprint in (state.get("file_fingerprints") or {}).items():
                            digest = str((fingerprint or {}).get("sha256") or "")
                            if digest:
                                file_hashes[str(rel_path)] = digest
                    hash_lines = [f"- {path}: sha256={digest}" for path, digest in sorted(file_hashes.items())]
                    recovery_note = (
                        "[Recovery Note]\n"
                        "Before editing, inspect the current workspace files and `git log` to confirm the saved work. "
                        "Keep verified completed steps intact and continue only unfinished steps.\n"
                        "Completed tasks:\n"
                        + ("\n".join(completed_notes) or "- None")
                        + "\nSaved file SHA-256 fingerprints:\n"
                        + ("\n".join(hash_lines) or "- None")
                        + f"\nPrevious interruption reason: {interruption_reason}"
                    )
                    if resumed_ids:
                        self._emit_timeline(None, f"[RESUME] 恢复 {len(resumed_ids)} 个已验证子任务: {resumed_ids}")
                    if rejected_ids:
                        self._emit_timeline(None, f"[RESUME] 拒绝 {len(rejected_ids)} 个指纹不匹配的子任务: {rejected_ids}")
                    if recovery_note:
                        for pending_task in self.plan.tasks:
                            if pending_task.role == "coder" and pending_task.id not in completed_ids:
                                if "[Recovery Note]" not in pending_task.instructions:
                                    pending_task.instructions = f"{pending_task.instructions}\n\n{recovery_note}"
                        self._emit_timeline(None, "[RESUME] 已向待继续写码任务注入 Recovery Note 与文件指纹")

        def _build_checkpoint_payload(wave_index: int, status: str = "in_progress") -> dict[str, Any]:
            import hashlib as _hashlib
            all_curr_patched: list[str] = []
            for t in self.plan.tasks:
                for pf in t.patched_files:
                    if pf not in all_curr_patched:
                        all_curr_patched.append(pf)
            return {
                "checkpoint_id": getattr(self, "_checkpoint_id", f"chk_{int(time.time())}"),
                "goal": self.plan.goal,
                "workspace_root": str(self.workspace_root),
                "plan_goal_hash": _hashlib.sha256(self.plan.goal.encode("utf-8")).hexdigest(),
                "plan_task_count": len(self.plan.tasks),
                "status": status,
                "interruption_reason": interruption_reason,
                "current_wave_index": wave_index,
                "total_waves": len(waves),
                "completed_task_ids": list(completed_ids),
                "failed_task_ids": list(failed_ids),
                "pending_task_ids": [t.id for t in self.plan.tasks if t.id not in completed_ids and t.id not in failed_ids],
                "patched_files": all_curr_patched,
                "task_states": {
                    t.id: {
                        "status": t.status,
                        "backend": t.backend,
                        "patched_files": t.patched_files,
                        "output": t.output or t.error or "",
                        "file_fingerprints": _capture_file_fingerprints(self.workspace_root, list(dict.fromkeys(t.patched_files + (t.target_files if t.role == "coder" else [])))),
                    }
                    for t in self.plan.tasks
                },
            }

        # Persist the plan before the first dispatch so a restart can distinguish stored work
        # from work that was never accepted by the orchestrator.
        save_managed_checkpoint(self.workspace_root, _build_checkpoint_payload(0, "in_progress"))

        def _execute_wave_task(task: SubagentTask, wave_idx: int) -> tuple[SubagentTask, bool]:
            nonlocal interruption_reason
            with self._pacer_lock:
                self.pacer.start_task(task)
                task.started_at = time.time()
            try:
                success = self._run_task_with_heartbeat(task)
            except Exception as exc:
                success = False
                task.error = f"Unhandled task execution error: {type(exc).__name__}: {exc}"
                logger.exception("Subtask %s crashed", task.id)
            task.finished_at = time.time()
            if success:
                task.status = "completed"
            elif task.status not in ("rejected",):
                task.status = "failed"
            with self._checkpoint_lock:
                with self._pacer_lock:
                    self.pacer.finish_task(task, "completed" if success else "failed")
                if success:
                    completed_ids.add(task.id)
                    self.canvas.update_scratchpad(completed_step=f"{task.id}:{task.title}")
                else:
                    failed_ids.add(task.id)
                    interruption_reason = f"{task.id} failed: {task.error or 'task verification failed'}"
                save_managed_checkpoint(
                    self.workspace_root,
                    _build_checkpoint_payload(wave_idx, "in_progress"),
                )
            self._emit_timeline(task, self._task_progress_line(task, success))
            return task, success

        tamper_interrupted = bool(self.guard_tampered_files)
        if tamper_interrupted:
            interruption_reason = "TamperGuard detected protected acceptance changes before worker dispatch"
        for wave_idx, wave in enumerate(waves):
            if tamper_interrupted:
                break
            if not wave:
                continue
            self._emit_timeline(wave[0], self.pacer.wave_header(wave_idx))
            ready_tasks: list[SubagentTask] = []
            for task in wave:
                if task.id in completed_ids:
                    self.pacer.mark_skipped(task)
                    self._emit_timeline(task, f"[RESUME] 任务 {task.id} 前次已通过物理考题验收，直接跳过")
                    continue
                if any(dep in failed_ids for dep in task.dependencies):
                    task.status = "skipped"
                    failed_ids.add(task.id)
                    self.pacer.mark_skipped(task)
                    self._notify(task, f"Skipped because dependency failed: {task.dependencies}")
                    continue

                unresolved = [dep for dep in task.dependencies if dep not in completed_ids]
                if unresolved:
                    task.status = "skipped"
                    failed_ids.add(task.id)
                    self.pacer.mark_skipped(task)
                    self._notify(task, f"Skipped: unmet dependencies {unresolved}")
                    continue

                self._inject_dependency_context(task, task_map)
                ready_tasks.append(task)

            for batch in _conflict_free_wave_batches(ready_tasks):
                if len(batch) == 1:
                    _execute_wave_task(batch[0], wave_idx)
                    continue
                max_workers = min(8, len(batch))
                with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"pacer-wave-{wave_idx}") as pool:
                    futures = [pool.submit(_execute_wave_task, task, wave_idx) for task in batch]
                    for future in as_completed(futures):
                        future.result()

            # Inter-wave TamperGuard integrity enforcement
            if self.enforce_test_tamper_guard and self.guard.protected_files:
                is_intact, alert, tampered = self.guard.verify_and_enforce()
                if not is_intact:
                    self.guard_tampered_files.extend(tampered)
                    self.tamper_violations_blocked += len(tampered)
                    self.mistakes_caught += len(tampered)
                    tamper_interrupted = True
                    interruption_reason = f"TamperGuard interrupted wave {wave_idx}: {alert}"
                    self._notify(wave[0], f"Tamper violation detected: {alert}")
                    for remaining_wave in waves[wave_idx + 1:]:
                        for remaining_task in remaining_wave:
                            remaining_task.status = "skipped"
                            failed_ids.add(remaining_task.id)
                    break

            # Persist checkpoint state across waves
            save_managed_checkpoint(self.workspace_root, _build_checkpoint_payload(wave_idx, "in_progress"))

            if any(t.status == "failed" for t in wave):
                for remaining_wave in waves[wave_idx + 1:]:
                    for remaining_task in remaining_wave:
                        remaining_task.status = "skipped"
                        failed_ids.add(remaining_task.id)
                break

        with self._write_lock:
            self._run_finalized = True
        total_elapsed = time.time() - t_start
        all_passed = (len(failed_ids) == 0 and len(completed_ids) == len(self.plan.tasks) and len(self.guard_tampered_files) == 0)
        self.canvas.update_goal(
            self.plan.goal,
            status="completed" if all_passed else "failed",
        )

        all_patched: list[str] = []
        for t in self.plan.tasks:
            for pf in t.patched_files:
                if pf not in all_patched:
                    all_patched.append(pf)
        acceptance_test_files = list(dict.fromkeys(
            str(task.acceptance_test.get("path"))
            for task in self.plan.tasks
            if isinstance(task.acceptance_test, dict) and task.acceptance_test.get("path")
        ))

        primary_b = self.plan.tasks[1].backend if len(self.plan.tasks) > 1 else (self.plan.tasks[0].backend if self.plan.tasks else "unknown")
        # Report honestly: only claim an effective backend when a dispatch
        # actually succeeded. A failed run must not imply some backend served it.
        effective_b = self._effective_backend or (primary_b if all_passed else "none")

        last_verif_cmd = ""
        last_verif_verdict = ""
        last_verif_out = ""
        for t in reversed(self.plan.tasks):
            if t.verification_command:
                last_verif_cmd = t.verification_command
                last_verif_verdict = "pass" if t.status == "completed" else ("fail" if t.status == "failed" else "skipped")
                last_verif_out = t.output or t.error or ""
                break

        # --- Real usage accounting (no invented baselines) ---
        # Every backend dispatch is recorded in _prompt_dispatches with the
        # estimated size of the prompt that was actually sent. Summing them
        # gives the true prompt volume of this run.
        measured_prompt_tokens = 0
        for dispatch in self._prompt_dispatches:
            try:
                measured_prompt_tokens += int(dispatch.get("estimated_prompt_tokens") or 0)
            except (TypeError, ValueError):
                continue
        # Tasks that were prepared but never dispatched still consumed a
        # planned prompt; count them only if no dispatch happened at all.
        if not self._prompt_dispatches:
            for t in self.plan.tasks:
                measured_prompt_tokens += int(getattr(t, "prompt_tokens", 0) or 0)

        measured_model_calls = len(self._prompt_dispatches)
        usage_measurement = "measured" if (measured_prompt_tokens or measured_model_calls) else "unknown"

        trace_payload = dict(
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
            prompt_tokens=measured_prompt_tokens,
            model_calls=measured_model_calls,
            usage_measurement=usage_measurement,
            metadata={
                "context": {
                    "measurement": "estimated_user_prompt_only",
                    "host_history_measured": False,
                    "dispatch_attempts": self._prompt_dispatches,
                },
            },
        )
        if self.defer_trace_recording:
            self.deferred_trace_payload = trace_payload
        else:
            record_pacer_trace(**trace_payload)

        # Release physical OS locks before returning so workspace files remain writable
        if self.enforce_test_tamper_guard:
            try:
                self.guard.unlock_all()
            except Exception:
                pass

        self.pacer.complete()
        timeline = self.pacer.summary()
        self._emit_timeline(
            None,
            f"[Pacer ETA] 实际耗时 {format_duration(timeline['actual_seconds'])} "
            f"· 预估 {format_duration(timeline['planned_eta_seconds'])} "
            f"· 偏差 {timeline['eta_error_pct']:+.1f}% "
            f"· 校准系数 {timeline['calibration']:.2f}",
        )

        # Managed Checkpoint & Dogfood Verification State Finalization
        dogfood_reports = [t.dogfood_evidence for t in self.plan.tasks if t.dogfood_evidence]
        dogfood_data: dict[str, Any] = dict(dogfood_reports[-1]) if dogfood_reports else {}
        if dogfood_reports:
            dogfood_data["task_reports"] = dogfood_reports
        if all_passed:
            clear_managed_checkpoint(self.workspace_root)
        else:
            if not interruption_reason:
                failed = next((task for task in self.plan.tasks if task.error), None)
                interruption_reason = (
                    f"{failed.id} failed: {failed.error}"
                    if failed is not None
                    else "The run stopped before all planned tasks reached verified completion."
                )
            save_managed_checkpoint(self.workspace_root, _build_checkpoint_payload(len(waves), "failed"))

        profile = self.context_profile_manager.load_profile() or self.context_profile
        profile.active_wish = self.plan.goal
        failed_tasks = [t for t in self.plan.tasks if t.error]
        profile.last_failure = ((failed_tasks[-1].error or "")[:200] if failed_tasks else "")
        profile.plan_items = [f"[{t.status}] {t.id} {t.role}: {t.title}" for t in self.plan.tasks]
        profile.current_contract = "; ".join(
            f"{t.id} -> {', '.join(t.target_files)}"
            for t in self.plan.tasks
            if t.role == "coder" and t.target_files
        )[:500]
        self.context_profile_manager.save_profile(profile)

        result_summary = {
            "success": all_passed,
            "goal": self.plan.goal,
            "complexity": getattr(self.plan, "complexity", "standard"),
            "routing_path": self.routing_decision.path.value,
            "mistakes_caught": self.mistakes_caught,
            "self_heals_succeeded": self.self_heals_succeeded,
            "slop_detected": self.slop_detected,
            "tamper_violations_blocked": self.tamper_violations_blocked,
            "false_greens_caught": self.false_greens_caught,
            "witness_certificates": self.witness_certificates,
            "dogfood_evidence": dogfood_data,
            "acceptance_test_files": acceptance_test_files,
            "total_tasks": len(self.plan.tasks),
            "completed_tasks": len(completed_ids),
            "failed_tasks": len(failed_ids),
            "elapsed_seconds": total_elapsed,
            "effective_backend": effective_b,
            "api_node_attempts": self._api_node_attempts,
            "verification_receipts": self._verification_receipts,
            "failover_path": self._failover_path,
            "planned_eta_seconds": timeline["planned_eta_seconds"],
            "eta_error_pct": timeline["eta_error_pct"],
            "wave_count": timeline["wave_count"],
            "waves": timeline["waves"],
            "heartbeats": timeline["heartbeats"],
            "timeline": timeline,
            "canvas": self.canvas.load().to_dict(),
            "plan": self.plan.to_dict(),
        }
        self._notify_task_completion(result_summary)
        return result_summary

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
        # Tests and embedding callers may inject an instance-level verifier. Its
        # result is already the authority in that mode, so do not invoke a real
        # workspace baseline command behind the injected implementation.
        probe_baseline = (
            self.probe_base_discrimination
            and "_run_verification_command" not in self.__dict__
        )
        if probe_baseline and task.verification_command:
            try:
                base_probe = probe_workspace_command(
                    command=task.verification_command,
                    workspace_root=self.workspace_root,
                    timeout_seconds=120.0,
                )
                missing_targets = [
                    p for p in task.target_files
                    if (self.workspace_root / p).resolve().is_relative_to(self.workspace_root.resolve())
                    and not (self.workspace_root / p).is_file()
                ]
                if task.role == "coder" and missing_targets and base_probe.get("status") == "gate_broken" and base_probe.get("exit_code") not in (None, 0):
                    base_probe = {**base_probe, "status": "failed_on_base", "failed_on_base": True,
                                  "reason": "target_or_test_missing_before_coding", "detail": ", ".join(missing_targets)}
            except Exception:
                base_probe = None

        base_instruction = task.instructions
        if task.role == "coder":
            base_instruction = (
                base_instruction
                + "\n\n[SCOPE BOUNDARY] Implement only behaviors stated in the task contract. "
                + "Do not invent unrelated input cutoffs, rejection rules, or other business constraints."
            )
        green_incremental = bool(
            task.role == "coder"
            and task.verification_command
            and self.green_incremental
            and not self.require_red_baseline
            and base_probe
            and base_probe.get("status") == "passed_on_base"
            and task.target_files
            and all((self.workspace_root / p).is_file() for p in task.target_files)
            and any(not is_test_path(p) for p in task.target_files)
        )
        if green_incremental:
            task._green_incremental = True
            base_instruction += (
                "\n\n[GREEN_INCREMENTAL MODE] The existing baseline is green; this is an authorized incremental edit, "
                "so continue coding instead of rejecting it. Implement the requested delta, make an actual change "
                "to a production target, update or add the focused test, and rerun the exact physical pytest command. "
                "If the requested behavior already exists, make the smallest meaningful source refactor that makes "
                "the requested rule explicit and tested; do not use a comment-only or unrelated change."
            )
            fingerprint_paths = list(dict.fromkeys(str(p).replace("\\", "/") for p in task.target_files))
            for raw_test in re.findall(
                r"(?i)(?:tests?[/\\][A-Za-z0-9_.-]+\.py|[A-Za-z0-9_.-]+_test\.py|test_[A-Za-z0-9_.-]+\.py)",
                str(task.verification_command or ""),
            ):
                normalized = raw_test.replace("\\", "/")
                candidate_path = (self.workspace_root / normalized).resolve()
                if candidate_path.is_relative_to(self.workspace_root.resolve()) and candidate_path.is_file():
                    fingerprint_paths.append(normalized)
            fingerprint_paths = list(dict.fromkeys(fingerprint_paths))
            task._green_incremental_test_paths = [p for p in fingerprint_paths if is_test_path(p)]
            task._green_baseline_fingerprints = {
                rel: hashlib.sha256((self.workspace_root / rel).read_bytes()).hexdigest()
                for rel in fingerprint_paths if (self.workspace_root / rel).is_file()
            }
            task._green_baseline_passed_count = base_probe.get("passed_count")
            task._green_baseline_output = str(base_probe.get("output_tail") or "")
        failing_file = ""
        failing_line: int | None = None

        while task.retries <= task.max_retries:
            # Keep Canvas projection snapshots coherent while independent tasks prepare prompts.
            with self._canvas_prompt_lock:
                self._populate_canvas_ast_focus(task, failing_file=failing_file, failing_line=failing_line)

            clean_snapshot = capture_workspace_snapshot(self.workspace_root, list(task.target_files or []))

            def revert_attempt(snapshot: dict[str, str | None] = clean_snapshot) -> None:
                restore_workspace_snapshot(self.workspace_root, snapshot, list(task.patched_files or []))
                task.patched_files = []

            if (
                probe_baseline
                and task.role == "coder"
                and task.verification_command
                and self.require_red_baseline
                and not green_incremental
                and not baseline_is_red(base_probe)
            ):
                task.status = "rejected"
                probe_status = str((base_probe or {}).get("status") or "")
                probe_reason = str((base_probe or {}).get("reason") or "")
                if probe_status in {"gate_broken", "unknown"}:
                    task.error = (
                        f"baseline exam could not be proven ({probe_status}"
                        + (f": {probe_reason}" if probe_reason else "")
                        + "); the verification command must load and execute before coding"
                    )
                else:
                    task.error = "baseline exam did not fail"
                task.elapsed_seconds = time.time() - t0
                self._notify(task, task.error)
                return False

            # 2. Assemble stateless single-turn prompt from Canvas (<= 1,500 tokens)
            with self._canvas_prompt_lock:
                if self.use_stateless_canvas:
                    self.context_profile = self.context_profile_manager.get_or_create_profile(
                        invariants=self.context_profile.active_invariants,
                        focus_files=task.target_files,
                    )
                    stateless_prompt = self.canvas.build_stateless_prompt(
                        base_instruction,
                        require_search_replace=(task.role == "coder"),
                        target_files=task.target_files,
                        acceptance_exam=task.acceptance_exam,
                        context_profile=self.context_profile.to_compact_markdown(max_tokens=150),
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
            if error:
                revert_attempt()
                task.status = "failed"
                task.elapsed_seconds = time.time() - t0
                return False

            # 4. If coder returned SEARCH/REPLACE blocks (or fenced file blocks), apply patch to disk
            if task.role == "coder" and run_output:
                default_target = task.target_files[0] if len(task.target_files) == 1 else ""
                sr_blocks = parse_search_replace_blocks(run_output, default_file=default_target)
                if sr_blocks:
                    try:
                        patch_res = self._apply_task_patch(task, run_output)
                    except TamperFatalError as exc:
                        revert_attempt()
                        self.false_greens_caught += 1
                        task.status = "failed"
                        task.error = str(exc)
                        task.elapsed_seconds = time.time() - t0
                        task.history.append({"category": "tamper_rejection", "error": str(exc)})
                        return False
                    if patch_res.failed_count > 0 or not patch_res.success:
                        task.patched_files = []
                        setattr(task, "_patch_batch_error", "; ".join(patch_res.errors))
                    else:
                        task.patched_files = list(patch_res.modified_files)
                    for mod_file in task.patched_files:
                        self.canvas.upsert_focus_file(mod_file)
            # A writer may edit files directly. Count a real content change, whichever backend did it.
            if task.role == "coder" and not task.patched_files:
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

            # 4.5 Anti-Slop AST Gate: Detect AI laziness, fake tests, and swallowed exceptions
            # Only gate code-generating roles (coder, architect) or tasks that actually produced code diffs
            if self.enforce_anti_slop and task.patched_files:
                files_to_check = list(dict.fromkeys(task.patched_files))
                slop_findings = detect_slop_in_files(files_to_check, workspace_root=self.workspace_root)
                slop_errors = [f for f in slop_findings if f.severity == "error"]
                if slop_errors:
                    self.mistakes_caught += len(slop_errors)
                    for f in slop_errors:
                        self.slop_detected.append(f.to_dict())
                    task.retries += 1
                    revert_attempt()
                    critique = format_slop_critique(slop_errors)
                    task.history.append({
                        "attempt": task.retries,
                        "backend": task.backend,
                        "category": "anti_slop_rejection",
                        "error": critique,
                    })
                    self.canvas.update_scratchpad(
                        latest_error=critique[:600],
                        attempt_count=task.retries,
                        last_failed_hypothesis=f"Attempt #{task.retries} rejected by Anti-Slop AST Gate: {slop_errors[0].category}",
                    )
                    if task.retries <= task.max_retries:
                        self._notify(task, f"Anti-slop AST gate rejected code ({len(slop_errors)} slop error(s)). Retrying (attempt {task.retries}/{task.max_retries})...")
                        task.instructions = f"{base_instruction}\n\n{critique}"
                        continue
                    else:
                        task.status = "failed"
                        task.error = critique
                        self._notify(task, f"Subtask failed after {task.retries} attempts (persistent AI slop).")
                        task.elapsed_seconds = time.time() - t0
                        return False

            # 4.8 Acceptance Exam (验收考题) Gate: Zero-trust physical validation of AST, contracts & assertions
            if task.acceptance_exam and (task.patched_files or task.role in ("coder", "verifier")):
                exam_passed, exam_results, exam_critique = verify_acceptance_exam(task, self.workspace_root)
                task.exam_results = exam_results
                if not exam_passed:
                    if task.role == "coder":
                        self._record_verification_strike(task.backend)
                    self.mistakes_caught += 1
                    task.retries += 1
                    revert_attempt()
                    task.history.append({
                        "attempt": task.retries,
                        "backend": task.backend,
                        "category": "acceptance_exam_rejection",
                        "error": exam_critique,
                        "exam_results": exam_results,
                    })
                    self.canvas.update_scratchpad(
                        latest_error=exam_critique[:600],
                        attempt_count=task.retries,
                        last_failed_hypothesis=f"Attempt #{task.retries} failed physical acceptance exam ({task.backend}).",
                    )
                    if task.retries <= task.max_retries:
                        old_backend = task.backend
                        task.backend = self._next_cascade_backend(old_backend, acceptance_exam=True)
                        task.status = "escalated"
                        self._notify(
                            task,
                            f"Acceptance exam failed. Switching from {old_backend} to {task.backend} (attempt {task.retries}/{task.max_retries})...",
                        )
                        task.instructions = f"{base_instruction}\n\n{exam_critique}"
                        continue
                    else:
                        task.status = "failed"
                        task.error = exam_critique
                        self._notify(task, f"Subtask failed after {task.retries} attempts (acceptance exam failure).")
                        task.elapsed_seconds = time.time() - t0
                        return False

            # 5. Automated verification & Canvas feedback loop
            if task.verification_command or task.verification_argv:
                self._notify(task, f"Running verification: {task.verification_command}")
                self._verification_context.task_id = task.id
                try:
                    verification_request = task.verification_argv or task.verification_command
                    verif_ok, verif_out = self._run_verification_command(verification_request)
                finally:
                    try:
                        del self._verification_context.task_id
                    except AttributeError:
                        pass
                if not verif_ok:
                    if task.role == "coder":
                        self._record_verification_strike(task.backend)
                    self._last_passed_verification = None
                    task.retries += 1
                    revert_attempt()
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
                                self._record_verification_strike(upstream_coder.backend)
                                upstream_coder.retries += 1
                                old_upstream = upstream_coder.backend
                                upstream_coder.backend = self._next_cascade_backend(old_upstream)
                                upstream_coder.status = "escalated"
                                self._execute_task_with_retry(upstream_coder)
                                continue
                            task.status = "failed"
                            task.error = verif_out[:500]
                            self._notify(task, "Verifier failed and will not edit code.")
                            task.elapsed_seconds = time.time() - t0
                            return False

                        signature = " ".join(str(verif_out or "").split())[:180]
                        seen = self._failure_signatures.get(signature, 0) + 1
                        self._failure_signatures[signature] = seen
                        if seen >= 2:
                            revert_attempt()
                            task.status = "failed"
                            task.error = signature
                            self._notify(task, "同一失败出现第二次，停止写码。")
                            task.elapsed_seconds = time.time() - t0
                            return False

                        old_backend = task.backend
                        task.backend = self._next_cascade_backend(old_backend)
                        task.status = "escalated"
                        self._notify(
                            task,
                            f"Verification failed. Switching from {old_backend} to {task.backend} (attempt {task.retries}/{task.max_retries}): {verif_out[:120]}...",
                        )
                        diagnosis = self._diagnose_with_gemini4(task, verif_out)
                        if diagnosis:
                            base_instruction += "\n[Gemini 4 诊断]\n" + diagnosis

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

                if green_incremental:
                    delta_ok, delta_detail = self._green_incremental_delta(task, verif_out)
                    if not delta_ok:
                        delta_error = "green_incremental delta gate failed: " + delta_detail
                        self._last_passed_verification = None
                        revert_attempt()
                        self.mistakes_caught += 1
                        task.retries += 1
                        task.history.append({
                            "attempt": task.retries,
                            "backend": task.backend,
                            "category": "green_incremental_delta_rejection",
                            "error": delta_error,
                        })
                        self.canvas.update_scratchpad(
                            latest_error=delta_error,
                            attempt_count=task.retries,
                            last_failed_hypothesis="The green incremental attempt did not produce a verified source and test delta.",
                        )
                        if task.retries <= task.max_retries:
                            old_backend = task.backend
                            task.backend = self._next_cascade_backend(old_backend)
                            task.status = "escalated"
                            task.error = delta_error
                            task.instructions = (
                                f"{base_instruction}\n\n[CRITICAL: Delta Gate rejected the previous attempt: {delta_detail}]\n"
                                "Change a production target file and update the focused test (or increase passing-test count), "
                                "then rerun the physical pytest command."
                            )
                            self._notify(task, f"Delta Gate rejected the no-op; switching from {old_backend} to {task.backend} (attempt {task.retries}/{task.max_retries}).")
                            continue
                        task.status = "failed"
                        task.error = delta_error
                        self._notify(task, task.error)
                        task.elapsed_seconds = time.time() - t0
                        return False
                else:
                    # Verification command passed. Guard against false greens where search-replace blocks failed to apply or zero diff produced
                    had_sr_blocks = bool(task.role == "coder" and run_output and parse_search_replace_blocks(run_output))
                    needs_mod = bool(task.role == "coder" and (task.target_files or had_sr_blocks))
                    if needs_mod and not task.patched_files:
                        self._last_passed_verification = None
                        task.retries += 1
                        revert_attempt()
                        self.mistakes_caught += 1
                        patch_err = getattr(task, "_patch_batch_error", "")
                        err_msg = (
                            f"SEARCH/REPLACE blocks from {task.backend} failed to match or apply to target files: {patch_err}"
                            if (had_sr_blocks and patch_err)
                            else f"Zero-Diff Defense: Coder task on {task.backend} did not modify any files on disk. Pre-existing passing tests cannot masquerade as completed work."
                        )
                        task.history.append({
                            "attempt": task.retries,
                            "backend": task.backend,
                            "category": "zero_diff_rejection",
                            "error": err_msg,
                        })
                        self.canvas.update_scratchpad(
                            latest_error=err_msg,
                            attempt_count=task.retries,
                            last_failed_hypothesis=f"Attempt #{task.retries} on {task.backend} produced no applied changes.",
                        )
                        if task.retries <= task.max_retries:
                            old_backend = task.backend
                            task.backend = self._next_cascade_backend(old_backend)
                            task.status = "escalated"
                            self._notify(task, f"Diff patch failed to apply ({patch_err or 'zero diff'}). Switching from {old_backend} to {task.backend} (attempt {task.retries}/{task.max_retries})...")
                            task.instructions = (
                                f"{base_instruction}\n\n"
                                f"[CRITICAL: SEARCH/REPLACE blocks failed to apply or produced no modifications on disk: {patch_err or 'zero diff'}]\n"
                                "Ensure SEARCH blocks match the exact existing file content or leave SEARCH empty if creating new files."
                            )
                            continue
                        else:
                            task.status = "failed"
                            task.error = err_msg
                            self._notify(task, f"Subtask failed after {task.retries} attempts (patch apply failure / zero diff).")
                            task.elapsed_seconds = time.time() - t0
                            return False

                    # 5.5 Witness-Based Verification Gate: Kill False Greens & Trivial Assertions
                    if self.enforce_witness_verifier:
                        witness_rejected = False
                        witness_critique = ""

                        # (a) Check test substance on any newly created or patched test files
                        patched_tests = [
                            f for f in task.patched_files
                            if f.endswith(".py") and (Path(f).name.startswith("test_") or "tests" in Path(f).parts)
                        ]
                        for tf in patched_tests:
                            tf_path = self.workspace_root / tf
                            if tf_path.is_file():
                                try:
                                    t_src = tf_path.read_text(encoding="utf-8")
                                    ok_sub, msg_sub, viols = self.witness_verifier.verify_assertion_substance(t_src)
                                    if not ok_sub:
                                        witness_rejected = True
                                        self.false_greens_caught += 1
                                        self.mistakes_caught += len(viols) or 1
                                        for v in viols:
                                            self.slop_detected.append({
                                                "file": tf,
                                                "category": v.get("type", "tautological_assertion"),
                                                "line": v.get("line", 1),
                                                "reason": v.get("message", msg_sub),
                                            })
                                        witness_critique = (
                                            f"[CRITICAL: Witness Gate Rejected - TRIVIAL / FAKE TEST DETECTED in {tf}]\n"
                                            f"{msg_sub}\n"
                                            "Tests must contain non-trivial dynamic assertions validating real implementation logic. "
                                            "Do NOT use 'assert True', 'assert 1 == 1', or empty test functions."
                                        )
                                        break
                                except Exception:
                                    pass

                        if witness_rejected:
                            self._last_passed_verification = None
                            task.retries += 1
                            revert_attempt()
                            task.history.append({
                                "attempt": task.retries,
                                "backend": task.backend,
                                "category": "witness_false_green_rejection",
                                "error": witness_critique,
                            })
                            self.canvas.update_scratchpad(
                                latest_error=witness_critique[:600],
                                attempt_count=task.retries,
                                last_failed_hypothesis=f"Attempt #{task.retries} on {task.backend} rejected by Witness Verification Gate (False Green).",
                            )
                            if task.retries <= task.max_retries:
                                old_backend = task.backend
                                task.backend = self._next_cascade_backend(old_backend)
                                task.status = "escalated"
                                self._notify(task, f"False-green detected. Switching from {old_backend} to {task.backend} (attempt {task.retries}/{task.max_retries})...")
                                task.instructions = f"{base_instruction}\n\n{witness_critique}"
                                continue
                            else:
                                task.status = "failed"
                                task.error = witness_critique
                                self._notify(task, f"Subtask failed after {task.retries} attempts (persistent false green).")
                                task.elapsed_seconds = time.time() - t0
                                return False

                    self.canvas.update_scratchpad(latest_error="", failing_line="")
                    updated_focus_fp = self._compute_focus_fingerprint()
                    if probe_baseline and task.role == "coder":
                        disc = classify_acceptance(
                            command_result={"command": task.verification_command, "verdict": "pass"},
                            base_probe=base_probe,
                        )
                        task.acceptance_tier = str(disc.get("tier") or "")
                        if disc.get("reason_code") != "acceptance_gate_discriminating":
                            self._last_passed_verification = None
                            task.retries += 1
                            revert_attempt()
                            self.mistakes_caught += 1
                            err_msg = "rejected (no red-to-green discrimination)"
                            task.history.append({
                                "attempt": task.retries,
                                "backend": task.backend,
                                "category": "red_to_green_rejection",
                                "error": err_msg,
                            })
                            if task.retries <= task.max_retries:
                                old_backend = task.backend
                                task.backend = self._next_cascade_backend(old_backend)
                                task.instructions = f"{base_instruction}\n\n{err_msg}"
                                self._notify(task, err_msg)
                                continue
                            task.status = "failed"
                            task.error = err_msg
                            self._notify(task, err_msg)
                            task.elapsed_seconds = time.time() - t0
                            return False
                    else:
                        # A verifier does not implement a change, so its passing
                        # gate must not be rejected for lacking red-to-green delta.
                        # Other non-baseline runs retain only regression-clear status.
                        task.acceptance_tier = "regression_clear"
                    self._last_passed_verification = (task.verification_command, updated_focus_fp)
            else:
                # No verification command on this subtask: require applied changes for coder subtasks
                has_target_files = bool(task.target_files or (task.role == "coder" and run_output and parse_search_replace_blocks(run_output)))
                if task.role == "coder" and has_target_files and not task.patched_files:
                    self._last_passed_verification = None
                    task.retries += 1
                    revert_attempt()
                    self.mistakes_caught += 1
                    err_msg = f"Coder task on {task.backend} did not modify any files (no valid changes applied to disk)."
                    task.history.append({
                        "attempt": task.retries,
                        "backend": task.backend,
                        "category": "zero_diff_rejection",
                        "error": err_msg,
                    })
                    self.canvas.update_scratchpad(
                        latest_error=err_msg,
                        attempt_count=task.retries,
                        last_failed_hypothesis=f"Attempt #{task.retries} on {task.backend} produced no applied changes.",
                    )
                    if task.retries <= task.max_retries:
                        old_backend = task.backend
                        task.backend = self._next_cascade_backend(old_backend)
                        task.status = "escalated"
                        self._notify(
                            task,
                            f"Coder produced no file modifications. Switching from {old_backend} to {task.backend} (attempt {task.retries}/{task.max_retries})...",
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
                revert_attempt()
                task.status = "failed"
                task.elapsed_seconds = time.time() - t0
                return False

            if (
                task.role == "coder"
                and task.verification_command
                and any("visual_agent" in path for path in task.patched_files)
            ):
                from .dogfood_local import run_local_dogfood
                report = run_local_dogfood(
                    self.workspace_root,
                    patched_files=[p for p in task.patched_files if p.replace('\\', '/').startswith('src/visual_agent/')],
                    smoke_test_command=task.verification_command if _is_safe_smoke_command(task.verification_command) else None,
                )
                task.dogfood_evidence = {**report, "task_id": task.id, "backend": task.backend,
                                         "patched_files": list(task.patched_files)}
                if not report.get("installed_artifact_verified"):
                    revert_attempt()
                    task.status = "failed"
                    mismatched = report.get("patched_files_mismatched", [])
                    detail = "; ".join(mismatched) if mismatched else report.get("reason", "dogfood_not_verified")
                    task.error = f"dogfood_not_verified: {detail}"
                    self._notify(task, task.error)
                    task.elapsed_seconds = time.time() - t0
                    return False
            if task.retries > 0:
                self.self_heals_succeeded += 1
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
            task.acceptance_tier = "local_static_only"
            ast_summaries = self._populate_canvas_ast_focus(task)
            summary_text = (
                "\n\n".join(ast_summaries)
                if ast_summaries
                else f"Local AST index completed for '{task.title}'."
            )
            return summary_text, None

        if backend == "local":
            return self._local_static_scan(task)

        from .provider_runtime import resolve_coder_routes
        cascade = os.environ.get("PACER_CODER_CASCADE") if task.role == "coder" else backend
        routes = resolve_coder_routes(preferred=backend, cascade=cascade, cooldowns=self._backend_cooldowns)
        candidates = [entry.backend_name for entry in routes]
        available_routes = [entry for entry in routes if not entry.cooldown_seconds and self._is_backend_live(entry.backend_name)]
        if not available_routes:
            return "", "No configured writer is available or all candidates are cooling down"
        available_candidates = [entry.backend_name for entry in available_routes]
        cooled = [entry.backend_name for entry in routes if entry.cooldown_seconds]
        if cooled:
            self._notify(task, "Skipping cooled-down backends: " + ", ".join(cooled))

        last_error: str | None = None
        for idx, candidate_backend in enumerate(available_candidates):
            self._prompt_dispatches.append({
                "task_id": task.id,
                "backend": candidate_backend,
                "retry": task.retries,
                "estimated_prompt_tokens": estimate_tokens(effective_prompt),
            })
            task._route_timeout_seconds = available_routes[idx].timeout_seconds
            out, err = self._invoke_single_backend(candidate_backend, task, effective_prompt)
            if out and not err:
                self._effective_backend = candidate_backend
                task.backend = candidate_backend
                # A success clears any prior cooldown for this backend.
                self._backend_cooldowns.pop(candidate_backend, None)
                return out, None

            last_error = err or "Empty model response"
            if is_infra_or_quota_error(err):
                # Open the circuit: demote this backend for a while so the next
                # task does not pay the same failure latency again.
                self._note_backend_failure(candidate_backend, err)
            if is_infra_or_quota_error(err) and idx + 1 < len(available_candidates):
                next_backend = available_candidates[idx + 1]
                hop = f"{candidate_backend} -> {next_backend} ({err})"
                self._failover_path.append(hop)
                self._notify(
                    task,
                    f"Zero-waiting failover: {candidate_backend} unavailable ({err}). Auto-switching to {next_backend}...",
                )
                continue
            elif not is_infra_or_quota_error(err):
                # Non-infra error (e.g. model returned garbage) — don't cascade,
                # let the retry loop handle backend switching.
                break

        return "", last_error

    #: Cooldown windows (seconds) after an infra failure, by error kind.
    COOLDOWN_QUOTA = 300.0
    COOLDOWN_AUTH = 600.0
    COOLDOWN_TRANSIENT = 60.0

    def _coder_cascade(self) -> list[str]:
        from .provider_runtime import resolve_coder_routes
        return [entry.backend_name for entry in resolve_coder_routes(cascade=os.environ.get("PACER_CODER_CASCADE"))]

    def _record_verification_strike(self, backend: str) -> None:
        strikes = self.__dict__.setdefault("_verification_strikes", {})
        name = (backend or "").lower()
        strikes[name] = strikes.get(name, 0) + 1

    def _next_cascade_backend(self, current: str, *, acceptance_exam: bool = False) -> str:
        # Task-level failures stay on the writer; provider outages use the route table.
        return current

    def _note_backend_failure(self, backend: str, error: str | None) -> None:
        """Open the circuit for a backend after an infra/quota failure."""
        name = (backend or "").lower()
        if not name:
            return
        msg = (error or "").lower()
        if any(k in msg for k in ("401", "403", "unauthorized", "forbidden", "authentication", "invalid_api_key")):
            window = self.COOLDOWN_AUTH
        elif any(k in msg for k in ("429", "quota", "额度", "余额", "rate limit")):
            window = self.COOLDOWN_QUOTA
        else:
            window = self.COOLDOWN_TRANSIENT
        until = time.monotonic() + window
        prev = self._backend_cooldowns.get(name, 0.0)
        # Never shorten an existing longer cooldown.
        self._backend_cooldowns[name] = max(prev, until)
        from .provider_runtime import note_failure
        note_failure("backend:" + name, error or "unavailable")

    def _backend_in_cooldown(self, backend: str) -> bool:
        name = (backend or "").lower()
        from .provider_runtime import cooldown_remaining
        if cooldown_remaining("backend:" + name):
            return True
        until = self._backend_cooldowns.get(name, 0.0)
        if until <= 0:
            return False
        if time.monotonic() >= until:
            self._backend_cooldowns.pop(name, None)
            return False
        return True

    def _is_backend_live(self, backend: str) -> bool:
        """Cheap pre-filter: is this backend *configured* and not cooling down?

        IMPORTANT: this does NOT prove the provider can generate. A backend can
        be configured (key present, binary installed, gateway listening) and
        still fail with 401/502/timeout at call time. The name reflects the
        cheap check it performs; callers must treat a real failure as
        authoritative and let the circuit breaker demote the backend.
        """
        name = (backend or "").lower()
        if self._backend_in_cooldown(name):
            return False
        if name in ("local", ""):
            return True
        if name in ("grok", "gork"):
            return is_grok_available()
        if name == "gemini38":
            return is_gemini38_available()
        if name == "gemini":
            return is_gemini_available()
        if name in ("gemini4", "gemini4_vps", "vps_gemini4"):
            try:
                from .gemini4_vps_client import (
                    gemini4_settings,
                    is_gemini4_vps_available,
                    is_gemini4_vps_pool_healthy,
                )
                if not is_gemini4_vps_available():
                    return False
                from .provider_runtime import _is_vps_gateway
                if _is_vps_gateway(gemini4_settings().get("base_url", "")):
                    return is_gemini4_vps_pool_healthy()
                return True
            except Exception:
                return False
        if name == "workbuddy":
            from .provider_runtime import load_providers
            if load_providers().get("tier1_free", {}).get("workbuddy", {}).get("enabled", True) is not True:
                return False
            return get_gateway_info() is not None
        if name == "codex":
            return bool(shutil_which("codex"))
        if name in ("gemini_official", "gemini_pool", "google_studio"):
            try:
                from .gemini_official_pool import gemini_official_pool
                # Check if the pool has any configured keys
                return bool(getattr(gemini_official_pool, "_keys", None) or
                            getattr(gemini_official_pool, "api_keys", None))
            except Exception:
                return False
        if name in {"domestic_api", "premium_api"}:
            from .provider_runtime import api_nodes
            return any(node.is_available() for node in api_nodes(name))
        if name == "api":
            try:
                return any(c["available"] for c in self._api_configs())
            except (ValueError, KeyError):
                return False
        return False

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
            system_prompt = "You are an expert coding subagent. Provide exact SEARCH/REPLACE diff blocks to update code."
            self._record_model_request(backend, system_prompt, effective_prompt)
            grok_res = invoke_grok(
                effective_prompt,
                system_prompt=system_prompt,
                timeout=getattr(task, "_route_timeout_seconds", 25.0),
            )
            return (grok_res.response, grok_res.error) if grok_res.success else ("", grok_res.error)

        elif backend in ("gemini", "gemini38"):
            if not is_gemini_available():
                return "", "Gemini CLI not available on system"
            is_gemini38 = backend == "gemini38"
            model = (
                os.environ.get("PACER_GEMINI38_FALLBACK_MODEL", "gemini-3.8-flash-low").strip()
                if is_gemini38
                else None
            )
            if is_gemini38 and not model.lower().startswith(("gemini-3.8", "gemini-3-8")):
                return "", "PACER_GEMINI38_FALLBACK_MODEL must name a Gemini 3.8 model"
            use_cli_yolo = False
            invoke = invoke_gemini38 if is_gemini38 else invoke_gemini
            system_prompt = (
                "You are Pacer's Gemini 3.8 stateless code writer. Return only exact "
                "SEARCH/REPLACE diff blocks for the requested target files. Emit a separate "
                "patch for every target file that the task requires to change, including test "
                "files when test updates are requested; a source-only patch is incomplete when "
                "the task also requires test changes. Do not edit files, run tools, or claim verification."
                if is_gemini38 else ""
            )
            self._record_model_request(backend, system_prompt, effective_prompt)
            result = invoke(
                effective_prompt,
                cwd=self.workspace_root,
                yolo=use_cli_yolo,
                model=model,
                output_format="text",
                timeout_seconds=getattr(task, "_route_timeout_seconds", 180 if is_gemini38 else 90),
                system_prompt=system_prompt or None,
            )
            return (result.response, result.error) if result.success else ("", result.error)

        elif backend == "workbuddy":
            self._record_model_request(backend, "", effective_prompt)
            result = invoke_workbuddy(
                effective_prompt,
                cwd=self.workspace_root,
                target_files=task.target_files,
                apply_changes=False,
                timeout_seconds=getattr(task, "_route_timeout_seconds", 90),
            )
            if result.success:
                return (result.response, None)
            return ("", result.error or "WorkBuddy gateway failed")

        elif backend in ("gemini_official", "gemini_pool", "google_studio"):
            from .gemini_official_pool import gemini_official_pool
            try:
                self._record_model_request(backend, "", effective_prompt)
                resp = gemini_official_pool.generate_content(
                    effective_prompt, request_options={"timeout": getattr(task, "_route_timeout_seconds", 90)}
                )
                text = resp.text if hasattr(resp, "text") else str(resp)
                return (text, None)
            except Exception as exc:
                return ("", f"Gemini Official Pool error: {exc}")

        elif backend in ("gemini4", "gemini4_vps", "vps_gemini4"):
            from .gemini4_vps_client import invoke_gemini4_vps
            if task.role == "coder":
                prompt = effective_prompt
                system_prompt = ("You are Pacer's stateless code writer. Return ONLY exact "
                                 "<<<<<<< SEARCH / ======= / >>>>>>> REPLACE diff blocks for the target files.")
            else:
                prompt = self._gemini4_request(task.instructions)
                system_prompt = "You are Pacer's Gemini 4 reviewer. Use only the portrait and the latest sentence."
            self._record_model_request(backend, system_prompt, prompt)
            resp = invoke_gemini4_vps(prompt, system_prompt=system_prompt, timeout_seconds=getattr(task, "_route_timeout_seconds", 90))
            if resp.success:
                return (resp.content, None)
            return ("", resp.error)

        elif backend == "codex":
            self._record_model_request(backend, "", getattr(task, "_stateless_prompt", None) or task.instructions)
            return self._run_codex_cli(task)

        elif backend in {"api", "domestic_api", "premium_api"}:
            return self._invoke_api_completion(effective_prompt, slot=backend, timeout_seconds=getattr(task, "_route_timeout_seconds", 90))

        elif backend == "local":
            return self._local_static_scan(task)

        return "", f"Unknown backend: {backend}"

    def _record_model_request(self, backend: str, system_prompt: str, user_prompt: str) -> None:
        SessionProjection(self.workspace_root).append_model_request(
            backend=backend,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            phase="subagent_dispatch",
        )

    def _local_static_scan(self, task: SubagentTask) -> tuple[str, str | None]:
        if task.role == "coder":
            return "", "Local backend cannot generate code; configure a writer backend"
        task.acceptance_tier = "local_static_only"
        findings = []
        errors = []
        for rel in task.target_files:
            path = (self.workspace_root / rel).resolve()
            if not path.is_relative_to(self.workspace_root.resolve()):
                errors.append(f"{rel}: outside workspace")
                continue
            try:
                content = path.read_text(encoding="utf-8")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if path.suffix != ".py":
                    findings.append(f"{rel}: {len(content.splitlines())} lines; sha256={digest}; AST not available")
                    continue
                tree = ast.parse(content, filename=rel)
                functions = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(tree))
                classes = sum(isinstance(n, ast.ClassDef) for n in ast.walk(tree))
                findings.append(f"{rel}: functions={functions}, classes={classes}; sha256={digest}")
                module_aliases = {"subprocess"}
                direct_calls = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        module_aliases.update(a.asname or a.name for a in node.names if a.name == "subprocess")
                    elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                        direct_calls.update(a.asname or a.name for a in node.names if a.name in {"Popen", "run"})
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    is_call = (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                               and func.value.id in module_aliases and func.attr in {"run", "Popen"}) or (isinstance(func, ast.Name) and func.id in direct_calls)
                    if is_call:
                        hidden = any(k.arg is None and isinstance(k.value, ast.Call)
                                     and isinstance(k.value.func, ast.Name) and k.value.func.id == "hidden_subprocess_kwargs"
                                     for k in node.keywords)
                        flags = any(k.arg in {"creationflags", "startupinfo"} for k in node.keywords)
                        findings.append(f"  line {node.lineno}: {ast.unparse(func)}; hidden_subprocess_kwargs={hidden}; explicit_window_flags={flags}")
            except (OSError, UnicodeError, SyntaxError) as exc:
                errors.append(f"{rel}: {type(exc).__name__}: {exc}")
        report = "index_only; local_static_only (static evidence, runtime behavior unverified)\n" + "\n".join(findings + errors)
        if not task.target_files:
            report += "\nNo target files supplied; no files inspected."
        return report, "; ".join(errors) or None

    def _api_configs(self, slot="api"):
        if slot != "api":
            from .provider_runtime import api_nodes
            return [node.completion_config() for node in api_nodes(slot)]
        default = None if (os.environ.get("PACER_API_POOL") or os.environ.get("PACER_API_MODEL")) else resolve_model_provider_config()
        return resolve_api_pool(default)

    def _invoke_api_completion(self, prompt: str, slot="api", timeout_seconds: float = 90) -> tuple[str, str | None]:
        from .security import redact_secret_text
        deadline = time.monotonic() + timeout_seconds
        errors = []
        try:
            configs = self._api_configs(slot)
        except (ValueError, KeyError) as exc:
            return "", f"Invalid API pool configuration: {exc}"
        for config in configs:
            if not config["available"]:
                errors.append("API configuration unavailable: " + ", ".join(config.get("blockers", [])))
                continue
            from .provider_runtime import _stable_node_id, cooldown_remaining, note_failure
            identity = _stable_node_id(config["provider"], config["model"], config.get("base_url", ""), config.get("_api_key", ""))
            node = config.get("node_id") or f"{config['provider']}:{config['model']}#{identity}"
            if self._api_node_cooldowns.get(node, 0) > time.monotonic() or cooldown_remaining(node):
                errors.append(f"{config['provider']}:{config['model']}: cooling down")
                continue
            if not config["available"]:
                errors.append("API configuration unavailable: " + ", ".join(config.get("blockers", [])))
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append("API route timeout")
                break
            attempt = {"slot": slot, "provider": config["provider"], "model": config["model"], "node_id": node, "success": False}
            self._api_node_attempts.append(attempt)
            try:
                self._record_model_request(
                    f"{config['provider']}:{config['model']}",
                    "You are Pacer's coding agent. Follow the task's patch format and return complete patches.",
                    prompt,
                )
                content = run_llm_completion(
                    backend=resolve_llm_backend(f"{config['provider']}:{config['model']}"),
                    system_prompt="You are Pacer's coding agent. Follow the task's patch format and return complete patches.",
                    prompt=prompt, max_tokens=8192, api_key=config["_api_key"],
                    base_url=config["base_url"], endpoint=config["endpoint"], timeout_seconds=remaining,
                )
                if not content.strip():
                    return "", "API completion returned empty content"
                attempt["success"] = True
                return content, None
            except Exception as exc:
                error = redact_secret_text(str(exc).replace(config["_api_key"], "[REDACTED]"))
                errors.append(error)
                if not is_infra_or_quota_error(error):
                    break
                window = self.COOLDOWN_AUTH if any(code in error for code in ("401", "403")) else self.COOLDOWN_QUOTA if "429" in error else self.COOLDOWN_TRANSIENT
                self._api_node_cooldowns[node] = time.monotonic() + window
                note_failure(node, error)
                self._failover_path.append(f"api:{config['provider']}:{config['model']} unavailable; trying next configured node")
        return "", "; ".join(errors) or "API pool is empty"

    def _run_codex_cli(self, task: SubagentTask) -> tuple[str, str | None]:
        """Run non-interactive codex command guarded by ProcessGuard."""
        codex_bin = resolve_codex_executable() or shutil_which("codex")
        if not codex_bin:
            return "", "Codex executable not found on PATH."

        prompt_text = getattr(task, "_stateless_prompt", None) or task.instructions
        cmd = prepare_subprocess_command([
            codex_bin,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "-",
        ])
        try:
            proc = guarded_run(
                cmd,
                input=prompt_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self.workspace_root),
                timeout=getattr(task, "_route_timeout_seconds", None) or self._codex_timeout(),
                # `codex exec` is the non-interactive batch mode: it never needs a
                # visible console, so hide it. The user-facing interactive Codex /
                # Claude / Gemini / `pacer task` windows are launched elsewhere and
                # deliberately left visible.
                **hidden_subprocess_kwargs(),
            )
            if proc.returncode == 0:
                return (proc.stdout or "").strip(), None
            return (proc.stdout or "").strip(), (proc.stderr or "").strip() or f"Codex exited with code {proc.returncode}"
        except Exception as exc:
            return "", f"Codex execution failed: {exc}"

    def _codex_timeout(self) -> float:
        """Bounded Codex CLI timeout.

        Codex without a verified login can sit waiting until the timeout fires.
        A 180s stall inside a failover chain is worse than a fast failure, so the
        default is short and overridable with PACER_CODEX_TIMEOUT_SECONDS.
        """
        raw = os.environ.get("PACER_CODEX_TIMEOUT_SECONDS", "").strip()
        if raw:
            try:
                val = float(raw)
                if val > 0:
                    return val
            except ValueError:
                pass
        return 60.0

    def _green_incremental_delta(self, task: SubagentTask, verification_output: str) -> tuple[bool, str]:
        """Require a real source delta plus a passing physical test improvement."""
        before = getattr(task, "_green_baseline_fingerprints", {})
        source_changed = False
        for rel in task.target_files:
            if is_test_path(rel) or rel not in before:
                continue
            path = self.workspace_root / rel
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() != before[rel]:
                source_changed = True
                break
        test_paths = list(getattr(task, "_green_incremental_test_paths", [])) or [
            rel for rel in task.target_files if is_test_path(rel)
        ]
        tests_changed = any(
            rel not in before
            or not (self.workspace_root / rel).is_file()
            or hashlib.sha256((self.workspace_root / rel).read_bytes()).hexdigest() != before.get(rel)
            for rel in test_paths
        )
        matches = re.findall(r"(?:^|\s)(\d+)\s+passed\b", str(verification_output or ""), re.IGNORECASE)
        current_count = int(matches[-1]) if matches else None
        baseline_count = getattr(task, "_green_baseline_passed_count", None)
        tests_improved = current_count is not None and baseline_count is not None and current_count > baseline_count
        if not source_changed:
            return False, "no target source SHA-256 changed"
        if not tests_changed and not tests_improved:
            return False, f"no test-file SHA-256 change or pass-count increase (baseline={baseline_count}, current={current_count})"
        if current_count is not None:
            task._green_incremental_result = {"baseline_passed": baseline_count, "current_passed": current_count}
        return True, "source and delta-test evidence verified"

    def _run_verification_command(self, cmd: str | list[str] | tuple[str, ...], *, task_id: str = "") -> tuple[bool, str]:
        """Execute verification command using Pacer's command_verification gate + tamper guards."""
        task_id = task_id or str(getattr(self._verification_context, "task_id", ""))
        if isinstance(cmd, str):
            command_text = cmd.strip()
            is_safe, safety_reason = validate_safe_verification_command(command_text, repo_root=self.workspace_root)
            if not is_safe:
                return False, f"Verification command rejected by safety policy: {safety_reason}"
            try:
                argv = parse_verification_command_argv(command_text)
            except ValueError as exc:
                return False, f"Verification command could not be parsed safely: {exc}"
        else:
            argv = [str(item) for item in cmd]
            command_text = subprocess.list2cmdline(argv)
        is_safe, safety_reason = validate_safe_verification_argv(argv, repo_root=self.workspace_root)
        if not is_safe:
            return False, f"Verification argv rejected by safety policy: {safety_reason}"

        if self.enforce_test_tamper_guard:
            # Acceptance chain files (pytest.ini, pyproject.toml, etc.) must NEVER be tampered with
            chain_now = snapshot_verification_files(repo_root=self.workspace_root, command=command_text, include_tests=False)
            tampered_chain = changed_snapshot_files(self._chain_baselines.get(command_text, {}), chain_now)
            if tampered_chain:
                return (
                    False,
                    f"Acceptance chain files changed after this task started ({', '.join(tampered_chain)}); restore the task baseline and fix product code instead.",
                )
            if self.guard and self.guard.acceptance_frozen:
                is_intact, alert, tampered_tests = self.guard.verify_and_enforce()
                if not is_intact:
                    brief = tamper_repair_brief(tampered_tests)
                    return (False, str(brief.get("repair_prompt") or brief.get("message") or "Acceptance suite tampering detected"))
            tests_now = snapshot_verification_files(repo_root=self.workspace_root)
            tampered_tests = changed_snapshot_files(self._test_baseline, tests_now)
            if tampered_tests:
                return (False, "Test files changed after this task started: " + ", ".join(tampered_tests)
                        + ". The acceptance suite is locked and must be restored from its captured baseline.")

        effective_argv = list(argv)
        effective_cmd = command_text
        try:
            task_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._-")[:48] or "verification"
            is_pytest = (
                Path(effective_argv[0]).name.casefold() in {"pytest", "pytest.exe", "pytest.cmd", "pytest.bat"}
                or effective_argv[1:3] == ["-m", "pytest"]
            )
            if task_id and is_pytest:
                effective_argv.extend(["-o", f"cache_dir=.pytest_cache_{task_slug}"])
            with tempfile.TemporaryDirectory(prefix=f"pacer-{task_slug}-") as scratch_dir:
                res = run_command_verification(
                    command=effective_argv,
                    repo_root=self.workspace_root,
                    timeout_seconds=120.0,
                    env={"TMP": scratch_dir, "TEMP": scratch_dir, "TMPDIR": scratch_dir},
                )
            self._verification_receipts.append({key: res.get(key) for key in ("command", "verdict", "exit_code", "output_tail")})
            verdict_ok = res.get("verdict") == "pass"
            output = str(res.get("output_tail") or res.get("reason") or "").strip()
            if res.get("shell_hint"):
                output = f"{output}\n{res['shell_hint']}".strip()
            return (verdict_ok, output)
        except Exception as exc:
            return (False, f"Verification execution error: {exc}")


def record_repair_lesson(module: str, reason: str, lessons_path: str | None = None) -> str:
    """Distill a failed->fixed cycle into a <=50-token rule for future invariants.

    Returns the compact rule string written to lessons.md.
    """
    from pathlib import Path
    rule = f"- {module}: {reason}".strip()
    # Hard cap at 50 tokens (simple whitespace split)
    tokens = rule.split()
    if len(tokens) > 50:
        rule = " ".join(tokens[:47]) + " ..."
    if lessons_path:
        p = Path(lessons_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(rule + "\n")
    return rule


def shutil_which(cmd: str) -> str | None:
    return shutil.which(cmd) or shutil.which(f"{cmd}.cmd") or shutil.which(f"{cmd}.exe")
