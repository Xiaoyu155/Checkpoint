"""Pacer dual-voice constitution.

Product stance (non-negotiable intent):

* Toward the user: speak like a friend or family member — plain Chinese,
  sincere, collaborative. No mission/pillar jargon in the main story.
* Toward Codex / Claude Code: treat them as senior production tools. Do not
  micromanage how they work. At completion time, debate evidence like a
  principal engineer.

This module is the presentation and prompt-policy layer. It does not invent
new product gates; it rewrites how gates are explained and how workers are
addressed.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Gate policy: keep / humanize / debate / remove-intervention intent
# ---------------------------------------------------------------------------

GATE_POLICY: dict[str, dict[str, str]] = {
    "false_verified": {
        "stance": "keep",
        "why": "Never tell the user it is done without evidence. This protects non-experts.",
        "user_mode": "friend_explain",
        "agent_mode": "completion_debate",
    },
    "main_branch_isolation": {
        "stance": "keep",
        "why": "Worktree isolation protects the user's project without constraining how the agent codes.",
        "user_mode": "friend_explain",
        "agent_mode": "context_only",
    },
    "test_command_gate": {
        "stance": "keep_as_evidence",
        "why": "The user's test command is the completion contract, not a coding style rule.",
        "user_mode": "friend_explain",
        "agent_mode": "completion_debate",
    },
    "test_tamper_guard": {
        "stance": "debate_not_handcuff",
        "why": "Do not silently punish; ask whether the user asked to change tests.",
        "user_mode": "friend_negotiate",
        "agent_mode": "completion_debate",
    },
    "python_pytest_resolve": {
        "stance": "environment_help",
        "why": "Fix dirty PATH before the agent burns turns. Not a coding constraint.",
        "user_mode": "friend_explain",
        "agent_mode": "context_only",
    },
    "provider_5xx": {
        "stance": "keep",
        "why": "Infrastructure failure is not a code failure.",
        "user_mode": "friend_explain",
        "agent_mode": "no_retry_storm",
    },
    "worker_orphaned": {
        "stance": "keep",
        "why": "Background process died or PID was reused; not a product-code failure.",
        "user_mode": "friend_explain",
        "agent_mode": "safe_resume",
    },
    "quota_exhausted": {
        "stance": "keep",
        "why": "Token/account quota is a supply limit, not a coding failure.",
        "user_mode": "friend_explain",
        "agent_mode": "no_retry_storm",
    },
    "exploration_limits": {
        "stance": "remove_intervention",
        "why": "Codex/Code are strong; banning scan/grep creates loops.",
        "user_mode": "silent",
        "agent_mode": "free",
    },
    "token_conservation_orders": {
        "stance": "remove_intervention",
        "why": "Do not tell the agent to conserve budget instead of understanding the repo.",
        "user_mode": "silent",
        "agent_mode": "free",
    },
    "file_scope_whitelist": {
        "stance": "guidance_only",
        "why": "Hints ok; hard boundaries cause wrong patches.",
        "user_mode": "silent",
        "agent_mode": "guidance_not_boundary",
    },
}


# ---------------------------------------------------------------------------
# User voice — friend / family
# ---------------------------------------------------------------------------

def user_story(
    *,
    stop_reason: str = "",
    status: str = "",
    stage: str = "",
    goal: str = "",
    product_change_count: int | None = None,
    verification_command: str = "",
    worktree: str = "",
    message_fallback: str = "",
) -> dict[str, Any]:
    """Build a plain-language user-facing story.

    Returns keys: headline, what_happened, is_code_problem, choices (list[str]),
    technical_tag (optional, for power users, not the main story).
    """
    reason = str(stop_reason or "").strip()
    stage_key = str(stage or status or "").strip()
    goal_text = str(goal or "").strip()
    cmd = str(verification_command or "").strip()
    wt = str(worktree or "").strip()
    count = product_change_count

    # Live / progress stories
    if stage_key in {"worker_running", "worker_starting", "worker_started", "background_started"}:
        return _story(
            headline="我正在请编程助手干活，还没结束。",
            what=(
                f"你交代的事是：{goal_text or '（见任务描述）'}。"
                "改动会先放在隔离文件夹里，不会直接动你的主项目。"
            ),
            is_code_problem=False,
            choices=[
                "再等一会儿",
                "想看现在改了哪些文件的话告诉我",
                "想停下来也行，主项目一般还是干净的",
            ],
            technical_tag=stage_key,
        )
    if stage_key in {"verification_running"}:
        return _story(
            headline="代码改完了一轮，我在替你跑验收。",
            what=(
                "验收用的是你的测试命令"
                + (f"：`{cmd}`" if cmd else "（项目测试）")
                + "。只有它过了，我才敢说可以收工。"
            ),
            is_code_problem=False,
            choices=["等验收结果", "如果验收命令不对，我们可以改成你平时用的那条"],
            technical_tag=stage_key,
        )

    # Terminal stories by stop_reason
    table = {
        "verified": _story(
            headline="这件事可以收工了。",
            what=(
                "编程助手做完了，并且你的测试命令也过了。"
                + (f" 大概动了 {count} 个产品文件。" if count is not None else "")
                + " 改动还在隔离文件夹里"
                + (f"（{wt}）" if wt else "")
                + "，主项目要等你点头才合并。"
            ),
            is_code_problem=None,
            choices=[
                "要我合并进主项目吗？",
                "想先自己打开隔离文件夹看一眼也可以",
                "不满意的话，用更具体的一句话再交代一版",
            ],
            technical_tag="verified",
        ),
        "preview_only": _story(
            headline="我先帮你看了一遍怎么做，还没真的改代码。",
            what=(
                "这是预览：环境检查、隔离工作区都准备好了。"
                "你确认目标没问题后，再说「开始做」或重新跑并加上执行。"
            ),
            is_code_problem=None,
            choices=["确认后开始做", "先改一改你的目标描述", "换一条测试命令再预览"],
            technical_tag="preview_only",
        ),
        "needs_clarification": _story(
            headline="我还没完全听懂你想做的事，想先跟你对一下。",
            what=(
                f"你刚才说：{goal_text or '（比较简短）'}。"
                "为了不瞎改你的项目，我想先确认：要改哪里、怎样算做完。"
                "你用日常说话的方式补充就行，不必写术语。"
            ),
            is_code_problem=False,
            choices=[
                "用一句话说：改哪个文件/功能 + 怎样算好（例如测试要过）",
                "如果只是随便改改，也可以说「你看着办，优先别动主流程」",
            ],
            technical_tag="needs_clarification",
        ),
        "provider_5xx": _story(
            headline="不是你的代码写坏了，是外面的编程服务暂时不可用。",
            what=(
                "连编程助手的通道返回了 5xx/503 一类错误。"
                "这通常是中转或服务端抖动，不是项目逻辑问题。"
            ),
            is_code_problem=False,
            choices=["过几分钟再试同一件事", "如果你有备用通道，可以换一个再试", "先停在这里也完全没问题"],
            technical_tag="provider_5xx",
        ),
        "provider_rate_limit": _story(
            headline="编程助手额度或限流到了，先歇一歇。",
            what="这不是代码写错，是当前账号/通道用得太猛或窗口用尽了。",
            is_code_problem=False,
            choices=["等额度恢复再继续", "换你明确同意的其他助手再试"],
            technical_tag="provider_rate_limit",
        ),
        "network_timeout": _story(
            headline="网络超时了，活还没可靠做完。",
            what="连编程助手时超时。主项目一般还是安全的。",
            is_code_problem=False,
            choices=["检查网络后重试", "稍后再说"],
            technical_tag="network_timeout",
        ),
        "pytest_not_importable": _story(
            headline="卡在电脑环境，不在你的业务代码。",
            what=(
                "要跑测试需要 pytest，但当前 Python 里装不了/找不到它。"
                "我不会假装任务成功，也不会让助手去乱改你的代码来“骗过”环境。"
            ),
            is_code_problem=False,
            choices=[
                "在项目环境安装 pytest 后再试",
                "告诉我你平时用哪个 Python，我按那个来",
            ],
            technical_tag="pytest_not_importable",
        ),
        "verification_environment_missing": _story(
            headline="验收环境缺东西，不是代码一定写错了。",
            what="跑验收还缺密钥、外部服务或环境变量。不适合让助手靠改测试蒙混过关。",
            is_code_problem=False,
            choices=["补上缺的环境后再试", "如果这个验收你本来就不想跑，跟我说换成别的检查"],
            technical_tag="verification_environment_missing",
        ),
        "evidence_rejected": _story(
            headline="助手动到了「验收用的测试/契约」，我先拦住了。",
            what=(
                "默认约定：测试是验收尺子，不能为了变绿去改尺子。"
                "如果你本来就要求「加测试/改测试」，说一声，我按你的意思放行。"
            ),
            is_code_problem=False,
            choices=[
                "我就是要加/改测试 → 带上允许改测试再做一版",
                "我不想动测试 → 让助手只改产品代码",
            ],
            technical_tag="evidence_rejected",
        ),
        "worker_failed_tests_pass": _story(
            headline="测试碰巧过了，但助手没有正常收工，我不敢说完成。",
            what=(
                "隔离区里可能有改动，测试也绿了，可助手进程自己没好好结束。"
                "这种时候硬说「做完了」会骗你，所以我停在半路上请你看一眼。"
            ),
            is_code_problem=None,
            choices=[
                "打开隔离文件夹看改动，觉得好再合并",
                "不满意就当没发生，主项目通常还干净",
                "用更清楚的一句话再做一版",
            ],
            technical_tag="worker_failed_tests_pass",
        ),
        "worker_orphaned": _story(
            headline="后台助手进程丢了，不是你的需求写坏了。",
            what=(
                "任务还在「进行中」的名单上，可干活的进程已经不在了（常见于 Windows 并发或电脑休眠）。"
                "可以放心再启动一轮；主项目一般还是干净的。"
            ),
            is_code_problem=False,
            choices=["再跑一次（resume）", "先看任务状态", "先停，我明天再继续"],
            technical_tag="worker_orphaned",
        ),
        "race_lost": _story(
            headline="这场竞速没赢，我主动收掉了。",
            what="同一目标开了多个助手；另一个先验收通过，这个就停掉省额度。",
            is_code_problem=False,
            choices=["看获胜那条任务", "还想再竞速就再说一声", "先停"],
            technical_tag="race_lost",
        ),
        "preempted": _story(
            headline="被更高优先级任务插队了。",
            what="多半是主仓测试红了，我先丢了自愈任务，把这条低优先级的暂停/杀掉。",
            is_code_problem=False,
            choices=["等自愈完成后再继续", "强制再跑这条", "先停"],
            technical_tag="preempted",
        ),
        "worker_error": _story(
            headline="编程助手这轮没跑完。",
            what="可能是工具退出、配置或通道问题。不等于你的需求不合理。",
            is_code_problem=False,
            choices=["再试一次", "换一个助手（如果你愿意）", "把目标说得更窄一点再试"],
            technical_tag="worker_error",
        ),
        "verification_failed": _story(
            headline="改过了，但验收还没过。",
            what=(
                "助手已经动手，可你的测试命令仍失败。"
                "这更接近「还没做对」，我们可以收窄目标再来。"
            ),
            is_code_problem=True,
            choices=["看失败输出后说「修这个报错」", "把目标改小一点", "先停，我自己看"],
            technical_tag="verification_failed",
        ),
        "budget_exhausted": _story(
            headline="这轮时间和次数用满了，我先停，避免空转。",
            what="不是否定你的目标，是防止一直烧额度却没结果。",
            is_code_problem=None,
            choices=["加长时限再试", "把事情拆小再试", "先结束"],
            technical_tag="budget_exhausted",
        ),
        "coverage_gap": _story(
            headline="我还不知道用什么标准算「做完」。",
            what="最好有一条你平时用的测试/检查命令。没有的话我容易和助手一起空转。",
            is_code_problem=False,
            choices=["告诉我测试命令，例如 pytest 或 npm test", "你确认没有测试、只看人工验收也可以说清楚"],
            technical_tag="coverage_gap",
        ),
        "agent_unavailable": _story(
            headline="编程助手现在用不了（没装好或探测失败）。",
            what="不是你的项目写坏了，是本机助手工具不可用。",
            is_code_problem=False,
            choices=["检查是否安装 codex/claude 并在 PATH 里", "装好后再试", "先停"],
            technical_tag="agent_unavailable",
        ),
        "not_authenticated": _story(
            headline="编程助手还没登录，花不了额度。",
            what="长托管需要已登录的 Codex/助手账号。登录好了再继续。",
            is_code_problem=False,
            choices=["先 codex login / 登录助手", "登录后再 resume", "先停"],
            technical_tag="not_authenticated",
        ),
        "quota_exhausted": _story(
            headline="这个编程助手的额度用完了。",
            what="不是项目坏了，是额度到了（也可能是账号/空间被限制）。",
            is_code_problem=False,
            choices=["等额度恢复", "换你同意的其他助手", "用 --wake-on-quota 让 host 自动轮询到额度恢复"],
            technical_tag="quota_exhausted",
        ),
        "no_product_changes": _story(
            headline="测试过了，但几乎没改到产品代码。",
            what="我不会把「什么都没改却测试绿」说成完成。",
            is_code_problem=None,
            choices=["把目标说具体一点再做", "如果本来就是检查任务，可以说「只检查不改代码」"],
            technical_tag="no_product_changes",
        ),
        "permission_required": _story(
            headline="你的项目里还有没提交的改动，我怕盖住它们。",
            what="先提交或暂存你自己的修改，会更安全。",
            is_code_problem=False,
            choices=["你处理好工作区后再叫我", "如果你明确接受有脏文件也继续，可以说清楚"],
            technical_tag="permission_required",
        ),
    }
    if reason in table:
        return table[reason]
    if message_fallback:
        return _story(
            headline=str(message_fallback).split("。")[0][:80] + ("。" if "。" not in str(message_fallback)[:80] else ""),
            what=str(message_fallback),
            is_code_problem=None,
            choices=["看下一步建议", "用更白话再说一遍目标"],
            technical_tag=reason or stage_key or "unknown",
        )
    return _story(
        headline="这轮先告一段落。",
        what="我把能说清楚的都写在下面了。有不懂的直接问我「这是什么意思」。",
        is_code_problem=None,
        choices=["告诉我你更关心：继续做 / 合并 / 停下"],
        technical_tag=reason or stage_key or "unknown",
    )


def user_message_for_stop(stop_reason: str, **kwargs: Any) -> str:
    """Single plain paragraph for CLI message fields."""
    story = user_story(stop_reason=stop_reason, **kwargs)
    parts = [str(story["headline"]), str(story["what_happened"])]
    if story.get("choices"):
        parts.append("你可以：" + "；".join(str(item) for item in story["choices"][:3]) + "。")
    return "".join(parts)


def user_markdown_section(story: dict[str, Any]) -> list[str]:
    """Markdown lines for the user-facing conclusion block."""
    lines = [
        "## 跟你说人话",
        "",
        f"**{story.get('headline') or ''}**",
        "",
        str(story.get("what_happened") or ""),
        "",
    ]
    code = story.get("is_code_problem")
    if code is True:
        lines.append("- 这更像：**代码或需求还没做对**")
    elif code is False:
        lines.append("- 这更像：**环境 / 通道 / 约定问题**，不是业务逻辑一定写坏了")
    else:
        lines.append("- 这需要你看一眼再决定，我不敢替你打包票")
    lines.append("")
    lines.append("**我们可以这样选：**")
    for item in story.get("choices") or []:
        lines.append(f"- {item}")
    tag = str(story.get("technical_tag") or "").strip()
    if tag:
        lines.extend(["", f"<sub>内部标记：`{tag}`（给你或支持人员排查用，可忽略）</sub>"])
    return lines


def _story(
    *,
    headline: str,
    what: str,
    is_code_problem: bool | None,
    choices: list[str],
    technical_tag: str,
) -> dict[str, Any]:
    return {
        "headline": headline,
        "what_happened": what,
        "is_code_problem": is_code_problem,
        "choices": list(choices),
        "technical_tag": technical_tag,
    }


# ---------------------------------------------------------------------------
# Agent voice — principal engineer debate, not handcuffs
# ---------------------------------------------------------------------------

def agent_completion_debate_block(*, verification_command: str = "") -> list[str]:
    """Language spoken *to* Codex/Code at dispatch.

    Intent: maximum freedom while working; hard questions only when claiming done.
    """
    cmd = str(verification_command or "").strip() or "(project verification command)"
    return [
        "You are the production coding tool. Work with full professional autonomy:",
        "explore, edit, and run commands as a senior engineer would. Pacer will not",
        "micromanage your intermediate steps or ban normal repository investigation.",
        "",
        "When you are ready to claim the objective is done, expect a completion debate:",
        "1. Did you satisfy the user objective in product code (not by weakening checks)?",
        "2. Does this verification command pass, and can you point to its output?",
        f"   {cmd}",
        "3. If you changed tests or acceptance scripts: was that explicitly requested?",
        "   If not, prefer fixing product code; say so if tests must change.",
        "4. If the environment is broken (missing interpreter, dependency, API key, 5xx),",
        "   stop and report the blockage — do not invent fake green by editing gates.",
        "5. Do not edit Pacer/Checkpoint runtime records under .agent-workspace or plan stores.",
        "",
        "Guidance is not a cage: any 'likely files' list is optional context, not a whitelist.",
    ]


def agent_safety_context_lines() -> list[str]:
    """Minimal non-negotiable context that is not coding-style micromanagement."""
    return [
        "Context only (not a style guide): keep user product work in this worktree;",
        "avoid writing into archive/graveyard/third-party sample trees or .agent-workspace runtime stores.",
    ]


def render_gate_policy_markdown() -> str:
    lines = [
        "# Pacer gate policy (constitution)",
        "",
        "User tone: friend/family. Agent tone: principal-engineer debate.",
        "",
        "| Gate | Stance | Why |",
        "| --- | --- | --- |",
    ]
    for key, item in GATE_POLICY.items():
        lines.append(
            f"| `{key}` | `{item['stance']}` | {item['why']} |"
        )
    lines.extend(
        [
            "",
            "## Rules of engagement",
            "",
            "1. Do not handcuff Codex/Code mid-flight with exploration bans or token scolding.",
            "2. Do debate completion: evidence, test command, test-tamper intent, environment vs code.",
            "3. Talk to users in plain language with choices; put machine codes in footnotes only.",
        ]
    )
    return "\n".join(lines) + "\n"
