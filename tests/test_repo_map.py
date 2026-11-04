from __future__ import annotations

import subprocess
from pathlib import Path

from visual_agent.repo_map import build_repo_map, render_repo_map


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
    )


def test_git_map_includes_untracked_source_but_excludes_credentials(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    source = tmp_path / "src"
    source.mkdir()
    (source / "greetings.py").write_text("def greet(name):\n    return name\n", encoding="utf-8")
    (tmp_path / "gemini_keys.json").write_text('{"api_key":"never expose"}', encoding="utf-8")

    payload = build_repo_map(repo_root=tmp_path)
    rendered = render_repo_map(payload, goal="improve greeting behavior")

    assert "src/greetings.py" in rendered
    assert "def greet" in rendered
    assert "gemini_keys.json" not in rendered
    assert "never expose" not in rendered
