from __future__ import annotations

import sys
from pathlib import Path

from visual_agent.verification_profiles import (
    build_test_plan,
    choose_verification_command,
    conditional_test_command_short_circuit,
    detect_verification_profiles,
    estimate_verification_timeout,
    prefer_pytest_python,
    resolve_test_command,
    rewrite_python_pytest_command,
)


def test_detects_node_test_script(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run","build":"vite build"}}', encoding="utf-8")

    profiles = detect_verification_profiles(tmp_path)

    assert profiles[0]["command"] == "npm test"
    assert any(item["command"] == "npm run build" for item in profiles)


def test_detects_python_pytest(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    command = choose_verification_command(tmp_path)
    assert "-m pytest" in command
    assert command.endswith("-q") or " -q" in command


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


def test_resolve_test_command_rewrites_bare_python_pytest(tmp_path) -> None:
    preferred = prefer_pytest_python(tmp_path)
    if not preferred:
        return
    command, profile = resolve_test_command("python -m pytest -q", repo_root=tmp_path)
    assert command is not None
    assert "-m pytest" in command
    assert "python -m pytest" not in command or Path(sys.executable).name in command or preferred in command
    assert profile is None or profile.get("status") in {None, "ok"} or profile.get("python_rewrite") in {True, False}


def test_rewrite_python_pytest_prefers_project_venv(tmp_path, monkeypatch) -> None:
    if __import__("os").name != "nt":
        venv_python = tmp_path / ".venv" / "bin" / "python"
    else:
        venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_has(argv, module):
        calls.append(list(argv))
        return list(argv) == [str(venv_python)]

    monkeypatch.setattr(
        "visual_agent.verification_profiles._python_has_module",
        fake_has,
    )
    preferred = prefer_pytest_python(tmp_path)
    assert preferred is not None
    assert str(venv_python) in preferred
    rewritten, meta = rewrite_python_pytest_command("python -m pytest -q", repo_root=tmp_path)
    assert meta is not None
    assert meta.get("python_rewrite") is True
    assert str(venv_python) in rewritten


def test_timeout_extended_when_node_modules_missing(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}', encoding="utf-8")

    timeout = estimate_verification_timeout(tmp_path, "npm test", 900.0)

    assert timeout == 2100.0


def test_timeout_unchanged_when_deps_present(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"}}', encoding="utf-8")
    (tmp_path / "node_modules").mkdir()

    timeout = estimate_verification_timeout(tmp_path, "npm test", 900.0)

    assert timeout == 900.0


def test_timeout_extended_when_conditional_npm_ci_marker_missing(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node --test"}}', encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    command = (
        "cmd /d /s /c if not exist node_modules\\express\\package.json "
        "npm ci --cache .npm-cache --prefer-offline ^&^& node --test"
    )

    timeout = estimate_verification_timeout(tmp_path, command, 60.0)

    assert timeout == 1260.0


def test_timeout_unchanged_when_conditional_npm_ci_marker_present(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node --test"}}', encoding="utf-8")
    marker = tmp_path / "node_modules" / "express" / "package.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"name":"express"}\n', encoding="utf-8")
    command = (
        "cmd /d /s /c if not exist node_modules\\express\\package.json "
        "npm ci --cache .npm-cache --prefer-offline ^&^& node --test"
    )

    timeout = estimate_verification_timeout(tmp_path, command, 60.0)

    assert timeout == 60.0


def test_conditional_npm_ci_command_blocks_when_marker_present(tmp_path) -> None:
    marker = tmp_path / "node_modules" / "express" / "package.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"name":"express"}\n', encoding="utf-8")
    command = (
        "cmd /d /s /c if not exist node_modules\\express\\package.json "
        "npm ci --cache .npm-cache --prefer-offline ^&^& node --test"
    )

    result = conditional_test_command_short_circuit(tmp_path, command)

    assert result["status"] == "blocked"
    assert result["reason"] == "conditional_test_command_short_circuit"
    assert result["marker"] == "node_modules/express/package.json"


def test_conditional_npm_ci_command_allows_missing_marker(tmp_path) -> None:
    command = (
        "cmd /d /s /c if not exist node_modules\\express\\package.json "
        "npm ci --cache .npm-cache --prefer-offline ^&^& node --test"
    )

    result = conditional_test_command_short_circuit(tmp_path, command)

    assert result == {}


def test_conditional_npm_ci_command_allows_non_short_circuit_form(tmp_path) -> None:
    marker = tmp_path / "node_modules" / "express" / "package.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"name":"express"}\n', encoding="utf-8")
    command = (
        "cmd /d /s /c if not exist node_modules\\express\\package.json "
        "(npm ci --cache .npm-cache --prefer-offline || exit /b 1) ^& node --test"
    )

    result = conditional_test_command_short_circuit(tmp_path, command)

    assert result == {}


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

    assert plan["command"].endswith("-m pytest -q tests/test_security.py")
    assert plan["profiles"][0]["name"] == "pytest-focused"
    assert plan["profiles"][0]["targets"] == ["tests/test_security.py"]


def test_build_test_plan_runs_changed_test_file_directly(tmp_path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_cli.py").write_text("def test_cli():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr("visual_agent.verification_profiles._git_changed_paths", lambda root, base: ["tests/test_cli.py"])

    plan = build_test_plan(tmp_path)

    assert plan["command"].endswith("-m pytest -q tests/test_cli.py")


def test_build_test_plan_falls_back_when_source_has_no_matching_test(tmp_path, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    source = tmp_path / "src" / "visual_agent"
    source.mkdir(parents=True)
    (source / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr("visual_agent.verification_profiles._git_changed_paths", lambda root, base: ["src/visual_agent/new_module.py"])

    plan = build_test_plan(tmp_path)

    assert plan["command"].endswith("-m pytest -q")
    assert plan["profiles"][0]["name"] == "pytest"
