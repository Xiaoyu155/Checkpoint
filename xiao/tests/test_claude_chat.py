"""C-layer chat window: headless turn driving and exact-boundary rotation."""

from __future__ import annotations

import json
import os
import sys
import time
import textwrap

import pytest

from visual_agent.claude_chat import (
    ClaudeSession,
    ChatState,
    RateLimit,
    TurnResult,
    build_chat_handoff,
    fold_event,
    format_tokens,
    receipt_text,
    run_claude_chat,
    status_text,
)


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
        assert "2.0M / 10.0M" in text
        assert "sess-1" in text


class TestQuotaDisplay:
    @pytest.mark.parametrize(
        "count,expected",
        [(0, "0"), (999, "999"), (24_210, "24k"), (2_000_000, "2.0M")],
    )
    def test_format_tokens_stays_readable_at_every_scale(self, count, expected):
        """The original receipt printed 0.0M for every normal turn — useless."""
        assert format_tokens(count) == expected

    def test_rate_limit_event_is_captured(self):
        result = TurnResult()
        fold_event(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "rateLimitType": "seven_day",
                    "utilization": 0.93,
                    "resetsAt": int(time.time()) + 3 * 86400,
                    "status": "allowed_warning",
                },
            },
            result,
        )
        limit = result.rate_limits["seven_day"]
        assert limit.utilization == pytest.approx(0.93)
        described = limit.describe()
        assert "周额度 已用 93%" in described
        assert "天后重置" in described

    def test_receipt_leads_with_real_weekly_quota(self):
        state = ChatState(session_tokens=24_210)
        state.rate_limits["seven_day"] = RateLimit(kind="seven_day", utilization=0.93)
        text = receipt_text(state, TurnResult(tokens=24_210), 10_000_000, 11.0)
        assert "本回合 24k" in text
        assert "周额度 93%" in text
        assert "本回合 0.0M" not in text, "a normal turn must never round away to zero"

    def test_receipt_without_quota_event_omits_it(self):
        text = receipt_text(ChatState(session_tokens=500), TurnResult(tokens=500), 10_000_000, 2.0)
        assert "周额度" not in text
        assert "本回合 500" in text


class TestStreaming:
    def test_text_delta_streams_incrementally(self):
        result = TurnResult()
        chunks: list[str] = []
        for piece in ("你", "好", "世界"):
            fold_event(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": piece},
                    },
                },
                result,
                on_text=chunks.append,
            )
        assert chunks == ["你", "好", "世界"]

    def test_thinking_delta_is_not_streamed_to_user(self):
        chunks: list[str] = []
        fold_event(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": "internal"},
                },
            },
            TurnResult(),
            on_text=chunks.append,
        )
        assert chunks == []

_FAKE_STREAM_CLAUDE = textwrap.dedent(
    """
    # Persistent fake engine: one process, many turns fed as stream-json on
    # stdin -- mirrors `claude -p --input-format stream-json`.
    import json, os, sys
    state = os.environ["FAKE_STATE"]
    procs_file = os.path.join(state, "procs.txt")
    p = int(open(procs_file).read()) + 1 if os.path.exists(procs_file) else 1
    open(procs_file, "w").write(str(p))
    with open(os.path.join(state, f"argv-{p}.json"), "w", encoding="utf-8") as f:
        json.dump(sys.argv[1:], f, ensure_ascii=False)
    turn = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        turn += 1
        message = json.loads(line)
        text = message["message"]["content"][0]["text"]
        with open(os.path.join(state, f"prompt-{p}-{turn}.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        events = [
            {"type": "stream_event", "event": {"type": "content_block_delta",
             "delta": {"type": "text_delta", "text": "进程%d回合%d" % (p, turn)}}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/app.py"}}]}},
            {"type": "rate_limit_event", "rate_limit_info": {
                "rateLimitType": "seven_day", "utilization": 0.42, "resetsAt": 0,
                "status": "allowed"}},
            {"type": "result", "subtype": "success", "session_id": "sess-%d" % p,
             "result": "进程%d回合%d" % (p, turn),
             "usage": {"input_tokens": 10, "output_tokens": 10,
                        "cache_read_input_tokens": 2000000,
                        "cache_creation_input_tokens": 0}},
        ]
        for event in events:
            sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\\n")
        sys.stdout.flush()
    """
)


