from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_capabilities import load_agent_profile, recommend_worker_config
from .dynamic_model_selector import select_model_for_task, selection_to_dict
from .model_router import route_task, tier_task_kind
from .codex_check import codex_coverage_summary, is_runtime_changed_file
from .git_diff import affected_workflows, changed_files
from .models import to_jsonable
from .mission_pipeline import plan_status_to_pipeline_state
from .project_memory import build_project_memory, project_memory_handoff_notes
from .security import permission_plan
from .workspace import WorkflowRef, discover_workflows, open_workspace


_CHECKPOINT_ARTIFACT_BASENAMES = {".visual-agent-status.md", "强制测试记录.md"}
_CHECKPOINT_ARTIFACT_DIR_PREFIXES = (".agent-workspace/", ".pw-browsers/", "artifacts/")
_NON_PRODUCT_DIR_PREFIXES = ("_graveyard_", "graveyard/", "archive/", "archives/", "_open_source_cases/")


# Codex is the default executable coding worker. It uses the user's existing
# Codex CLI login/subscription; Claude Code stays available as an explicit
# switch with --agent claude-code.
DEFAULT_AGENT_ORDER = ("codex",)
ORCHESTRATION_PATTERNS = (
    {
        "name": "worktree_isolation",
        "why": "Run competing coding agents in isolated branches before merging a winner.",
    },
    {
        "name": "cli_agent_adapter",
        "why": "Treat CLI agents as adapters behind one plan, but default to one coding worker and keep inspection lanes read-only.",
    },
    {
        "name": "acceptance_gate",
        "why": "Do not let a worker claim success without product workflow evidence.",
    },
    {
        "name": "failure_feedback_loop",
        "why": "Route failed verification evidence back to the implementation worker before handoff.",
    },
)


@dataclass(frozen=True)
class ChiefPlan:
    schema_version: int
    status: str
    objective: str
    workspace_root: str
    repo_root: str
    changed_files: list[str]
    selected_workflows: list[str]
    skipped_slow_workflows: list[str]
    coverage: dict[str, Any] = field(default_factory=dict)
    verification_mode: str = "workflow"
    acceptance_criteria: list[str] = field(default_factory=list)
    worker_tracks: list[dict[str, Any]] = field(default_factory=list)
    verification_commands: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    risks: list[dict[str, str]] = field(default_factory=list)
    handoff_prompt: str = ""
    clarity: dict[str, Any] = field(default_factory=dict)
    clarifying_questions: list[str] = field(default_factory=list)
    project_memory: dict[str, Any] = field(default_factory=dict)
    permission_plan: dict[str, Any] = field(default_factory=dict)
    pipeline_state: str = ""
    plan_id: str = ""
    patterns_reused: tuple[dict[str, str], ...] = ORCHESTRATION_PATTERNS


