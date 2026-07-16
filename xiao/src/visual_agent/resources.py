from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path


_DISTRIBUTION_DATA_DIR = Path("share") / "visual-agent"


def distribution_data_root() -> Path:
    """Return the source-tree or installed data directory for bundled assets."""

    configured = str(os.environ.get("VISUAL_AGENT_DATA_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    source_root = Path(__file__).resolve().parents[2]
    if all((source_root / name).is_dir() for name in ("templates", "examples", "workflows")):
        return source_root

    candidates: list[Path] = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidates.append(Path(frozen_root) / _DISTRIBUTION_DATA_DIR)
    data_prefix = sysconfig.get_path("data")
    if data_prefix:
        candidates.append(Path(data_prefix) / _DISTRIBUTION_DATA_DIR)

    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()

    return (Path(data_prefix or sys.prefix) / _DISTRIBUTION_DATA_DIR).resolve()


def bundled_path(category: str, *parts: str) -> Path:
    return distribution_data_root().joinpath(category, *parts)
