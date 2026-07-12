from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from visual_agent.workbench_app import (
    _grounding_section,
    resolve_workspace,
    start_dashboard_process,
    start_portfolio_dashboard_process,
    validate_launch,
    workbench_agents,
)


def test_workbench_agents_returns_coding_agents(monkeypatch):
    # Deterministic: never depend on what is actually installed on this machine.
    monkeypatch.setattr(
        "visual_agent.workbench_app.agents_doctor",
        lambda: [
            {"agent": "claude-code", "installed": True},
            {"agent": "codex", "installed": True},
        ],
    )
    monkeypatch.setattr("visual_agent.agent_backends.resolve_backend_by_name", lambda _name: None)
    assert workbench_agents() == ["codex", "claude-code"]


def test_workbench_agents_falls_back_when_none_installed(monkeypatch):
    monkeypatch.setattr("visual_agent.workbench_app.agents_doctor", lambda: [])
    monkeypatch.setattr("visual_agent.agent_backends.resolve_backend_by_name", lambda _name: None)
    assert workbench_agents() == ["codex", "claude-code"]


def test_workbench_agents_only_offers_installed(monkeypatch):
    monkeypatch.setattr(
        "visual_agent.workbench_app.agents_doctor",
        lambda: [
            {"agent": "claude-code", "installed": True},
            {"agent": "codex", "installed": False},
        ],
    )
    monkeypatch.setattr("visual_agent.agent_backends.resolve_backend_by_name", lambda _name: None)
    assert workbench_agents() == ["claude-code"]


def test_workbench_agents_offers_bugteam_when_backend_configured(monkeypatch):
    monkeypatch.setattr(
        "visual_agent.workbench_app.agents_doctor",
        lambda: [{"agent": "claude-code", "installed": True}],
    )
    monkeypatch.setattr("visual_agent.agent_backends.resolve_backend_by_name", lambda name: {"name": name} if name == "bugteam" else None)

    # Low-cost backend goes first when configured: subscription quota is only
    # burned when the user opts in.
    assert workbench_agents() == ["bugteam", "claude-code"]


def test_workbench_agents_keeps_mimo_compatibility(monkeypatch):
    monkeypatch.setattr(
        "visual_agent.workbench_app.agents_doctor",
        lambda: [{"agent": "claude-code", "installed": True}],
    )
    monkeypatch.setattr("visual_agent.agent_backends.resolve_backend_by_name", lambda name: {"name": name} if name == "mimo" else None)

    # MiMo remains a compatibility fallback for existing local setups.
    # default so subscription quota is only burned when the user opts in.
    assert workbench_agents() == ["mimo", "claude-code"]


def test_start_dashboard_process_launches_independent_cli(tmp_path, monkeypatch):
    captured = {}

    class FakeProc:
        pid = 4321

    def fake_popen(cmd, cwd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr("visual_agent.workbench_app.hidden_subprocess_kwargs", lambda *, detached=False: {"creationflags": 12345})
    monkeypatch.setattr("subprocess.Popen", fake_popen)

    pid = start_dashboard_process(tmp_path / ".agent-workspace")

    assert pid == 4321
    assert captured["cmd"][1:4] == ["-m", "visual_agent.cli", "dashboard"]
    assert captured["cmd"][-2:] == ["--workspace-root", str(tmp_path / ".agent-workspace")]
    assert captured["cwd"] == str(tmp_path)
    assert captured["kwargs"]["creationflags"] == 12345


def test_start_portfolio_dashboard_process_launches_independent_cli(tmp_path, monkeypatch):
    captured = {}

    class FakeProc:
        pid = 9876

    def fake_popen(cmd, cwd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr("visual_agent.workbench_app.hidden_subprocess_kwargs", lambda *, detached=False: {"creationflags": 12345})
    monkeypatch.setattr("subprocess.Popen", fake_popen)

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    pid = start_portfolio_dashboard_process([a, b])

    assert pid == 9876
    assert captured["cmd"][1:4] == ["-m", "visual_agent.cli", "portfolio-dashboard"]
    assert captured["cmd"].count("--project") == 2
    assert captured["cwd"] == str(tmp_path)
    assert captured["kwargs"]["creationflags"] == 12345


def test_validate_launch_requires_project_goal():
    assert validate_launch(project_dir="", goal="x", test_command="pytest")["ok"] is False
    assert validate_launch(project_dir=".", goal="  ", test_command="pytest")["ok"] is False


def test_validate_launch_warns_without_test_command(tmp_path):
    result = validate_launch(project_dir=str(tmp_path), goal="修一个 bug", test_command="")
    assert result["ok"] is True
    assert "验收命令" in result["warning"]


def test_validate_launch_ok(tmp_path):
    result = validate_launch(project_dir=str(tmp_path), goal="修一个 bug", test_command="pytest -q")
    assert result == {"ok": True}


def test_resolve_workspace_creates_and_gitignores(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    ws = resolve_workspace(tmp_path)
    assert ws.exists() and ws.name == ".agent-workspace"
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".agent-workspace/" in gitignore


def test_grounding_section_extracts_review_block():
    report = (
        "## DevPacer Mission\n\nStatus: `stopped`\n\n### Plan\n\n- Status: x\n\n"
        "### 计划审查（先看项目里写了什么，再决定）\n\n"
        "审查过的文档：`docs/roadmap.md`\n\n**需要和你确认：**\n- 先做哪个？\n\n"
        "### Rounds\n\n- Round 0\n"
    )
    section = _grounding_section(report)
    assert "docs/roadmap.md" in section
    assert "先做哪个？" in section
    assert "Rounds" not in section
    assert _grounding_section("## 没有审查章节\n") == ""


def test_desktop_app_window_builds_and_closes():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available for Tk")
    root.destroy()

    import visual_agent.workbench_app as wa

    original = tk.Tk

    class AutoTk(original):
        def mainloop(self, n: int = 0) -> None:
            self.after(200, self.destroy)
            super().mainloop(n)

    tk.Tk = AutoTk
    try:
        assert wa.launch_desktop_app(project_dir=None) == 0
    finally:
        tk.Tk = original


def test_desktop_app_has_product_overview_sections():
    import visual_agent.workbench_app as wa

    source = Path(wa.__file__).read_text(encoding="utf-8")
    for text in ("工作台概览", "最近任务", "待办队列", "MiMo 节省", "时间节省", "综合效率"):
        assert text in source
    assert "Pacer 工作台" in source
    assert "当前执行日志" in source
    assert "多项目观察台" in source
