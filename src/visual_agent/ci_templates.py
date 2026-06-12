from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CiTemplateInstall:
    root: Path
    workspace_root: str
    github_workflow: Path
    github_config: Path
    githooks_dir: Path
    pre_push_hook: Path
    powershell_script: Path
    batch_script: Path
    risk_policy_check_command: str
    risk_policy_plan_command: str
    quality_gate_command: str
    cloud_server_command: str
    cloud_run_command: str
    hook_setup_command: str
    fast_verify_command: str


def install_ci_templates(
    root: str | Path,
    *,
    workspace_root: str = ".agent-workspace",
    overwrite: bool = False,
) -> CiTemplateInstall:
    target_root = Path(root)
    workflow_path = target_root / ".github" / "workflows" / "checkpoint-quality-gate.yml"
    config_path = target_root / ".github" / "checkpoint.yml"
    powershell_path = target_root / "scripts" / "quality_gate.ps1"
    batch_path = target_root / "scripts" / "quality_gate.bat"
    hooks_dir = target_root / ".githooks"
    pre_push_path = hooks_dir / "pre-push"
    outputs = (workflow_path, config_path, powershell_path, batch_path, pre_push_path)

    existing = [path for path in outputs if path.exists()]
    if existing and not overwrite:
        names = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"CI templates already exist: {names}")

    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    powershell_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_dir.mkdir(parents=True, exist_ok=True)

    workflow_path.write_text(github_actions_template(workspace_root), encoding="utf-8")
    config_path.write_text(visual_agent_ci_config_template(workspace_root), encoding="utf-8")
    powershell_path.write_text(powershell_quality_gate_template(workspace_root), encoding="utf-8")
    batch_path.write_text(batch_quality_gate_template(workspace_root), encoding="utf-8")
    pre_push_path.write_text(pre_push_hook_template(workspace_root), encoding="utf-8")

    return CiTemplateInstall(
        root=target_root,
        workspace_root=workspace_root,
        github_workflow=workflow_path,
        github_config=config_path,
        githooks_dir=hooks_dir,
        pre_push_hook=pre_push_path,
        powershell_script=powershell_path,
        batch_script=batch_path,
        risk_policy_check_command=risk_policy_check_command(workspace_root),
        risk_policy_plan_command=risk_policy_plan_command(workspace_root),
        quality_gate_command=quality_gate_command(workspace_root),
        cloud_server_command=cloud_server_command(workspace_root),
        cloud_run_command=cloud_run_command(workspace_root),
        hook_setup_command=hook_setup_command(),
        fast_verify_command=fast_verify_command(workspace_root),
    )


def ci_template_install_to_dict(install: CiTemplateInstall) -> dict[str, Any]:
    return {
        "root": str(install.root),
        "workspace_root": install.workspace_root,
        "github_workflow": str(install.github_workflow),
        "github_config": str(install.github_config),
        "githooks_dir": str(install.githooks_dir),
        "pre_push_hook": str(install.pre_push_hook),
        "powershell_script": str(install.powershell_script),
        "batch_script": str(install.batch_script),
        "risk_policy_check_command": install.risk_policy_check_command,
        "risk_policy_plan_command": install.risk_policy_plan_command,
        "quality_gate_command": install.quality_gate_command,
        "cloud_server_command": install.cloud_server_command,
        "cloud_run_command": install.cloud_run_command,
        "hook_setup_command": install.hook_setup_command,
        "fast_verify_command": install.fast_verify_command,
        "notes": [
            "Run risk_policy_check_command before CI quality gates to fail fast on invalid workspace risk policy.",
            "Run risk_policy_plan_command to preview missing workspace.json quality policy defaults.",
            "For remote CI execution, run cloud_server_command on the browser host and cloud_run_command from CI with CHECKPOINT_CLOUD_ENDPOINT and CHECKPOINT_CLOUD_API_KEY set from secrets.",
            "Add --required-org <org> to cloud_server_command when the browser host should enforce X-Visual-Agent-Org.",
            "Never commit cloud server API keys; use CHECKPOINT_CLOUD_SERVER_API_KEY and CI secret storage.",
            "Run hook_setup_command once to activate the .githooks pre-push hook.",
            "The pre-push hook runs fast_verify_command before the full quality gate.",
        ],
    }