def build_chief_plan(
    *,
    goal: str,
    workspace_root: str | Path = ".agent-workspace",
    repo_root: str | Path = ".",
    base: str = "HEAD",
    agents: tuple[str, ...] = DEFAULT_AGENT_ORDER,
    include_slow: bool = False,
    max_workflows: int = 5,
    run_profile: str = "dry-run",
    interview: bool = False,
    answers: tuple[str, ...] = (),
) -> ChiefPlan:
    objective = str(goal).strip()
    if not objective:
        raise ValueError("chief-plan requires a non-empty goal.")

    workspace_path = Path(workspace_root).expanduser().resolve()
    repo_path = Path(repo_root).expanduser().resolve()
    verification_agents = tuple(dict.fromkeys(agent.strip() for agent in agents if agent.strip())) or DEFAULT_AGENT_ORDER
    answer_list = [str(item).strip() for item in answers if str(item).strip()]
    clarity = assess_goal_clarity(objective, answers=answer_list)

    if not workspace_path.exists():
        init_command = f"checkpoint init --root {_quote_cli(workspace_path)}"
        permissions = permission_plan([init_command], repo_root=repo_path)
        return ChiefPlan(
            schema_version=1,
            status="blocked",
            objective=objective,
            workspace_root=str(workspace_path),
            repo_root=str(repo_path),
            changed_files=[],
            selected_workflows=[],
            skipped_slow_workflows=[],
            acceptance_criteria=_acceptance_criteria(objective, coverage_status="missing_workspace"),
            worker_tracks=[],
            verification_commands=[init_command],
            next_actions=[
                "Initialize a Checkpoint workspace before dispatching coding agents.",
                f"Run: {init_command}",
            ],
            risks=[
                {
                    "level": "blocking",
                    "area": "workspace",
                    "message": "Workspace root does not exist, so no product workflows can be selected.",
                }
            ],
            handoff_prompt=_handoff_prompt(objective, [], [init_command], status="blocked", permission_summary=permissions),
            clarity=clarity,
            clarifying_questions=list(clarity["questions"]) if clarity["questions"] else [],
            permission_plan=permissions,
            pipeline_state=plan_status_to_pipeline_state("blocked"),
        )

    project_memory = build_project_memory(workspace_root=workspace_path, repo_root=repo_path, goal=objective, limit=5)
    memory_notes = project_memory_handoff_notes(project_memory)
    project_memory["usage"] = _project_memory_usage(project_memory, memory_notes)

    # Requirements gate: refuse to dispatch a goal with no verifiable definition
    # of done. Withholding dispatch here is the point — it saves a wasted agent
    # run and forces the objective to be sharpened first.
    if not clarity["ok"]:
        return ChiefPlan(
            schema_version=1,
            status="needs_clarification",
            objective=objective,
            workspace_root=str(workspace_path),
            repo_root=str(repo_path),
            changed_files=[],
            selected_workflows=[],
            skipped_slow_workflows=[],
            coverage={},
            acceptance_criteria=_acceptance_criteria(objective, coverage_status=""),
            worker_tracks=[],
            verification_commands=[],
            next_actions=["Answer the clarifying questions, then rerun chief-plan (optionally with --answer)."]
            + list(clarity["questions"]),
            risks=[
                {
                    "level": "blocking",
                    "area": "requirements",
                    "message": "The objective lacks a verifiable definition of done, so dispatch is withheld to avoid wasting a coding-agent run.",
                }
            ],
            handoff_prompt="",
            clarity=clarity,
            clarifying_questions=list(clarity["questions"]),
            project_memory=project_memory,
            pipeline_state=plan_status_to_pipeline_state("needs_clarification"),
        )

    workspace = open_workspace(workspace_path)
    all_refs = list(discover_workflows(workspace, include_slow=True))
    verification_refs = [ref for ref in all_refs if "verification" in set(ref.tags)]
    skipped_slow = [ref.name for ref in verification_refs if "slow" in set(ref.tags) and not include_slow]
    runnable_refs = [ref for ref in verification_refs if include_slow or "slow" not in set(ref.tags)]
    changed = _product_changed_files(changed_files(base=base, cwd=repo_path), repo_root=repo_path)
    selected_refs = affected_workflows(runnable_refs, changed=changed)[: max(0, int(max_workflows))]
    coverage = codex_coverage_summary(changed, runnable_refs)
    coverage_status = str(coverage.get("status") or "")
    selected_names = [ref.name for ref in selected_refs]
    commands = _verification_commands(
        workspace_path=workspace_path,
        repo_path=repo_path,
        base=base,
        selected_refs=selected_refs,
        run_profile=run_profile,
        include_slow=include_slow,
    )
    status = _plan_status(
        changed=changed,
        runnable_refs=runnable_refs,
        selected_refs=selected_refs,
        coverage_status=coverage_status,
    )
    permissions = permission_plan(commands, repo_root=repo_path)
    return ChiefPlan(
        schema_version=1,
        status=status,
        objective=objective,
        workspace_root=str(workspace_path),
        repo_root=str(repo_path),
        changed_files=changed,
        selected_workflows=selected_names,
        skipped_slow_workflows=skipped_slow,
        coverage=coverage,
        acceptance_criteria=_acceptance_criteria(objective, coverage_status=coverage_status, answers=answer_list),
        worker_tracks=_worker_tracks(
            objective=objective,
            agents=verification_agents,
            workspace_path=workspace_path,
            repo_path=repo_path,
            verification_commands=commands,
            memory_notes=memory_notes,
            changed_files=changed,
            acceptance_criteria=_acceptance_criteria(objective, coverage_status=coverage_status, answers=answer_list),
        ),
        verification_commands=commands,
        next_actions=_next_actions(status=status, coverage=coverage, selected_refs=selected_refs, commands=commands),
        risks=_risks(changed=changed, runnable_refs=runnable_refs, selected_refs=selected_refs, coverage=coverage),
        clarity=clarity,
        clarifying_questions=list(clarity["questions"]) if (interview and clarity["questions"]) else [],
        project_memory=project_memory,
        permission_plan=permissions,
        pipeline_state=plan_status_to_pipeline_state(status),
        handoff_prompt=_handoff_prompt(objective, selected_names, commands, status=status, memory_notes=memory_notes, permission_summary=permissions),
    )


