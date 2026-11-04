"""Per-task native Claude windows: model picked from the task, handoff
distilled from Claude's own on-disk transcript, never a fake REPL."""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from visual_agent.pacer_task import (
    parse_task_model_map,
    pick_model,
    read_task_input,
    run_pacer_task,
    task_handoff_prompt,
    DEFAULT_MODEL_MAP,
)


class TestPickModel:
    def test_mechanical_task_maps_to_sonnet(self):
        model, reason = pick_model("改一下登录页的错别字", DEFAULT_MODEL_MAP)
        assert model == "sonnet"
        assert "轻任务" in reason

    def test_reasoning_heavy_task_maps_to_opus(self):
        model, reason = pick_model("重构支付回调的验签逻辑", DEFAULT_MODEL_MAP)
        assert model == "opus"
        assert "重任务" in reason

    def test_repeated_failure_escalates(self):
        model, _ = pick_model("把首页加个搜索框", DEFAULT_MODEL_MAP, repeated_failure=True)
        assert model == "opus"

    def test_custom_map_overrides_one_tier(self):
        mapping = parse_task_model_map("strong=fable", DEFAULT_MODEL_MAP)
        model, _ = pick_model("重构整个架构", mapping)
        assert model == "fable"
        # 其余档位不受影响
        cheap_model, _ = pick_model("改个错别字", mapping)
        assert cheap_model == "sonnet"


class TestTaskHandoffPrompt:
    def test_carries_handoff_and_new_task(self):
        prompt = task_handoff_prompt("## 上个任务做了什么", "接着优化一下")
        assert "上个任务做了什么" in prompt
        assert "接着优化一下" in prompt
        assert "新任务" in prompt


class TestReadTaskInput:
    def test_single_line_passes_through(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *a: "做个登录页")
        monkeypatch.setattr("visual_agent.pacer_task._has_buffered_line", lambda: False)
        assert read_task_input("> ") == "做个登录页"

    def test_pasted_multiline_task_is_joined(self, monkeypatch):
        values = iter(["技术逻辑：xxx", "素材：yyy"])
        more = iter([True, False])
        monkeypatch.setattr("builtins.input", lambda *a: next(values))
        monkeypatch.setattr("visual_agent.pacer_task._has_buffered_line", lambda: next(more))
        assert read_task_input("> ") == "技术逻辑：xxx\n素材：yyy"


_FAKE_CLAUDE = textwrap.dedent(
    """
    # One native "session" per invocation: records argv, writes a session
    # transcript like the real Claude Code CLI would under ~/.claude/projects,
    # then exits -- simulating the user finishing the task and closing the
    # window (Ctrl+D / /exit).
    import json, os, sys
    state = os.environ["FAKE_STATE"]
    runs_file = os.path.join(state, "runs.txt")
    n = int(open(runs_file).read()) + 1 if os.path.exists(runs_file) else 1
    open(runs_file, "w").write(str(n))
    with open(os.path.join(state, f"argv-{n}.json"), "w", encoding="utf-8") as f:
        json.dump(sys.argv[1:], f, ensure_ascii=False)
    proj = os.path.join(os.environ["FAKE_PROJECTS"], "proj%d" % n)
    os.makedirs(proj, exist_ok=True)
    cwd = os.environ["FAKE_CWD"]
    records = [
        {"type": "user", "cwd": cwd,
         "message": {"content": [{"type": "text", "text": "任务%d的目标" % n}]}},
        {"type": "assistant", "cwd": cwd,
         "message": {"content": [{"type": "text", "text": "任务%d做完了" % n}],
                     "usage": {"input_tokens": 10, "output_tokens": 5}}},
    ]
    with open(os.path.join(proj, "s%d.jsonl" % n), "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\\n")
    sys.exit(0)
    """
)


def _install_fake_claude(tmp_path, monkeypatch):
    state = tmp_path / "state"
    projects = tmp_path / "projects"
    workdir = tmp_path / "workdir"
    for item in (state, projects, workdir):
        item.mkdir()
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE, encoding="utf-8")
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("FAKE_PROJECTS", str(projects))
    monkeypatch.setenv("FAKE_CWD", str(workdir))
    monkeypatch.chdir(workdir)
    monkeypatch.setattr("shutil.which", lambda name: sys.executable)
    monkeypatch.setattr("visual_agent.pacer_task.claude_projects_root", lambda: projects)
    return state, projects, workdir, script


