"""Session rotation proxy: metering, handoff distillation, and launch wiring.

The discriminative pairs matter most here: a gate that always rotates (or
never rotates) equals no gate. Each behaviour is tested in both directions.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from visual_agent import cli
from visual_agent.claude_rotation import (
    argv_with_model,
    DEFAULT_ROTATE_TOKENS,
    SessionMeter,
    build_handoff,
    discover_session_jsonl,
    is_interactive_launch,
    rotate_threshold,
    rotation_enabled,
    rotation_prompt,
    run_claude_with_rotation,
)


def _assistant_record(*, text="", tools=(), usage=None, cwd="D:/proj", model=""):
    content = [{"type": "tool_use", "name": name, "input": {"file_path": path}} for name, path in tools]
    if text:
        content.append({"type": "text", "text": text})
    record = {"type": "assistant", "cwd": cwd, "message": {"content": content}}
    if usage is not None:
        record["message"]["usage"] = usage
    if model:
        record["message"]["model"] = model
    return record


def _user_record(text, *, cwd="D:/proj"):
    return {"type": "user", "cwd": cwd, "message": {"content": [{"type": "text", "text": text}]}}


def _write_jsonl(path: Path, records) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


class TestSessionMeter:
    def test_accumulates_all_usage_components(self, tmp_path):
        transcript = tmp_path / "s.jsonl"
        usage = {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 40,
        }
        _write_jsonl(transcript, [_assistant_record(text="done", usage=usage)])
        meter = SessionMeter(path=transcript)
        meter.refresh()
        assert meter.total_tokens == 100
        assert meter.last_context_tokens == 80

    def test_incremental_refresh_does_not_double_count(self, tmp_path):
        transcript = tmp_path / "s.jsonl"
        usage = {"input_tokens": 5, "output_tokens": 5}
        _write_jsonl(transcript, [_assistant_record(text="one", usage=usage)])
        meter = SessionMeter(path=transcript)
        meter.refresh()
        meter.refresh()
        assert meter.total_tokens == 10
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_assistant_record(text="two", usage=usage)) + "\n")
        meter.refresh()
        assert meter.total_tokens == 20

    def test_turn_boundary_true_only_for_final_assistant_text(self, tmp_path):
        transcript = tmp_path / "s.jsonl"
        _write_jsonl(transcript, [_assistant_record(text="all done")])
        meter = SessionMeter(path=transcript)
        meter.refresh()
        meter.last_growth = time.time() - 999
        assert meter.at_turn_boundary()

    def test_no_boundary_while_tool_call_in_flight(self, tmp_path):
        transcript = tmp_path / "s.jsonl"
        _write_jsonl(transcript, [_assistant_record(text="editing", tools=[("Edit", "a.py")])])
        meter = SessionMeter(path=transcript)
        meter.refresh()
        meter.last_growth = time.time() - 999
        assert not meter.at_turn_boundary()

    def test_no_boundary_after_user_message(self, tmp_path):
        transcript = tmp_path / "s.jsonl"
        _write_jsonl(transcript, [_assistant_record(text="done"), _user_record("next task")])
        meter = SessionMeter(path=transcript)
        meter.refresh()
        meter.last_growth = time.time() - 999
        assert not meter.at_turn_boundary()

    def test_no_boundary_when_file_recently_grew(self, tmp_path):
        transcript = tmp_path / "s.jsonl"
        _write_jsonl(transcript, [_assistant_record(text="done")])
        meter = SessionMeter(path=transcript)
        meter.refresh()  # last_growth = now
        assert not meter.at_turn_boundary()


class TestModelPreservation:
    """A /model switch mid-session must survive rotation, or the user's choice
    silently reverts to the default on the next window."""

    def test_meter_tracks_latest_model(self, tmp_path):
        transcript = tmp_path / "s.jsonl"
        _write_jsonl(
            transcript,
            [
                _assistant_record(text="a", model="claude-fable-5"),
                _assistant_record(text="b", model="claude-opus-5"),
            ],
        )
        meter = SessionMeter(path=transcript)
        meter.refresh()
        assert meter.last_model == "claude-opus-5"

    def test_argv_with_model_replaces_existing_choice(self):
        argv = ["--permission-mode", "bypassPermissions", "--model", "sonnet"]
        assert argv_with_model(argv, "claude-opus-5") == [
            "--model",
            "claude-opus-5",
            "--permission-mode",
            "bypassPermissions",
        ]
        assert argv_with_model(["--model=sonnet", "-x"], "claude-opus-5") == [
            "--model",
            "claude-opus-5",
            "-x",
        ]

    def test_argv_unchanged_when_no_model_observed(self):
        argv = ["--permission-mode", "bypassPermissions"]
        assert argv_with_model(argv, "") == argv


class TestDiscoverSessionJsonl:
    def test_matches_by_cwd_and_excludes_known(self, tmp_path):
        root = tmp_path / "projects"
        mine = root / "d--proj"
        other = root / "d--other"
        mine.mkdir(parents=True)
        other.mkdir(parents=True)
        target = mine / "abc.jsonl"
        _write_jsonl(target, [_user_record("hello", cwd="D:/proj")])
        _write_jsonl(other / "zzz.jsonl", [_user_record("hi", cwd="D:/other")])

        found = discover_session_jsonl(projects_root=root, cwd="D:/proj", started_after=0)
        assert found == target
        assert (
            discover_session_jsonl(projects_root=root, cwd="D:/proj", started_after=0, exclude={target}) is None
        )

    def test_ignores_stale_transcripts(self, tmp_path):
        root = tmp_path / "projects"
        mine = root / "d--proj"
        mine.mkdir(parents=True)
        _write_jsonl(mine / "old.jsonl", [_user_record("hello", cwd="D:/proj")])
        found = discover_session_jsonl(projects_root=root, cwd="D:/proj", started_after=time.time() + 3600)
        assert found is None


class TestHandoff:
    def test_extracts_goal_files_and_recent_messages(self, tmp_path):
        transcript = tmp_path / "s.jsonl"
        records = [
            _user_record("<local-command-caveat>noise</local-command-caveat>"),
            _user_record("把登录页做出来"),
            _assistant_record(text="开始", tools=[("Edit", "src/login.vue"), ("Write", "src/api.ts")]),
            _user_record("按钮颜色改成蓝色"),
            _assistant_record(text="登录页完成，按钮已改为蓝色。"),
        ]
        _write_jsonl(transcript, records)
        handoff = build_handoff(transcript)
        assert "把登录页做出来" in handoff
        assert "src/login.vue" in handoff
        assert "src/api.ts" in handoff
        assert "按钮颜色改成蓝色" in handoff
        assert "登录页完成" in handoff
        assert "noise" not in handoff

    def test_handoff_respects_max_chars(self, tmp_path):
        transcript = tmp_path / "s.jsonl"
        _write_jsonl(transcript, [_user_record("x" * 5000), _assistant_record(text="y" * 5000)])
        assert len(build_handoff(transcript, max_chars=1000)) <= 1000

    def test_rotation_prompt_instructs_silent_continuation(self):
        prompt = rotation_prompt("## 交接内容")
        assert "交接内容" in prompt
        assert "静默" in prompt
        assert "以磁盘为准" in prompt


class TestConfig:
    def test_rotation_enabled_default_and_off(self):
        assert rotation_enabled({})
        assert not rotation_enabled({"PACER_CLAUDE_ROTATE": "0"})
        assert not rotation_enabled({"PACER_CLAUDE_ROTATE": "off"})

    def test_threshold_parsing_falls_back_on_garbage(self):
        assert rotate_threshold({}) == DEFAULT_ROTATE_TOKENS
        assert rotate_threshold({"PACER_CLAUDE_ROTATE_TOKENS": "5000000"}) == 5_000_000
        assert rotate_threshold({"PACER_CLAUDE_ROTATE_TOKENS": "banana"}) == DEFAULT_ROTATE_TOKENS
        assert rotate_threshold({"PACER_CLAUDE_ROTATE_TOKENS": "-3"}) == DEFAULT_ROTATE_TOKENS


class TestInteractiveLaunchDetection:
    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["--permission-mode", "bypassPermissions"],
            ["--permission-mode", "bypassPermissions", "帮我修个bug"],
        ],
    )
    def test_interactive_forms_qualify(self, argv):
        assert is_interactive_launch(argv)

    @pytest.mark.parametrize(
        "argv",
        [
            ["-p", "one shot"],
            ["--print", "one shot"],
            ["--resume", "abc"],
            ["--continue"],
            ["mcp", "list"],
            ["--help"],
        ],
    )
    def test_non_interactive_forms_fall_back(self, argv):
        assert not is_interactive_launch(argv)


class TestLaunchWiring:
    def test_rotation_path_taken_when_enabled(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(cli.shutil, "which", lambda name: "C:/bin/claude.cmd" if "claude" in name else None)
        monkeypatch.delenv("PACER_CLAUDE_ROTATE", raising=False)

        def fake_rotation(executable, argv, **kwargs):
            calls["rotation"] = (executable, argv)
            return 0

        monkeypatch.setattr("visual_agent.claude_rotation.run_claude_with_rotation", fake_rotation)
        assert cli.launch_claude_code(["--permission-mode", "bypassPermissions"]) == 0
        assert calls["rotation"][1] == ["--permission-mode", "bypassPermissions"]

    def test_disabled_env_falls_back_to_passthrough(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(cli.shutil, "which", lambda name: "C:/bin/claude.cmd" if "claude" in name else None)
        monkeypatch.setenv("PACER_CLAUDE_ROTATE", "0")

        class Completed:
            returncode = 7

        def fake_guarded_run(command, **kwargs):
            calls["passthrough"] = command
            return Completed()

        monkeypatch.setattr("visual_agent.process_guard.guarded_run", fake_guarded_run)
        assert cli.launch_claude_code(["--permission-mode", "bypassPermissions"]) == 7
        assert "passthrough" in calls

    def test_print_mode_never_rotates(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(cli.shutil, "which", lambda name: "C:/bin/claude.cmd" if "claude" in name else None)
        monkeypatch.delenv("PACER_CLAUDE_ROTATE", raising=False)

        class Completed:
            returncode = 0

        def fake_guarded_run(command, **kwargs):
            calls["passthrough"] = command
            return Completed()

        monkeypatch.setattr("visual_agent.process_guard.guarded_run", fake_guarded_run)

        def fail_rotation(*args, **kwargs):  # pragma: no cover - would mean a bug
            raise AssertionError("print mode must not rotate")

        monkeypatch.setattr("visual_agent.claude_rotation.run_claude_with_rotation", fail_rotation)
        assert cli.launch_claude_code(["-p", "one shot"]) == 0
        assert "passthrough" in calls


_FAKE_CLAUDE = textwrap.dedent(
    """
    import json, os, sys, time
    state = os.environ["FAKE_STATE"]
    runs_file = os.path.join(state, "runs.txt")
    n = int(open(runs_file).read()) + 1 if os.path.exists(runs_file) else 1
    open(runs_file, "w").write(str(n))
    with open(os.path.join(state, f"argv-{n}.json"), "w", encoding="utf-8") as f:
        json.dump(sys.argv[1:], f, ensure_ascii=False)
    if n == 1:
        proj = os.path.join(os.environ["FAKE_PROJECTS"], "d--proj")
        os.makedirs(proj, exist_ok=True)
        cwd = os.environ["FAKE_CWD"]
        records = [
            {"type": "user", "cwd": cwd,
             "message": {"content": [{"type": "text", "text": "把登录页做出来"}]}},
            {"type": "assistant", "cwd": cwd,
             "message": {"content": [{"type": "text", "text": "登录页完成"}],
                         "usage": {"input_tokens": 0, "output_tokens": 5,
                                   "cache_read_input_tokens": 2000000,
                                   "cache_creation_input_tokens": 0}}},
        ]
        with open(os.path.join(proj, "first.jsonl"), "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\\n")
        time.sleep(120)  # simulate an idle interactive session; rotation must kill us
        sys.exit(9)
    sys.exit(0)
    """
)


class TestRotationEndToEnd:
    def test_monster_session_is_rotated_with_handoff(self, tmp_path):
        """Full loop against a fake claude: launch -> threshold -> kill ->
        relaunch with the distilled handoff -> clean exit."""
        state = tmp_path / "state"
        projects = tmp_path / "projects"
        workdir = tmp_path / "workdir"
        for item in (state, projects, workdir):
            item.mkdir()
        script = tmp_path / "fake_claude.py"
        script.write_text(_FAKE_CLAUDE, encoding="utf-8")

        environ = dict(os.environ)
        environ.update(
            {
                "FAKE_STATE": str(state),
                "FAKE_PROJECTS": str(projects),
                "FAKE_CWD": str(workdir),
                "PACER_CLAUDE_ROTATE_TOKENS": "1000000",
            }
        )
        started = time.time()
        code = run_claude_with_rotation(
            sys.executable,
            [str(script)],
            environ=environ,
            projects_root=projects,
            cwd=str(workdir),
            poll_seconds=0.2,
            idle_seconds=0.2,
        )
        assert code == 0, "second (fresh) session should exit cleanly"
        assert time.time() - started < 60, "rotation must not wait out the fake session's sleep"
        assert (state / "runs.txt").read_text() == "2"
        second_argv = json.loads((state / "argv-2.json").read_text(encoding="utf-8"))
        prompt = second_argv[-1]
        assert "把登录页做出来" in prompt, "handoff must carry the original goal"
        assert "静默" in prompt
        handoffs = list((workdir / ".pacer" / "session-handoffs").glob("rotation-*.md"))
        assert handoffs, "handoff must be archived on disk"

    def test_session_under_threshold_never_rotated(self, tmp_path):
        """Discriminative pair: with a high threshold the same monster session
        must NOT be killed; the runner exits only when the child does."""
        state = tmp_path / "state"
        projects = tmp_path / "projects"
        workdir = tmp_path / "workdir"
        for item in (state, projects, workdir):
            item.mkdir()
        script = tmp_path / "fake_claude.py"
        # Same script but exits quickly on first run instead of sleeping.
        script.write_text(_FAKE_CLAUDE.replace("time.sleep(120)", "time.sleep(1)"), encoding="utf-8")

        environ = dict(os.environ)
        environ.update(
            {
                "FAKE_STATE": str(state),
                "FAKE_PROJECTS": str(projects),
                "FAKE_CWD": str(workdir),
                "PACER_CLAUDE_ROTATE_TOKENS": str(10**12),
            }
        )
        code = run_claude_with_rotation(
            sys.executable,
            [str(script)],
            environ=environ,
            projects_root=projects,
            cwd=str(workdir),
            poll_seconds=0.2,
            idle_seconds=0.2,
        )
        assert code == 9, "under threshold the child's own exit code must pass through"
        assert (state / "runs.txt").read_text() == "1"