def _product_changed_files(paths: list[str], *, repo_root: Path | None = None) -> list[str]:
    """Remove Checkpoint's own runtime artifacts from product planning input."""
    kept: list[str] = []
    for path in paths:
        normalized = str(path).replace("\\", "/").strip()
        if not normalized:
            continue
        basename = normalized.rsplit("/", 1)[-1]
        if basename in _CHECKPOINT_ARTIFACT_BASENAMES:
            continue
        if normalized == ".gitignore" and repo_root is not None and _gitignore_change_is_only_checkpoint_runtime(repo_root):
            continue
        if is_runtime_changed_file(normalized, repo_root=repo_root):
            continue
        if any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in _CHECKPOINT_ARTIFACT_DIR_PREFIXES):
            continue
        if _is_non_product_path(normalized):
            continue
        kept.append(normalized)
    return kept


def _is_non_product_path(path: str) -> bool:
    normalized = str(path).replace("\\", "/").strip().lstrip("/")
    if not normalized:
        return True
    parts = normalized.split("/")
    if any(part.endswith(".checkpoint-worktrees") or ".checkpoint-worktrees" in part for part in parts):
        return True
    return any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in _NON_PRODUCT_DIR_PREFIXES)


def _gitignore_change_is_only_checkpoint_runtime(repo_root: Path) -> bool:
    gitignore = repo_root / ".gitignore"
    allowed = {"# DevPacer / Checkpoint generated runtime files", ".agent-workspace/"}
    if not gitignore.exists():
        return False
    diff = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--", ".gitignore"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if diff.returncode == 0 and diff.stdout.strip():
        saw_generated_addition = False
        for raw in diff.stdout.splitlines():
            if raw.startswith(("+++", "---", "@@")):
                continue
            if raw.startswith("-"):
                return False
            if raw.startswith("+"):
                value = raw[1:].strip()
                if not value:
                    continue
                if value not in allowed:
                    return False
                saw_generated_addition = True
        return saw_generated_addition
    try:
        lines = [line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return False
    return bool(lines) and all(line in allowed for line in lines)


def chief_plan_to_dict(plan: ChiefPlan) -> dict[str, Any]:
    return to_jsonable(plan)


def chief_plan_to_markdown(plan: ChiefPlan) -> str:
    lines = [
        "## Chief Engineer Plan",
        "",
        f"Status: `{plan.status}`",
        f"Pipeline state: `{plan.pipeline_state or plan_status_to_pipeline_state(plan.status)}`",
        f"Objective: {plan.objective}",
        f"Workspace: `{plan.workspace_root}`",
        f"Repo: `{plan.repo_root}`",
        "",
        "### Context",
        "",
    ]
    if plan.changed_files:
        lines.append("Changed files:")
        for path in plan.changed_files[:10]:
            lines.append(f"- `{path}`")
        if len(plan.changed_files) > 10:
            lines.append(f"- ... {len(plan.changed_files) - 10} more")
    else:
        lines.append("Changed files: none detected from git diff.")
    lines.extend(
        [
            f"Coverage: `{plan.coverage.get('status', 'unknown')}`" if plan.coverage else "Coverage: unavailable",
            f"Selected workflows: {', '.join(plan.selected_workflows) if plan.selected_workflows else 'none'}",
        ]
    )
    if plan.skipped_slow_workflows:
        lines.append("Skipped slow workflows: " + ", ".join(plan.skipped_slow_workflows))
    if plan.permission_plan:
        lines.extend(
            [
                f"Permission decision: `{plan.permission_plan.get('decision', 'unknown')}`",
                f"Permission risk: `{plan.permission_plan.get('risk', 'unknown')}`",
            ]
        )
    memory_entries = plan.project_memory.get("entries") if isinstance(plan.project_memory, dict) else []
    if memory_entries:
        lines.extend(["", "### Project Memory", ""])
        for note in project_memory_handoff_notes(plan.project_memory):
            lines.append(f"- {note}")
        if not project_memory_handoff_notes(plan.project_memory):
            for item in memory_entries[:3]:
                lines.append(f"- `{item.get('mission_id')}` [{item.get('status')}] {item.get('objective')}")
    lines.extend(["", "### Acceptance Criteria", ""])
    lines.extend(f"- {item}" for item in plan.acceptance_criteria)
    if plan.clarifying_questions:
        lines.extend(["", "### Clarifying Questions", ""])
        lines.extend(f"- {question}" for question in plan.clarifying_questions)
    lines.extend(["", "### Worker Tracks", ""])
    if plan.worker_tracks:
        for track in plan.worker_tracks:
            lines.append(f"- `{track['id']}` using `{track['agent']}`: {track['role']}")
            if track.get("tier"):
                reason = track.get("route_reason") or ""
                lines.append(f"  Cost tier: `{track['tier']}`" + (f" — {reason}" if reason else ""))
            if track.get("model"):
                sandbox = (track.get("sandbox") or {}).get("name") or ""
                approval = (track.get("approval") or {}).get("name") or ""
                posture = ", ".join(part for part in (f"model {track['model']}", f"sandbox {sandbox}" if sandbox else "", f"approval {approval}" if approval else "") if part)
                lines.append(f"  Recommended: {posture}")
            selection = track.get("model_selection") if isinstance(track.get("model_selection"), dict) else {}
            selected_model = selection.get("selected") if isinstance(selection.get("selected"), dict) else {}
            if selected_model:
                lines.append(
                    f"  Dynamic model: `{selected_model.get('id')}` "
                    f"(tier `{selection.get('required_tier')}`, quota `{float(selection.get('quota_used_percentage') or 0):.1f}%`)"
                )
            command = track.get("command") or track.get("command_template") or ""
            lines.append(f"  Command: `{command}`")
            if track.get("parallelism_hint"):
                lines.append(f"  Parallel: {track['parallelism_hint']}")
    else:
        if plan.status == "needs_clarification":
            lines.append("- Withheld: clarify the objective above before dispatching a worker.")
        else:
            lines.append("- No worker tracks until the blocking setup issue is fixed.")
    lines.extend(["", "### Verification Commands", ""])
    for command in plan.verification_commands:
        lines.append(f"```powershell\n{command}\n```")
    lines.extend(["", "### Risks", ""])
    if plan.risks:
        for risk in plan.risks:
            lines.append(f"- `{risk['level']}` {risk['area']}: {risk['message']}")
    else:
        lines.append("- No blocking risk detected in the plan.")
    lines.extend(["", "### Next Actions", ""])
    lines.extend(f"- {item}" for item in plan.next_actions)
    lines.extend(["", "### Worker Handoff Prompt", "", "```text", plan.handoff_prompt.strip(), "```"])
    return "\n".join(lines)


def _plan_status(
    *,
    changed: list[str],
    runnable_refs: list[WorkflowRef],
    selected_refs: list[WorkflowRef],
    coverage_status: str,
) -> str:
    if not runnable_refs:
        return "needs_workflow"
    if coverage_status in {"uncovered", "fallback_only"}:
        return "needs_workflow_coverage"
    if changed and not selected_refs:
        return "needs_workflow_coverage"
    return "ready"


# Keyword -> domain-specific acceptance criteria. A better chief engineer infers
# what "done" means for the product area instead of only asserting "tests pass".
DOMAIN_CRITERIA: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("checkout", "cart", "order", "payment", "purchase", "结算", "下单", "购物车", "支付"),
        (
            "Order total, currency, and line items display correctly.",
            "A successful order shows an explicit confirmation state (not a silent success).",
            "Payment or checkout errors are surfaced clearly, not swallowed.",
        ),
    ),
    (
        ("login", "logout", "auth", "signup", "sign-in", "password", "登录", "注册", "认证", "密码"),
        (
            "Valid credentials reach the authenticated state.",
            "Invalid credentials show a clear error, not a blank screen or crash.",
        ),
    ),
    (
        ("dashboard", "chart", "report", "table", "list", "metric", "仪表盘", "图表", "报表", "列表"),
        (
            "Data renders with no undefined, NaN, or stray placeholder where real data exists.",
            "The empty state is intentional and readable when there is no data.",
        ),
    ),
    (
        ("form", "input", "validation", "submit", "field", "表单", "校验", "提交", "输入"),
        (
            "Valid input submits and persists; the success state is visible.",
            "Invalid input is rejected with a specific, visible validation message.",
        ),
    ),
    (
        ("total", "price", "amount", "discount", "currency", "金额", "价格", "折扣", "总价"),
        (
            "Computed totals are numerically correct, including edge cases (zero, discount, rounding).",
            "Currency and formatting are consistent across the flow.",
        ),
    ),
)


