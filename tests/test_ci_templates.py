from __future__ import annotations

from pathlib import Path

import pytest

from visual_agent.ci_templates import install_ci_templates
from visual_agent.cli import main


def test_install_ci_templates_writes_quality_gate_files(tmp_path) -> None:
    result = install_ci_templates(tmp_path, workspace_root=".agent-workspace")

    workflow = result.github_workflow.read_text(encoding="utf-8")
    powershell = result.powershell_script.read_text(encoding="utf-8")
    batch = result.batch_script.read_text(encoding="utf-8")

    assert result.github_workflow.exists()
    assert result.github_config.exists()
    assert result.githooks_dir.exists()
    assert result.pre_push_hook.exists()
    assert result.powershell_script.exists()
    assert result.batch_script.exists()
    assert result.risk_policy_check_command == "python -m visual_agent.cli workspace-risk-policy-check --root .agent-workspace"
    assert result.risk_policy_plan_command == "python -m visual_agent.cli workspace-risk-policy-plan --root .agent-workspace"
    assert result.quality_gate_command == "python -m visual_agent.cli quality-gate --profile ci --workspace-root .agent-workspace --run"
    assert result.cloud_server_command == (
        "python -m visual_agent.cli cloud-server --workspace-root .agent-workspace "
        "--host 0.0.0.0 --port 7890 --api-key-env VISUAL_AGENT_CLOUD_SERVER_API_KEY"
    )
    assert result.cloud_run_command == (
        "python -m visual_agent.cli cloud-run --workspace-root .agent-workspace --workflow checkout "
        "--execute --transport http --timeout-seconds 30 --max-retries 1 --format json"
    )
    assert result.hook_setup_command == "git config core.hooksPath .githooks"
    assert result.fast_verify_command == (
        "python -m visual_agent.cli verify --workspace-root .agent-workspace "
        "--tags fast --max-workflows 5 --run-profile dry-run --wait-lock --format json"
    )
    assert "Check workspace risk policy" in workflow
    assert "workspace-risk-policy-check --root .agent-workspace" in workflow
    assert "verify --workspace-root .agent-workspace --tags fast --max-workflows 5 --run-profile dry-run --wait-lock --format json" in workflow
    assert "quality-gate --profile ci --workspace-root .agent-workspace --run --fail-on-risk-policy-error --fail-on-secret-leak --ci --junit-output .runs/quality_gates/junit.xml" in workflow
    assert "VISUAL_AGENT_CLOUD_ENDPOINT" in workflow
    assert "VISUAL_AGENT_CLOUD_API_KEY" in workflow
    assert "cloud-run --workspace-root .agent-workspace" in workflow
    assert "VISUAL_AGENT_CLOUD_SERVER_API_KEY" not in workflow
    assert "schema_version: 1" in result.github_config.read_text(encoding="utf-8")
    assert "pre_push:" in result.github_config.read_text(encoding="utf-8")
    assert "quality-gate --profile ci --workspace-root .agent-workspace --run --fail-on-risk-policy-error --fail-on-secret-leak --ci --junit-output .runs/quality_gates/junit.xml" in result.pre_push_hook.read_text(encoding="utf-8")
    assert "actions/upload-artifact@v4" in workflow
    assert "visual_agent.cli" in powershell
    assert ".venv\\Scripts\\python.exe" in powershell
    assert "workspace-risk-policy-check" in powershell
    assert "--workspace-root" in powershell
    assert ".venv\\Scripts\\python.exe" in batch
    assert "workspace-risk-policy-check --root .agent-workspace" in batch
    assert "quality-gate --profile %PROFILE% --workspace-root .agent-workspace --run" in batch


def test_install_ci_templates_refuses_overwrite_by_default(tmp_path) -> None:
    install_ci_templates(tmp_path)

    with pytest.raises(FileExistsError):
        install_ci_templates(tmp_path)


def test_install_ci_templates_cli(tmp_path, capsys) -> None:
    exit_code = main(
        [
            "install-ci-templates",
            "--root",
            str(tmp_path),
            "--workspace-root",
            "workspace",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "visual-agent-quality-gate.yml" in output
    assert "risk_policy_check_command" in output
    assert "cloud_server_command" in output
    assert "cloud_run_command" in output
    assert "hook_setup_command" in output
    assert "workspace-risk-policy-plan --root workspace" in output
    assert (tmp_path / ".github" / "workflows" / "visual-agent-quality-gate.yml").exists()
    assert (tmp_path / ".github" / "visual-agent.yml").exists()
    assert (tmp_path / ".githooks" / "pre-push").exists()
    assert (tmp_path / "scripts" / "quality_gate.ps1").exists()


def test_generate_ci_cli_outputs_workflow_yaml(tmp_path, capsys) -> None:
    exit_code = main(
        [
            "generate-ci",
            "--workspace-root",
            ".agent-workspace",
            "--python-version",
            "3.11",
            "--node-version",
            "20",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert "name: CI" in output
    assert "pull_request:" in output
    assert "pull-requests: write" in output
    assert "python-version: \"3.11\"" in output
    assert "node-version: \"20\"" in output
    assert "ruff check src tests cloud_api" in output
    assert "python -m pip install -e \".[test,web,mcp,cloud]\"" in output
    assert "verify --workspace-root .agent-workspace --tags fast --max-workflows 5 --run-profile dry-run --wait-lock --format json" in output
    assert "quality-gate --profile ci --workspace-root .agent-workspace --run --fail-on-risk-policy-error --fail-on-secret-leak --ci --junit-output .runs/quality_gates/junit.xml" in output
    assert "Comment PR failure" in output
    assert "github-pr-comment --report-root .runs --quality-root .runs/quality_gates --artifact-url" in output


def test_generate_ci_cli_can_write_file(tmp_path, capsys) -> None:
    output_path = tmp_path / ".github" / "workflows" / "ci.yml"
    exit_code = main(
        [
            "generate-ci",
            "--output",
            str(output_path),
        ]
    )

    capsys.readouterr()

    assert exit_code == 0
    assert output_path.exists()
    assert "name: CI" in output_path.read_text(encoding="utf-8")


def test_security_workflow_includes_python_and_node_audits() -> None:
    workflow_path = Path(".github/workflows/security.yml")

    text = workflow_path.read_text(encoding="utf-8")

    assert workflow_path.exists()
    assert "name: Security" in text
    assert "pip-audit" in text
    assert "npm audit --audit-level=high" in text