def risk_policy_check_command(workspace_root: str) -> str:
    return f"python -m visual_agent.cli workspace-risk-policy-check --root {workspace_root}"


def risk_policy_plan_command(workspace_root: str) -> str:
    return f"python -m visual_agent.cli workspace-risk-policy-plan --root {workspace_root}"


def quality_gate_command(workspace_root: str, *, profile: str = "ci") -> str:
    return f"python -m visual_agent.cli quality-gate --profile {profile} --workspace-root {workspace_root} --run"


def cloud_server_command(workspace_root: str) -> str:
    return (
        "python -m visual_agent.cli cloud-server "
        f"--workspace-root {workspace_root} "
        "--host 0.0.0.0 --port 7890 "
        "--api-key-env CHECKPOINT_CLOUD_SERVER_API_KEY"
    )


def cloud_run_command(workspace_root: str, *, workflow: str = "checkout") -> str:
    return (
        "python -m visual_agent.cli cloud-run "
        f"--workspace-root {workspace_root} "
        f"--workflow {workflow} "
        "--execute --transport http --timeout-seconds 30 --max-retries 1 --format json"
    )


def hook_setup_command() -> str:
    return "git config core.hooksPath .githooks"


def fast_verify_command(workspace_root: str) -> str:
    return (
        "python -m visual_agent.cli verify "
        f"--workspace-root {workspace_root} "
        "--tags fast --max-workflows 5 --run-profile dry-run --wait-lock --format json"
    )


def github_actions_template(workspace_root: str) -> str:
    return f"""name: Checkpoint Quality Gate

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
          python -m pip install -e ".[test,web,mcp,cloud]"

      - name: Check workspace risk policy
        run: {risk_policy_check_command(workspace_root)}

      - name: Run fast verification workflows
        run: {fast_verify_command(workspace_root)}

      - name: Run CI quality gate
        run: {quality_gate_command(workspace_root)} --fail-on-risk-policy-error --fail-on-secret-leak --ci --junit-output .runs/quality_gates/junit.xml

      # Optional remote browser execution through a separately hosted Checkpoint cloud-server.
      # Required repository secrets:
      #   CHECKPOINT_CLOUD_ENDPOINT: http(s)://<browser-host>:7890/v1/run
      #   CHECKPOINT_CLOUD_API_KEY: bearer token expected by cloud-server
      # Optional repository variable:
      #   CHECKPOINT_CLOUD_ORG: org header required by cloud-server
      # Enable this step after the browser host is reachable from CI.
      # - name: Run remote Checkpoint workflow
      #   env:
      #     CHECKPOINT_LICENSE_TIER: pro
      #     CHECKPOINT_CLOUD_ENDPOINT: ${{{{ secrets.CHECKPOINT_CLOUD_ENDPOINT }}}}
      #     CHECKPOINT_CLOUD_API_KEY: ${{{{ secrets.CHECKPOINT_CLOUD_API_KEY }}}}
      #     CHECKPOINT_CLOUD_ORG: ${{{{ vars.CHECKPOINT_CLOUD_ORG }}}}
      #   run: {cloud_run_command(workspace_root)}

      - name: Upload quality reports
        id: upload_quality_reports
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: checkpoint-quality-reports
          path: |
            .runs/quality_gates/**
            {workspace_root}/reports/quality_gates/**
            {workspace_root}/reports/regression_runs/**
          if-no-files-found: ignore

      - name: Comment PR failure
        if: failure() && github.event_name == 'pull_request'
        env:
          GITHUB_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
          GITHUB_EVENT_PATH: ${{{{ github.event_path }}}}
          GITHUB_REPOSITORY: ${{{{ github.repository }}}}
          GITHUB_SERVER_URL: ${{{{ github.server_url }}}}
          GITHUB_RUN_ID: ${{{{ github.run_id }}}}
        run: python -m visual_agent.cli github-pr-comment --report-root .runs --quality-root .runs/quality_gates --artifact-url "${{{{ steps.upload_quality_reports.outputs.artifact-url }}}}" --format markdown
"""


