from __future__ import annotations

from pathlib import Path
from typing import Generator

import pytest

from .sdk import VisualSession


@pytest.fixture
def visual_session(tmp_path: Path) -> Generator[VisualSession, None, None]:
    with VisualSession(workspace=tmp_path, dry_run=True) as session:
        yield session


@pytest.fixture
def visual_session_live(tmp_path: Path) -> Generator[VisualSession, None, None]:
    with VisualSession(workspace=tmp_path, dry_run=False) as session:
        yield session