def _install_fake_claude(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    script = tmp_path / "fake_stream_claude.py"
    script.write_text(_FAKE_STREAM_CLAUDE, encoding="utf-8")
    monkeypatch.setenv("FAKE_STATE", str(state))
    return state, script


class TestPersistentSession:
    def test_one_process_serves_many_turns(self, tmp_path, monkeypatch):
        """The whole point of the persistent process: turn two must not pay
        another process launch."""
        state, script = _install_fake_claude(tmp_path, monkeypatch)
        session = ClaudeSession(sys.executable, [str(script)])
        try:
            streamed: list[str] = []
            progress: list[str] = []
            first = session.send("一", on_text=streamed.append, on_progress=progress.append)
            second = session.send("二", on_text=streamed.append)
        finally:
            session.close()
        assert first.ok and second.ok
        assert first.session_id == "sess-1" == second.session_id
        assert first.tokens == 2_000_020
        assert streamed == ["进程1回合1", "进程1回合2"]
        assert any("Edit" in line for line in progress)
        assert first.rate_limits["seven_day"].utilization == pytest.approx(0.42)
        assert (state / "procs.txt").read_text() == "1", "second turn must reuse the process"

    def test_dead_session_reports_instead_of_hanging(self, tmp_path, monkeypatch):
        _, script = _install_fake_claude(tmp_path, monkeypatch)
        session = ClaudeSession(sys.executable, [str(script)])
        session.close()
        result = session.send("hi")
        assert not result.ok
        assert result.error == "session_dead"


class TestChatEndToEnd:
    def test_threshold_rotation_between_turns(self, tmp_path, monkeypatch, capsys):
        """Over threshold the window must spawn a SECOND process with no
        --resume, and its first prompt must carry the handoff."""
        state, script = _install_fake_claude(tmp_path, monkeypatch)
        monkeypatch.setenv("PACER_CLAUDE_ROTATE_TOKENS", "1000000")
        monkeypatch.chdir(tmp_path)
        # run_claude_chat resolves "claude" via shutil.which; point it at python
        # and smuggle the script in through base flags (argv passthrough).
        monkeypatch.setattr("shutil.which", lambda name: sys.executable)
        inputs = iter(["第一个任务", "第二个任务", "/exit"])
        monkeypatch.setattr("builtins.input", lambda *args: next(inputs))

        assert run_claude_chat([str(script)]) == 0

        assert (state / "procs.txt").read_text() == "2", "rotation must open a new engine process"
        argv_second = json.loads((state / "argv-2.json").read_text(encoding="utf-8"))
        assert "--resume" not in argv_second, "a rotated session must not resume the old one"
        prompt_second = (state / "prompt-2-1.txt").read_text(encoding="utf-8")
        assert "会话交接" in prompt_second
        assert "第一个任务" in prompt_second, "handoff must carry the previous goal"
        assert "第二个任务" in prompt_second, "the user's new message must ride along"
        handoffs = list((tmp_path / ".pacer" / "session-handoffs").glob("rotation-*.md"))
        assert handoffs, "rotation must archive the handoff"
        output = capsys.readouterr().out
        assert "无损轮转" in output
        assert "周额度 42%" in output, "receipt must show the real quota from the engine"

    def test_under_threshold_keeps_one_process(self, tmp_path, monkeypatch):
        """Discriminative pair: with a high threshold there must be exactly one
        engine process and no handoff in the second prompt."""
        state, script = _install_fake_claude(tmp_path, monkeypatch)
        monkeypatch.setenv("PACER_CLAUDE_ROTATE_TOKENS", str(10**12))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("shutil.which", lambda name: sys.executable)
        inputs = iter(["第一个任务", "第二个任务", "/exit"])
        monkeypatch.setattr("builtins.input", lambda *args: next(inputs))

        assert run_claude_chat([str(script)]) == 0

        assert (state / "procs.txt").read_text() == "1"
        prompt_second = (state / "prompt-1-2.txt").read_text(encoding="utf-8")
        assert prompt_second == "第二个任务"
        assert not (tmp_path / ".pacer").exists(), "no rotation means no handoff archive"

    def test_manual_rotate_command_carries_handoff(self, tmp_path, monkeypatch):
        state, script = _install_fake_claude(tmp_path, monkeypatch)
        monkeypatch.setenv("PACER_CLAUDE_ROTATE_TOKENS", str(10**12))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("shutil.which", lambda name: sys.executable)
        inputs = iter(["第一个任务", "/rotate", "第二个任务", "/exit"])
        monkeypatch.setattr("builtins.input", lambda *args: next(inputs))

        assert run_claude_chat([str(script)]) == 0

        assert (state / "procs.txt").read_text() == "2"
        assert "第一个任务" in (state / "prompt-2-1.txt").read_text(encoding="utf-8")

    def test_new_command_starts_clean_without_handoff(self, tmp_path, monkeypatch):
        state, script = _install_fake_claude(tmp_path, monkeypatch)
        monkeypatch.setenv("PACER_CLAUDE_ROTATE_TOKENS", str(10**12))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("shutil.which", lambda name: sys.executable)
        inputs = iter(["第一个任务", "/new", "第二个任务", "/exit"])
        monkeypatch.setattr("builtins.input", lambda *args: next(inputs))

        assert run_claude_chat([str(script)]) == 0

        assert (state / "procs.txt").read_text() == "2"
        assert (state / "prompt-2-1.txt").read_text(encoding="utf-8") == "第二个任务"