def visual_agent_ci_config_template(workspace_root: str) -> str:
    return f"""schema_version: 1
workspace_root: {workspace_root}
ci:
  quality_gate_profile: ci
  fail_on_secret_leak: true
  fail_on_risk_policy_error: true
  report_root: .runs/quality_gates
  junit_output: .runs/quality_gates/junit.xml
  timeout_seconds: 300
verification:
  tags: [fast]
  max_workflows: 5
  run_profile: dry-run
  wait_lock: true
  format: json
pre_push:
  tags: [fast]
  max_workflows: 5
  run_profile: dry-run
  wait_lock: true
  format: json
  quality_gate_profile: ci
  junit_output: .runs/quality_gates/junit.xml
"""


def pre_push_hook_template(workspace_root: str) -> str:
    return f"""#!/bin/sh
set -eu

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="$REPO_ROOT/.venv/Scripts/python.exe"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python"
fi

cd "$REPO_ROOT"

"$PYTHON" -m visual_agent.cli verify --workspace-root {workspace_root} --tags fast --max-workflows 5 --run-profile dry-run --wait-lock --format json
"$PYTHON" -m visual_agent.cli quality-gate --profile ci --workspace-root {workspace_root} --run --fail-on-risk-policy-error --fail-on-secret-leak --ci --junit-output .runs/quality_gates/junit.xml
"""


def ci_workflow_template(*, workspace_root: str = ".agent-workspace", python_version: str = "3.11", node_version: str = "20") -> str:
    return f"""name: CI

on:
  push:
  pull_request:
  workflow_dispatch:

permissions:
  contents: read
  pull-requests: write

jobs:
  python:
    name: Python tests and lint
    runs-on: windows-latest
    timeout-minutes: 20

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "{python_version}"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install ruff
          python -m pip install -e ".[test,web,mcp,cloud]"

      - name: Ruff lint
        run: ruff check src tests cloud_api

      - name: Run fast verification workflows
        run: python -m visual_agent.cli verify --workspace-root {workspace_root} --tags fast --max-workflows 5 --run-profile dry-run --wait-lock --format json

      - name: Run Checkpoint quality gate
        run: python -m visual_agent.cli quality-gate --profile ci --workspace-root {workspace_root} --run --fail-on-risk-policy-error --fail-on-secret-leak --ci --junit-output .runs/quality_gates/junit.xml

      - name: Upload quality reports
        id: upload_quality_reports
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: checkpoint-quality-reports
          path: |
            .runs/quality_gates/**
            {workspace_root}/reports/quality_gates/**
            {workspace_root}/reports/regression_runs/**
          if-no-files-found: ignore

      - name: Comment PR failure
        if: failure() && github.event_name == 'pull_request'
        env:
          GITHUB_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
          GITHUB_EVENT_PATH: ${{{{ github.event_path }}}}
          GITHUB_REPOSITORY: ${{{{ github.repository }}}}
          GITHUB_SERVER_URL: ${{{{ github.server_url }}}}
          GITHUB_RUN_ID: ${{{{ github.run_id }}}}
        run: python -m visual_agent.cli github-pr-comment --report-root .runs --quality-root .runs/quality_gates --artifact-url "${{{{ steps.upload_quality_reports.outputs.artifact-url }}}}" --format markdown

  vscode-extension:
    name: VS Code extension compile
    runs-on: windows-latest
    timeout-minutes: 10

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Node
        uses: actions/setup-node@v4
        with:
          node-version: "{node_version}"
          cache: npm
          cache-dependency-path: vscode-extension/package-lock.json

      - name: Install dependencies
        working-directory: vscode-extension
        run: npm ci

      - name: Compile and test
        working-directory: vscode-extension
        run: npm test
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

