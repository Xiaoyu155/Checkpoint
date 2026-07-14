from __future__ import annotations

import tomllib
from pathlib import Path


def test_release_metadata_requires_mcp_in_the_core_install() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["version"] == "0.1.2"
    assert "mcp>=1.0.0" in project["dependencies"]
    assert project["optional-dependencies"]["mcp"] == []
