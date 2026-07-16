from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def build_mcp_startup_doctor(
    *,
    workspace_root: str | Path = ".agent-workspace",
    repo_root: str | Path = ".",
    python: str | None = None,
    client: str = "codex",
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    repo = Path(repo_root).expanduser().resolve()
    workspace = _resolve_workspace(workspace_root, repo_root=repo)
    python_command = str(python or sys.executable)
    python_path = _resolve_command(python_command)
    src_path = repo / "src"
    checks: list[dict[str, Any]] = [
        _check(
            "python_command",
            "success" if python_path else "failed",
            f"Python executable is available: {python_path or python_command}",
            required=True,
        ),
        _check(
            "repo_root",
            "success" if repo.exists() else "failed",
            f"Repository root exists: {repo}",
            required=True,
        ),
        _check(
            "pythonpath_src",
            "success" if (src_path / "visual_agent").exists() else "failed",
            f"visual_agent package source exists: {src_path / 'visual_agent'}",
            required=True,
        ),
        _check(
            "workspace_root",
            "success" if workspace.exists() else "warning",
            f"Workspace root {'exists' if workspace.exists() else 'does not exist yet'}: {workspace}",
            required=False,
        ),
    ]
    if python_path and repo.exists() and src_path.exists():
        checks.append(_import_check(python_command, repo=repo, src_path=src_path, timeout_seconds=timeout_seconds))
    else:
        checks.append(
            _check(
                "mcp_import",
                "failed",
                "Skipped MCP import check because python, repo_root, or src is unavailable.",
                required=True,
            )
        )

    failed_required = [item for item in checks if item["required"] and item["status"] == "failed"]
    warnings = [item for item in checks if item["status"] == "warning"]
    status = "failed" if failed_required else ("warning" if warnings else "success")
    return {
        "schema_version": 1,
        "status": status,
        "client": str(client).lower(),
        "python": python_command,
        "python_resolved": python_path,
        "repo_root": str(repo),
        "workspace_root": str(workspace),
        "recommended_env": {"PYTHONPATH": str(src_path)},
        "recommended_args": ["-m", "visual_agent.mcp_server", "--workspace-root", str(workspace)],
        "checks": checks,
        "summary": {
            "failed_required": len(failed_required),
            "warnings": len(warnings),
            "check_count": len(checks),
        },
        "next_steps": _next_steps(status, checks),
    }


def mcp_startup_doctor_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# MCP Startup Doctor",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Client: `{payload.get('client')}`",
        f"- Python: `{payload.get('python')}`",
        f"- Repo root: `{payload.get('repo_root')}`",
        f"- Workspace root: `{payload.get('workspace_root')}`",
        "",
        "| check | status | required | message |",
        "| --- | --- | --- | --- |",
    ]
    for check in payload.get("checks", []) if isinstance(payload.get("checks"), list) else []:
        if isinstance(check, dict):
            lines.append(
                "| "
                + " | ".join(
                    _markdown_cell(value)
                    for value in (
                        check.get("id"),
                        check.get("status"),
                        check.get("required"),
                        check.get("message"),
                    )
                )
                + " |"
            )
    next_steps = payload.get("next_steps") if isinstance(payload.get("next_steps"), list) else []
    if next_steps:
        lines.extend(["", "## Next Steps", ""])
        for step in next_steps:
            lines.append(f"- {step}")
    lines.append("")
    return "\n".join(lines)


def _resolve_workspace(workspace_root: str | Path, *, repo_root: Path) -> Path:
    raw = Path(workspace_root).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    return (repo_root / raw).resolve()


def _resolve_command(command: str) -> str | None:
    raw = Path(command)
    if raw.exists():
        return str(raw.resolve())
    return shutil.which(command)


def _import_check(python_command: str, *, repo: Path, src_path: Path, timeout_seconds: float) -> dict[str, Any]:
    code = (
        "from mcp.server.stdio import stdio_server\n"
        "from visual_agent import mcp_server\n"
        "assert mcp_server.server is not None\n"
        "assert mcp_server.stdio_server is not None\n"
        "print('mcp_import_ok')\n"
    )
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(src_path) + (os.pathsep + existing if existing else "")
    try:
        completed = subprocess.run(
            [python_command, "-c", code],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception as exc:
        return _check("mcp_import", "failed", f"MCP import probe could not run: {type(exc).__name__}: {exc}", required=True)
    if completed.returncode == 0:
        return _check("mcp_import", "success", "MCP package and visual_agent.mcp_server imported successfully.", required=True)
    stderr = (completed.stderr or completed.stdout or "").strip().splitlines()
    detail = stderr[-1] if stderr else f"exit code {completed.returncode}"
    return _check("mcp_import", "failed", f"MCP import probe failed: {detail}", required=True)


def _check(check_id: str, status: str, message: str, *, required: bool) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "required": required,
        "message": message,
    }


def _next_steps(status: str, checks: list[dict[str, Any]]) -> list[str]:
    failed = {str(item.get("id")) for item in checks if item.get("status") == "failed"}
    warnings = {str(item.get("id")) for item in checks if item.get("status") == "warning"}
    steps: list[str] = []
    if "python_command" in failed:
        steps.append("Update the MCP client command to an existing Python executable, or pass --python to mcp-client-config/mcp-doctor.")
    if "pythonpath_src" in failed:
        steps.append("Run the command from the visual-agent checkout or pass --repo-root to the checkout containing src/visual_agent.")
    if "mcp_import" in failed:
        steps.append("Install the Pacer runtime in the selected Python: python -m pip install -e .")
    if "workspace_root" in warnings:
        steps.append("Initialize the workspace before using tools that require it: python -m visual_agent.cli init --root <workspace_root>")
    if status == "success":
        steps.append("Use the generated MCP config command/env, then restart the MCP client.")
    return steps


def _markdown_cell(value: Any) -> str:
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text
