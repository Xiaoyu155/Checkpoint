from __future__ import annotations

from visual_agent import goal_intake
from visual_agent.goal_intake import intake_to_markdown, refine_goal


def test_vague_goal_deterministic_fallback():
    r = refine_goal("改一下", enable_model=False)
    assert r["source"] == "deterministic"
    assert r["already_clear"] is False
    assert r["clarifying_questions"]


def test_clear_goal_is_marked_clear():
    r = refine_goal("把 src/calc.py 的 add 改成返回 a+b，让 pytest 通过", enable_model=False)
    assert r["already_clear"] is True


def test_empty_goal_is_safe():
    r = refine_goal("   ", enable_model=False)
    assert r["source"] == "deterministic"
    assert r["suggested_goal"] == ""


def test_model_failure_degrades_gracefully():
    # A bogus model id must not raise; it falls back and records the error.
    r = refine_goal("改一下", model_id="definitely-not-a-model", enable_model=True)
    assert r["source"] == "deterministic"
    assert r.get("model_error")


def test_model_path_parses_json(monkeypatch):
    def fake_call(goal, *, answers, model_id, timeout_seconds):
        return (
            'Here you go: {"clarifying_questions": ["哪个文件?"], '
            '"suggested_goal": "修复 calc.py 的 add 使其返回 a+b", '
            '"acceptance_hint": "运行 pytest 确认通过"}'
        )

    monkeypatch.setattr(goal_intake, "_call_intake_model", fake_call)
    r = refine_goal("改一下 add", enable_model=True)
    assert r["source"] == "model"
    assert r["clarifying_questions"] == ["哪个文件?"]
    assert "a+b" in r["suggested_goal"]
    assert "pytest" in r["acceptance_hint"]


def test_intake_markdown_renders():
    r = refine_goal("改一下", enable_model=False)
    text = intake_to_markdown(r)
    assert "目标接待" in text
    assert "建议先回答" in text
