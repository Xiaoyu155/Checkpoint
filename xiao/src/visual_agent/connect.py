from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .workspace import init_workspace


SUPPORTED_PLATFORMS = ("claude-code", "cursor", "codex")


@dataclass(frozen=True)
class ConnectResult:
    platform: str
    workspace_root: Path
    config_path: Path
    status: str
    message: str


def connect_platform(
    platform: str,
    *,
    workspace_root: str | Path = ".agent-workspace",
    repo_root: str | Path = ".",
    python: str = "python",
    global_config: bool = False,
) -> ConnectResult:
    normalized = str(platform).strip().lower()
    if normalized not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {platform}")
    repo = Path(repo_root).resolve()
    workspace = init_workspace(repo / workspace_root if not Path(workspace_root).is_absolute() else workspace_root)
    if normalized == "claude-code":
        return connect_claude_code(workspace.root, repo_root=repo, python=python, global_config=global_config)
    if normalized == "cursor":
        return connect_cursor(workspace.root, repo_root=repo, python=python)
    return connect_codex(workspace.root, repo_root=repo)


def connect_claude_code(workspace_root: Path, *, repo_root: Path, python: str, global_config: bool) -> ConnectResult:
    path = Path.home() / ".claude" / "settings.json" if global_config else repo_root / ".claude" / "settings.json"
    data = read_json_object(path)
    servers = data.setdefault("mcpServers", {})
    servers["visual-agent"] = mcp_server_config(workspace_root, repo_root=repo_root, python=python)
    write_json_object(path, data)
    return ConnectResult(
        platform="claude-code",
        workspace_root=workspace_root,
        config_path=path,
        status="connected",
        message="Claude Code MCP config updated.",
    )


def connect_cursor(workspace_root: Path, *, repo_root: Path, python: str) -> ConnectResult:
    path = repo_root / ".cursor" / "mcp.json"
    data = read_json_object(path)
    servers = data.setdefault("mcpServers", {})
    servers["visual-agent"] = mcp_server_config(workspace_root, repo_root=repo_root, python=python)
    write_json_object(path, data)
    return ConnectResult(
        platform="cursor",
        workspace_root=workspace_root,
        config_path=path,
        status="connected",
        message="Cursor MCP config updated.",
    )


def connect_codex(workspace_root: Path, *, repo_root: Path) -> ConnectResult:
    path = repo_root / "AGENTS.md"
    section = codex_agent_section(workspace_root)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if "## Checkpoint" in existing:
        text = replace_visual_agent_section(existing, section)
    else:
        text = existing.rstrip() + ("\n\n" if existing.strip() else "") + section
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return ConnectResult(
        platform="codex",
        workspace_root=workspace_root,
        config_path=path,
        status="connected",
        message="Codex AGENTS.md instructions updated.",
    )


def mcp_server_config(workspace_root: Path, *, repo_root: Path, python: str) -> dict[str, Any]:
    return {
        "command": python,
        "args": ["-m", "visual_agent.mcp_server"],
        "cwd": str(repo_root),
        "env": {
            "VISUAL_AGENT_WORKSPACE": str(workspace_root),
            "PYTHONPATH": str(repo_root / "src"),
        },
    }


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON config: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON config must be an object: {path}")
    return data


def write_json_object(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def codex_agent_section(workspace_root: Path) -> str:
    return (
        "## Checkpoint\n\n"
        "Read `.visual-agent-status.md` for current verification state before planning fixes.\n\n"
        "Use Checkpoint after UI or workflow-related code changes:\n\n"
        f"- Workspace root: `{workspace_root}`\n"
        "- Fast check: `python -m visual_agent.cli codex-check --workspace-root .agent-workspace`\n"
        "- Include slow visual/OCR workflows only when needed: add `--include-slow`\n"
        "- Resume context in a new chat: `python -m visual_agent.cli context-snapshot --workspace-root .agent-workspace --format markdown`\n"
        "- Read compact failure details before opening full reports: `python -m visual_agent.cli summarize-latest-failure --workspace-root .agent-workspace --format json`\n"
    )


def replace_visual_agent_section(existing: str, section: str) -> str:
    marker = "## Checkpoint"
    start = existing.find(marker)
    if start < 0:
        return existing.rstrip() + "\n\n" + section
    next_section = existing.find("\n## ", start + len(marker))
    if next_section < 0:
        return existing[:start].rstrip() + "\n\n" + section
    return existing[:start].rstrip() + "\n\n" + section.rstrip() + "\n\n" + existing[next_section:].lstrip()


def connect_result_to_dict(result: ConnectResult) -> dict[str, Any]:
    return {
        "platform": result.platform,
        "workspace_root": str(result.workspace_root),
        "config_path": str(result.config_path),
        "status": result.status,
        "message": result.message,
    }

