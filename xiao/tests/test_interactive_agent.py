from __future__ import annotations

from pathlib import Path

from visual_agent.interactive_agent import MENU_TEXT, run_interactive_agent


def test_slash_menu_and_exit(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    workspace = repo / ".agent-workspace"
    workspace.mkdir()

    inputs = iter(["/菜单", "/测试", "/退出"])
    outputs: list[str] = []

    def fake_input(_prompt: str = "") -> str:
        return next(inputs)

    code = run_interactive_agent(
        repo_root=repo,
        workspace_root=workspace,
        task_runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no task")),
        input_func=fake_input,
        output_func=outputs.append,
    )
    assert code == 0
    blob = "\n".join(outputs)
    assert "/状态" in blob or "菜单" in blob
    assert "pytest" in blob.lower() or "验收" in blob


def test_menu_text_has_core_commands() -> None:
    assert "/帮助" in MENU_TEXT
    assert "/状态" in MENU_TEXT
    assert "/验收" in MENU_TEXT
    assert "/退出" in MENU_TEXT
