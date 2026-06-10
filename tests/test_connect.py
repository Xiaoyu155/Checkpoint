from __future__ import annotations

import json

from visual_agent.connect import connect_platform


def test_connect_claude_code_writes_local_mcp_config(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    result = connect_platform("claude-code", workspace_root=".agent-workspace", repo_root=repo, python="python-test")
    data = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))

    assert result.status == "connected"
    assert (repo / ".agent-workspace" / "workspace.json").exists()
    assert data["mcpServers"]["visual-agent"]["command"] == "python-test"
    assert data["mcpServers"]["visual-agent"]["args"] == ["-m", "visual_agent.mcp_server"]
    assert data["mcpServers"]["visual-agent"]["env"]["VISUAL_AGENT_WORKSPACE"].endswith(".agent-workspace")


def test_connect_cursor_preserves_existing_mcp_servers(tmp_path) -> None:
    repo = tmp_path / "repo"
    path = repo / ".cursor" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"existing": {"command": "old"}}}), encoding="utf-8")

    connect_platform("cursor", workspace_root=".agent-workspace", repo_root=repo)
    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["mcpServers"]["existing"]["command"] == "old"
    assert data["mcpServers"]["visual-agent"]["command"] == "python"


def test_connect_codex_writes_agents_instructions(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    result = connect_platform("codex", workspace_root=".agent-workspace", repo_root=repo)
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")

    assert result.config_path == repo / "AGENTS.md"
    assert "## Checkpoint" in text
    assert "codex-check" in text
    assert "context-snapshot" in text

