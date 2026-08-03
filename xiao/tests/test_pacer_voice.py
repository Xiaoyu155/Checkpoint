from __future__ import annotations

from visual_agent.chief_dispatch import build_worker_prompt
from visual_agent.chief_run import _message_for_stop, chief_run_to_markdown
from visual_agent.pacer_voice import (
    agent_completion_debate_block,
    user_message_for_stop,
    user_story,
)


def test_user_story_provider_5xx_is_not_code_blame() -> None:
    story = user_story(stop_reason="provider_5xx")
    assert story["is_code_problem"] is False
    assert "代码" in story["headline"] or "服务" in story["headline"]
    text = user_message_for_stop("provider_5xx")
    assert "agents doctor" not in text
    assert "503" in text or "5xx" in text or "服务" in text


def test_user_story_verified_offers_merge_choice() -> None:
    story = user_story(stop_reason="verified", product_change_count=2)
    assert any("合并" in item for item in story["choices"])


def test_message_for_stop_uses_friend_voice() -> None:
    text = _message_for_stop("needs_clarification")
    assert "听懂" in text or "确认" in text or "白话" in text


def test_markdown_leads_with_human_section() -> None:
    md = chief_run_to_markdown(
        {
            "status": "stopped",
            "stop_reason": "provider_5xx",
            "message": "",
            "mission": {"mission_id": "m1", "objective": "修登录"},
            "plan": {"status": "ready"},
            "rounds": [],
            "dispatch": {},
        }
    )
    assert md.index("跟你说人话") < md.index("结论")
    assert "503" in md or "服务" in md or "5xx" in md


def test_worker_prompt_is_debate_not_handcuff() -> None:
    prompt = build_worker_prompt(
        plan={
            "objective": "Add multiply",
            "plan_id": "p1",
            "acceptance_criteria": ["tests pass"],
            "selected_workflows": [],
            "changed_files": ["calc.py"],
        },
        track={"id": "t1", "agent": "codex"},
        worktree=__import__("pathlib").Path("."),
        verification_command="python -m pytest -q",
        dispatch_mode="tracked",
    )
    assert "completion debate" in prompt.lower() or "completion debate" in "\n".join(
        agent_completion_debate_block(verification_command="python -m pytest -q")
    ).lower() or "When you are ready to claim" in prompt
    assert "do not scan" not in prompt.lower()
    assert "conserve" not in prompt.lower() or "budget" not in prompt.lower()
    assert "guidance only" in prompt.lower() or "not a whitelist" in prompt.lower()
    assert "micromanage" in prompt.lower() or "autonomy" in prompt.lower()
