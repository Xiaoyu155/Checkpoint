"""Turn one user sentence plus the project portrait into either work or one question."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from .context_profile import ContextProfile, ContextProfileManager
from .managed_checkpoint import is_checkpoint_resumable, load_managed_checkpoint
from .subagent_orchestrator import discover_target_files_for_goal


_CONTINUATION = {"继续", "继续做", "接着", "接着做", "continue", "resume"}
_QUOTED_SPEC_PATH = re.compile(r'["“]([^"”\r\n]+?\.(?:md|txt))["”]', re.IGNORECASE)
_SPEC_CUE = re.compile(r"按照|根据|依据|读取|加载|参考|许愿|\b(?:wish|spec(?:ification)?)\b", re.IGNORECASE)
_PACER_ACTION = re.compile(r"完善|改进|优化|修改|改动|修复|开发|重构|实现|接入|\b(?:improve|refine|fix|implement|refactor)\b", re.IGNORECASE)
_PACER_SELF_SCOPE = re.compile(
    r"(?:pacer.{0,24}(?:自己|自身|本身|源码|源代码|代码|内部|内核|核心|dogfood|自举)"
    r"|(?:自己|自身|本身).{0,16}pacer|pacer.{0,20}(?:self[- ]?work|self[- ]?development|dogfood)"
    r"|src[/\\]visual_agent)",
    re.IGNORECASE,
)
_PACER_CORE_FILES = (
    "pacer_model.py",
    "wish_intake.py",
    "model_workflow.py",
    "subagent_orchestrator.py",
    "context_profile.py",
    "managed_checkpoint.py",
    "acceptance_discrimination.py",
    "command_verification.py",
)
_PATH_REPLY = re.compile(r'[^<>:"|?*\s\\/]+(?:[\\/][^<>:"|?*\s\\/]+)*')
_VAGUE_GOAL = re.compile(
    r"^(?:(?:请|麻烦)?(?:帮我)?(?:把)?(?:继续)?(?:这个|那个|这段|那段|它)?"
    r"(?:优化|改进|完善|修改|修复|调整|处理|改)(?:一下吧|一下|下吧|下|吧)?"
    r"(?:这个|那个|这段|那段)?(?:页面|界面|文件|代码|功能|项目|东西|部分|模块)?)$"
)


@dataclass
class WishDecision:
    proceed: bool
    goal: str
    question: str = ""
    files: list[str] = field(default_factory=list)
    resume: bool = False
    status: str = "ready"


HOST_MAX_STEPS = 50


def format_progress_frame(
    task_id: str,
    role: str,
    backend: str,
    message: str,
    remaining_steps: int,
    quota_note: str = "按订阅窗口",
) -> str:
    """One line the Code window can show while a hosted task is running."""
    summary = " ".join(str(message or "").split())[:180]
    return (
        f"[{task_id} {role} | 当前岗位: {backend} | 考题状态: {summary} "
        f"| 剩余托管步数: {max(0, int(remaining_steps))} | 配额剩余时间: {quota_note}]"
    )


def host_stop_reason(step: int, max_steps: int, exit_code: int, previous: str, current: str) -> str:
    """Decide whether a managed step may continue. Empty means run another step."""
    if exit_code == 0:
        return ""
    if exit_code == 2:
        return "需要你回答问题"
    if previous and current and previous == current:
        return "同一失败重复出现"
    if step + 1 >= max_steps:
        return "已达到本次托管步数"
    return ""


def append_live_log(workspace: str | Path, line: str) -> None:
    root = Path(workspace).expanduser().resolve()
    path = root / ".pacer" / "live.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip() + "\n")


def _include_referenced_spec(goal: str, root: Path) -> str:
    """Include an explicitly referenced, workspace-local wish file in the plan input."""
    if "[用户指定的许愿文件:" in goal:
        return goal
    if not _SPEC_CUE.search(goal):
        return goal
    for match in _QUOTED_SPEC_PATH.finditer(goal):
        raw_path = match.group(1).strip()
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError, RuntimeError):
            continue
        if not resolved.is_file() or resolved.suffix.lower() not in {".md", ".txt"}:
            continue
        try:
            contents = resolved.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if contents:
            rel_path = resolved.relative_to(root).as_posix()
            return (
                f"{goal}\n\n[用户指定的许愿文件: {rel_path}]\n"
                f"{contents[:18000]}"
            )
    return goal


def _is_pacer_self_request(goal: str) -> bool:
    return bool(_PACER_SELF_SCOPE.search(str(goal or "")) and _PACER_ACTION.search(goal))


def _pacer_core_candidates(root: Path) -> list[str]:
    """Anchor Pacer self-work to this installed source tree, not nearby demo folders."""
    package_dir = Path(__file__).resolve().parent
    try:
        package_dir.relative_to(root)
    except ValueError:
        return []
    return [
        (package_dir / filename).relative_to(root).as_posix()
        for filename in _PACER_CORE_FILES
        if (package_dir / filename).is_file()
    ]


def _is_file_path_question(question: str) -> bool:
    return "相对路径" in question or "要改哪个文件" in question


def _is_path_only_reply(text: str) -> bool:
    """Only a bare, workspace-relative filename can answer a file-path prompt."""
    candidate = str(text or "").strip().strip("`\"'")
    if not candidate or any(char.isspace() for char in candidate):
        return False
    if not _PATH_REPLY.fullmatch(candidate) or candidate.startswith(("/", "\\")):
        return False
    if re.match(r"^[A-Za-z]:", candidate):
        return False
    parts = re.split(r"[\\/]", candidate)
    if any(part in {".", ".."} for part in parts):
        return False
    return bool(re.search(r"\.[A-Za-z0-9]{1,12}$", parts[-1]))


def _clear_task_context(profile: ContextProfile) -> None:
    """Keep repository knowledge while dropping task-local state before a new goal."""
    profile.active_wish = ""
    profile.confirmed_decisions = []
    profile.open_questions = []
    profile.plan_items = []
    profile.current_contract = ""
    profile.last_failure = ""


def _needs_file_clarification(goal: str) -> bool:
    normalized = re.sub(r"[\s，,。.!！?？]+", "", str(goal or ""))
    if _VAGUE_GOAL.fullmatch(normalized):
        return True
    return bool(
        re.fullmatch(
            r"(?i)(?:please)?(?:fix|improve|update|refactor)(?:this|that|the|it)?"
            r"(?:page|screen|file|code|feature|module)?",
            normalized,
        )
    )


def resolve_wish(goal: str, workspace: str | Path) -> WishDecision:
    """Read the portrait and the latest sentence. Do not read a chat transcript."""
    root = Path(workspace).expanduser().resolve()
    text = " ".join(str(goal or "").split())
    manager = ContextProfileManager(root)
    profile = manager.load_profile() or manager.get_or_create_profile()
    checkpoint = load_managed_checkpoint(root)

    if text.lower() in _CONTINUATION or text in _CONTINUATION:
        if is_checkpoint_resumable(checkpoint):
            stored = profile.active_wish or (checkpoint.goal if checkpoint else "")
            stored = _include_referenced_spec(stored, root)
            files = _pacer_core_candidates(root) if _is_pacer_self_request(stored) else []
            if not files:
                files = discover_target_files_for_goal(stored, root)
            profile.open_questions = []
            manager.save_profile(profile)
            return WishDecision(True, stored or text, files=files, resume=True, status="ready")
        question = "画像里没有未完成的计划。请直接说出要改的文件和想要的结果。"
        profile.open_questions = [question]
        manager.save_profile(profile)
        return WishDecision(False, text, question=question, status="awaiting_input")

    working = text
    if profile.open_questions:
        pending = profile.open_questions[-1]
        if _is_file_path_question(pending) and _is_path_only_reply(text):
            profile.confirmed_decisions.append(text)
            profile.open_questions = []
            if profile.active_wish:
                working = f"{profile.active_wish}\n用户确认: {text}"
            else:
                profile.active_wish = text
        elif _is_file_path_question(pending) or _PACER_ACTION.search(text):
            # A new natural-language task must not inherit an old task's question,
            # goal, plan, or failure from the shared project portrait.
            _clear_task_context(profile)
            working = text
        else:
            profile.confirmed_decisions.append(text)
            profile.open_questions = []
            if profile.active_wish:
                working = f"{profile.active_wish}\n用户确认: {text}"
            else:
                profile.active_wish = text
    else:
        # Each request starts from the durable project portrait, not a prior task.
        _clear_task_context(profile)

    working = _include_referenced_spec(working, root)
    explicit_paths = re.search(r"[\w./\\-]+\.(?:py|ts|js|tsx|jsx)\b", working, re.I)
    recent_focus: list[str] = []
    if not explicit_paths:
        from .canvas_manager import CanvasManager
        for raw in CanvasManager(root).load().recent_focus_files:
            rel = str(raw).replace("\\", "/").strip()
            candidate = (root / rel).resolve()
            if not Path(rel).is_absolute() and ".." not in Path(rel).parts and candidate.is_relative_to(root) and candidate.is_file():
                recent_focus.append(rel)
        if recent_focus:
            working += "\n相关文件: " + ", ".join(recent_focus)
    implicit_followup = bool(re.search(r"刚才|刚刚|上一个", text)) and not explicit_paths
    if implicit_followup and not recent_focus:
        question = "没有可继续聚焦的上一轮目标文件；请重跑上一轮任务，或补充目标文件相对路径。"
        profile.active_wish = working
        profile.open_questions = [question]
        manager.save_profile(profile)
        return WishDecision(False, working, question=question, status="awaiting_input")
    files = _pacer_core_candidates(root) if _is_pacer_self_request(working) else []
    if not files:
        files = recent_focus or discover_target_files_for_goal(working, root)
    action = re.search(r"加|加上|补充|更新|删|删除|移除|调整|校验|规则|函数|模块|接口|单测|测试|跑通|刚才|上一个", text)
    if files or action or not _needs_file_clarification(working):
        profile.active_wish = working
        profile.open_questions = []
        profile.current_contract = ", ".join(files)
        manager.save_profile(profile)
        return WishDecision(True, working, files=files, status="ready")

    question = "要改哪个文件？请发相对路径，例如 greetings.py。这句话已经记在项目画像里，下一句只回答路径即可。"
    profile.active_wish = working
    profile.open_questions = [question]
    manager.save_profile(profile)
    return WishDecision(False, working, question=question, status="awaiting_input")
