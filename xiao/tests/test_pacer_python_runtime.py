from __future__ import annotations

from pathlib import Path
import sys

import pytest

from visual_agent import codex_launcher, mcp_server
from visual_agent.pacer_launch_context import initialize_active_launch, read_active_launch


def _active_runtime(
    tmp_path: Path,
    *,
    pytest_available: bool = True,
    executable: Path | None = None,
    source: str = "project_venv",
) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    interpreter = executable or repo / ".venv" / "Scripts" / "python.exe"
    if executable is None:
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("fixture", encoding="utf-8")
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-1.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={
            "launch_id": "launch-1",
            "repo_root": str(repo),
            "runtime": {
                "python": {
                    "executable": str(interpreter),
                    "source": source,
                    "available": True,
                    "pytest_available": pytest_available,
                    "probe_status": "ok",
                    "trusted_venv": source != "environment",
                    "root": str(repo),
                    "bound_repo_root": str(repo),
                }
            },
        },
    )
    return workspace, repo, interpreter


def test_verification_reuses_project_python_and_disables_pytest_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace, repo, interpreter = _active_runtime(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mcp_server,
        "run_pacer_commands_payload",
        lambda args: captured.update(args) or {"status": "passed"},
    )

    result = mcp_server.run_pacer_verification_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "steps": [{"name": "tests", "argv": ["python", "-m", "pytest", "-q"]}],
        }
    )

    step = captured["steps"][0]
    assert step["argv"][0] == str(interpreter)
    assert step["argv"][1:] == ["-m", "pytest", "-p", "no:cacheprovider", "-q"]
    assert step["pytest_plugin_policy"] == "native"
    assert step["pytest_cache_policy"] == "disabled"
    assert "env" not in step
    assert result["runtime"]["python"]["pytest_available"] is True
    assert result["pytest_plugin_policy"] == "preserved_except_pacer_fallback_and_cacheprovider"
    assert result["pytest_cache_policy"] == "disabled_for_pytest"


def test_verification_disables_only_pacer_plugin_for_fallback_runtime(tmp_path: Path, monkeypatch) -> None:
    workspace, repo, _ = _active_runtime(tmp_path, source="known_root_venv")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mcp_server,
        "run_pacer_commands_payload",
        lambda args: captured.update(args) or {"status": "passed"},
    )

    result = mcp_server.run_pacer_verification_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "steps": [{"name": "tests", "argv": ["pytest", "-q"]}],
        }
    )

    step = captured["steps"][0]
    assert step["argv"][1:5] == ["-m", "pytest", "-p", "no:visual_agent"]
    assert step["pytest_plugin_policy"] == "pacer_plugin_disabled"
    assert "no:cacheprovider" in step["argv"]
    assert step["pytest_cache_policy"] == "disabled"
    assert "env" not in step
    assert result["pytest_plugin_policy"] == "preserved_except_pacer_fallback_and_cacheprovider"


def test_verification_reports_bound_runtime_when_pytest_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    workspace, repo, interpreter = _active_runtime(tmp_path, pytest_available=False)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mcp_server,
        "run_pacer_commands_payload",
        lambda args: captured.update(args) or {"status": "failed", "failed": 1},
    )

    result = mcp_server.run_pacer_verification_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "steps": [{"name": "tests", "argv": ["python", "-m", "pytest", "-q"]}],
        }
    )

    assert captured["steps"][0]["argv"][0] == str(interpreter)
    assert result["status"] == "failed"
    assert result["runtime"]["python"]["pytest_available"] is False


def test_verification_blocks_pacer_plugin_but_loads_legitimate_plugins(tmp_path: Path, monkeypatch) -> None:
    workspace, repo, _ = _active_runtime(
        tmp_path,
        executable=Path(sys.executable),
        source="known_root_venv",
    )
    (repo / "test_smoke.py").write_text(
        "def test_smoke(request):\n"
        "    assert request.config._legitimate_plugin_loaded is True\n",
        encoding="utf-8",
    )
    plugin_root = tmp_path / "poison-site"
    plugin_root.mkdir()
    (plugin_root / "legitimate_plugin.py").write_text(
        "def pytest_configure(config):\n"
        "    config._legitimate_plugin_loaded = True\n",
        encoding="utf-8",
    )
    distribution = plugin_root / "poison_plugin-1.0.dist-info"
    distribution.mkdir(parents=True)
    (distribution / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: poison-plugin\nVersion: 1.0\n",
        encoding="utf-8",
    )
    (distribution / "entry_points.txt").write_text(
        "[pytest11]\n"
        "visual_agent = pacer_test_plugin_that_does_not_exist\n"
        "legitimate = legitimate_plugin\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(plugin_root))

    result = mcp_server.run_pacer_verification_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "steps": [{"name": "tests", "argv": ["python", "-m", "pytest", "-q"]}],
        }
    )

    assert result["status"] == "passed"
    assert result["passed"] == 1
    assert "1 passed" in result["records"][0]["stdout_tail"]


