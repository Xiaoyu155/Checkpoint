from __future__ import annotations

import sys

from visual_agent.chief_dispatch import default_worktree_path, dispatch_chief_plan
from visual_agent.chief_engineer import build_chief_plan, chief_plan_to_dict
from visual_agent.chief_plans_store import load_worker_records, save_plan
from visual_agent.preflight import dependency_preflight
from visual_agent.workspace import init_workspace


def write_verification_workflow(workspace, name: str, *, affects: str = "src/payment/") -> None:
    workspace.workflows_dir.joinpath(f"{name}.yaml").write_text(
        "schema_version: 1\n"
        f"name: {name}\n"
        "version: 1\n"
        "affects:\n"
        f"  - {affects}\n"
        "tags:\n"
        "  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_ocr\n"
        "    mock_text: ready\n",
        encoding="utf-8",
    )


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


def test_missing_declared_env_blocks_dispatch_without_worktree(tmp_path, monkeypatch) -> None:
    env_name = "PACER_TEST_REQUIRED_ENV"
    monkeypatch.delenv(env_name, raising=False)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    plan = build_chief_plan(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        agents=("codex",),
    )
    saved = save_plan(chief_plan_to_dict(plan), workspace_root=workspace.root, plan_id="preflight-env")
    plan_id = saved["plan_id"]
    worktree = default_worktree_path(repo_root=tmp_path, plan_id=plan_id, track_id="track_1_codex")

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("preflight should block before worker launch")

    payload = dispatch_chief_plan(
        workspace_root=workspace.root,
        plan_id=plan_id,
        execute=True,
        dry_run=False,
        command_runner=should_not_run,
        test_command=f'"{sys.executable}" -c "exit(0)"',
        verification_env=[{"kind": "env_var", "name": env_name}],
    )

    assert payload["status"] == "preflight_blocked"
    assert payload["reason"] == "verification_environment_missing"
    assert env_name in payload["message"]
    assert payload["preflight"]["verification_env"]["missing_env_vars"] == [env_name]
    assert not worktree.exists()
    assert load_worker_records(workspace.root, plan_id) == []


def test_native_dep_flags_risk(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"dependencies":{"sharp":"0.33.0"}}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")

    result = dependency_preflight(tmp_path, "npm test")

    assert result["native_install_risk"] is True
    assert result["estimated_install_minutes"] == 12
    assert "native_dependency_risk" in result["warnings"]
