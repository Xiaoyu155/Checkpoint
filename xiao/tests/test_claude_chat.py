"""C-layer chat window: headless turn driving and exact-boundary rotation."""

from __future__ import annotations

import json
import os
import sys
import textwrap

import pytest

from visual_agent.claude_chat import (
    ChatState,
    TurnResult,
    build_chat_handoff,
    fold_event,
    headless_argv,
    run_claude_chat,
    run_turn,
    status_text,
)


class TestHeadlessArgv:
    def test_fresh_session_has_no_resume(self):
        argv = headless_argv(["--permission-mode", "bypassPermissions"], resume_id="", prompt="hi")
        assert "--resume" not in argv
        assert argv[-1] == "hi"
        assert "-p" in argv and "stream-json" in argv and "--verbose" in argv

    def test_resume_session_carries_id(self):
        argv = headless_argv([], resume_id="sess-1", prompt="hi")
        assert argv[argv.index("--resume") + 1] == "sess-1"


class TestFoldEvent:
    def test_text_and_tool_progress(self):
        result = TurnResult()
        progress = fold_event(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}},
                    ]
                },
            },
            result,
        )
        assert progress == "  · Edit a.py"
        assert result.touched == ["a.py"]

    def test_result_event_sums_usage_and_session(self):
        result = TurnResult()
        fold_event(
            {
                "type": "result",
                "subtype": "success",
                "session_id": "sess-9",
                "result": "都做完了",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_creation_input_tokens": 3,
                    "cache_read_input_tokens": 4,
                },
            },
            result,
        )
        assert result.session_id == "sess-9"
        assert result.tokens == 10
        assert result.text == "都做完了"
        assert result.ok

    def test_error_result_is_captured(self):
        result = TurnResult()
        fold_event({"type": "result", "subtype": "error_during_execution", "is_error": True}, result)
        assert not result.ok


class TestChatState:
    def test_record_turn_accumulates_and_caps_history(self):
        state = ChatState()
        for index in range(20):
            state.record_turn(f"msg{index}", TurnResult(session_id="s", tokens=5, touched=["f.py"]))
        assert state.session_tokens == 100
        assert len(state.history) == 12
        assert state.touched == ["f.py"]

    def test_handoff_contains_goal_files_and_recent(self):
        state = ChatState()
        state.record_turn("把登录页做出来", TurnResult(text="做完了", touched=["src/login.vue"]))
        handoff = build_chat_handoff(state)
        assert "把登录页做出来" in handoff
        assert "src/login.vue" in handoff
        assert "做完了" in handoff

    def test_status_text_mentions_budget(self):
        state = ChatState(session_id="sess-1", session_tokens=2_000_000, rotations=1)
        text = status_text(state, 10_000_000)
        assert "2.0M / 10M" in text
        assert "sess-1" in text


_FAKE_STREAM_CLAUDE = textwrap.dedent(
    """
    import json, os, sys
    state = os.environ["FAKE_STATE"]
    runs_file = os.path.join(state, "runs.txt")
    n = int(open(runs_file).read()) + 1 if os.path.exists(runs_file) else 1
    open(runs_file, "w").write(str(n))
    with open(os.path.join(state, f"argv-{n}.json"), "w", encoding="utf-8") as f:
        json.dump(sys.argv[1:], f, ensure_ascii=False)
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/app.py"}}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "第%d回合完成" % n}]}},
        {"type": "result", "subtype": "success", "session_id": "sess-%d" % n,
         "result": "第%d回合完成" % n,
         "usage": {"input_tokens": 10, "output_tokens": 10,
                    "cache_read_input_tokens": 2000000, "cache_creation_input_tokens": 0}},
    ]
    for event in events:
        print(json.dumps(event, ensure_ascii=False))
    """
)


def _install_fake_claude(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    script = tmp_path / "fake_stream_claude.py"
    script.write_text(_FAKE_STREAM_CLAUDE, encoding="utf-8")
    monkeypatch.setenv("FAKE_STATE", str(state))
    return state, script


class TestRunTurn:
    def test_real_subprocess_stream_parsing(self, tmp_path, monkeypatch):
        state, script = _install_fake_claude(tmp_path, monkeypatch)
        progress: list[str] = []
        result = run_turn(
            sys.executable,
            [str(script)],
            prompt="hi",
            resume_id="",
            on_progress=progress.append,
        )
        assert result.ok
        assert result.session_id == "sess-1"
        assert result.tokens == 2_000_020
        assert result.text == "第1回合完成"
        assert result.touched == ["src/app.py"]
        assert any("Edit" in line for line in progress)


class TestChatEndToEnd:
    def test_threshold_rotation_between_turns(self, tmp_path, monkeypatch, capsys):
        """Two turns with a tiny threshold: the second turn must start a fresh
        session (no --resume) carrying the distilled handoff plus new message."""
        state, script = _install_fake_claude(tmp_path, monkeypatch)
        monkeypatch.setenv("PACER_CLAUDE_ROTATE_TOKENS", "1000000")
        monkeypatch.chdir(tmp_path)
        # run_claude_chat resolves "claude" via shutil.which; point it at python
        # and smuggle the script in through base flags (argv passthrough).
        monkeypatch.setattr("shutil.which", lambda name: sys.executable)
        inputs = iter(["第一个任务", "第二个任务", "/exit"])
        monkeypatch.setattr("builtins.input", lambda *args: next(inputs))

        assert run_claude_chat([str(script)]) == 0

        argv_first = json.loads((state / "argv-1.json").read_text(encoding="utf-8"))
        argv_second = json.loads((state / "argv-2.json").read_text(encoding="utf-8"))
        assert "--resume" not in argv_first
        assert "--resume" not in argv_second, "over threshold the next turn must NOT resume"
        prompt_second = argv_second[-1]
        assert "会话交接" in prompt_second
        assert "第一个任务" in prompt_second, "handoff must carry the previous goal"
        assert "第二个任务" in prompt_second, "the user's new message must ride along"
        handoffs = list((tmp_path / ".pacer" / "session-handoffs").glob("rotation-*.md"))
        assert handoffs, "rotation must archive the handoff"
        output = capsys.readouterr().out
        assert "无损轮转" in output

    def test_under_threshold_resumes_same_session(self, tmp_path, monkeypatch):
        """Discriminative pair: with a high threshold the second turn must
        resume the session from the first turn."""
        state, script = _install_fake_claude(tmp_path, monkeypatch)
        monkeypatch.setenv("PACER_CLAUDE_ROTATE_TOKENS", str(10**12))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("shutil.which", lambda name: sys.executable)
        inputs = iter(["第一个任务", "第二个任务", "/exit"])
        monkeypatch.setattr("builtins.input", lambda *args: next(inputs))

        assert run_claude_chat([str(script)]) == 0

        argv_second = json.loads((state / "argv-2.json").read_text(encoding="utf-8"))
        assert "--resume" in argv_second
        assert argv_second[argv_second.index("--resume") + 1] == "sess-1"