class TestRunPacerTaskEndToEnd:
    def test_each_task_gets_its_own_native_window_with_a_picked_model(self, tmp_path, monkeypatch, capsys):
        state, projects, workdir, script = _install_fake_claude(tmp_path, monkeypatch)
        inputs = iter(["重构支付验签逻辑", "改个错别字", "/exit"])
        monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
        monkeypatch.setattr("visual_agent.pacer_task._has_buffered_line", lambda: False)

        assert run_pacer_task([str(script)]) == 0

        assert (state / "runs.txt").read_text() == "2", "two tasks must open two native windows"
        first_argv = json.loads((state / "argv-1.json").read_text(encoding="utf-8"))
        assert "--model" in first_argv and "opus" in first_argv, "heavy task must pick opus"
        assert first_argv[-1] == "重构支付验签逻辑", "first task carries no handoff -- nothing came before it"

        second_argv = json.loads((state / "argv-2.json").read_text(encoding="utf-8"))
        assert "--model" in second_argv and "sonnet" in second_argv, "light task must pick sonnet"
        assert "任务1的目标" in second_argv[-1], "second window must carry the first task's handoff"
        assert "改个错别字" in second_argv[-1]

        handoffs = list((workdir / ".pacer" / "session-handoffs").glob("rotation-*.md"))
        assert handoffs, "handoff must be archived to disk between windows"
        output = capsys.readouterr().out
        assert "重任务" in output and "轻任务" in output

    def test_defaults_to_bypass_permissions_since_a_window_opens_per_task(self, tmp_path, monkeypatch):
        state, projects, workdir, script = _install_fake_claude(tmp_path, monkeypatch)
        inputs = iter(["做点什么", "/exit"])
        monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
        monkeypatch.setattr("visual_agent.pacer_task._has_buffered_line", lambda: False)

        assert run_pacer_task([str(script)]) == 0

        argv = json.loads((state / "argv-1.json").read_text(encoding="utf-8"))
        idx = argv.index("--permission-mode")
        assert argv[idx + 1] == "bypassPermissions"

    def test_does_not_override_an_explicit_permission_mode(self, tmp_path, monkeypatch):
        state, projects, workdir, script = _install_fake_claude(tmp_path, monkeypatch)
        inputs = iter(["做点什么", "/exit"])
        monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
        monkeypatch.setattr("visual_agent.pacer_task._has_buffered_line", lambda: False)

        assert run_pacer_task([str(script), "--permission-mode", "default"]) == 0

        argv = json.loads((state / "argv-1.json").read_text(encoding="utf-8"))
        assert argv.count("--permission-mode") == 1
        idx = argv.index("--permission-mode")
        assert argv[idx + 1] == "default"

    def test_model_command_forces_next_window_only_until_reset(self, tmp_path, monkeypatch):
        state, projects, workdir, script = _install_fake_claude(tmp_path, monkeypatch)
        inputs = iter(["/model fable", "随便什么小事", "/model auto", "改个错别字", "/exit"])
        monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
        monkeypatch.setattr("visual_agent.pacer_task._has_buffered_line", lambda: False)

        assert run_pacer_task([str(script)]) == 0

        first_argv = json.loads((state / "argv-1.json").read_text(encoding="utf-8"))
        assert "--model" in first_argv and "fable" in first_argv, "/model must force the very next window"
        second_argv = json.loads((state / "argv-2.json").read_text(encoding="utf-8"))
        assert "--model" in second_argv and "sonnet" in second_argv, "/model auto must restore the routed pick"

    def test_missing_transcript_still_lets_the_next_task_start(self, tmp_path, monkeypatch, capsys):
        """If Pacer can't find the session's transcript (e.g. cwd mismatch),
        the loop must degrade to no-handoff, not crash or hang."""
        state = tmp_path / "state"
        projects = tmp_path / "projects"  # left empty -- discover will find nothing
        workdir = tmp_path / "workdir"
        for item in (state, projects, workdir):
            item.mkdir()
        script = tmp_path / "silent_claude.py"
        script.write_text(
            textwrap.dedent(
                """
                import json, os, sys
                state = os.environ["FAKE_STATE"]
                runs_file = os.path.join(state, "runs.txt")
                n = int(open(runs_file).read()) + 1 if os.path.exists(runs_file) else 1
                open(runs_file, "w").write(str(n))
                with open(os.path.join(state, f"argv-{n}.json"), "w", encoding="utf-8") as f:
                    json.dump(sys.argv[1:], f, ensure_ascii=False)
                sys.exit(0)
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("FAKE_STATE", str(state))
        monkeypatch.chdir(workdir)
        monkeypatch.setattr("shutil.which", lambda name: sys.executable)
        monkeypatch.setattr("visual_agent.pacer_task.claude_projects_root", lambda: projects)
        inputs = iter(["做点什么", "/exit"])
        monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
        monkeypatch.setattr("visual_agent.pacer_task._has_buffered_line", lambda: False)

        assert run_pacer_task([str(script)]) == 0
        assert (state / "runs.txt").read_text() == "1"
        assert "未找到会话记录" in capsys.readouterr().out