def _domain_criteria(objective: str) -> list[str]:
    lower = objective.lower()
    seen: list[str] = []
    for keywords, criteria in DOMAIN_CRITERIA:
        if any(keyword in lower for keyword in keywords):
            for item in criteria:
                if item not in seen:
                    seen.append(item)
    return seen


def _acceptance_criteria(objective: str, *, coverage_status: str, answers: list[str] | None = None) -> list[str]:
    criteria = [
        f"The implementation satisfies the user objective: {objective}",
        "Project tests and build checks pass before product acceptance is claimed.",
        "Checkpoint verification returns no failed workflows.",
        "Any inspection-only result is treated as partial evidence, not final acceptance.",
    ]
    criteria.extend(_domain_criteria(objective))
    for answer in answers or []:
        criteria.append(f"Clarified requirement: {answer}")
    if coverage_status in {"uncovered", "fallback_only", "missing_workspace"}:
        criteria.append("Workflow coverage gaps are resolved before claiming the task is done.")
    return criteria


# Terms that signal vagueness without a concrete, checkable outcome.
_VAGUE_TERMS = {
    "better", "nicer", "improve", "improved", "improvement", "fix", "fixes", "cleanup",
    "clean", "optimize", "optimise", "tweak", "polish", "stuff", "things", "somehow",
    "etc", "update", "handle", "refactor", "enhance", "modernize",
    "优化", "改进", "完善", "修改", "调整", "美化", "更好", "处理一下", "弄一下", "搞一下",
}
# Concrete product surfaces / nouns that make a goal actionable.
_DOMAIN_ANCHORS = {kw for keywords, _ in DOMAIN_CRITERIA for kw in keywords} | {
    "button", "modal", "dialog", "redirect", "api", "endpoint", "search", "filter",
    "upload", "download", "toast", "navbar", "menu", "settings", "profile", "empty",
    "state", "error", "banner", "tab", "route", "page", "screen", "component",
    "按钮", "弹窗", "页面", "接口", "跳转", "搜索", "筛选", "上传", "下载", "空状态",
}


