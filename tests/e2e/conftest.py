from pathlib import Path

import pytest

from .helpers import ROOT, run_cli


@pytest.fixture()
def e2e_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "agent-workspace"
    result = run_cli("init-workspace", "--root", str(workspace), "--overwrite")
    assert result.returncode == 0, result.stdout + result.stderr
    return workspace


@pytest.fixture()
def playwright_env() -> dict[str, str]:
    browsers = ROOT / ".pw-browsers"
    return {"PLAYWRIGHT_BROWSERS_PATH": str(browsers)} if browsers.exists() else {}
