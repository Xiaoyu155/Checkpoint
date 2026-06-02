from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CiTemplateInstall:
    root: Path
    workspace_root: str
    github_workflow: Path
    powershell_script: Path
    batch_script: Path
    risk_policy_check_command: str
    risk_policy_plan_command: str
    quality_gate_command: str


def install_ci_templates(
    root: str | Path,
    *,
    workspace_root: str = ".agent-workspace",
    overwrite: bool = False,
) -> CiTemplateInstall:
    target_root = Path(root)
    workflow_path = target_root / ".github" / "workflows" / "visual-agent-quality-gate.yml"
    powershell_path = target_root / "scripts" / "quality_gate.ps1"
    batch_path = target_root / "scripts" / "quality_gate.bat"
    outputs = (workflow_path, powershell_path, batch_path)

    existing = [path for path in outputs if path.exists()]
    if existing and not overwrite:
        names = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"CI templates already exist: {names}")

    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    powershell_path.parent.mkdir(parents=True, exist_ok=True)

    workflow_path.write_text(github_actions_template(workspace_root), encoding="utf-8")
    powershell_path.write_text(powershell_quality_gate_template(workspace_root), encoding="utf-8")
    batch_path.write_text(batch_quality_gate_template(workspace_root), encoding="utf-8")

    return CiTemplateInstall(
        root=target_root,
        workspace_root=workspace_root,
        github_workflow=workflow_path,
        powershell_script=powershell_path,
        batch_script=batch_path,
        risk_policy_check_command=risk_policy_check_command(workspace_root),
        risk_policy_plan_command=risk_policy_plan_command(workspace_root),
        quality_gate_command=quality_gate_command(workspace_root),
    )


def ci_template_install_to_dict(install: CiTemplateInstall) -> dict[str, Any]:
    return {
        "root": str(install.root),
        "workspace_root": install.workspace_root,
        "github_workflow": str(install.github_workflow),
        "powershell_script": str(install.powershell_script),
        "batch_script": str(install.batch_script),
        "risk_policy_check_command": install.risk_policy_check_command,
        "risk_policy_plan_command": install.risk_policy_plan_command,
        "quality_gate_command": install.quality_gate_command,
        "notes": [
            "Run risk_policy_check_command before CI quality gates to fail fast on invalid workspace risk policy.",
            "Run risk_policy_plan_command to preview missing workspace.json quality policy defaults.",
        ],
    }


def risk_policy_check_command(workspace_root: str) -> str:
    return f"python -m visual_agent.cli workspace-risk-policy-check --root {workspace_root}"


def risk_policy_plan_command(workspace_root: str) -> str:
    return f"python -m visual_agent.cli workspace-risk-policy-plan --root {workspace_root}"


def quality_gate_command(workspace_root: str, *, profile: str = "ci") -> str:
    return f"python -m visual_agent.cli quality-gate --profile {profile} --workspace-root {workspace_root} --run"


def github_actions_template(workspace_root: str) -> str:
    return f"""name: Visual Agent Quality Gate

on:
  push:
  pull_request:
  workflow_dispatch:

jobs:
  quality-gate:
    runs-on: windows-latest
    timeout-minutes: 20

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.13"

      - name: Install package
        run: |
          python -m pip install --upgrade pip
          python -m pip install -e ".[test]"

      - name: Check workspace risk policy
        run: {risk_policy_check_command(workspace_root)}

      - name: Run CI quality gate
        run: {quality_gate_command(workspace_root)}

      - name: Upload quality reports
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: visual-agent-quality-reports
          path: |
            .runs/quality_gates/**
            {workspace_root}/reports/quality_gates/**
            {workspace_root}/reports/regression_runs/**
          if-no-files-found: ignore
"""


def powershell_quality_gate_template(workspace_root: str) -> str:
    return f"""param(
    [ValidateSet("local", "ci")]
    [string]$Profile = "local",
    [string]$WorkspaceRoot = "{workspace_root}",
    [switch]$Run,
    [double]$TimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $RepoRoot ".venv\\Scripts\\python.exe"
$Python = "python"
if (Test-Path $VenvPython) {{
    $Python = $VenvPython
}}

$ArgsList = @(
    "-m",
    "visual_agent.cli",
    "quality-gate",
    "--profile",
    $Profile,
    "--workspace-root",
    $WorkspaceRoot,
    "--timeout-seconds",
    [string]$TimeoutSeconds
)

$RiskPolicyArgs = @(
    "-m",
    "visual_agent.cli",
    "workspace-risk-policy-check",
    "--root",
    $WorkspaceRoot
)

& $Python @RiskPolicyArgs
if ($LASTEXITCODE -ne 0) {{
    exit $LASTEXITCODE
}}

if ($Run) {{
    $ArgsList += "--run"
}}

& $Python @ArgsList
exit $LASTEXITCODE
"""


def batch_quality_gate_template(workspace_root: str) -> str:
    return f"""@echo off
setlocal

set PROFILE=%~1
if "%PROFILE%"=="" set PROFILE=local

set PYTHON=python
if exist "%~dp0..\\.venv\\Scripts\\python.exe" set PYTHON=%~dp0..\\.venv\\Scripts\\python.exe

"%PYTHON%" -m visual_agent.cli workspace-risk-policy-check --root {workspace_root}
if errorlevel 1 exit /b %ERRORLEVEL%

"%PYTHON%" -m visual_agent.cli quality-gate --profile %PROFILE% --workspace-root {workspace_root} --run
exit /b %ERRORLEVEL%
"""