# Diagnosis goals ("why didn't X run today?") carry their own verifiable
# outcome — a written root-cause report — so the clarity gate must not bounce
# them back with "what is the observable result?" questions.
_DIAGNOSIS_PATTERNS = (
    re.compile(r"为什么[^。！？\n]{0,50}(没|不|失败|停|出错|报错|异常)"),
    re.compile(r"(没|不)[^。！？\n]{0,20}(推送|运行|执行|触发|生效|工作)[^。！？\n]{0,20}(为什么|什么原因|怎么回事)?"),
    re.compile(r"(排查|诊断|查明|定位)"),
    re.compile(r"(怎么回事|什么原因|哪里出了?问题)"),
    re.compile(r"\b(why|diagnose|investigate|root\s*cause)\b", re.IGNORECASE),
)


def is_diagnosis_goal(objective: str) -> bool:
    text = str(objective or "")
    return any(pattern.search(text) for pattern in _DIAGNOSIS_PATTERNS)


def assess_goal_clarity(objective: str, *, answers: list[str] | None = None) -> dict[str, Any]:
    """Judge whether an objective has a verifiable definition of done.

    Conservative on purpose: it only passes goals that have a concrete anchor
    (observable result, path/file, domain term, or user clarification).
    """
    text = objective.strip()
    lower = text.lower()
    _split_pat = re.compile(r'[\s,.;:!?，。；：！？、]+')
    tokens = [tok for tok in _split_pat.split(text) if tok]
    _cjk_pat = re.compile(r'[一-鿿]')
    meaningful = [tok for tok in tokens if len(tok) >= 3 or _cjk_pat.search(tok)]
    # Chinese text has no word separators, so whole clauses become one token.
    # Count each 3 CJK characters as roughly one meaningful unit so Chinese
    # goals aren't penalised by English-biased tokenisation.
    cjk_chars = len(_cjk_pat.findall(text))
    meaningful_count = len(meaningful) + cjk_chars // 3
    has_number = any(ch.isdigit() for ch in text)
    _quote_chars = '”\'“”「」『』`'
    has_quote = any(q in text for q in _quote_chars)
    # Detect file paths and extensions (e.g., utils.py, src/main.js, test.ts)
    # Use regex to match common file patterns before tokenization splits on '.'
    _file_pattern = re.compile(r'[a-zA-Z0-9_]+\.[a-zA-Z0-9_]{1,10}')
    has_pathish = '/' in text or bool(_file_pattern.search(text))
    domain_hit = any(anchor in lower for anchor in _DOMAIN_ANCHORS)
    vague_hit = any(term in lower for term in _VAGUE_TERMS)
    answered = bool(answers)

    concrete_signal = has_number or has_quote or has_pathish or domain_hit
    diagnosis = is_diagnosis_goal(text)
    # Keep the gate intentionally conservative: plain natural language without
    # an observable anchor should keep asking questions instead of being marked
    # "clear" just because it is long enough. Diagnosis goals pass because
    # their definition of done is inherent: a root-cause report.
    ok = answered or concrete_signal or diagnosis

    questions: list[str] = []
    if diagnosis:
        pass  # deliverable is the diagnosis report; no clarification needed
    else:
        if not concrete_signal:
            questions.append(
                "这个目标完成后，用户能看到什么可验证结果？请给出具体文字、数字或状态。"
            )
        if not domain_hit and not has_pathish:
            questions.append("这次修改会影响哪个页面、屏幕或文件？")
        questions.append("有什么内容绝对不能改坏？")

    return {
        "ok": bool(ok),
        "objective": text,
        "signals": {
            "has_number": has_number,
            "has_quote": has_quote,
            "has_pathish": has_pathish,
            "domain_hit": domain_hit,
            "vague_hit": vague_hit,
            "diagnosis": diagnosis,
            "answered": answered,
            "meaningful_words": len(meaningful),
            "cjk_chars": cjk_chars,
            "meaningful_count": meaningful_count,
        },
        "questions": questions,
    }


