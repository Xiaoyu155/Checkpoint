from __future__ import annotations

from visual_agent.verification_profiles import (
    build_test_plan,
    choose_verification_command,
    detect_verification_profiles,
    resolve_test_command,
)


def test_detects_node_test_script(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run","build":"vite build"}}', encoding="utf-8")

    profiles = detect_verification_profiles(tmp_path)

    assert profiles[0]["command"] == "npm test"
    assert any(item["command"] == "npm run build" for item in profiles)


def test_detects_python_pytest(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    assert choose_verification_command(tmp_path) == "python -m pytest -q"


def test_resolve_test_command_auto(tmp_path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")

    command, profile = resolve_test_command("auto", repo_root=tmp_path)

    assert command == "go test ./..."
    assert profile is not None
    assert profile["status"] == "found"


def test_resolve_test_command_empty_does_not_auto_detect(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    command, profile = resolve_test_command(None, repo_root=tmp_path)

    assert command is None
    assert profile is None


def test_resolve_test_command_keeps_explicit_command(tmp_path) -> None:
    command, profile = resolve_test_command("npm run check", repo_root=tmp_path)

    assert command == "npm run check"
    assert profile is None


def test_build_test_plan_focuses_pytest_for_changed_source(tmp_path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    source = tmp_path / "src" / "visual_agent"
    source.mkdir(parents=True)
    (source / "security.py").write_text("VALUE = 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_security.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr("visual_agent.verification_profiles._git_changed_paths", lambda root, base: ["src/visual_agent/security.py"])

    plan = build_test_plan(tmp_path)

    assert plan["command"] == "python -m pytest -q tests/test_security.py"
    assert plan["profiles"][0]["name"] == "pytest-focused"
    assert plan["profiles"][0]["targets"] == ["tests/test_security.py"]


def test_build_test_plan_runs_changed_test_file_directly(tmp_path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_cli.py").write_text("def test_cli():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr("visual_agent.verification_profiles._git_changed_paths", lambda root, base: ["tests/test_cli.py"])

    plan = build_test_plan(tmp_path)

    assert plan["command"] == "python -m pytest -q tests/test_cli.py"


def test_build_test_plan_falls_back_when_source_has_no_matching_test(tmp_path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    source = tmp_path / "src" / "visual_agent"
    source.mkdir(parents=True)
    (source / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr("visual_agent.verification_profiles._git_changed_paths", lambda root, base: ["src/visual_agent/new_module.py"])

    plan = build_test_plan(tmp_path)

    assert plan["command"] == "python -m pytest -q"
    assert plan["profiles"][0]["name"] == "pytest"
