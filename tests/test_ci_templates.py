from __future__ import annotations

import pytest

from visual_agent.ci_templates import install_ci_templates
from visual_agent.cli import main


def test_install_ci_templates_writes_quality_gate_files(tmp_path) -> None:
    result = install_ci_templates(tmp_path, workspace_root=".agent-workspace")

    workflow = result.github_workflow.read_text(encoding="utf-8")
    powershell = result.powershell_script.read_text(encoding="utf-8")
    batch = result.batch_script.read_text(encoding="utf-8")

    assert result.github_workflow.exists()
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
    assert "Check workspace risk policy" in workflow
    assert "workspace-risk-policy-check --root .agent-workspace" in workflow
    assert "quality-gate --profile ci --workspace-root .agent-workspace --run" in workflow
    assert "VISUAL_AGENT_CLOUD_ENDPOINT" in workflow
    assert "VISUAL_AGENT_CLOUD_API_KEY" in workflow
    assert "cloud-run --workspace-root .agent-workspace" in workflow
    assert "VISUAL_AGENT_CLOUD_SERVER_API_KEY" not in workflow
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
    assert "workspace-risk-policy-plan --root workspace" in output
    assert (tmp_path / ".github" / "workflows" / "visual-agent-quality-gate.yml").exists()
    assert (tmp_path / "scripts" / "quality_gate.ps1").exists()
