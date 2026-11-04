from pathlib import Path

from visual_agent.subagent_orchestrator import generate_subagent_plan
from visual_agent.wish_intake import host_stop_reason, resolve_wish


def test_vague_wish_asks_and_leaves_files_untouched(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("x = 1\n", encoding="utf-8")
    before = source.read_text(encoding="utf-8")

    decision = resolve_wish("优化一下", tmp_path)

    assert decision.proceed is False
    assert "哪个文件" in decision.question
    assert source.read_text(encoding="utf-8") == before
    portrait = (tmp_path / ".pacer" / "context_profile.md").read_text(encoding="utf-8")
    assert "优化一下" in portrait
    assert "哪个文件" in portrait


def test_answer_to_open_question_locks_the_named_file(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    first = resolve_wish("优化一下", tmp_path)
    assert first.proceed is False

    second = resolve_wish("app.py", tmp_path)

    assert second.proceed is True
    assert second.files == ["app.py"]
    assert "优化一下" in second.goal
    assert "app.py" in second.goal


def test_plan_without_a_matching_test_does_not_run_the_whole_suite(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    plan = generate_subagent_plan("修改 app.py 的返回值", repo_root=tmp_path, fast_local_plan=True, gemini_available=False)
    assert plan.tasks
    assert all("pytest" not in (task.verification_command or "") for task in plan.tasks)


def test_host_stops_on_a_repeated_failure() -> None:
    assert host_stop_reason(0, 3, 1, "same error", "same error") == "同一失败重复出现"
    assert host_stop_reason(0, 3, 0, "", "") == ""
    assert host_stop_reason(0, 1, 1, "", "new") == "已达到本次托管步数"


def test_continue_without_checkpoint_asks(tmp_path: Path) -> None:
    decision = resolve_wish("继续", tmp_path)
    assert decision.proceed is False
    assert decision.resume is False
    assert "没有未完成" in decision.question
