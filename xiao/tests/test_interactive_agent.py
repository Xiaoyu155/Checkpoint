from __future__ import annotations

import json

from visual_agent.interactive_agent import run_interactive_agent


def test_interactive_agent_keeps_context_across_spoken_tasks(tmp_path) -> None:
    inputs = iter(["/provider subscription", "修复登录错误", "/provider relay custom", "继续补测试", "/状态", "/退出"])
    outputs: list[str] = []
    goals: list[str] = []
    providers: list[str] = []

    def fake_task(goal, **kwargs):
        goals.append(goal)
        providers.append(kwargs["codex_provider"])
        return {
            "status": "completed",
            "program_id": f"program-{len(goals)}",
            "tasks": [{"task_id": "task-001", "mission_id": f"mission-{len(goals)}", "status": "verified", "reason": ""}],
        }

    code = run_interactive_agent(
        repo_root=tmp_path,
        workspace_root=tmp_path / ".agent-workspace",
        task_runner=fake_task,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert goals[0] == "修复登录错误"
    assert "上一任务：修复登录错误" in goals[1]
    assert "上一 Program：program-1" in goals[1]
    assert providers == ["openai", "custom"]
    assert any("Codex 用户订阅" in line for line in outputs)
    assert any("Codex 中转 provider custom" in line for line in outputs)
    assert any("Program program-2" in line for line in outputs)
    session = next((tmp_path / ".agent-workspace" / "sessions").glob("*.jsonl"))
    events = [json.loads(line) for line in session.read_text(encoding="utf-8").splitlines()]
    assert [event["type"] for event in events] == [
        "provider_selected",
        "user_task",
        "task_result",
        "provider_selected",
        "user_task",
        "task_result",
    ]


def test_interactive_agent_answers_capability_question_without_dispatch(tmp_path) -> None:
    inputs = iter(["可以对我的页面板块进行优化和开发吗", "/退出"])
    outputs: list[str] = []
    calls: list[str] = []

    def fake_task(goal, **_kwargs):
        calls.append(goal)
        return {"status": "completed", "program_id": "unexpected", "tasks": []}

    code = run_interactive_agent(
        repo_root=tmp_path,
        workspace_root=tmp_path / ".agent-workspace",
        task_runner=fake_task,
        input_func=lambda _prompt: next(inputs),
        output_func=outputs.append,
    )

    assert code == 0
    assert calls == []
    assert any("不会把询问句直接当成开发命令" in line for line in outputs)
    assert not (tmp_path / ".agent-workspace" / "programs").exists()