def _worker_tracks(
    *,
    objective: str,
    agents: tuple[str, ...],
    workspace_path: Path,
    repo_path: Path,
    verification_commands: list[str],
    memory_notes: list[str] | None = None,
    changed_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    notes = [item for item in (memory_notes or []) if item]
    for index, agent in enumerate(agents, start=1):
        profile = load_agent_profile(agent)
        primary_role = str((profile or {}).get("primary_role") or "")
        is_inspector = primary_role == "multimodal_inspection"
        model_selection: dict[str, Any] | None = None
        if is_inspector:
            role = "multimodal inspection reviewer"
            task_kind = "inspection"
            prompt = _inspection_prompt(objective, verification_commands)
            route = None
        else:
            role = "implementation worker" if index == 1 else "review and alternative implementation worker"
            route = route_task(
                objective=objective,
                changed_files=changed_files or [],
                acceptance_criteria=acceptance_criteria or [],
            )
            model_selection = selection_to_dict(
                select_model_for_task(
                    objective=objective,
                    changed_files=changed_files or [],
                    acceptance_criteria=acceptance_criteria or [],
                    workspace_root=workspace_path,
                )
            )
            task_kind = tier_task_kind(route.tier)
            prompt = _single_line_prompt(objective, verification_commands)
        prompt = _with_memory_notes(prompt, notes)
        model = ""
        sandbox: dict[str, str] = {}
        approval: dict[str, str] = {}
        reasoning_effort = "inherit"
        parallelism_hint = ""
        if profile:
            config = dict(recommend_worker_config(profile, task_kind=task_kind))
            selected = (
                model_selection.get("selected")
                if isinstance(model_selection, dict) and isinstance(model_selection.get("selected"), dict)
                else {}
            )
            if (
                agent == "codex"
                and str(model_selection.get("status") or "") == "selected"
                and str(selected.get("agent_backend") or "") == "codex"
                and str(selected.get("model") or "")
            ):
                config["model"] = str(selected["model"])
                config["model_flag"] = f"--model {selected['model']}"
            model = str(config.get("model") or "")
            sandbox = config.get("sandbox") or {}
            approval = config.get("approval") or {}
            reasoning_effort = str(config.get("reasoning_effort") or "inherit")
            parallelism_hint = str(config.get("parallelism_hint") or "")
            command = _agent_command(profile, prompt=prompt, config=config)
        else:
            command = _agent_command_template(agent, prompt)
        tracks.append(
            {
                "id": f"track_{index}_{_safe_token(agent)}",
                "agent": agent,
                "role": role,
                "track_kind": "inspection" if is_inspector else "implementation",
                "workspace_strategy": "read-only evidence lane" if is_inspector else "git worktree per agent before merging",
                "repo_root": str(repo_path),
                "checkpoint_workspace": str(workspace_path),
                "model": model,
                "tier": route.tier if route else "",
                "route_reason": route.reason if route else "",
                "model_selection": model_selection if route else None,
                "routing_decision_id": str((model_selection or {}).get("decision_id") or ""),
                "selected_provider": str(((model_selection or {}).get("selected") or {}).get("provider") or ""),
                "sandbox": sandbox,
                "approval": approval,
                "reasoning_effort": reasoning_effort,
                "parallelism_hint": parallelism_hint,
                "command": command,
                "command_template": _agent_command_template(agent, prompt),
            }
        )
    return tracks


def _agent_command(profile: dict[str, Any], *, prompt: str, config: dict[str, Any]) -> str:
    headless = profile.get("headless") if isinstance(profile.get("headless"), dict) else {}
    base_template = str(headless.get("command") or "").strip()
    headless_perm = str(headless.get("permission_flag") or "").strip()
    headless_extra = str(headless.get("extra_flags") or "").strip()
    if headless_perm or headless_extra:
        # Headless-safe flags (e.g. acceptEdits + json) so the previewed command
        # matches the executed one and does not hang on interactive prompts.
        flags = _dedupe_flags([str(config.get("model_flag") or "").strip(), headless_perm, headless_extra])
    else:
        flags = _dedupe_flags(
            [
                str(config.get("model_flag") or "").strip(),
                str((config.get("sandbox") or {}).get("flag") or "").strip(),
                str((config.get("approval") or {}).get("flag") or "").strip(),
            ]
        )
    if base_template and re.search(r"(^|\s)-p\s+\"?<prompt>\"?", base_template):
        base = re.sub(r"\s+-p\s+\"?<prompt>\"?", "", base_template).strip()
        parts = [base, *flags, "-p", _quote_cli(prompt)]
    elif base_template:
        base = re.sub(r'"?<prompt>"?', "", base_template).strip()
        parts = [base, *flags, _quote_cli(prompt)]
    else:
        parts = [str(profile.get("executable") or "").strip(), *flags, _quote_cli(prompt)]
    return " ".join(part for part in parts if part)


def _dedupe_flags(flags: list[str]) -> list[str]:
    # Some agents (Claude Code) express sandbox and approval through the same
    # option (--permission-mode); keep only the first use of each option name.
    seen: set[str] = set()
    result: list[str] = []
    for flag in flags:
        if not flag:
            continue
        option = flag.split()[0]
        if option in seen:
            continue
        seen.add(option)
        result.append(flag)
    return result


def _agent_command_template(agent: str, prompt: str) -> str:
    normalized = agent.strip().lower()
    quoted = _quote_cli(prompt)
    if normalized in {"codex", "openai-codex"}:
        return f"codex exec {quoted}"
    if normalized in {"claude", "claude-code"}:
        return f"claude {quoted}"
    if normalized in {"gemini", "gemini-cli"}:
        return f"gemini {quoted}"
    if normalized in {"opencode", "open-code"}:
        return f"opencode {quoted}"
    return f"{agent} {quoted}"


def _verification_commands(
    *,
    workspace_path: Path,
    repo_path: Path,
    base: str,
    selected_refs: list[WorkflowRef],
    run_profile: str,
    include_slow: bool,
) -> list[str]:
    root = _quote_cli(workspace_path)
    repo = _quote_cli(repo_path)
    slow = " --include-slow" if include_slow else ""
    commands = [
        (
                "python -m visual_agent.cli codex-check"
            f" --workspace-root {root}"
            f" --repo-root {repo}"
            f" --base {_quote_cli(base)}"
            f" --run-profile {run_profile}"
            f"{slow}"
            " --format markdown"
        )
    ]
    if selected_refs:
        workflow_args = " ".join(f"--workflow {_quote_cli(ref.name)}" for ref in selected_refs)
        commands.append(
            (
                "python -m visual_agent.cli verify-now"
                f" --workspace-root {root}"
                f" {workflow_args}"
                f" --run-profile {run_profile}"
                f"{slow}"
                " --format markdown"
            )
        )
    else:
        commands.append(f"python -m visual_agent.cli verify-now --workspace-root {root} --run-profile {run_profile}{slow} --format markdown")
    return commands


def _next_actions(
    *,
    status: str,
    coverage: dict[str, Any],
    selected_refs: list[WorkflowRef],
    commands: list[str],
) -> list[str]:
    if status == "needs_workflow":
        return [
            "Record or generate at least one verification workflow for the product area.",
            "Run the generated workflow once before dispatching coding agents.",
        ]
    if status == "needs_workflow_coverage":
        actions = [str(coverage.get("next_action") or "Add precise workflow coverage for changed files.")]
        if coverage.get("suggested_new_workflows"):
            actions.append("Create the suggested workflow draft, then rerun chief-plan.")
        if coverage.get("suggested_affects"):
            actions.append("Add the suggested affects paths to the fallback workflow, then rerun chief-plan.")
        return actions
    actions = ["Dispatch the implementation worker with the handoff prompt."]
    if len(selected_refs) > 1:
        actions.append("Use one git worktree per worker track if running agents in parallel.")
    actions.append(f"After changes, run: {commands[0]}")
    return actions


def _risks(
    *,
    changed: list[str],
    runnable_refs: list[WorkflowRef],
    selected_refs: list[WorkflowRef],
    coverage: dict[str, Any],
) -> list[dict[str, str]]:
    risks: list[dict[str, str]] = []
    if not runnable_refs:
        risks.append(
            {
                "level": "high",
                "area": "verification",
                "message": "No verification-tagged workflows are available for product acceptance.",
            }
        )
    if coverage.get("status") in {"uncovered", "fallback_only"}:
        risks.append(
            {
                "level": "high",
                "area": "coverage",
                "message": str(coverage.get("next_action") or "Changed files are not precisely covered by workflows."),
            }
        )
    if changed and not selected_refs:
        risks.append(
            {
                "level": "medium",
                "area": "routing",
                "message": "Git changed files exist, but no workflow was selected for them.",
            }
        )
    if not changed:
        risks.append(
            {
                "level": "medium",
                "area": "scope",
                "message": "No git changes were detected; the chief plan cannot route by changed files yet.",
            }
        )
    return risks


def _handoff_prompt(
    objective: str,
    workflows: list[str],
    commands: list[str],
    *,
    status: str,
    memory_notes: list[str] | None = None,
    permission_summary: dict[str, Any] | None = None,
) -> str:
    workflow_text = ", ".join(workflows) if workflows else "no selected workflow yet"
    command_text = "\n".join(f"- {command}" for command in commands)
    lines = [
        f"Objective: {objective}",
        f"Chief plan status: {status}",
        f"Selected Checkpoint workflows: {workflow_text}",
        "Work only inside the repository. Keep changes scoped to the objective.",
        "Before claiming done, run the verification command and read the failure evidence if it fails.",
    ]
    notes = [item for item in (memory_notes or []) if item]
    if notes:
        lines.extend(
            [
                "Project memory (advisory and non-exhaustive; inspect any repository files needed):",
                *[f"- {item}" for item in notes],
            ]
        )
    if permission_summary:
        decision = str(permission_summary.get("decision") or "unknown")
        risk = str(permission_summary.get("risk") or "unknown")
        lines.append(f"Permission plan: {decision} / {risk}. If any command is ask or deny, stop and surface the reason instead of bypassing it.")
    lines.extend(["Verification commands:", command_text])
    return "\n".join(lines)


def _single_line_prompt(objective: str, commands: list[str]) -> str:
    command = commands[0] if commands else "python -m visual_agent.cli verify-now --workspace-root .agent-workspace --format markdown"
    return f"{objective} After editing, run this verification command: {command}. Summarize evidence before claiming done."


def _inspection_prompt(objective: str, commands: list[str]) -> str:
    command = commands[0] if commands else "python -m visual_agent.cli verify-now --workspace-root .agent-workspace --format markdown"
    return (
        f"{objective} Inspect Checkpoint evidence, screenshots, and acceptance criteria as a "
        f"read-only multimodal reviewer. Do not edit code. If more evidence is needed, suggest "
        f"running: {command}. Summarize visual risks and missing acceptance evidence."
    )


def _with_memory_notes(prompt: str, notes: list[str]) -> str:
    if not notes:
        return prompt
    memory = " ".join(f"Project memory: {item}" for item in notes[:3])
    return (
        f"{prompt} Memory is advisory and non-exhaustive; inspect any repository files needed. "
        f"{memory}"
    )


def _project_memory_usage(payload: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    joined = " ".join(notes)
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    instruction_memory = payload.get("instruction_memory") if isinstance(payload.get("instruction_memory"), dict) else {}
    instruction_files = instruction_memory.get("files") if isinstance(instruction_memory.get("files"), list) else []
    return {
        "retrieval_invoked": True,
        "selected_entries": len(entries),
        "injected": bool(notes),
        "injected_note_count": len(notes),
        "injected_chars": len(joined),
        "injected_memory_ids": [
            str(item.get("memory_id"))
            for item in entries
            if isinstance(item, dict) and str(item.get("memory_id") or "") in joined
        ],
        "injected_instruction_paths": [
            str(item.get("path"))
            for item in instruction_files
            if isinstance(item, dict) and str(item.get("path") or "") in joined
        ],
        "index_hits": int(((payload.get("index") or {}).get("hits") or 0)),
        "index_misses": int(((payload.get("index") or {}).get("misses") or 0)),
    }


def _safe_token(value: str) -> str:
    token = "".join(char.lower() if char.isalnum() else "_" for char in value.strip())
    while "__" in token:
        token = token.replace("__", "_")
    return token.strip("_") or "agent"


def _quote_cli(value: str | Path) -> str:
    text = str(value)
    if not text:
        return '""'
    if any(char.isspace() for char in text) or any(char in text for char in '"`&()[]{}'):
        return '"' + text.replace('"', '\\"') + '"'
    return text
