from __future__ import annotations

from visual_agent.verification_profiles import choose_verification_command, detect_verification_profiles, resolve_test_command


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


def test_resolve_test_command_keeps_explicit_command(tmp_path) -> None:
    command, profile = resolve_test_command("npm run check", repo_root=tmp_path)

    assert command == "npm run check"
    assert profile is None