def test_verification_fails_fast_when_no_managed_python_exists(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "visual_agent.pacer_launch_context.resolve_python_runtime",
        lambda *_args, **_kwargs: {
            "executable": "",
            "source": "unavailable",
            "available": False,
            "pytest_available": False,
            "probe_status": "not_found",
        },
    )

    with pytest.raises(ValueError, match="managed Python runtime is unavailable"):
        mcp_server.run_pacer_verification_payload(
            {
                "workspace_root": str(repo / ".agent-workspace"),
                "repo_root": str(repo),
                "steps": [{"name": "tests", "argv": ["python", "-m", "pytest", "-q"]}],
            }
        )


def test_parent_fallback_rebinds_to_child_venv_and_memory_exposes_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (child / "pyproject.toml").write_text("[project]\nname='child'\n", encoding="utf-8")
    fallback = tmp_path / "pacer-fallback" / "Scripts" / "python.exe"
    fallback.parent.mkdir(parents=True)
    fallback.write_text("fallback", encoding="utf-8")
    child_python = child / ".venv" / "Scripts" / "python.exe"
    child_python.parent.mkdir(parents=True)
    child_python.write_text("child", encoding="utf-8")
    workspace = parent / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-parent.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={
            "launch_id": "launch-parent",
            "repo_root": str(parent),
            "runtime": {
                "python": {
                    "executable": str(fallback),
                    "source": "known_root_venv",
                    "available": True,
                    "pytest_available": True,
                    "probe_status": "ok",
                    "trusted_venv": True,
                    "root": str(tmp_path / "pacer-fallback"),
                    "bound_repo_root": str(parent),
                }
            },
        },
    )
    launch_environment = {"PATH": str(tmp_path / "system-bin")}
    codex_launcher._apply_managed_python_environment(
        launch_environment,
        read_active_launch(workspace),
    )
    assert "PACER_PYTHON" not in launch_environment
    assert str(fallback.parent) not in launch_environment["PATH"].split(codex_launcher.os.pathsep)

    # Simulate a process started by the previous launcher implementation.
    monkeypatch.setenv("PACER_PYTHON", str(fallback))
    memory = mcp_server.get_pacer_memory_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(child),
            "detail": "full",
            "limit": 1,
        }
    )

    rebound = memory["runtime"]["python"]
    assert rebound["executable"] == str(child_python)
    assert rebound["source"] == "project_venv"
    assert rebound["bound_repo_root"] == str(child.resolve())
    assert read_active_launch(workspace)["runtime"]["python"] == rebound

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mcp_server,
        "run_pacer_commands_payload",
        lambda args: captured.update(args) or {"status": "passed"},
    )
    mcp_server.run_pacer_verification_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(child),
            "steps": [{"name": "tests", "argv": ["python", "-m", "pytest", "-q"]}],
        }
    )
    assert captured["steps"][0]["argv"][0] == str(child_python)
    assert captured["steps"][0]["pytest_plugin_policy"] == "native"


def test_user_explicit_python_override_remains_fixed_across_project_binding(tmp_path: Path, monkeypatch) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (child / "pyproject.toml").write_text("[project]\nname='child'\n", encoding="utf-8")
    explicit = tmp_path / "explicit-python.exe"
    explicit.write_text("explicit", encoding="utf-8")
    child_python = child / ".venv" / "Scripts" / "python.exe"
    child_python.parent.mkdir(parents=True)
    child_python.write_text("child", encoding="utf-8")
    workspace = parent / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-explicit.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={
            "launch_id": "launch-explicit",
            "repo_root": str(parent),
            "runtime": {
                "python": {
                    "executable": str(explicit),
                    "source": "environment",
                    "available": True,
                    "pytest_available": True,
                    "probe_status": "ok",
                    "trusted_venv": False,
                    "root": "",
                    "bound_repo_root": str(parent),
                }
            },
        },
    )
    monkeypatch.setenv("PACER_PYTHON", str(explicit))
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mcp_server,
        "run_pacer_commands_payload",
        lambda args: captured.update(args) or {"status": "passed"},
    )

    result = mcp_server.run_pacer_verification_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(child),
            "steps": [{"name": "tests", "argv": ["python", "-m", "pytest", "-q"]}],
        }
    )

    assert captured["steps"][0]["argv"][0] == str(explicit)
    assert result["runtime"]["python"]["bound_repo_root"] == str(child.resolve())
