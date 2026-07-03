from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from visual_agent.workbench_app import resolve_workspace, validate_launch, workbench_agents


def test_workbench_agents_returns_coding_agents():
    agents = workbench_agents()
    assert agents  # never empty
    assert all(a in {"claude-code", "codex"} for a in agents)


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
    assert ".agent-workspace/missions/" in gitignore


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
