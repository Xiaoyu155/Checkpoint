from __future__ import annotations

from visual_agent import goal_intake
from visual_agent.goal_intake import intake_dialogue_lines, intake_to_markdown, refine_goal


def test_vague_goal_deterministic_fallback():
    r = refine_goal("改一下", enable_model=False)
    assert r["source"] == "deterministic"
    assert r["already_clear"] is False
    assert r["clarifying_questions"]


def test_clear_goal_is_marked_clear():
    r = refine_goal("把 src/calc.py 的 add 改成返回 a+b，让 pytest 通过", enable_model=False)
    assert r["already_clear"] is True


def test_mobile_install_goal_gets_domain_questions_without_model():
    r = refine_goal("把已经做好的元思轻语app通过数据线传输到我手机上", enable_model=False)
    text = "\n".join(r["clarifying_questions"]) + "\n" + r["acceptance_hint"]
    assert r["already_clear"] is False
    assert "Android" in text
    assert "adb install -r" in text
    assert "USB 调试" in text


def test_empty_goal_is_safe():
    r = refine_goal("   ", enable_model=False)
    assert r["source"] == "deterministic"
    assert r["suggested_goal"] == ""


def test_model_failure_degrades_gracefully():
    # A bogus model id must not raise; it falls back and records the error.
    r = refine_goal("改一下", model_id="definitely-not-a-model", enable_model=True)
    assert r["source"] == "deterministic"
    assert r.get("model_error")
    assert r["already_clear"] is False
    assert r.get("model_unavailable") is True


def test_model_path_parses_json(monkeypatch):
    def fake_call(goal, *, answers, model_id, timeout_seconds, **kwargs):
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


def test_auto_intake_backend_reads_credentials(tmp_path, monkeypatch):
    cred = tmp_path / "model_api_keys.txt"
    fake_deepseek_key = "sk-" + "x" * 32
    fake_mimo_key = "tp-" + "z" * 32
    cred.write_text(
        f"deepseek {fake_deepseek_key}\nmimo {fake_mimo_key}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CHECKPOINT_MODEL_CREDENTIALS", str(cred))
    from visual_agent.goal_intake import auto_intake_backend

    backend = auto_intake_backend()
    assert backend is not None
    # DeepSeek is preferred over MiMo for intake.
    assert backend["model_id"] == "deepseek:deepseek-chat"
    assert backend["api_key"].startswith("sk-")
    assert "deepseek.com" in backend["base_url"]


def test_auto_intake_backend_none_without_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_MODEL_CREDENTIALS", str(tmp_path / "nope.txt"))
    monkeypatch.chdir(tmp_path)
    from visual_agent.goal_intake import auto_intake_backend

    # The explicit override is exclusive, so the repo's own credential file
    # must NOT be silently picked up.
    assert auto_intake_backend() is None


def test_auto_intake_backend_prefers_mimo_environment_token(tmp_path, monkeypatch):
    token = "tp-new-mimo-plan-token-1234567890"
    monkeypatch.setenv("CHECKPOINT_MIMO_TOKEN", token)
    monkeypatch.setenv("CHECKPOINT_MODEL_CREDENTIALS", str(tmp_path / "missing.txt"))

    backend = goal_intake.resolve_cheap_backend(("mimo",))

    assert backend is not None
    assert backend["model_id"] == "xiaomimimo:mimo-v2.5"
    assert backend["api_key"] == token


def test_intake_markdown_renders():
    r = refine_goal("改一下", enable_model=False)
    text = intake_to_markdown(r)
    assert "目标接待" in text
    assert "我还需要确认这些点" in text


def test_intake_dialogue_lines_reflect_answers_and_suggestion():
    payload = {
        "already_clear": False,
        "clarifying_questions": ["哪个页面?", "完成标志是什么?"],
        "suggested_goal": "把登录页的按钮文案改成“继续”",
        "input_goal": "改一下登录页",
        "acceptance_hint": "打开登录页确认按钮文案已变更",
    }
    lines = intake_dialogue_lines(payload, answers=["先改登录页"])
    assert "你刚才补充了：" in lines
    assert any("哪个页面?" in line for line in lines)
    assert any("建议改写：" in line for line in lines)
    assert any("建议验收：" in line for line in lines)


def test_intake_dialogue_lines_use_chinese_fallback_questions():
    payload = {
        "already_clear": False,
        "clarifying_questions": [
            "这个目标完成后，用户能看到什么可验证结果？请给出具体文字、数字或状态。",
            "这次修改会影响哪个页面、屏幕或文件？",
            "有什么内容绝对不能改坏？",
        ],
    }
    lines = intake_dialogue_lines(payload)
    assert any("可验证结果" in line for line in lines)
    assert any("哪个页面、屏幕或文件" in line for line in lines)
    assert any("不能改坏" in line for line in lines)
