from __future__ import annotations

from visual_agent.mission_contract import normalize_requirement_contract, requirement_contract_planning_answers


def test_requirement_contract_prefers_final_goal_and_dedupes_answers(tmp_path) -> None:
    contract = normalize_requirement_contract(
        {
            "source": "goal_intake",
            "input_goal": "改一下",
            "suggested_goal": "修复结算页 checkout 金额显示",
            "final_goal": "修复结算页 checkout 金额显示，并保持优惠展示",
            "answers": ["保留现有优惠展示", "保留现有优惠展示"],
            "acceptance_hint": "pytest checkout 测试通过",
            "clarifying_questions": ["完成后用户看到什么？", "完成后用户看到什么？"],
            "model_id": "codex:cli",
            "intake_policy": "selected_agent_cli",
            "model_unavailable": False,
        },
        goal="改一下",
        answers=["金额必须等于行项目之和", "保留现有优惠展示"],
        repo_root=tmp_path,
        test_command="python -m pytest -q",
        agent="codex",
    )

    assert contract["input_goal"] == "改一下"
    assert contract["suggested_goal"] == "修复结算页 checkout 金额显示"
    assert contract["final_goal"] == "修复结算页 checkout 金额显示，并保持优惠展示"
    assert contract["answers"] == ["保留现有优惠展示", "金额必须等于行项目之和"]
    assert contract["clarifying_questions"] == ["完成后用户看到什么？"]
    assert contract["model_id"] == "codex:cli"
    assert contract["intake_policy"] == "selected_agent_cli"
    assert contract["model_unavailable"] is False
    assert contract["repo_root"] == str(tmp_path)
    assert contract["test_command"] == "python -m pytest -q"
    assert contract["agent"] == "codex"

    planning_answers = requirement_contract_planning_answers(contract)
    assert "原始目标：改一下" in planning_answers
    assert "模型建议目标：修复结算页 checkout 金额显示" in planning_answers
    assert "用户补充：金额必须等于行项目之和" in planning_answers
    assert "验收提示：pytest checkout 测试通过" in planning_answers
    assert "验收命令：python -m pytest -q" in planning_answers


def test_requirement_contract_omits_plain_goal_without_intake() -> None:
    assert normalize_requirement_contract(None, goal="修复结算页金额") == {}
