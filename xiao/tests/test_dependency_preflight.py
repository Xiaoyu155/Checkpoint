from __future__ import annotations

import sys

from visual_agent.preflight import dependency_preflight


def test_node_project_missing_node_modules_estimates_install(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest run"},"dependencies":{"vue":"3.0.0"}}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")

    result = dependency_preflight(tmp_path, "npm test")

    assert result["package_manager"] == "npm"
    assert result["lockfile"] == "package-lock.json"
    assert result["deps_installed"] is False
    assert result["estimated_install_minutes"] == 9
    assert "node_dependencies_not_installed" in result["warnings"]


def test_python_project_with_pytest_importable_passes(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    result = dependency_preflight(tmp_path, f'"{sys.executable}" -m pytest -q')

    assert result["package_manager"] == "pip"
    assert result["deps_installed"] is True
    assert result["estimated_install_minutes"] == 0
    assert result["warnings"] == []


def test_native_dep_flags_risk(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"dependencies":{"sharp":"0.33.0"}}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")

    result = dependency_preflight(tmp_path, "npm test")

    assert result["native_install_risk"] is True
    assert result["estimated_install_minutes"] == 12
    assert "native_dependency_risk" in result["warnings"]
